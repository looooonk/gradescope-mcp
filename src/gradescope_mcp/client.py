import asyncio
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from gradescope_mcp import parsers
from gradescope_mcp.catalog import GradescopeError, validate, validate_request
from gradescope_mcp.config import ORIGIN, Settings, tls_context

MAX_RESPONSE = 12 * 1024 * 1024


class GradescopeClient:
    def __init__(self, settings: Settings, *, transport=None):
        self.settings = settings
        self.http = httpx.AsyncClient(
            base_url=ORIGIN,
            timeout=20,
            verify=tls_context(),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )
        self.slots = asyncio.Semaphore(3)
        self.auth_lock = asyncio.Lock()
        self.authenticated = False
        self.cache = {}

    async def close(self):
        await self.http.aclose()

    async def _send(self, method, route, **kwargs):
        validate_request(method, route, kwargs.get("params"))
        try:
            async with self.slots, self.http.stream(method, route, **kwargs) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE:
                        raise GradescopeError(
                            "Gradescope response exceeds the byte limit.", "size_limit"
                        )
                return response.status_code, response.headers, bytes(body)
        except httpx.RequestError:
            raise GradescopeError(
                "Gradescope could not be reached securely.", "connection_error"
            ) from None

    async def _login(self):
        async with self.auth_lock:
            if self.authenticated:
                return
            self.http.cookies.clear()
            status, _, body = await self._send("GET", "/")
            if status != 200:
                raise GradescopeError(
                    "Gradescope login page is unavailable.", "authentication_error", status
                )
            soup = BeautifulSoup(body, "html.parser")
            token = soup.select_one('form[action="/login"] input[name="authenticity_token"]')
            if token is None or not token.get("value"):
                raise GradescopeError(
                    "Gradescope login form changed or requires browser sign-in.",
                    "authentication_error",
                )
            status, headers, _ = await self._send(
                "POST",
                "/login",
                data={
                    "session[email]": self.settings.email,
                    "session[password]": self.settings.password,
                    "session[remember_me]": "0",
                    "session[remember_me_sso]": "0",
                    "authenticity_token": token["value"],
                    "commit": "Log In",
                },
                headers={"Origin": ORIGIN, "Referer": ORIGIN + "/"},
            )
            location = urlsplit(headers.get("location", ""))
            if (
                status not in (302, 303)
                or location.path != "/account"
                or location.query
                or location.fragment
                or (location.netloc and location.netloc != "www.gradescope.com")
                or (location.scheme and location.scheme != "https")
            ):
                raise GradescopeError(
                    "Login failed or requires manual sign-in. Check the local .env.",
                    "authentication_error",
                    status,
                )
            self.authenticated = True

    async def _get(self, route, *, keys=(), react=False):
        params = {"content": "react"} if react else {}
        if keys:
            params["only_keys[]"] = list(keys)
        for attempt in range(2):
            await self._login()
            status, headers, body = await self._send("GET", route, params=params)
            location = urlsplit(headers.get("location", ""))
            expired = status == 401 or (
                status in (302, 303) and location.path in {"/login", "/account/auth", "/"}
            )
            if expired and attempt == 0:
                self.authenticated = False
                self.cache.clear()
                continue
            if expired:
                raise GradescopeError(
                    "Session renewal failed. Check the local credentials.",
                    "authentication_error",
                    status,
                )
            if status != 200:
                raise GradescopeError(
                    "Gradescope denied this read or is unavailable; this is not empty data.",
                    "http_error",
                    status,
                )
            if (
                "text/html" in headers.get("content-type", "")
                and b'name="session[password]"' in body
            ):
                self.authenticated = False
                if attempt == 0:
                    continue
                raise GradescopeError("Gradescope returned a login page.", "authentication_error")
            return body
        raise GradescopeError("Session renewal failed.", "authentication_error")

    async def _courses(self):
        cached = self.cache.get("courses")
        if cached and time.monotonic() - cached[0] < 30:
            return cached[1]
        value = parsers.courses(await self._get("/account"))
        self.cache["courses"] = (time.monotonic(), value)
        return value

    async def _course(self, course_id):
        items = (await self._courses())["courses"]
        course = next((c for c in items if c["id"] == course_id), None)
        if course is None:
            raise GradescopeError("Course was not found in your account.", "not_discovered")
        if course["role"] != "student":
            raise GradescopeError(
                "This integration reads student courses only.", "student_access_required"
            )
        return course

    async def _assignments(self, course_id):
        await self._course(course_id)
        return parsers.assignments(await self._get(f"/courses/{course_id}"), course_id)

    async def _raw_submission(self, path):
        rows = (await self._assignments(path["course_id"]))["assignments"]
        assignment = next((a for a in rows if a["id"] == path["assignment_id"]), None)
        if assignment is None:
            raise GradescopeError(
                "Assignment was not found in this student's course list.", "not_discovered"
            )
        current = assignment["submission_id"]
        if not current:
            raise GradescopeError(
                "No existing submission is linked. This MCP cannot start or submit assignments.",
                "no_submission",
            )
        base = f"/courses/{path['course_id']}/assignments/{path['assignment_id']}/submissions/"
        requested = path.get("submission_id", current)
        if requested != current:
            history = parsers.parse_json(
                await self._get(base + current + ".json", keys=("past_submissions",), react=True)
            )
            attempts = history.get("past_submissions")
            if not isinstance(attempts, list):
                raise parsers.parse_error()
            if requested not in {str(s.get("id")) for s in attempts}:
                raise GradescopeError(
                    "Submission was not discovered in your attempt history.", "not_discovered"
                )
        route = base + requested
        raw = parsers.parse_json(await self._get(route + ".json", react=True))
        data = parsers.submission(raw)
        if (
            str(data["submission"].get("id")) != requested
            or str(data["assignment"].get("id")) != path["assignment_id"]
        ):
            raise parsers.parse_error()
        user_id = raw["current_user"].get("id")
        if not any(o.get("user_id") == user_id for o in raw.get("ownerships", [])):
            raise GradescopeError(
                "Submission ownership could not be verified.", "student_access_required"
            )
        return route, raw, data

    async def read(self, operation, path=None):
        path = validate(operation, path)
        source = ORIGIN + "/account"
        if operation == "courses":
            data = await self._courses()
        elif operation == "profile":
            source += "/edit"
            data = parsers.profile(await self._get("/account/edit"))
        elif operation == "course":
            data = dict(await self._course(path["course_id"]))
            rows = (await self._assignments(path["course_id"]))["assignments"]
            data["assignment_count"] = len(rows)
            source = data["source_url"]
        elif operation in {"assignments", "assignment"}:
            data = await self._assignments(path["course_id"])
            source = ORIGIN + f"/courses/{path['course_id']}"
            if operation == "assignment":
                data = next(
                    (a for a in data["assignments"] if a["id"] == path["assignment_id"]), None
                )
                if data is None:
                    raise GradescopeError(
                        "Assignment was not found in the course list.", "not_discovered"
                    )
        else:
            route, raw, data = await self._raw_submission(path)
            source = ORIGIN + route
            if operation == "submission_history":
                data = parsers.parse_json(
                    await self._get(route + ".json", keys=("past_submissions",), react=True)
                )
                if not isinstance(data.get("past_submissions"), list):
                    raise parsers.parse_error()
                data = {
                    "past_submissions": [
                        parsers.select(s, SUBMISSION_HISTORY_KEYS) for s in data["past_submissions"]
                    ]
                }
                if not raw["grades_visible"] or not raw["assignment"].get(
                    "show_old_submission_scores"
                ):
                    for past in data["past_submissions"]:
                        past.pop("score", None)
            elif operation == "submission_files":
                extra = parsers.parse_json(
                    await self._get(
                        route + ".json", keys=("text_files", "file_comments"), react=True
                    )
                )
                data.update(parsers.select(extra, ("text_files", "file_comments")))
                if not raw["grades_visible"]:
                    data["file_comments"] = []
                data = parsers.select(
                    data, ("pdf_attachment", "image_attachments", "text_files", "file_comments")
                )
            elif operation == "questions":
                data = parsers.select(data, ("grades_visible", "questions"))
            elif operation == "autograder":
                data = parsers.select(data, ("grades_visible", "autograder_results"))
            elif operation == "regrades":
                data = parsers.select(data, ("grades_visible", "regrade_requests"))
            elif operation == "group_members":
                data = {"members": data["members"]}
        return {
            "data": data,
            "source_url": source,
            "untrusted_content": True,
            "read_at": datetime.now(UTC).isoformat(),
            "note": "Course discovery may be cached for up to 30 seconds. Other reads are fresh.",
        }


SUBMISSION_HISTORY_KEYS = ("id", "created_at", "score", "active", "status", "source", "url")
