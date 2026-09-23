.PHONY: check install live verify serve
check:
	sh scripts/check.sh
install:
	uv sync --locked
	uv run python scripts/install_codex.py
live:
	uv run python scripts/live_smoke.py
verify:
	uv run python scripts/verify_codex.py
serve:
	uv run gradescope-mcp
