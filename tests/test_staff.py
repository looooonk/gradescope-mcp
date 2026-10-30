import html
import json

import httpx
import pytest
from test_reads import ACCOUNT

from gradescope_mcp.catalog import GradescopeError, validate_request
from gradescope_mcp.client import GradescopeClient
from gradescope_mcp.config import Settings
from gradescope_mcp.staff import assignments, regrades, request_detail

ROW = {
    "id": "assignment_20",
    "type": "assignment",
    "title": "Synthetic assignment",
    "num_active_submissions": 10,
    "grading_progress": 50,
    "open_regrade_request_count": 1,
    "regrade_request_url": "/courses/10/assignments/20/regrade_requests",
}
DASHBOARD = (
    '<div data-react-class="AssignmentsTable" data-react-props="'
    + html.escape(json.dumps({"table_data": [ROW]}))
    + '"></div>'
)
QUEUE = """<table class="js-regradeRequestsTable"><thead><tr>
<th>Student</th><th>Question</th><th>Completed</th><th>Review</th>
</tr></thead><tbody>
<tr><td>Anonymous 1</td>
<td><a href="/courses/10/assignments/20/submissions/30#Question_40">Q1</a></td>
<td>No</td><td><a href="/courses/10/questions/40/submissions/30/grade">Review</a></td>
</tr></tbody></table>"""
RAW = {
    "questions": [{"id": 40, "anchor": "Question_40"}],
    "current_user": {"is_instructor": True},
    "assignment": {"id": 20},
    "assignment_submission": {"id": 30},
    "question_submissions": [{"id": 50, "question_id": 40}],
    "regrade_requests": [
        {
            "id": 60,
            "question_submission_id": 50,
            "student_comment": "Please check",
            "staff_comment": "Pending response",
            "completed": False,
        },
        {"id": 61, "question_submission_id": 99, "student_comment": "unrelated"},
    ],
}
PATH = {
    "course_id": "10",
    "assignment_id": "20",
    "submission_id": "30",
    "question_anchor": "Question_40",
}


def test_staff_parse_counts_queue_and_conversation():
    result = assignments(DASHBOARD, "10")["assignments"][0]
    assert result["id"] == "20" and result["grading_progress"] == 50
    assert "edit_url" not in result
    requests = regrades(QUEUE, "10", "20")["regrade_requests"]
    assert requests[0]["id"] == "30:Question_40"
    assert "Review" not in requests[0]["fields"]
    detail = request_detail(RAW, PATH)["regrade_requests"]
    assert len(detail) == 1 and detail[0]["staff_comment"] == "Pending response"


async def test_staff_routes_discovery_and_student_separation():
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == "/account":
            return httpx.Response(
                200, text=ACCOUNT.replace("Student Courses", "Instructor Courses")
            )
        if request.url.path == "/courses/10":
            return httpx.Response(200, text=DASHBOARD)
        if request.url.path.endswith("/regrade_requests"):
            return httpx.Response(200, text=QUEUE)
        if request.url.path.endswith("/30.json"):
            return httpx.Response(200, json=RAW)
        pytest.fail("Unreviewed route reached the network")

    client = GradescopeClient(
        Settings("synthetic@example.edu", "secret"), transport=httpx.MockTransport(handle)
    )
    client.authenticated = True
    result = await client.read("staff_regrade", PATH)
    assert len(result["data"]["regrade_requests"]) == 1
    assert all(r.method == "GET" and "/grade" not in r.url.path for r in calls)
    with pytest.raises(GradescopeError, match="not discovered"):
        await client.read("staff_regrade", PATH | {"submission_id": "999"})
    assert not any("999" in r.url.path for r in calls)
    with pytest.raises(GradescopeError, match="student"):
        await client.read("assignments", {"course_id": "10"})
    await client.close()


async def test_student_cannot_read_staff_queue():
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.path == "/account"
        return httpx.Response(200, text=ACCOUNT)

    c = GradescopeClient(
        Settings("synthetic@example.edu", "secret"), transport=httpx.MockTransport(handle)
    )
    c.authenticated = True
    with pytest.raises(GradescopeError, match="Staff membership"):
        await c.read("staff_regrades", {"course_id": "10", "assignment_id": "20"})
    assert len(calls) == 1
    await c.close()


@pytest.mark.parametrize(
    "method,route,params",
    [
        ("POST", "/courses/10/assignments/20/regrade_requests", {}),
        ("GET", "/courses/10/questions/40/submissions/30/grade", {}),
        ("GET", "/courses/10/assignments/20/regrade_requests", {"view": 1}),
        ("PATCH", "/regrade_requests/60", {}),
    ],
)
def test_regrade_mutations_and_review_routes_blocked(method, route, params):
    with pytest.raises(GradescopeError):
        validate_request(method, route, params)
