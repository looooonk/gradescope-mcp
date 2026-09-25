import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from gradescope_mcp.config import Settings
from gradescope_mcp.server import create_server


async def test_validation_hints_are_safe_and_classified(tmp_path):
    server = create_server(Settings("fake@example.test", "fake-password"), tmp_path)
    with pytest.raises(ToolError) as caught:
        await server.call_tool(
            "gradescope_read",
            {"operation": "courses", "limit": 101, "fields": [{"private-marker": "secret-value"}]},
        )
    message = str(caught.value)
    for hint in ["limit", "maximum 100", "fields"]:
        assert hint in message
    assert "private-marker" not in message and "secret-value" not in message
    raw = (tmp_path / "gradescope-mcp.jsonl").read_text()
    assert "private-marker" not in raw and "secret-value" not in raw
    assert json.loads(raw)["code"] == "invalid_arguments"


async def test_nested_validation_locations_and_runtime_errors_are_not_echoed(tmp_path):
    server = create_server(Settings("fake@example.test", "fake-password"), tmp_path)

    @server.tool()
    def nested(values: dict[str, int]):
        raise RuntimeError("private-runtime-error")

    with pytest.raises(ToolError) as caught:
        await server.call_tool("nested", {"values": {"private-key": "private-value"}})
    assert "values" in str(caught.value)
    assert "private" not in str(caught.value)
    with pytest.raises(ToolError) as caught:
        await server.call_tool("nested", {"values": {}})
    assert "tool failed" in str(caught.value)
    assert "private" not in str(caught.value)
    assert "private" not in (tmp_path / "gradescope-mcp.jsonl").read_text()


async def test_stdio_verification_log_directory_is_isolated(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GRADESCOPE_EMAIL=fake@example.test\nGRADESCOPE_PASSWORD=fake-password\n")
    log_dir = tmp_path / "verification" / "logs"
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "gradescope_mcp.server",
            "--env-file",
            str(env_file),
            "--log-dir",
            str(log_dir),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("CANVAS_", "ED_", "GRADESCOPE_"))
        },
    )
    with (tmp_path / "stderr.log").open("w") as err:
        async with (
            stdio_client(params, errlog=err) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "gradescope_read",
                {
                    "operation": "courses",
                    "limit": 101,
                    "fields": [{"private-marker": "secret-value"}],
                },
            )
            assert result.isError
            response = result.model_dump_json()
            assert "Invalid arguments" in response and "private-marker" not in response
    log = log_dir / "gradescope-mcp.jsonl"
    assert log.is_file() and log.stat().st_mode & 0o777 == 0o600
    assert json.loads(log.read_text())["code"] == "invalid_arguments"
    assert "private-marker" not in (tmp_path / "stderr.log").read_text()
    assert not (tmp_path / ".local/logs").exists()
