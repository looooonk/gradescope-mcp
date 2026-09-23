"""Bound document parsing in a short-lived process, without a credential environment."""

import json
import logging
import resource
import sys

from gradescope_mcp.content import MAX_FILE_BYTES, extract_text


def main():
    logging.disable(logging.CRITICAL)
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    try:
        body = sys.stdin.buffer.read(MAX_FILE_BYTES + 1)
        if len(body) > MAX_FILE_BYTES:
            raise ValueError
        text, warning = extract_text(body, sys.argv[1], sys.argv[2])
        if len(text) > 2_000_000:
            raise ValueError
        print(json.dumps({"text": text, "warning": warning}))
    except Exception:
        print(json.dumps({"error": "Document extraction failed or exceeded its limits."}))


if __name__ == "__main__":
    main()
