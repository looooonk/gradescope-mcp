import copy

import httpx
import pytest

from gradescope_mcp.catalog import GradescopeError
from gradescope_mcp.client import GradescopeClient
from gradescope_mcp.config import Settings
from gradescope_mcp.parsers import assignments, courses, submission

ACCOUNT = """<div id="account-show"><h2 class="pageHeading">Student Courses</h2>
<div class="courseList"><div class="courseList--term">Fall 2026</div>
<a href="/courses/10"><h3 class="courseBox--shortname">TEST</h3>
<div class="courseBox--name">Synthetic course</div></a></div></div>"""
COURSE = """<table id="assignments-student-table"><tbody>
<tr><th><a href="/courses/10/assignments/20/submissions/30">Synthetic assignment</a></th>
<td>8.5 / 10</td><td>
<time class="submissionTimeChart--releaseDate" datetime="2026-09-01T12:00:00-04:00"></time>
<time class="submissionTimeChart--dueDate" datetime="2026-09-20T12:00:00-04:00"></time>
<time class="submissionTimeChart--dueDate" datetime="2026-09-21T12:00:00-04:00"></time></td></tr>
<tr><th><button data-assignment-id="21">Not submitted</button></th><td>No Submission</td></tr>
<tr><th>Unavailable assignment</th><td>No Submission</td></tr></tbody></table>"""
RAW = {
    "assignment": {"id": 20, "title": "Synthetic assignment", "show_old_submission_scores": False},
    "assignment_submission": {"id": 30, "score": "8.5", "start_time_utility_submission": False},
    "grades_visible": True,
    "current_user": {"id": 1, "is_instructor": False, "is_admin": False},
    "ownerships": [{"user_id": 1}],
    "course_members": [{"id": 1, "name": "Student"}, {"id": 2, "name": "Not a member"}],
    "questions": [{"id": 40, "title": "Question"}],
    "question_submissions": [
        {"id": 50, "question_id": 40, "score": "8.5", "evaluations": [{"comments": "Feedback"}]}
    ],
    "rubric_items": [{"question_id": 40, "description": "Criterion"}],
    "paths": {"resubmit_path": "/write", "ssh_session_path": "/execute"},
}


class Site:
    def __init__(self):
        self.calls = []
        self.expire = False
        self.raw = copy.deepcopy(RAW)

    def handle(self, request):
        self.calls.append(request)
        path = request.url.path
        if path == "/":
            return httpx.Response(
                200,
                text='<form action="/login"><input name="authenticity_token" value="csrf"></form>',
            )
        if path == "/login":
            assert request.method == "POST" and not request.url.query
            assert b"session%5Bpassword%5D=secret" in request.content
            return httpx.Response(
                302,
                headers={
                    "location": "/account",
                    "set-cookie": "_gradescope_session=secret-cookie; Path=/; Secure",
                },
            )
        if self.expire:
            self.expire = False
            return httpx.Response(302, headers={"location": "/login"})
        if path == "/account":
            return httpx.Response(200, text=ACCOUNT)
        if path == "/courses/10":
            return httpx.Response(200, text=COURSE)
        if path in {
            "/courses/10/assignments/20/submissions/30.json",
            "/courses/10/assignments/20/submissions/29.json",
        }:
            keys = request.url.params.get_list("only_keys[]")
            if keys == ["past_submissions"]:
                return httpx.Response(200, json={"past_submissions": [{"id": 29, "score": "3"}]})
            if keys == ["text_files", "file_comments"]:
                return httpx.Response(200, json={"text_files": [], "file_comments": []})
            raw = copy.deepcopy(self.raw)
            if path.endswith("/29.json"):
                raw["assignment_submission"]["id"] = 29
            return httpx.Response(200, json=raw)
        raise AssertionError("An unreviewed route reached the transport")


def client(site):
    return GradescopeClient(
        Settings("test@example.test", "secret"), transport=httpx.MockTransport(site.handle)
    )


@pytest.mark.parametrize(
    "op,path",
    [
        ("submit", {}),
        ("assignments", {"course_id": "../10"}),
        ("assignments", {"course_id": True}),
        ("assignments", {"course_id": "1?view=true"}),
        ("courses", {"url": "https://example.test"}),
        ("submission", {"course_id": "10"}),
        ("courses", {"method": "POST"}),
        ("courses", {"auto_mark_as_read": True}),
    ],
)
async def test_bad_operation_arguments_make_no_network_requests(op, path):
    site = Site()
    c = client(site)
    with pytest.raises(GradescopeError):
        await c.read(op, path)
    assert not site.calls
    await c.close()


@pytest.mark.parametrize(
    "method,route,params",
    [
        ("POST", "/courses/10", {}),
        ("GET", "/logout", {}),
        ("GET", "https://example.test/account", {}),
        ("GET", "/courses/10/assignments/20", {}),
        ("GET", "/courses/10/assignments/20/submissions/new", {}),
        ("GET", "/courses/10/assignments/20/submissions/30", {}),
        ("GET", "/account", {"view": 1}),
        (
            "GET",
            "/courses/10/assignments/20/submissions/30.json",
            {"content": "react", "only_keys[]": ["secrets"]},
        ),
    ],
)
async def test_transport_allowlist_rejects_writes_and_html_before_network(method, route, params):
    site = Site()
    c = client(site)
    with pytest.raises(GradescopeError):
        await c._send(method, route, params=params)
    assert not site.calls
    await c.close()


async def test_login_read_ownership_history_and_expiry():
    site = Site()
    c = client(site)
    path = {"course_id": "10", "assignment_id": "20"}
    result = await c.read("submission", path)
    assert result["data"]["questions"][0]["submission"]["evaluations"][0]["comments"] == "Feedback"
    assert "paths" not in result["data"]
    assert len(result["data"]["members"]) == 1
    history = await c.read("submission_history", path)
    assert "score" not in history["data"]["past_submissions"][0]
    old = await c.read("submission", dict(path, submission_id="29"))
    assert old["data"]["submission"]["id"] == 29
    with pytest.raises(GradescopeError, match="history"):
        await c.read("submission", dict(path, submission_id="999"))
    assert not any("999.json" in str(r.url) for r in site.calls)
    site.expire = True
    assert (await c.read("assignments", {"course_id": "10"}))["data"]["assignments"]
    assert len([r for r in site.calls if r.url.path == "/login"]) == 2
    assert all(r.method == "GET" or r.url.path == "/login" for r in site.calls)
    await c.close()


async def test_no_submission_or_unowned_submission_never_enables_other_access():
    site = Site()
    c = client(site)
    with pytest.raises(GradescopeError) as caught:
        await c.read("submission", {"course_id": "10", "assignment_id": "21"})
    assert caught.value.code == "no_submission"
    site.raw["ownerships"] = [{"user_id": 2}]
    with pytest.raises(GradescopeError) as caught:
        await c.read("submission", {"course_id": "10", "assignment_id": "20"})
    assert caught.value.code == "student_access_required"
    await c.close()


def test_parsers_preserve_unknown_scores_and_unavailable_assignments():
    assert courses(ACCOUNT)["courses"][0]["role"] == "student"
    rows = assignments(COURSE, "10")["assignments"]
    assert rows[0]["score"] == 8.5 and rows[0]["late_due_at"]
    assert rows[1]["score"] is None and rows[1]["submission_id"] is None
    assert rows[2]["id"] is None
    with pytest.raises(GradescopeError):
        courses("<html>Changed structure</html>")
    with pytest.raises(GradescopeError):
        courses('<div id="account-show"></div>')
    with pytest.raises(GradescopeError):
        assignments(COURSE, "999")
    with pytest.raises(GradescopeError):
        assignments(COURSE.replace("<th>", "<td>").replace("</th>", "</td>"), "10")


def test_unpublished_scores_are_omitted_and_timed_placeholders_blocked():
    raw = copy.deepcopy(RAW)
    raw["grades_visible"] = False
    data = submission(raw)
    assert "score" not in data["submission"]
    assert "score" not in data["questions"][0]["submission"]
    assert "rubric_items" not in data["questions"][0]
    raw["assignment_submission"]["start_time_utility_submission"] = True
    with pytest.raises(GradescopeError) as caught:
        submission(raw)
    assert caught.value.code == "timed_assignment"


def test_hidden_reference_answers_do_not_hide_own_answers():
    raw = copy.deepcopy(RAW)
    raw["questions"][0]["content"] = [
        {"type": "text", "value": "Prompt"},
        {"type": "text_input", "answer": "Reference answer"},
        {"type": "radio_input", "choices": [{"value": "Choice", "answer": True}]},
        {"type": "explanation", "value": "Hidden explanation"},
    ]
    raw["question_submissions"][0]["answers"] = {"0": "Own answer"}
    question = submission(raw)["questions"][0]
    assert len(question["content"]) == 3
    assert "answer" not in question["content"][1]
    assert "answer" not in question["content"][2]["choices"][0]
    assert question["submission"]["answers"] == {"0": "Own answer"}
    raw["assignment"]["show_explanations_after_correct"] = True
    question = submission(raw)["questions"][0]
    assert len(question["content"]) == 4 and "answer" not in question["content"][1]
    raw["assignment"]["show_answers"] = True
    assert submission(raw)["questions"][0]["content"] == raw["questions"][0]["content"]


def test_rubric_and_debug_output_follow_student_visibility():
    raw = copy.deepcopy(RAW)
    raw["rubric_items"] = [
        {"question_id": 40, "present": True, "group_id": 60},
        {"question_id": 40, "present": False, "group_id": 61},
    ]
    raw["rubric_item_groups"] = [{"question_id": 40, "id": 60}, {"question_id": 40, "id": 61}]
    raw["autograder_results"] = {
        "output": "Visible student output",
        "stdout": "Staff diagnostics",
        "stdout_shown_to_students": False,
    }
    raw["assignment"]["rubric_visibility_setting"] = "show_only_applied_rubric_items"
    result = submission(raw)
    assert len(result["questions"][0]["rubric_items"]) == 1
    assert len(result["questions"][0]["rubric_groups"]) == 1
    assert result["autograder_results"]["output"] == "Visible student output"
    assert "stdout" not in result["autograder_results"]
    raw["assignment"]["rubric_visibility_setting"] = "hide_all_rubric_items"
    assert submission(raw)["questions"][0]["rubric_items"] == []
    raw["assignment"]["rubric_visibility_setting"] = "show_all_rubric_items"
    raw["autograder_results"]["stdout_shown_to_students"] = True
    result = submission(raw)
    assert len(result["questions"][0]["rubric_items"]) == 2
    assert result["autograder_results"]["stdout"] == "Staff diagnostics"


def test_missing_role_flags_fail_closed():
    raw = copy.deepcopy(RAW)
    raw["current_user"].pop("is_instructor")
    with pytest.raises(GradescopeError) as caught:
        submission(raw)
    assert caught.value.code == "student_access_required"
