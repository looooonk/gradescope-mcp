"""Local feed operation adapter; no upstream route or tool-signature changes."""

from gradescope_mcp.changefeed import VERSION, FeedError

SUMMARY = "Incremental change feed for a course: baseline, checkpoints and explicit coverage"


def arguments(path):
    if not isinstance(path, dict) or set(path) - {"course_id", "feed"}:
        raise FeedError("changes path accepts only course_id and the optional feed object.")
    if "course_id" not in path:
        raise FeedError("changes requires path.course_id.")
    options = path.get("feed", {})
    if not isinstance(options, dict) or set(options) - {
        "cursor",
        "continuation",
        "limit",
        "budget",
    }:
        raise FeedError("path.feed accepts only cursor, continuation, limit and budget.")
    return {"course_id": path["course_id"], **options}


def envelope(result):
    return {"operation": "changes", "data": result, "untrusted_content": True}


def describe(prefix, scope):
    return {
        "operation": "changes",
        "summary": SUMMARY,
        "execution": "Local snapshot reconciliation using existing allowlisted campus reads",
        "upstream_route": None,
        "path_parameters": {
            "course_id": {
                "required": True,
                "type": "positive decimal string or integer, at most 19 digits",
            },
            "feed": {
                "required": False,
                "type": "object",
                "additional_properties": False,
                "properties": {
                    "cursor": "Optional chg_ checkpoint from a finished prior scan",
                    "continuation": "Optional scan_ or page_ token; do not also supply cursor",
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
                    "budget": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
                },
            },
        },
        "query": {},
        "wrapper_options": (
            "Keep query/cursor/fields omitted, offset=0 and the wrapper limit at its default. "
            "Only path.feed supplies feed options. Ed compact has no effect on this operation."
        ),
        "schema_version": VERSION,
        "result": "The unmodified feed result is in data; coverage and cursors are never filtered.",
        "pagination": (
            "First omit path.feed cursors for a baseline with no historical alerts. Follow "
            "data.continuation in path.feed.continuation until null, then save data.next_cursor. "
            "For later checks pass the saved value in path.feed.cursor. Do not use API "
            "pagination, top-level cursor, offset or per_page. Process all event pages before "
            "saving the checkpoint; deduplicate by event_id."
        ),
        "coverage": scope,
        "notes": (
            "Same state, validation and cursors as the dedicated changes tool on this server. "
            "Permission is rechecked; cursors are account/course/role/server scoped and expire "
            "after seven days. Partial failures never imply deletion. Only local state is written."
        ),
        "examples": [
            {
                "tool": prefix + "_read",
                "arguments": {
                    "operation": "changes",
                    "path": {"course_id": "123", "feed": {"budget": 1}},
                },
            },
            {
                "tool": prefix + "_read",
                "arguments": {
                    "operation": "changes",
                    "path": {"course_id": "123", "feed": {"continuation": "<data.continuation>"}},
                },
            },
            {
                "tool": prefix + "_read",
                "arguments": {
                    "operation": "changes",
                    "path": {"course_id": "123", "feed": {"cursor": "<data.next_cursor>"}},
                },
            },
        ],
        "documentation": f"https://github.com/looooonk/{prefix}-mcp/blob/main/docs/change-feed.md",
    }
