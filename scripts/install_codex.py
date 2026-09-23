"""Install the local stdio command in the user's persistent Codex configuration."""

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from gradescope_mcp.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def main():
    Settings.load(ROOT / ".env")
    (ROOT / ".env").chmod(0o600)
    command = ROOT / ".venv/bin/gradescope-mcp"
    if not command.exists():
        raise SystemExit("Run uv sync --locked first.")
    codex = shutil.which("codex")
    if not codex:
        raise SystemExit("Codex CLI is not on PATH.")
    config = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    if config.exists():
        backup_dir = ROOT / ".local/config-backups"
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = backup_dir / f"config-{datetime.now():%Y%m%d-%H%M%S-%f}.toml"
        shutil.copyfile(config, backup)
        backup.chmod(0o600)
    subprocess.run(
        [
            codex,
            "mcp",
            "add",
            "gradescope",
            "--",
            str(command),
            "--env-file",
            str(ROOT / ".env"),
        ],
        check=True,
    )
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=ROOT, check=True)
    print("Gradescope is registered persistently. Start a new Codex task to load it.")


if __name__ == "__main__":
    main()
