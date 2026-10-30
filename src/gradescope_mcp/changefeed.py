"""Versioned local snapshot reconciliation; never sends upstream writes.

This file is identical in the three campus repositories. See docs/change-feed.md.
"""

import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

VERSION = 1
RETENTION = 7 * 86400
MAX_RECORDS = 20000
MAX_BYTES = 32 * 1024 * 1024


class FeedError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def now():
    return datetime.now(UTC).isoformat()


def canonical(value):
    if isinstance(value, dict):
        return {
            k: canonical(v)
            for k, v in sorted(value.items())
            if k not in {"avatar_image_url", "thumbnail_url", "read_state", "read", "view_count"}
        }
    if isinstance(value, list):
        items = [canonical(v) for v in value]
        if items and all(isinstance(v, dict) and v.get("id") is not None for v in items):
            items.sort(key=lambda v: str(v["id"]))
        return items
    if isinstance(value, str) and value.startswith("https://"):
        url = urlsplit(value)
        keys = {k.lower() for k, _ in parse_qsl(url.query)}
        if keys & {"x-amz-signature", "signature", "verifier", "sf_verifier"}:
            return urlunsplit(url._replace(query=""))
    return value


def record(kind, identifier, title, url, values):
    """Keep compact comparisons, not full coursework or signed attachment URLs."""
    fields = {}
    for key, value in sorted(canonical(values).items()):
        encoded = json.dumps(value, sort_keys=True)
        fields[key] = value if len(encoded) <= 160 else {"sha256": digest(value)}
    return {
        "key": f"{kind}:{identifier}",
        "kind": kind,
        "id": str(identifier),
        "title": str(title or kind)[:200],
        "source_url": url,
        "fields": fields,
    }


def selected(value, keys):
    return {key: value[key] for key in keys.split() if key in value}


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)

    @contextmanager
    def open(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise FeedError("Change state directory must not be a symlink.")
        os.chmod(self.directory, 0o700)
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
        with os.fdopen(os.open(self.directory / "lock", flags, 0o600), "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise FeedError(
                    "Another change scan is running. Retry the same cursor shortly."
                ) from None
            path = self.directory / "feed.sqlite3"
            fd = os.open(path, flags, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA journal_mode=MEMORY")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS objects "
                    "(id TEXT PRIMARY KEY, scope TEXT, kind TEXT, created REAL, value TEXT)"
                )
                db.execute("DELETE FROM objects WHERE created < ?", (time.time() - RETENTION,))
                db.commit()
                yield db

    def put(self, db, scope, kind, value):
        raw = json.dumps(value, separators=(",", ":"))
        if len(raw.encode()) > MAX_BYTES:
            raise FeedError("Change scan exceeds the local state limit; cursor was not advanced.")
        size = db.execute("SELECT COALESCE(SUM(LENGTH(value)),0) FROM objects").fetchone()[0]
        if size + len(raw) > 256 * 1024 * 1024:
            raise FeedError(
                "Change state is full. Wait for seven-day expiry or remove local feed state."
            )
        token = {"snapshot": "chg_", "scan": "scan_", "events": "page_"}[
            kind
        ] + secrets.token_urlsafe(24)
        db.execute("INSERT INTO objects VALUES (?,?,?,?,?)", (token, scope, kind, time.time(), raw))
        db.commit()
        return token

    def get(self, db, scope, token, kind):
        if not isinstance(token, str) or not re.fullmatch(
            r"(?:chg|scan|page)_[A-Za-z0-9_-]{32}", token
        ):
            raise FeedError("Invalid change cursor. API pagination cursors cannot be used here.")
        row = db.execute("SELECT scope,kind,value FROM objects WHERE id=?", (token,)).fetchone()
        if not row or row[0] != scope or row[1] != kind:
            raise FeedError(
                "Cursor expired or belongs to a different account, course, role, or server. "
                "Omit cursors to establish a new baseline."
            )
        return json.loads(row[2])

    def reply(self, db, token, value, result):
        value["reply"] = result
        db.execute("UPDATE objects SET value=? WHERE id=?", (json.dumps(value), token))
        db.commit()


class ChangeFeed:
    def __init__(self, directory, identity, adapter):
        self.store = Store(directory)
        self.identity = identity
        self.adapter = adapter

    async def read(self, client, course_id, cursor=None, continuation=None, limit=30, budget=10):
        if type(course_id) not in (str, int) or not re.fullmatch(
            r"[1-9][0-9]{0,18}", str(course_id)
        ):
            raise FeedError("course_id must be a positive decimal ID.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise FeedError("limit must be 1-100.")
        if type(budget) is not int or not 1 <= budget <= 20:
            raise FeedError("budget must be 1-20 read tasks per call.")
        if cursor and continuation:
            raise FeedError("Pass cursor or continuation, not both.")
        for token, prefix in ((cursor, "chg_"), (continuation, ("scan_", "page_"))):
            if token is not None and (
                not isinstance(token, str)
                or not token.startswith(prefix)
                or not re.fullmatch(r"[a-z]+_[A-Za-z0-9_-]{32}", token)
            ):
                raise FeedError("Invalid change cursor; do not use an API pagination cursor.")
        course_id = str(course_id)
        with self.store.open() as db:
            role = await self.adapter.authorize(client, course_id)
            scope = digest([VERSION, self.identity, course_id, role])
            if continuation and continuation.startswith("page_"):
                page = self.store.get(db, scope, continuation, "events")
                return self._page(db, scope, page, limit)
            if continuation:
                state = self.store.get(db, scope, continuation, "scan")
                if "reply" in state:
                    return state["reply"]
            else:
                previous = self.store.get(db, scope, cursor, "snapshot") if cursor else None
                state = {
                    "baseline": previous is None,
                    "previous": previous["records"] if previous else {},
                    "previous_observed_at": previous["observed_at"] if previous else None,
                    "records": {},
                    "queue": self.adapter.tasks(course_id, role),
                    "seen_tasks": [],
                    "failures": [],
                    "completed": [],
                    "started_at": now(),
                    "role": role,
                    "course_id": course_id,
                }
            started = time.monotonic()
            for _ in range(budget):
                if not state["queue"] or time.monotonic() - started > 20:
                    break
                task = state["queue"].pop(0)
                key = digest(task)
                if key in state["seen_tasks"]:
                    continue
                state["seen_tasks"].append(key)
                try:
                    records, more = await self.adapter.collect(client, course_id, task)
                except self.adapter.errors as error:
                    status = getattr(error, "http_status", None)
                    state["failures"].append(
                        {
                            "source": task["source"],
                            "code": getattr(error, "code", "read_failed"),
                            "http_status": status,
                            "meaning": "Not observed; no deletion or empty-result inference.",
                        }
                    )
                    continue
                for item in records:
                    state["records"][item["key"]] = item
                if len(state["records"]) > MAX_RECORDS or len(state["seen_tasks"]) > MAX_RECORDS:
                    raise FeedError("Course exceeds the scan limit; no change cursor was advanced.")
                state["queue"].extend(more)
                if len(state["queue"]) > MAX_RECORDS:
                    raise FeedError("Course exceeds the pending-read limit.")
                if task["source"] not in state["completed"]:
                    state["completed"].append(task["source"])
            if state["queue"]:
                token = self.store.put(db, scope, "scan", state)
                result = {
                    "schema_version": VERSION,
                    "status": "scanning",
                    "baseline": state["baseline"],
                    "course_id": course_id,
                    "changes": [],
                    "next_cursor": None,
                    "continuation": token,
                    "pending_tasks": len(state["queue"]),
                    "coverage": self._coverage(state, False),
                    "summary": "Scan in progress. Continue before concluding what changed.",
                }
            else:
                result = self._finish(db, scope, state, limit)
            if continuation:
                self.store.reply(db, continuation, state, result)
            return result

    def _coverage(self, state, complete):
        return {
            "scan_complete": complete,
            "status": "partial" if state["failures"] else "complete" if complete else "in_progress",
            "sources_observed": state["completed"],
            "failures": state["failures"][:100],
            "failure_count": len(state["failures"]),
            "scope": self.adapter.coverage,
            "started_at": state["started_at"],
            "previous_observed_at": state["previous_observed_at"],
            "limitations": [
                (
                    "Snapshot comparison, not an upstream event log; intermediate "
                    "changes may be missed."
                ),
                "Reads are not atomic; changes during pagination may appear on the next full scan.",
                "Missing/inaccessible records are retained; absence never implies deletion.",
            ],
        }

    def _finish(self, db, scope, state, limit):
        observed = now()
        events = []
        old = state["previous"]
        for key, item in sorted(state["records"].items()):
            previous = old.get(key)
            if state["baseline"] or previous == item:
                continue
            before = previous["fields"] if previous else {}
            after = item["fields"]
            changes = {
                k: {"before": before.get(k), "after": after.get(k)}
                for k in sorted(set(before) | set(after))
                if before.get(k) != after.get(k)
            }
            if previous and not changes:
                continue
            kind = "updated" if previous else "newly_observed"
            events.append(
                {
                    "event_id": digest([scope, state["previous_observed_at"], key, before, after]),
                    "change": kind,
                    "resource_type": item["kind"],
                    "resource_id": item["id"],
                    "title": item["title"],
                    "source_url": item["source_url"],
                    "summary": (
                        f"{item['title']}: "
                        + (", ".join(changes) + " changed" if previous else "newly observed")
                    ),
                    "changed_fields": list(changes),
                    "details": changes,
                    "observed_at": observed,
                }
            )
        merged = old | state["records"]
        if len(merged) > MAX_RECORDS:
            raise FeedError(
                "Retained course history exceeds the record limit. Establish a new baseline."
            )
        next_cursor = self.store.put(
            db, scope, "snapshot", {"records": merged, "observed_at": observed}
        )
        page = {
            "schema_version": VERSION,
            "status": "baseline" if state["baseline"] else "changes",
            "baseline": state["baseline"],
            "course_id": state["course_id"],
            "observed_at": observed,
            "coverage": self._coverage(state, True),
            "observed_records": len(state["records"]),
            "total_changes": len(events),
            "summary": (
                "Baseline captured; existing records are not reported as new."
                if state["baseline"]
                else f"{len(events)} meaningful changes observed."
            ),
            "all_changes": events,
            "offset": 0,
            "final_cursor": next_cursor,
            "untrusted_content": True,
        }
        if state["failures"]:
            page["summary"] += " Coverage is partial; failed reads were not treated as empty."
        return self._page(db, scope, page, limit)

    def _page(self, db, scope, page, limit):
        offset = page["offset"]
        events = []
        size = 0
        for event in page["all_changes"][offset : offset + limit]:
            length = len(json.dumps(event).encode())
            if events and size + length > 100000:
                break
            events.append(event)
            size += length
        more = offset + len(events) < len(page["all_changes"])
        continuation = (
            self.store.put(db, scope, "events", page | {"offset": offset + len(events)})
            if more
            else None
        )
        return {
            k: v for k, v in page.items() if k not in {"all_changes", "offset", "final_cursor"}
        } | {
            "changes": events,
            "continuation": continuation,
            "next_cursor": None if more else page["final_cursor"],
            "cursor_expires_after_days": 7,
        }
