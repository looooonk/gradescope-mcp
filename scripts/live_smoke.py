"""Explicit read-only verification through a fresh MCP process; print safe metadata only."""

import asyncio
import json
import logging
import os
from pathlib import Path

from bs4 import BeautifulSoup
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from gradescope_mcp.client import GradescopeClient
from gradescope_mcp.config import Settings
from gradescope_mcp.diagnostics import ErrorLog

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"


async def main():
    LOCAL.mkdir(exist_ok=True, mode=0o700)
    work = LOCAL / "empty-working-directory"
    work.mkdir(exist_ok=True, mode=0o700)
    expected = {}
    summary = {"transport": "stdio", "outside_repository_cwd": True}
    server = StdioServerParameters(
        command=str(ROOT / ".venv/bin/gradescope-mcp"),
        args=["--env-file", str(ROOT / ".env")],
        cwd=str(work),
        env={"PATH": os.defpath},
    )
    with (LOCAL / "live-stderr.log").open("w") as err:
        async with (
            stdio_client(server, errlog=err) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert len(tools) == 7 and all(t.annotations.readOnlyHint for t in tools)
            summary["tools"] = len(tools)

            async def call(name, args):
                result = await session.call_tool(name, args)
                if result.isError:
                    raise RuntimeError("MCP read failed; consult safe logs")
                return result.structuredContent

            async def read(operation, path=None, **kwargs):
                return await call(
                    "gradescope_read", {"operation": operation, "path": path, **kwargs}
                )

            catalog = await call("gradescope_find_operations", {})
            summary["operations"] = len(catalog)
            for name in catalog:
                await call("gradescope_describe_operation", {"operation": name})
            await read("profile")
            first = await read("courses", limit=1)
            courses = first["data"]["courses"]
            offset = first["pagination"]["next_offset"]
            while offset is not None:
                page = await read("courses", offset=offset, limit=100)
                courses += page["data"]["courses"]
                offset = page["pagination"]["next_offset"]
            expected["course_count"] = len(courses)
            summary["course_pagination"] = "verified"
            candidates = []
            for course in [c for c in courses if c["role"] == "student"][:12]:
                path = {"course_id": course["id"]}
                rows = []
                offset = 0
                while offset is not None:
                    page = await read("assignments", path, offset=offset, limit=100)
                    rows += page["data"]["assignments"]
                    offset = page["pagination"]["next_offset"]
                submitted = [r for r in rows if r["submission_id"]]
                for row in submitted[:2]:
                    candidates.append((course, rows, row))
                if len(candidates) >= 6:
                    break
            if not candidates:
                raise RuntimeError("No existing student submission available for verification")
            course, rows, row = candidates[0]
            path = {"course_id": course["id"], "assignment_id": row["id"]}
            await read("course", {"course_id": course["id"]})
            await read("assignment", path)
            detail = (
                await read(
                    "submission", path, fields=["assignment", "submission", "grades_visible"]
                )
            )["data"]
            questions = await read("questions", path, limit=1)
            history = await read("submission_history", path, limit=1)
            for name, fields in [
                ("submission_files", ["file_comments"]),
                ("autograder", ["grades_visible"]),
                ("regrades", None),
                ("group_members", None),
            ]:
                await read(name, path, fields=fields)
            title = await call(
                "gradescope_read_text",
                {
                    "operation": "submission",
                    "path": path,
                    "field": "assignment.title",
                    "max_chars": 2000,
                },
            )
            expected.update(
                course_id=course["id"],
                course_name=course["name"],
                assignment_count=len(rows),
                assignment_id=row["id"],
                assignment_name=row["name"],
                due_at=row["due_at"],
                submission_id=str(detail["submission"]["id"]),
                score=row["score"],
                question_count=questions["pagination"]["total_items"],
                history_count=history["pagination"]["total_items"],
                title_text=title["text"],
            )
            # Independently compare MCP assignment metadata with the fresh website table.
            direct = GradescopeClient(Settings.load(ROOT / ".env"))
            try:
                html = await direct._get(f"/courses/{course['id']}")
                soup = BeautifulSoup(html, "html.parser")
                html_rows = soup.select("#assignments-student-table tbody tr")
                assert len([r for r in html_rows if r.find("th")]) == len(rows)
                link = soup.find("a", href=detail_source(path, expected["submission_id"]))
                assert link and link.get_text(" ", strip=True) == row["name"]
                status = (
                    link.find_parent("tr")
                    .find_all("td", recursive=False)[0]
                    .get_text(" ", strip=True)
                )
                assert status == row["status"]
            finally:
                await direct.close()
            summary["website_metadata_comparison"] = "verified"
            summary["operations_exercised"] = len(catalog)
            for arguments in [
                {"operation": "submit", "path": path},
                {"operation": "courses", "query": {"view": 1}},
                {"operation": "courses", "path": {"url": "https://example.invalid"}},
            ]:
                assert (await session.call_tool("gradescope_read", arguments)).isError
            summary["rejected_writes_and_tracking"] = "verified"
            summary["text_file"] = "no_sample"
            summary["image"] = "no_sample"
            summary["pdf"] = "no_sample"
            for candidate_course, _, candidate in candidates:
                source = {"course_id": candidate_course["id"], "assignment_id": candidate["id"]}
                listing = await call("gradescope_list_files", {"path": source})
                for kind in ("source", "pdf", "page_image", "image"):
                    item = next((f for f in listing["files"] if f["kind"] == kind), None)
                    if not item:
                        continue
                    params = {"path": source, "index": item["index"]}
                    if kind in {"page_image", "image"} and summary["image"] != "verified":
                        result = await session.call_tool("gradescope_read_image", params)
                        assert not result.isError and any(c.type == "image" for c in result.content)
                        expected["image_source"] = params
                        summary["image"] = "verified"
                    elif kind in {"pdf", "source"}:
                        key = "pdf" if kind == "pdf" else "text_file"
                        if summary[key] == "verified":
                            continue
                        result = await call("gradescope_read_file", params | {"max_chars": 2000})
                        summary[key] = "verified"
                        if kind == "source" and len(result["text"].strip()) >= 30:
                            expected["file_source"] = params
                            expected["file_text_sample"] = result["text"]
                if all(summary[k] == "verified" for k in ("text_file", "pdf", "image")):
                    break
            summary["result"] = "verified"
    (LOCAL / "live-expected.json").write_text(json.dumps(expected, indent=2))
    (LOCAL / "live-summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary))


def detail_source(path, submission_id):
    base = f"/courses/{path['course_id']}/assignments/{path['assignment_id']}"
    return f"{base}/submissions/{submission_id}"


if __name__ == "__main__":
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(main())
    except Exception as error:
        ErrorLog(LOCAL / "logs").record("live_verification_failure", error)
        raise SystemExit(
            "Live verification failed; see safe local diagnostics. Private values withheld."
        ) from None
