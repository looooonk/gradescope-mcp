# Verification

Verified on September 23, 2026, on the local macOS checkout with Python 3.12.

## Offline checks

`make check` passes Ruff lint/format checks and **41 tests**. Coverage includes rejected routes
before network access, login secrets in form bodies, session renewal, student ownership, attempt
discovery, hidden grades/reference answers/autograder debug output, rubric visibility, timed
placeholders, parser failures, pagination, logging redaction/rotation, storage host/path
restrictions, cookie-free downloads, blocked redirects, and bounded text/image decoding.

The installed pre-commit hook also rejects private files and configured credential bytes in
staged content. No live checks or GitHub Actions run automatically when committing.

## Live server check

`make live` starts a new stdio server process from outside the repository with a minimal
environment. It verified:

- Seven tools, all marked read-only, and all twelve catalog operations.
- Native email/password login, course pagination and existing student submissions.
- Assignment names/counts/status against the fresh Gradescope website table.
- Existing source-file text, original PDF text and scanned-page images.
- Rejection of an unsupported write operation, a tracking query and an arbitrary URL argument.

The configured executable and `.env` paths are absolute. The new-process check demonstrates
that an existing process, terminal, working directory or saved cookie is unnecessary; the
computer itself was not rebooted during verification.

## Codex subscription check

`make verify` uses an ephemeral **GPT-6 Luna** Codex session with the existing ChatGPT
subscription login. API-key environment variables are removed. Only Gradescope is registered
for the check; shell tools, apps, web search and multi-agent features are disabled.

The model used all seven tool types, matched **12 exact reference fields**, reproduced a source
file excerpt, and successfully loaded a scanned-page image. Reference fields cover course and
assignment identities/counts, deadline, visible score, current submission ID, question/attempt
counts and assignment-title text. Matching answers test the model's tool use; the independent
website comparison above provides an additional check of assignment metadata parsing.

Private references, answers and transcripts remain under ignored, owner-only `.local/`. Only
aggregate pass/fail metadata is printed. Repeat `make live` before `make verify` to refresh the
reference data. These explicit verification artifacts are separate from redacted automatic
error logs in `.local/logs/gradescope-mcp.jsonl`.

## Limits of verification

Live samples covered programming and uploaded-PDF submissions on this account. Not every
assignment format or instructor setting had a representative live sample. Online-question
visibility and timed-start rejection have synthetic regression coverage; unstarted assessments
were never entered. Image delivery was verified, not handwriting-recognition accuracy.

No course/account mutation or generated export endpoint was exercised. Gradescope's server is
not public: JSON-read view counting could not be independently measured from a student's
session. See [security and audit limits](security.md). The unsupported private HTML/JSON
interface may require maintenance after website changes.
