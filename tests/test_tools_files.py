import asyncio
import io
import json

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from PIL import Image

from gradescope_mcp.catalog import GradescopeError
from gradescope_mcp.config import Settings
from gradescope_mcp.content import text_slice
from gradescope_mcp.files import (
    attachment_items,
    download_file,
    extract,
    read_file,
    safe_storage_url,
)
from gradescope_mcp.server import create_server

URL = "https://production-gradescope-uploads.s3-us-west-2.amazonaws.com/uploads/text_file/file/123/code.py?X-Amz-Signature=private-signature"
PATH = {"course_id": "10", "assignment_id": "20"}


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/file",
        URL.replace("https:", "http:"),
        URL.replace(".com/", ".com.evil.test/"),
        URL.replace(".com/", ".com@evil.test/"),
        URL.replace("/code.py", "/%252e%252e"),
        URL.replace("/code.py", "/%2e%2e/%2fsecret"),
        URL.replace(".com/", ".com:443/"),
        URL + "&view=1",
        URL + "#fragment",
        "https://www.gradescope.com/courses/10/assignments/20/submissions/30.pdf",
    ],
)
def test_unsafe_storage_urls_rejected(url):
    assert not safe_storage_url(url)


class Stub:
    def __init__(self):
        self.calls = []
        self.slots = asyncio.Semaphore(1)

    async def read(self, operation, path=None):
        self.calls.append((operation, path))
        if operation == "submission_files":
            data = {"text_files": [{"path": "code.py", "file": {"url": URL}}]}
        elif operation == "courses":
            data = {"courses": [{"id": str(i)} for i in range(3)]}
        else:
            raise RuntimeError("private-response-credential")
        return {"data": data, "source_url": "https://www.gradescope.com/account"}

    async def close(self):
        pass


async def test_storage_has_no_account_cookies_and_redirects_are_blocked():
    client = Stub()
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET"
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(
            200, content=b"print('example')", headers={"content-type": "text/plain"}
        )

    result = await read_file(client, PATH, 0, max_chars=5, transport=httpx.MockTransport(respond))
    assert result["text"] == "print" and result["next_offset"] == 5
    assert "private-signature" not in json.dumps(result)

    def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://example.test/secret"})

    with pytest.raises(GradescopeError, match="redirects"):
        await download_file(client, PATH, 0, transport=httpx.MockTransport(redirect))
    assert len(calls) == 2


async def test_invalid_file_args_do_not_read_source():
    client = Stub()
    with pytest.raises(GradescopeError):
        await download_file(client, PATH, -1)
    with pytest.raises(GradescopeError):
        await download_file(client, {"url": URL}, 0)
    with pytest.raises(GradescopeError):
        await read_file(client, PATH, 0, max_chars=0)
    assert client.calls == []


async def test_worker_image_and_text_extraction():
    stream = io.BytesIO()
    Image.new("RGB", (2100, 40), "white").save(stream, format="PNG")
    output = await extract(stream.getvalue(), "page.png", "image/png", image=True)
    with Image.open(io.BytesIO(output)) as image:
        assert image.width == 2000
    with pytest.raises(GradescopeError):
        await extract(b"not an image", "bad.png", "image/png", image=True)
    assert (await extract(b"hello world", "hello.txt", "text/plain"))[0] == "hello world"


async def test_tools_strict_arguments_safe_errors_and_logged_failures(tmp_path):
    client = Stub()
    server = create_server(Settings("test@example.test", "secret"), tmp_path, client=client)
    tools = await server.list_tools()
    assert len(tools) == 7
    assert all(t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in tools)
    for arguments in [
        {"operation": "courses", "url": "private"},
        {"operation": "courses", "path": {"arbitrary": "private"}},
        {"operation": "courses", "offset": -1},
        {"operation": "courses", "limit": True},
        {"operation": "courses", "fields": []},
    ]:
        with pytest.raises(ToolError):
            await server.call_tool("gradescope_read", arguments)
    assert not client.calls
    with pytest.raises(ToolError) as error:
        await server.call_tool("gradescope_read", {"operation": "profile"})
    assert "private-response" not in str(error.value)
    logs = (tmp_path / "gradescope-mcp.jsonl").read_text()
    assert "private" not in logs and "secret" not in logs
    assert len(logs.splitlines()) == 6


async def test_tool_pagination_and_text_slices(tmp_path):
    server = create_server(Settings("test@example.test", "secret"), tmp_path, client=Stub())
    _, result = await server.call_tool(
        "gradescope_read", {"operation": "courses", "offset": 1, "limit": 1}
    )
    assert result["data"]["courses"] == [{"id": "1"}]
    assert result["pagination"]["next_offset"] == 2
    assert result["pagination"]["total_items"] == 3
    assert text_slice("abcdef", 1, 2)["text"] == "bc"


async def test_field_selection_is_rooted_after_pagination_and_missing_fields_fail(tmp_path):
    server = create_server(Settings("test@example.test", "secret"), tmp_path, client=Stub())
    _, result = await server.call_tool(
        "gradescope_read",
        {"operation": "courses", "offset": 1, "limit": 1, "fields": ["courses.0.id"]},
    )
    assert result["data"] == {"courses.0.id": "1"}
    assert result["pagination"]["next_offset"] == 2
    with pytest.raises(ToolError) as caught:
        await server.call_tool("gradescope_read", {"operation": "courses", "fields": ["private"]})
    message = str(caught.value)
    assert "data root" in message and "courses.0.id" in message and "Omit fields" in message
    assert "private" not in message


def test_untrusted_links_are_not_generic_download_targets():
    assert attachment_items({"text_files": [{"url": "https://example.test/", "path": "bad"}]}) == []
