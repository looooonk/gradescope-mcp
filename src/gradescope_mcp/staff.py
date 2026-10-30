"""Staff dashboard and request index reads; never enters the grading/review UI."""

import json
import re

from bs4 import BeautifulSoup

from gradescope_mcp.catalog import GradescopeError
from gradescope_mcp.config import ORIGIN
from gradescope_mcp.parsers import parse_error, select


def assignments(html, course_id):
    soup = BeautifulSoup(html, "html.parser")
    element = soup.select_one('[data-react-class="AssignmentsTable"]')
    if element is None:
        raise parse_error()
    try:
        props = json.loads(element["data-react-props"])
        rows = props["table_data"]
        if not isinstance(rows, list):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise parse_error() from None
    result = []
    for row in rows:
        if row.get("type") == "section":
            continue
        if row.get("type") != "assignment":
            raise GradescopeError(
                "Versioned assignment containers are not supported yet.", "unsupported_assignment"
            )
        identifier = str(row.get("id", "")).removeprefix("assignment_")
        if not re.fullmatch(r"[1-9][0-9]*", identifier):
            raise parse_error()
        expected = f"/courses/{course_id}/assignments/{identifier}/regrade_requests"
        regrade_path = row.get("regrade_request_url")
        if regrade_path not in (None, "", expected):
            raise GradescopeError("Staff assignment link shape changed.", "parse_error")
        item = select(
            row,
            (
                "title",
                "type",
                "total_points",
                "submission_window",
                "num_active_submissions",
                "grading_progress",
                "is_published",
                "regrade_requests_open",
                "regrade_requests_possible",
                "open_regrade_request_count",
            ),
        )
        item.update(
            id=identifier,
            source_url=ORIGIN + f"/courses/{course_id}",
            regrades_available=regrade_path == expected,
        )
        result.append(item)
    return {"course_id": course_id, "assignments": result}


def regrades(html, course_id, assignment_id):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one(".js-regradeRequestsTable")
    if table is None:
        raise parse_error()
    headings = [cell.get_text(" ", strip=True) for cell in table.select("thead th")]
    rows = []
    for row in table.select("tbody tr"):
        cells = row.find_all("td", recursive=False)
        if row.select_one(".dataTables_empty"):
            continue
        if not cells or len(cells) != len(headings):
            raise parse_error()
        values = {
            key: cell.get_text(" ", strip=True)
            for key, cell in zip(headings, cells, strict=True)
            if key.casefold() not in {"review", "actions", ""}
        }
        links = [
            re.fullmatch(
                rf"/courses/{course_id}/assignments/{assignment_id}/submissions/([1-9][0-9]*)#"
                r"(Question_[1-9][0-9]*(?:[._][0-9]+)*)",
                a["href"],
            )
            for a in row.select("a[href]")
        ]
        match = next((m for m in links if m), None)
        if match is None:
            raise parse_error()
        rows.append(
            {
                "id": f"{match[1]}:{match[2]}",
                "submission_id": match[1],
                "question_anchor": match[2],
                "fields": values,
            }
        )
    return {
        "course_id": course_id,
        "assignment_id": assignment_id,
        "regrade_requests": rows,
        "note": "Read-only request index. Review links are not followed or marked reviewed.",
    }


def request_detail(raw, path):
    if (
        raw.get("current_user", {}).get("is_instructor") is not True
        or str(raw.get("assignment", {}).get("id")) != path["assignment_id"]
        or str(raw.get("assignment_submission", {}).get("id")) != path["submission_id"]
    ):
        raise GradescopeError(
            "Staff submission identity could not be verified.", "staff_access_required"
        )
    submissions = raw.get("question_submissions")
    requests = raw.get("regrade_requests")
    if not isinstance(submissions, list) or not isinstance(requests, list):
        raise parse_error()
    question_ids = {
        str(q["id"]) for q in raw.get("questions", []) if q.get("anchor") == path["question_anchor"]
    }
    ids = {str(q["id"]) for q in submissions if str(q.get("question_id")) in question_ids}
    result = [
        select(
            r,
            (
                "id",
                "student_comment",
                "staff_comment",
                "completed",
                "created_at",
                "updated_at",
                "question_submission_id",
            ),
        )
        for r in requests
        if str(r.get("question_submission_id")) in ids
    ]
    if not result:
        raise GradescopeError(
            "Request details are unavailable in the authorized response.", "unavailable"
        )
    return {
        "regrade_requests": result,
        "submission_id": path["submission_id"],
        "question_anchor": path["question_anchor"],
    }
