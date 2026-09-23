# Operation evidence

Reviewed September 23, 2026. Gradescope confirms [there is no public API](https://guides.gradescope.com/hc/en-us/articles/36028522325901-Gradescope-Public-API).
The supported surface is the student's website and its first-party JSON refresh requests.

## Sources

- Authenticated account/course HTML, inspected locally without executing scripts.
- First-party assets indexed by <https://cdn.gradescope.com/packs/manifest.json>.
- [Submission viewer](https://cdn.gradescope.com/packs/assignment_submission_viewer-401a2238e5316b49a568.js):
  `submission_react_path` GET requests, including `only_keys[]=past_submissions` and
  `only_keys[]=text_files&only_keys[]=file_comments`, plus state/selectors for question
  submissions, evaluation comments, rubrics, annotations, ownerships and autograder results.
- [Shared application code](https://cdn.gradescope.com/assets/application-3848269f45e6e88a06a71dea51fe3c254cb7bb04ea3cbcad42d728415f799fc0.js):
  account form and student course tables; mutation callbacks are not invoked.
- [Student timed-assignment guide](https://guides.gradescope.com/hc/en-us/articles/21864654527245-Submitting-timed-assignments):
  starting an assessment starts a non-pausable timer. No starts or assignment-detail entry routes.
- [Export and view-count documentation](https://guides.gradescope.com/hc/en-us/articles/22239057709453-Exporting-Grades-Evaluations-and-Submissions):
  exports may generate artifacts/email; view counts exist. Neither submission HTML nor generated
  exports are fetched. JSON refresh behavior is discussed in the security audit limits.

These sources establish client behavior; they are not a public API contract. Bundle hashes can
change. Private evidence and response shapes are retained only under ignored `.local/audit/`.
Third-party library code was used as a research reference, not executed as the transport.

## Catalog

| Operation | Read source | Exposed content |
| --- | --- | --- |
| profile | GET `/account/edit` | Selected name/email/institution/timezone form values |
| courses | GET `/account` | Course IDs, names, roles and terms |
| course | GET `/account`, `/courses/{course_id}` | Student metadata and assignment count |
| assignments | GET `/courses/{course_id}` | Assignment rows, deadlines, status, visible grades |
| assignment | Same course list; local selection | One row; never opens/starts the assignment |
| submission | GET existing submission `.json?content=react` | Selected student state, excluding action paths |
| questions | Same JSON; local selection | Questions, own answers, visible scoring, feedback, annotations |
| submission_history | Same JSON + fixed `only_keys[]=past_submissions` | Existing attempts; respect old-score visibility |
| submission_files | Same JSON + fixed text-files/comments keys | Existing files and page images; visible file comments |
| autograder | Same student JSON | Student-facing autograder tests/output |
| regrades | Same JSON; local selection | Existing request/comment/response history |
| group_members | Same JSON; ownership-filtered | Members of this submission only |

The submission route template is
`/courses/{course_id}/assignments/{assignment_id}/submissions/{submission_id}.json`.
IDs must first appear in the student's course list or current submission's history.
The server confirms returned assignment/submission IDs and ownership. Timed-start utility
submissions and staff views are rejected. Hidden final scores/rubrics/comments are omitted.
Immediate autograder results are distinct from final grade release and may be available earlier.
The viewer additionally gates `stdout` on `stdout_shown_to_students`, reference `answer` fields
on `show_answers`, and explanation components on `show_answers` or
`show_explanations_after_correct`. The parser applies those gates separately from your own saved
answers. Rubric items honor the assignment's all/applied/hidden visibility setting.
Regrade staff replies and review timestamps appear only after `completed` is true, matching
the viewer's conversation component.

Original files come only from source metadata pointing at the Gradescope production uploads
S3 bucket under reviewed `/uploads/{pdf_attachment,image_attachment,page,text_file}/file/` paths.
Server-issued S3 signing parameters are accepted, but never supplied by tool callers.
No `.pdf`/`.zip` submission-generation route, arbitrary browser navigation, recursive link crawler,
standalone instructor endpoint, or server-provided action URL is exposed.
