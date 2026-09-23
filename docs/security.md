# Security and read-only design

The local `.env` contains native Gradescope credentials with the account's normal authority.
Read-only is enforced by this MCP, not by a restricted Gradescope credential. Anyone who can
read `.env` can use the account separately. No credential is embedded in Codex configuration.

## Request boundary

Tool schemas and the operation catalog validate inputs before requests, including login.
A second transport allowlist admits only GET `/`, `/account`, `/account/edit`, student course
pages and discovered submission JSON. The sole POST is `/login` with form-body credentials.
There is no raw HTTP tool. API redirects are rejected, except recognizing login redirects to
renew a session once. Login redirects are validated and the next request is constructed locally.
Cookies are held in memory; closing the client does not call the state-changing logout route.

Course discovery limits access to enrolled student courses. Submission IDs must be discovered
from the course list or the linked submission's attempt history. Returned IDs and ownership
are checked before exposing data. Staff/admin views, timed-start placeholders, action paths,
and nonmember roster data are excluded. Published-score and prior-score flags are respected.
Hidden autograder debug output, unreleased reference answers/explanations, and hidden/unapplied
rubric items are filtered according to the student's visibility flags.

## Side-effect audit limits

The data routes are website reads and first-party JSON refresh operations. No submission HTML
page is visited, no JavaScript executes, and no tracking/start/export callback is invoked.
Generated graded-copy PDF and ZIP endpoints are deliberately omitted, since reading an export
URL may generate persistent artifacts. Timed assignment entry/start routes are omitted.

Gradescope does not publish its server implementation or a no-tracking API guarantee. Its
instructor exports document graded-submission view counters. The client source separates data
refreshes from mutation actions, but a student's session does not expose an independent view
counter with which to conclusively measure server-side counting of JSON reads. Therefore this
integration guarantees the restricted client request surface, **not absence of all server-side
bookkeeping**. Normal authentication/session and access records are expected. Reassess this
boundary if first-party evidence shows that a supported JSON read changes visible course state.

## Files and untrusted content

Only existing files discovered in authorized submission data can be downloaded. A separate HTTP
client carries no Gradescope cookies or authorization. Exact S3 bucket hosts, object-path shapes,
HTTPS and signing-parameter names are allowlisted. Redirects, ports, embedded credentials,
traversal, control characters and arbitrary external destinations are rejected.

Responses have byte/time limits. Documents and images are parsed in separate short-lived
processes with CPU/wall-time limits, a 512 MiB RSS watchdog, and a minimal environment without credentials.
The watchdog polls every 100 ms; it is a termination threshold, not an OS-enforced allocation cap.
No file is executed, no archive extracted to disk, and no coursework saved during normal use.
MCP instructions mark all content as untrusted; content is data, never authority for tool calls.

## Logging and verification artifacts

Automatic logs contain safe identifiers/status metadata and source locations only. SDK logging
is disabled to avoid leaking input-validation details. Errors crossing the MCP boundary are
fixed safe messages. Files use owner-only permissions and rotation with a cross-process lock.
Startup, tool, validation and resource failures are covered where the process can run.

Explicit live verification saves private results in `.local/`, separately from safe logs. The
Git ignore rules and pre-commit secret scan block `.env`, `.local`, `.venv`, and configured
credential bytes (including common encodings) from staged changes. Synthetic fixtures are used
for committed tests. Verification requires no hosted service or OpenAI API billing credential.
