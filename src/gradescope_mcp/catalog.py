import re
from dataclasses import asdict, dataclass


class GradescopeError(Exception):
    def __init__(self, message, code="invalid_request", http_status=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class Operation:
    summary: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    collection: str | None = None
    evidence: str = "docs/operations.md"


COURSE = ("course_id",)
ASSIGNMENT = (*COURSE, "assignment_id")
SUBMISSION = ("submission_id",)
OPERATIONS = {
    "profile": Operation("Your account name, email, institution and timezone; no account edits"),
    "courses": Operation("All enrolled course names, terms and roles", collection="courses"),
    "course": Operation("Student course metadata and assignment counts", COURSE),
    "assignments": Operation(
        "Assignments, release/due/late dates, submission status and visible grades",
        COURSE,
        collection="assignments",
    ),
    "assignment": Operation(
        "One assignment's course-list metadata without starting it", ASSIGNMENT
    ),
    "submission": Operation(
        "Your existing submission, visible feedback, rubric, annotations and assignment details",
        ASSIGNMENT,
        SUBMISSION,
    ),
    "questions": Operation(
        "Question text, your answers, scores, rubric items, comments and annotations",
        ASSIGNMENT,
        SUBMISSION,
        "questions",
    ),
    "submission_history": Operation(
        "Your existing submission attempts, timestamps and available prior scores",
        ASSIGNMENT,
        SUBMISSION,
        "past_submissions",
    ),
    "submission_files": Operation(
        "Submitted source files, file comments, original PDFs and scanned-page metadata",
        ASSIGNMENT,
        SUBMISSION,
    ),
    "autograder": Operation(
        "Student-visible programming test results and autograder feedback",
        ASSIGNMENT,
        SUBMISSION,
    ),
    "regrades": Operation(
        "Your existing regrade requests and staff responses; cannot create or update requests",
        ASSIGNMENT,
        SUBMISSION,
        "regrade_requests",
    ),
    "group_members": Operation(
        "Members associated with your submission; cannot change group membership",
        ASSIGNMENT,
        SUBMISSION,
        "members",
    ),
}


def describe(name):
    if not isinstance(name, str) or name not in OPERATIONS:
        raise GradescopeError("Unknown operation. Use gradescope_find_operations.")
    return {
        "operation": name,
        **asdict(OPERATIONS[name]),
        "notes": (
            "Student courses and your discovered submissions only. Omit submission_id "
            "for the current attempt; submission_history discovers older attempts. Null grades "
            "are not zero. No arbitrary query parameters. offset/limit paginate locally."
        ),
    }


def validate(name, path=None):
    describe(name)
    op = OPERATIONS[name]
    if path is None:
        path = {}
    if not isinstance(path, dict) or set(path) - set(op.required + op.optional):
        raise GradescopeError("Unexpected path parameters; inspect gradescope_describe_operation.")
    if set(op.required) - set(path):
        raise GradescopeError("Required path parameters are missing.")
    result = {}
    for key, value in path.items():
        if type(value) not in (int, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", str(value)):
            raise GradescopeError("IDs must be positive decimal integers or decimal strings.")
        result[key] = str(value)
    return result


def validate_request(method, route, params=None):
    paths = (
        r"/",
        r"/account(?:/edit)?",
        r"/courses/[1-9][0-9]*",
        r"/courses/[1-9][0-9]*/assignments/[1-9][0-9]*/submissions/[1-9][0-9]*\.json",
    )
    if method == "POST" and route == "/login" and not params:
        return
    if method != "GET" or not any(re.fullmatch(pattern, route) for pattern in paths):
        raise GradescopeError("Request route or method is not allowlisted.", "blocked_request")
    params = params or {}
    if set(params) - {"content", "only_keys[]"}:
        raise GradescopeError("Request parameters are not allowlisted.", "blocked_request")
    if params and (not route.endswith(".json") or params.get("content") != "react"):
        raise GradescopeError("Unexpected request parameters.", "blocked_request")
    if "only_keys[]" in params and params["only_keys[]"] not in [
        ["past_submissions"],
        ["text_files", "file_comments"],
    ]:
        raise GradescopeError("JSON field request is not allowlisted.", "blocked_request")
