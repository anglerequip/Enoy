# Enoy 0.4.0 architecture

`app.build()` registers exactly two chat providers: Codex and Claude Code. Both use
supported signed-in command-line clients. API and local models (Ollama) are used
only by the Build workspace, described at the end.

`coordinator.start()` evaluates a prompt with ChatGPT, validates its task plan,
and passes it to `TeamRunner`. Manual selection supports Codex or Claude only.
Every cloud request checks the chat/project cloud policy and scrubs excluded files,
private documents, quoted private content and detected secrets from shared state.
`native.py` resolves the explicit Workspace/Full Access policy and native session
scope. Native requests preserve provider tools, system instructions and supported
customizations. Codex uses workspace-write with automatic review, or the supported
Full Access sandbox policy with on-request approvals. Claude uses auto permission
review. Neither adapter uses approval-bypass flags.

`TeamRunner` stores dependencies and assignments. Native workers inspect, edit,
and verify directly; they run serially in the shared worktree. Enoy observes text
file diffs for review and records live tool outcomes. Its separate verification
runner uses allowlisted checks. Filtered context requests still use validated
patches, backups and review gates. Configured private-file filters disable native
tools because arbitrary native commands cannot enforce those filters. Claude
quota failures hand work to Codex. Budgets cap tasks, calls, retries and corrections.

`backends/process.py` streams native CLI events and cancels the process tree on
cancel or timeout. Native session IDs persist per chat/provider, scoped to the
workspace, access mode, model and effort. Full Access can start in a general chat
and discover projects using native tools. Workspace general chats have read-only
Enoy MCP folder discovery. Native tools act under Windows and provider policies;
Enoy does not grant administrator rights or override mandatory approvals.

The localhost HTTP API requires a per-launch token and a localhost Host header.
SQLite stores projects, chats, messages, changes, activity, team tasks and results.
The UI and Activity panel expose only Codex and Claude provider controls. Historical
messages retain the original provider ID for accurate attribution.

Historical design notes for removed workflows are in `docs/history/`.

## Build workspace

The approved plan is in `planning.md` (repository root). Modules:

- `devplan.py`: plan format and validator, planner context, `ask_model` (read-only planner and reviewer requests),
  `Planner` (start, draft, revise, approve).
- `devstore.py`: plans, versions, tasks, runs and frozen tests in SQLite. SQLite triggers refuse changes to
  approved versions and frozen fingerprints; task states change only through allowed transitions; claims are atomic.
- `planfile.py`: `planning.md` rendering and safe writing, the read-only archive of approved versions, CLAUDE.md drafts.
- `gitwork.py`: integration branch per plan, worktree and branch per task attempt (outside the project), merges
  with conflict detection, the user-requested final merge. Git runs with repository hooks and fsmonitor off.
- `devagent.py`: `Scope` (what a phase may read, write and run), `ToolLoop`, `task_context`, the test phase and
  implementation phase, `scrub_worktree` (secrets and private files removed from task copies).
- `devgate.py`: Enoy's own checks in a fresh checkout, the independent review, merge and completion, and
  `record_if_merged` for recovery.
- `devrunner.py`: manual, sequential and optional parallel scheduling, retries, offline waits, stop, resume.
- `providers.py`, `spend.py`, `backends/http.py`, `backends/openai_compat.py`, `backends/anthropic_api.py`,
  `backends/ollama.py`, `localguard.py`: API and local model access, spending limits, memory limits.
- `buildservice.py` and the `/api/build/*` routes; `ui/build.js` and Settings → Providers.

Rules that matter for safety are enforced in code rather than prompts: implementers use only Enoy's tools; writes
are limited to the phase's files; frozen tests are checked by hash; commands go through the command policy; every
request checks the spending limit first and is redacted and recorded; the user's branch changes only on request.
