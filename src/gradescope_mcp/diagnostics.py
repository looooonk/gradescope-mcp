import asyncio
import fcntl
import json
import os
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from gradescope_mcp.catalog import OPERATIONS, GradescopeError

TOOL_NAMES = {
    "gradescope_find_operations",
    "gradescope_describe_operation",
    "gradescope_read",
    "gradescope_read_text",
    "gradescope_list_files",
    "gradescope_read_file",
    "gradescope_read_image",
}


class ErrorLog:
    def __init__(self, directory: Path, max_bytes: int = 1024 * 1024, backups: int = 3):
        self.directory, self.max_bytes, self.backups = directory, max_bytes, backups

    def record(self, event: str, error: Exception, *, tool=None, operation=None):
        cause = error
        seen = set()
        while id(cause) not in seen:
            seen.add(id(cause))
            if isinstance(cause, BaseExceptionGroup) and cause.exceptions:
                cause = cause.exceptions[0]
            elif cause.__cause__ is not None:
                cause = cause.__cause__
            else:
                break
        record = {
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            "pid": os.getpid(),
            "error_type": type(cause).__name__,
            "tool": tool if tool in TOOL_NAMES else None,
            "operation": operation
            if isinstance(operation, str) and operation in OPERATIONS
            else None,
            "code": cause.code if isinstance(cause, GradescopeError) else "tool_or_runtime_error",
            "http_status": cause.http_status if isinstance(cause, GradescopeError) else None,
            "frames": [
                {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(cause.__traceback__)[-6:]
            ],
        }
        # Omit exception messages, arguments, URLs, headers, and response bodies entirely.
        line = json.dumps(record) + "\n"
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            log = self.directory / "gradescope-mcp.jsonl"
            with os.fdopen(
                os.open(self.directory / ".lock", os.O_CREAT | os.O_RDWR, 0o600), "w"
            ) as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if log.exists() and log.stat().st_size + len(line.encode()) > self.max_bytes:
                    for index in range(self.backups, 0, -1):
                        source = log if index == 1 else log.with_suffix(f".jsonl.{index - 1}")
                        if source.exists():
                            source.replace(log.with_suffix(f".jsonl.{index}"))
                with os.fdopen(
                    os.open(log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600), "a"
                ) as out:
                    out.write(line)
        except OSError:
            print("Gradescope MCP could not write its local diagnostic log.", file=sys.stderr)


class LoggedMCP(FastMCP):
    def __init__(self, *args, error_log: ErrorLog, **kwargs):
        self.error_log = error_log
        super().__init__(*args, **kwargs)

    async def call_tool(self, name, arguments):
        from mcp.server.fastmcp.exceptions import ToolError

        try:
            tool = self._tool_manager.get_tool(name)
            if tool and isinstance(arguments, dict):
                allowed = set(tool.parameters.get("properties", {}))
                if set(arguments) - allowed:
                    raise GradescopeError("Unexpected tool arguments; inspect the tool schema.")
            async with asyncio.timeout(55):
                return await super().call_tool(name, arguments)
        except Exception as error:
            operation = arguments.get("operation") if isinstance(arguments, dict) else None
            self.error_log.record("tool_failure", error, tool=name, operation=operation)
            cause = error
            while cause.__cause__ is not None:
                cause = cause.__cause__
            message = (
                str(cause)
                if isinstance(cause, GradescopeError)
                else (
                    "Gradescope tool failed or arguments were invalid. Inspect the tool schema; "
                    "safe diagnostics are in .local/logs/gradescope-mcp.jsonl."
                )
            )
            raise ToolError(message) from None

    async def read_resource(self, uri):
        from mcp.server.fastmcp.exceptions import ToolError

        try:
            return await super().read_resource(uri)
        except Exception as error:
            self.error_log.record("resource_failure", error)
            raise ToolError("Gradescope resource failed; see the local diagnostic log.") from None
