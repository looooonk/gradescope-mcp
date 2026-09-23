#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run python scripts/check_secrets.py
