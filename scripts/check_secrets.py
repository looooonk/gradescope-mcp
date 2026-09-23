"""Reject private files and configured credentials before committing."""

import io
import json
import subprocess
from contextlib import redirect_stderr
from pathlib import Path
from urllib.parse import quote, quote_plus

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def main():
    paths = (
        subprocess.check_output(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"], cwd=ROOT
        )
        .decode()
        .split("\0")
    )
    with redirect_stderr(io.StringIO()):
        values = dotenv_values(ROOT / ".env", interpolate=False)
    secrets = set()
    for key in ("GRADESCOPE_EMAIL", "GRADESCOPE_PASSWORD"):
        value = values.get(key)
        if value:
            secrets.update(
                s.encode()
                for s in (value, quote(value), quote_plus(value), json.dumps(value)[1:-1])
            )
    for name in filter(None, paths):
        path = Path(name)
        if any(p in {".local", ".venv"} for p in path.parts) or (
            path.name.startswith(".env") and path.name != ".env.example"
        ):
            raise SystemExit("Commit blocked: a private local artifact is staged.")
        content = subprocess.check_output(["git", "show", f":{name}"], cwd=ROOT)
        if any(secret in content for secret in secrets):
            raise SystemExit("Commit blocked: configured credentials occur in staged content.")


if __name__ == "__main__":
    main()
