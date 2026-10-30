import json
import sqlite3

import pytest

from gradescope_mcp.changefeed import ChangeFeed, FeedError, record


class Adapter:
    errors = (FeedError,)
    coverage = "Synthetic records"

    def __init__(self):
        self.role = "student"
        self.calls = 0
        self.rows = [
            record("assignment", 1, "Homework", "https://example.edu/a/1", {"due_at": "Monday"})
        ]
        self.fail = False

    async def authorize(self, client, course):
        return self.role

    def tasks(self, course, role):
        return [{"source": "first"}, {"source": "second"}]

    async def collect(self, client, course, task):
        self.calls += 1
        if self.fail:
            raise FeedError("private upstream message")
        return self.rows, []


async def complete(feed, **kwargs):
    result = await feed.read(None, "1", **kwargs)
    while result["continuation"]:
        result = await feed.read(None, "1", continuation=result["continuation"])
    return result


async def test_baseline_resume_restart_update_and_no_change(tmp_path):
    adapter = Adapter()
    feed = ChangeFeed(tmp_path, "account", adapter)
    first = await feed.read(None, "1", budget=1)
    assert first["status"] == "scanning" and first["next_cursor"] is None
    token = first["continuation"]
    result = await feed.read(None, "1", continuation=token)
    assert result["baseline"] and result["changes"] == []
    calls = adapter.calls
    assert await feed.read(None, "1", continuation=token) == result
    assert adapter.calls == calls
    feed = ChangeFeed(tmp_path, "account", adapter)
    unchanged = await complete(feed, cursor=result["next_cursor"])
    assert unchanged["total_changes"] == 0
    adapter.rows[0]["fields"]["due_at"] = "Tuesday"
    changed = await complete(feed, cursor=unchanged["next_cursor"])
    assert len(changed["changes"]) == 1
    event = changed["changes"][0]
    assert event["details"]["due_at"] == {"before": "Monday", "after": "Tuesday"}
    assert event["source_url"] == "https://example.edu/a/1"
    replay = await complete(feed, cursor=unchanged["next_cursor"])
    assert replay["changes"][0]["event_id"] == event["event_id"]


async def test_partial_failure_retains_state_and_never_infers_deletion(tmp_path):
    a = Adapter()
    feed = ChangeFeed(tmp_path, "account", a)
    baseline = await complete(feed)
    a.fail = True
    failed = await complete(feed, cursor=baseline["next_cursor"])
    assert failed["changes"] == [] and failed["coverage"]["status"] == "partial"
    assert "private upstream" not in json.dumps(failed)
    a.fail = False
    a.rows = []
    absent = await complete(feed, cursor=failed["next_cursor"])
    assert absent["changes"] == []
    a.rows = [record("assignment", 1, "Homework", "https://example.edu/a/1", {"due_at": "Friday"})]
    recovered = await complete(feed, cursor=absent["next_cursor"])
    assert recovered["changes"][0]["change"] == "updated"


async def test_event_pagination_is_separate_and_replayable(tmp_path):
    a = Adapter()
    feed = ChangeFeed(tmp_path, "account", a)
    baseline = await complete(feed)
    a.rows.extend(
        record("reply", n, "Reply", "https://example.edu/thread/1", {"text": str(n)})
        for n in range(2, 5)
    )
    result = await feed.read(None, "1", cursor=baseline["next_cursor"], limit=1)
    assert result["next_cursor"] is None and result["continuation"].startswith("page_")
    a.fail = True
    page = await feed.read(None, "1", continuation=result["continuation"], limit=1)
    replay = await feed.read(None, "1", continuation=result["continuation"], limit=1)
    assert page["changes"] == replay["changes"]
    last = await feed.read(None, "1", continuation=page["continuation"])
    assert last["continuation"] is None and last["next_cursor"].startswith("chg_")
    assert last["total_changes"] == 3


async def test_scope_role_expiry_tamper_and_api_cursors(tmp_path):
    a = Adapter()
    feed = ChangeFeed(tmp_path, "account", a)
    baseline = await complete(feed)
    token = baseline["next_cursor"]
    for wrong in ["api-cursor", "../etc/passwd", "chg_" + "a" * 32]:
        with pytest.raises(FeedError):
            await feed.read(None, "1", cursor=wrong)
    with pytest.raises(FeedError):
        await feed.read(None, "2", cursor=token)
    with pytest.raises(FeedError):
        await ChangeFeed(tmp_path, "different-account", a).read(None, "1", cursor=token)
    a.role = "instructor"
    with pytest.raises(FeedError):
        await feed.read(None, "1", cursor=token)
    a.role = "student"
    with sqlite3.connect(tmp_path / "feed.sqlite3") as db:
        db.execute("UPDATE objects SET created=0")
    with pytest.raises(FeedError):
        await feed.read(None, "1", cursor=token)
    assert (tmp_path / "feed.sqlite3").stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("value", [True, 0, -1, "1/../../", {}, "0", 1.1])
async def test_invalid_course_before_reads(tmp_path, value):
    a = Adapter()
    with pytest.raises(FeedError):
        await ChangeFeed(tmp_path, "account", a).read(None, value)
    assert a.calls == 0


def test_large_content_is_hashed_not_persisted():
    result = record("reply", 1, "Reply", "https://example.edu", {"body": "private work " * 100})
    assert set(result["fields"]["body"]) == {"sha256"}
    assert "private work" not in json.dumps(result)


def test_signed_url_rotation_and_reply_order_do_not_create_noise():
    from gradescope_mcp.changefeed import canonical

    left = {
        "comments": [
            {"id": 2, "url": "https://storage.example/file?X-Amz-Signature=old"},
            {"id": 1, "content": "hello"},
        ]
    }
    right = {
        "comments": [
            {"id": 1, "content": "hello"},
            {"id": 2, "url": "https://storage.example/file?X-Amz-Signature=new"},
        ]
    }
    assert canonical(left) == canonical(right)
