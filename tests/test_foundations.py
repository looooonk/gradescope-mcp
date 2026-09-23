import json

import pytest

from gradescope_mcp.config import Settings
from gradescope_mcp.diagnostics import ErrorLog


def test_credentials_never_in_repr_and_no_expansion(tmp_path):
    path = tmp_path / ".env"
    path.write_text("GRADESCOPE_EMAIL=sample@example.test\nGRADESCOPE_PASSWORD='test$PASSWORD'\n")
    settings = Settings.load(path)
    assert settings.password == "test$PASSWORD"
    assert "sample" not in repr(settings) and "PASSWORD" not in repr(settings)
    with pytest.raises(ValueError):
        Settings("", "")


def test_logs_omit_private_messages_and_arguments_and_rotate(tmp_path):
    log = ErrorLog(tmp_path, max_bytes=1)
    for _ in range(3):
        log.record(
            "tool_failure", ValueError("private body/password"), tool="private", operation="private"
        )
    files = list(tmp_path.glob("*.jsonl*"))
    assert len(files) == 3
    for path in files:
        assert "private" not in path.read_text()
        data = json.loads(path.read_text())
        assert data["tool"] is None and data["operation"] is None
        assert path.stat().st_mode & 0o777 == 0o600
