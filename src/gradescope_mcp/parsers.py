import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from gradescope_mcp.catalog import GradescopeError
from gradescope_mcp.config import ORIGIN

SUBMISSION_ROUTE = re.compile(
    r"/courses/([1-9][0-9]*)/assignments/([1-9][0-9]*)/submissions/([1-9][0-9]*)"
)


def parse_error():
    return GradescopeError(
        "Gradescope page structure changed or access is restricted.", "parse_error"
    )


def select(data, keys):
    return {k: data[k] for k in keys if k in data}


def courses(html):
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#account-show")
    if root is None or root.select_one(".courseList") is None:
        raise parse_error()
    result = []
    seen = set()
    for link in root.select("a[href]"):
        match = re.fullmatch(r"/courses/([1-9][0-9]*)/?", link["href"])
        if not match or match[1] in seen:
            continue
        name = link.select_one(".courseBox--shortname")
        full = link.select_one(".courseBox--name")
        if not name or not full:
            raise parse_error()
        heading = link.find_previous("h2", class_="pageHeading")
        label = heading.get_text(" ", strip=True).casefold() if heading else ""
        role = (
            "student"
            if "student" in label
            else "instructor"
            if "instructor" in label
            else "unknown"
        )
        if role == "unknown" and soup.select_one("button.js-createNewCourse") is None:
            role = "student"
        term = link.find_previous("div", class_="courseList--term")
        result.append(
            {
                "id": match[1],
                "code": name.get_text(" ", strip=True),
                "name": full.get_text(" ", strip=True),
                "role": role,
                "term": term.get_text(" ", strip=True) if term else None,
                "source_url": ORIGIN + "/courses/" + match[1],
            }
        )
        seen.add(match[1])
    return {"courses": result}


def assignments(html, course_id):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#assignments-student-table")
    if table is None:
        raise GradescopeError(
            "This course does not expose a student assignment list.", "student_access_required"
        )
    result = []
    for row in table.select("tbody tr"):
        header = row.find("th")
        cells = row.find_all("td", recursive=False)
        if not header:
            if row.select_one("td.dataTables_empty"):
                continue
            raise parse_error()
        if not cells:
            raise parse_error()
        link = header.find("a", href=True)
        button = header.select_one("button[data-assignment-id]")
        match = SUBMISSION_ROUTE.fullmatch(urlsplit(link["href"]).path) if link else None
        if match and match[1] != course_id:
            raise parse_error()
        assignment_id = match[2] if match else button.get("data-assignment-id") if button else None
        if assignment_id is not None and not re.fullmatch(r"[1-9][0-9]*", assignment_id):
            raise parse_error()
        status = cells[0].get_text(" ", strip=True)
        score_match = re.fullmatch(r"\s*(-?[\d.]+)\s*/\s*([\d.]+)\s*", status)
        score = maximum = None
        if score_match:
            try:
                score, maximum = map(float, score_match.groups())
            except ValueError:
                raise parse_error() from None
        release = row.select_one("time.submissionTimeChart--releaseDate[datetime]")
        due = row.select("time.submissionTimeChart--dueDate[datetime]")
        result.append(
            {
                "id": assignment_id,
                "name": header.get_text(" ", strip=True),
                "submission_id": match[3] if match else None,
                "status": status,
                "score": score,
                "max_score": maximum,
                "release_at": release.get("datetime") if release else None,
                "due_at": due[0]["datetime"] if due else None,
                "late_due_at": due[1]["datetime"] if len(due) > 1 else None,
                "submission_format": button.get("data-submission-format") if button else None,
                "source_url": ORIGIN + match[0] if match else ORIGIN + f"/courses/{course_id}",
            }
        )
    if not result and table.select("tbody tr th"):
        raise parse_error()
    return {"course_id": course_id, "assignments": result}


def profile(html):
    soup = BeautifulSoup(html, "html.parser")
    fields = {}
    for name, key in {
        "user[name]": "name",
        "user[first_name]": "first_name",
        "user[last_name]": "last_name",
        "user[email]": "email",
        "user[school]": "institution",
        "user[time_zone]": "timezone",
    }.items():
        element = soup.find(["input", "select"], attrs={"name": name})
        if element is not None:
            option = element.find("option", selected=True) if element.name == "select" else None
            fields[key] = option.get("value") if option else element.get("value")
    if not fields:
        raise parse_error()
    return fields


ASSIGNMENT_KEYS = (
    "id",
    "title",
    "total_points",
    "student_submission",
    "submission_format",
    "due_date",
    "group_size",
    "regrade_request_start",
    "regrade_request_end",
    "regrade_requests_open",
    "time_limit_in_minutes",
    "time_remaining_in_minutes",
    "enforce_time_limit",
    "timezone",
    "show_answers",
    "show_explanations_after_correct",
    "show_old_submission_scores",
    "template_url",
    "rubric_visibility_setting",
)
SUBMISSION_KEYS = (
    "id",
    "created_at",
    "status",
    "source",
    "score",
    "lateness_in_words",
    "active",
    "start_time_utility_submission",
)
QUESTION_KEYS = (
    "id",
    "type",
    "title",
    "index",
    "weight",
    "parameters",
    "scoring_type",
    "content",
    "full_index",
    "numbered_title",
    "anchor",
)


def question_content(value, *, answers, explanations):
    if isinstance(value, list):
        return [
            question_content(item, answers=answers, explanations=explanations)
            for item in value
            if explanations or not isinstance(item, dict) or item.get("type") != "explanation"
        ]
    if isinstance(value, dict):
        return {
            key: question_content(item, answers=answers, explanations=explanations)
            for key, item in value.items()
            if answers or key != "answer"
        }
    return value


def autograder(data):
    if data is None:
        return None
    result = dict(data)
    if result.get("stdout_shown_to_students") is not True:
        result.pop("stdout", None)
    return result


def submission(data):
    if not isinstance(data, dict) or not all(
        k in data for k in ("assignment", "assignment_submission", "current_user", "grades_visible")
    ):
        raise parse_error()
    if any(data["current_user"].get(k) is not False for k in ("is_instructor", "is_admin")):
        raise GradescopeError(
            "Submission reads require the student's own view.", "student_access_required"
        )
    if data["assignment_submission"].get("start_time_utility_submission"):
        raise GradescopeError(
            "Timed-assignment placeholders are excluded; this tool cannot start work.",
            "timed_assignment",
        )
    visible = data["grades_visible"] is True
    assignment = data["assignment"]
    show_answers = assignment.get("show_answers") is True
    show_explanations = show_answers or assignment.get("show_explanations_after_correct") is True
    rubric_visibility = assignment.get("rubric_visibility_setting")
    own = select(data["assignment_submission"], SUBMISSION_KEYS)
    if not visible:
        own.pop("score", None)
    questions = []
    answers = {q["question_id"]: q for q in data.get("question_submissions", [])}
    for raw in data.get("questions", []):
        item = select(raw, QUESTION_KEYS)
        if "content" in item:
            item["content"] = question_content(
                item["content"], answers=show_answers, explanations=show_explanations
            )
        answer = answers.get(raw["id"], {})
        item["submission"] = select(answer, ("id", "data", "answers"))
        if visible:
            item["submission"].update(select(answer, ("score", "evaluations", "annotations")))
            item["rubric_items"] = [
                r
                for r in data.get("rubric_items", [])
                if r.get("question_id") == raw["id"]
                and (
                    rubric_visibility == "show_all_rubric_items"
                    or rubric_visibility == "show_only_applied_rubric_items"
                    and r.get("present") is True
                )
            ]
            group_ids = {r.get("group_id") for r in item["rubric_items"]}
            item["rubric_groups"] = [
                r
                for r in data.get("rubric_item_groups", [])
                if r.get("question_id") == raw["id"] and r.get("id") in group_ids
            ]
        questions.append(item)
    owners = {o.get("user_id"): o for o in data.get("ownerships", [])}
    members = [
        dict(select(m, ("id", "name", "email")), uploader=owners[m["id"]].get("uploader"))
        for m in data.get("course_members", [])
        if m.get("id") in owners
    ]
    return {
        "assignment": select(data["assignment"], ASSIGNMENT_KEYS),
        "submission": own,
        "grades_visible": visible,
        "questions": questions,
        "regrade_requests": data.get("regrade_requests", []) if visible else [],
        "members": members,
        "autograder_results": autograder(data.get("autograder_results")),
        "pdf_attachment": data.get("pdf_attachment"),
        "image_attachments": data.get("image_attachments", []),
        "text_files": data.get("text_files", []),
        "file_comments": data.get("file_comments", []) if visible else [],
    }


def parse_json(body):
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise parse_error() from None
    if not isinstance(value, dict):
        raise parse_error()
    return value
