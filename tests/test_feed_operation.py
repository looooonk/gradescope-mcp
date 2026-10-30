import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from gradescope_mcp import server as module
from gradescope_mcp.catalog import OPERATIONS, GradescopeError
from gradescope_mcp.changefeed import FeedError, record
from gradescope_mcp.client import GradescopeClient
from gradescope_mcp.config import Settings


class Adapter:
    errors = (FeedError,)
    coverage = "Synthetic records"
    instance = None

    def __init__(self):
        Adapter.instance = self
        self.role = "student"
        self.authorizations = 0
        self.collects = 0
        self.fail = False
        self.rows = [
            record("assignment", i, "Homework", "https://example.edu/a/1", {"due": "Mon"})
            for i in (1, 2)
        ]

    async def authorize(self, client, course):
        self.authorizations += 1
        return self.role

    def tasks(self, course, role):
        return [{"source": "one"}, {"source": "two"}]

    async def collect(self, client, course, task):
        self.collects += 1
        if self.fail:
            raise FeedError("upstream failed")
        return self.rows, []


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "GradescopeChanges", Adapter)
    mcp = module.create_server(Settings("synthetic@example.edu", "synthetic"), tmp_path)
    monkeypatch.setattr(
        mcp,
        "get_context",
        lambda: SimpleNamespace(request_context=SimpleNamespace(lifespan_context=None)),
    )
    return mcp


async def invoke(server, tool, **args):
    _, result = await server.call_tool("gradescope_" + tool, args)
    return result


async def test_legacy_discovery_and_input_schemas_are_compatible(server):
    expected = json.loads((Path(__file__).parent / "fixtures/legacy-read-schemas.json").read_text())
    tools = {t.name: t for t in await server.list_tools()}
    assert {name: tools[name].inputSchema for name in expected} == expected
    discovered = await invoke(server, "find_operations", search="changes")
    assert '"changes"' in json.dumps(discovered)
    description = await invoke(server, "describe_operation", operation="changes")
    assert description["upstream_route"] is None
    assert description["schema_version"] == 2
    assert description["path_parameters"]["feed"]["properties"]["limit"]["default"] == 30
    assert Adapter.instance.authorizations == 0
    assert all(
        t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in tools.values()
    )


async def test_wrapper_and_dedicated_share_cursors_and_preserve_pages(server):
    first = await invoke(
        server, "read", operation="changes", path={"course_id": "1", "feed": {"budget": 1}}
    )
    assert set(first) == {"operation", "data", "untrusted_content"}
    assert first["data"]["status"] == "scanning" and first["data"]["next_cursor"] is None
    baseline = await invoke(
        server, "changes", course_id="1", continuation=first["data"]["continuation"]
    )
    assert baseline["baseline"] and not baseline["changes"]
    for item in Adapter.instance.rows:
        item["fields"]["due"] = "Tue"
    page = (
        await invoke(
            server,
            "read",
            operation="changes",
            path={"course_id": "1", "feed": {"cursor": baseline["next_cursor"], "limit": 1}},
        )
    )["data"]
    assert len(page["changes"]) == 1 and page["next_cursor"] is None
    assert page["continuation"].startswith("page_") and page["coverage"]["status"] == "complete"
    last = await invoke(server, "changes", course_id="1", continuation=page["continuation"])
    replay = (
        await invoke(
            server,
            "read",
            operation="changes",
            path={"course_id": "1", "feed": {"continuation": page["continuation"]}},
        )
    )["data"]
    assert replay == last and last["next_cursor"] and not last["continuation"]
    assert page["changes"][0]["event_id"] != last["changes"][0]["event_id"]
    Adapter.instance.role = "instructor"
    with pytest.raises(ToolError, match="different account, course, role"):
        await invoke(
            server,
            "read",
            operation="changes",
            path={"course_id": "1", "feed": {"continuation": page["continuation"]}},
        )


async def test_wrapper_retains_partial_coverage_without_projection(server):
    Adapter.instance.fail = True
    result = (await invoke(server, "read", operation="changes", path={"course_id": "1"}))["data"]
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["failure_count"] == 2 and not result["changes"]


@pytest.mark.parametrize(
    "path",
    [
        None,
        {},
        {"course_id": True},
        {"course_id": "1", "cursor": "chg_" + "a" * 32},
        {"course_id": "1", "feed": None},
        {"course_id": "1", "feed": []},
        {"course_id": "1", "feed": {"view": 1}},
        {"course_id": "1", "feed": {"budget": True}},
        {"course_id": "1", "feed": {"limit": "30"}},
        {"course_id": "1", "feed": {"cursor": "api-pagination"}},
        {
            "course_id": "1",
            "feed": {"cursor": "chg_" + "a" * 32, "continuation": "scan_" + "b" * 32},
        },
    ],
)
async def test_invalid_nested_options_fail_before_any_reads(server, path):
    with pytest.raises(ToolError):
        await invoke(server, "read", operation="changes", path=path)
    assert Adapter.instance.authorizations == 0 and Adapter.instance.collects == 0


@pytest.mark.parametrize(
    "options", [{"offset": 1}, {"limit": 1}, {"fields": []}, {"fields": ["changes"]}]
)
async def test_old_list_options_are_not_silently_reinterpreted(server, options):
    with pytest.raises(ToolError):
        await invoke(server, "read", operation="changes", path={"course_id": "1"}, **options)
    assert Adapter.instance.authorizations == 0


async def test_local_operation_never_enters_upstream_catalog():
    assert "changes" not in OPERATIONS

    def forbidden(request):
        pytest.fail("Local operation must be rejected before any HTTP request or login")

    client = GradescopeClient(
        Settings("synthetic@example.edu", "synthetic"), transport=httpx.MockTransport(forbidden)
    )
    try:
        with pytest.raises(GradescopeError):
            await client.read("changes", {"course_id": "1", "feed": {"budget": 1}})
    finally:
        await client.close()
