from gradescope_mcp.catalog import GradescopeError
from gradescope_mcp.changefeed import FeedError, record, selected
from gradescope_mcp.config import ORIGIN


class GradescopeChanges:
    errors = (GradescopeError, FeedError)
    coverage = (
        "Student: assignments/deadlines and visible submission "
        "grades/feedback/regrades. Staff: grading summaries and existing "
        "regrade queues."
    )

    async def authorize(self, client, course):
        client.cache.pop("courses", None)
        rows = (await client.read("courses"))["data"]["courses"]
        membership = next((c for c in rows if str(c["id"]) == course), None)
        if membership is None or membership["role"] not in {"student", "instructor"}:
            raise FeedError("A supported current course membership is required.")
        return membership["role"]

    def tasks(self, course, role):
        return [{"source": "staff_assignments" if role == "instructor" else "assignments"}]

    async def collect(self, client, course, task):
        name = task["source"]
        path = {"course_id": course}
        path.update(
            {k: task[k] for k in ("assignment_id", "submission_id", "question_anchor") if k in task}
        )
        result = await client.read(name, path)
        data, source = result["data"], result["source_url"]
        records, more = [], []
        if name in {"assignments", "staff_assignments"}:
            for item in data["assignments"]:
                identifier = item["id"]
                if identifier is None:
                    continue
                values = selected(
                    item,
                    (
                        "name title status score max_score release_at due_at late_due_at "
                        "submission_id total_points submission_window "
                        "num_active_submissions grading_progress is_published "
                        "regrade_requests_open open_regrade_request_count"
                    ),
                )
                records.append(
                    record(
                        "assignment",
                        identifier,
                        item.get("name") or item.get("title"),
                        source,
                        values,
                    )
                )
                if name == "staff_assignments" and item["regrades_available"]:
                    more.append({"source": "staff_regrades", "assignment_id": identifier})
                elif name == "assignments" and item.get("submission_id"):
                    more.append({"source": "submission", "assignment_id": identifier})
        elif name == "submission":
            values = selected(
                data,
                (
                    "grades_visible submission questions regrade_requests "
                    "autograder_score autograder_results"
                ),
            )
            records.append(
                record(
                    "submission",
                    task["assignment_id"],
                    data.get("assignment", {}).get("title") or "Submission feedback",
                    source,
                    values,
                )
            )
        elif name == "staff_regrades":
            for item in data["regrade_requests"]:
                records.append(
                    record(
                        "regrade",
                        f"{task['assignment_id']}:{item['id']}",
                        "Regrade request",
                        ORIGIN
                        + f"/courses/{course}/assignments/{task['assignment_id']}/regrade_requests",
                        item,
                    )
                )
                more.append(
                    {
                        "source": "staff_regrade",
                        "assignment_id": task["assignment_id"],
                        "submission_id": item["submission_id"],
                        "question_anchor": item["question_anchor"],
                    }
                )
        else:
            for item in data["regrade_requests"]:
                records.append(
                    record("regrade_conversation", item["id"], "Regrade conversation", source, item)
                )
        return records, more
