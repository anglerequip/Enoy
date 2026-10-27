# Architecture

Enoy is a Python application with a web-technology UI shown in a
native Windows window. Everything runs on your PC except optional Claude requests.

```
┌──────────────────────── Desktop window (pywebview / Edge WebView2) ───────────────────────┐
│  ui/index.html + app.js + styles.css   (no build step, no external scripts)              │
└───────────────▲───────────────────────────────────────────────────────────────────────────┘
                │ HTTP + Server-Sent Events on 127.0.0.1, per-launch token
┌───────────────┴──────────── server.py (API) ──────────────────────────────────────────────┐
│                                                                                           │
│   orchestrator.py ── routing.py (classify / decide / should_escalate: pure functions)      │
│        │        └─── context.py (file selection, prompts, continuation brief)             │
│        │        └─── patches.py (parse model output, validate, diff, apply, revert)       │
│        │        └─── commands.py (command policy, sandbox-less runner with timeout)       │
│        │        └─── project.py (path guard, tree, index, search, retrieval)              │
│        │        └─── storage.py (SQLite: projects, tasks, events, change sets, runs)      │
│        ▼                                                                                   │
│   backends/ollama.py  ── Ollama HTTP API ── qwen3.5:9b  (local, default)                   │
│   backends/claude_code.py ── `claude -p` (official Claude Code CLI, optional)              │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

## Why this stack

No earlier prototype existed on this machine, so the stack was chosen fresh:

- **Python 3.12** was already installed, has SQLite built in, and makes the
  orchestration logic easy to test. The app needs only one third-party runtime
  package (pywebview) plus its Windows dependencies.
- **pywebview** opens a native window using Microsoft Edge WebView2, which is part
  of Windows 11. No Electron, no Node build, no Rust toolchain.
- Fallbacks: if pywebview fails, Edge opens in app mode (a chromeless window);
  failing that, the default browser.
- A local HTTP API keeps the UI separate from the logic and lets the same API be
  tested without a window.

## Components

| Module | Responsibility |
|---|---|
| `config.py` | Defaults, schema validation, atomic save with timestamped backups |
| `storage.py` | SQLite (WAL). Projects, tasks (with JSON state), messages, events, change sets, file changes, command runs, transmissions, cache |
| `project.py` | `Workspace`: path guard, ignored folders, protected secret files, file tree, index with symbols, search, keyword retrieval, test-command discovery |
| `redact.py` | Secret patterns (API keys, tokens, private keys, JWTs, Roblox cookies, `key = "value"` assignments) |
| `patches.py` | Block format for model edits, parsing, validation (path safety, exact-match search/replace, syntax checks, stale-file detection), unified diffs, all-or-nothing apply with backups, guarded revert |
| `commands.py` | Policy levels (allowlisted / approval / high_risk / blocked), shell-free execution, credential env vars removed, timeout and cancellation |
| `routing.py` | Task classification and routing rules as pure functions |
| `privacy.py` | Cloud package rules: cloud-private file patterns, local-only reasons (Settings, project, chat), scrubbing lines that quote private files, final redaction, the privacy-check summary |
| `activity.py` | Activity panel data (workflow stages, next action, model cards, counts) built from stored steps and events; never calls a model |
| `orchestrator.py` | Task lifecycle, retries, escalation, Claude confirmation gate, review/plan/compare workflows, approvals, test-fix loop, continuation briefs |
| `backends/ollama.py` | Streaming `/api/chat`, status from `/api/version`, `/api/tags`, `/api/show`, `/api/ps` |
| `backends/claude_code.py` | `claude -p --output-format json --tools ""` with the prompt on stdin, `claude auth status` for sign-in state |
| `server.py` | JSON API, SSE event stream, static UI, token + Host-header checks |
| `app.py` | Wiring, logging (redacted), single-instance check, window |

## Task lifecycle

1. **Context.** `context.gather` ranks project files by keyword match (explicit
   path mentions first, then names and symbols, then content), reads the top few
   within a character budget, redacts them, and records each file's SHA-256.
2. **Classification.** `routing.classify` decides *ask* vs *edit* and *low /
   medium / high* complexity from wording and signals. You can override the type.
3. **Routing.** `routing.decide` applies the routing mode, the workflow, and your
   per-task override. An explicit override always wins; if you pick Claude while it
   is unavailable, the task stops with a message instead of silently using Qwen.
4. **Local attempts.** Qwen answers. For edits, its output is parsed and
   validated. Invalid output is sent back with the exact errors, up to
   `max_local_attempts` times.
5. **Escalation.** `routing.should_escalate` uses objective signals only: invalid
   outputs, backend errors, failed checks after a fix attempt. "I'm not sure" from
   the model counts only together with an objective failure. Escalations per task
   are capped (`max_escalations`).
6. **Claude gate.** Before anything goes to Claude, the exact prompt is stored and
   (by default) shown to you. You can send it, preview it, or keep the task local.
7. **Proposal.** A valid change becomes a *change set* with per-file diffs. Nothing
   is written until you approve (unless auto-apply is turned on; deletes always ask).
8. **Apply.** All files are re-checked against the hashes they were proposed
   from. If anything changed, nothing is written. Originals are backed up.
9. **Checks.** You run checks from the UI. A failing check can be handed back
   ("Ask AI to fix"); after `max_test_fix_attempts` it escalates to Claude.
10. **Record.** Events, messages, transmissions and results stay in SQLite.

### Smart routing (mode `auto`)

`routing.classify` returns a category, an evidence score, risk (how costly or hard
to detect a wrong result is), expected scope, and needs (edits, tests, multi-step).
The score adds up evidence:

- *Categories that favour Claude:* architecture, concurrency, hard-to-reproduce
  bugs, security, integration/migration, persistent data, client/server game logic,
  performance, new features.
- *Scope:* several modules named or implied.
- *Requirements:* several interacting requirements, or a long list of them.
- *Earlier attempts:* the user says previous attempts failed.

Routine categories and "small/simple" wording lower the score. A short sentence can
still score high.

`routing.decide` compares the score with a threshold set by `aggressiveness`
(prefer_qwen 5, balanced 4, prefer_claude 3):

- **At or above the threshold:** Claude.
- **Up to two points below it:** Claude only when the risk is high (a mistake would
  be costly or hard to detect); otherwise Qwen, because the result is easy to verify.
- **Below that:** Qwen.

Per-category history of past tasks ("Qwen needed Claude in 4 of 5") moves the score
by at most one point and needs at least five finished tasks. The 70/30 target never
influences a decision.

The result is a `Decision` record (category, complexity, rule-margin confidence,
scope, risk, needs, escalation conditions, privacy note, preferred model, source:
router / manual / policy). It is stored once per task in `state.route`, together
with `initial_backend`. Retries, resumes and follow-ups reuse it.

Order of precedence:

1. **Privacy.** Settings "never", a local-only project, or a local-only chat.
2. **Manual model choice.**
3. **Routing mode and workflow.**
4. **The classifier.**

If Claude is wanted but unavailable, `claude_fallback` decides between continuing
on Qwen (and saying so) and stopping.

**Outcome-aware escalation.** `should_escalate` uses only measured facts:

- invalid proposals;
- backend errors;
- failed checks;
- the same failure signature after a fix (the last error line, with timings removed);
- a change that reaches more files than the routed scope, combined with a failure.

It is capped by `max_escalations` and `max_handoffs`. When Qwen hands over,
`_handoff_package` builds the summary Claude receives:

- the original request and later messages;
- the reason for the handoff;
- the plan, findings and decisions;
- the files inspected and the files already changed;
- the checks that ran, with real output;
- errors, Qwen's last answer and any unapplied diff;
- uncertainties and what remains.

**Privacy package.** Before every Claude request, `_request_claude`:

1. drops cloud-private files;
2. scrubs any line of them that reappears in Qwen's answer, a diff or command output;
3. redacts secrets in the final text;
4. logs a `privacy.checked` event with the counts.

**Statistics.** `routing_stats` counts the last `stats_window` completed or failed
tasks. Running, waiting, cancelled and interrupted tasks are left out. A task counts
as Claude's if Claude handled any part of it. The result explains any deviation from
the target from the stored decisions: routed directly (by category), escalated, or
picked by you.

### TEAM ROLES (`team.py`)

Turned on per chat (`state.team`, default `team.default_new_chats`). Only edit
requests in a project start a run; questions are answered directly. Roles describe
work and providers execute it, so one model can serve several logical agents, each
with its own team task, instructions, context package and status.

| Role | Runs on | When |
|---|---|---|
| Orchestrator | Code (`TeamRunner`) | Always. Owns the plan, the task graph, scheduling, retries, budgets and the final verdict |
| Architect | Router: Qwen or Claude | New features, refactors, integration work, multi-module scope, or high complexity. Returns a JSON plan |
| Developer (+ specialist) | Router per task | One per plan task. A specialist is extra instructions, activated only when the request or plan mentions its area |
| QA | Real check commands | After all developer tasks. Only allowlisted commands. "Could not run" (missing tool) is reported as unverified, not repaired |
| Reviewer | Deterministic checks; a model only from `review_min_lines` added lines or for high-risk work | After QA |
| Debugger | Router (Claude preferred when allowed) | When a failure repeats or in the second repair round. A first, simple failure goes back to a developer |

**Run flow**

1. **Understand.** An analysis step: gather the relevant files and classify.
2. **Plan.**
   - Small requests get a deterministic single-task plan; no model is used.
   - Otherwise the architect plans. `parse_plan` validates the plan: JSON, the task
     limit, duplicate ids and titles, unknown dependencies, unsafe paths, cycles.
   - Tasks that touch the same file are chained.
   - An unusable plan falls back to one task.
3. **Assign.** Each task is routed by `routing.classify` and `routing.decide`.
4. **Implement.** Tasks run in dependency order.
   - A batch is at most `max_parallel` tasks, with disjoint files and at most one
     task per provider. Qwen handles one request at a time anyway.
   - Developers get a task brief: goal, acceptance criteria, architecture notes,
     what their dependencies changed, and the files they own. No chat history.
   - Proposals are validated; invalid ones are retried, then escalated to Claude
     once when allowed.
5. **Approval.** Change sets wait for your approval unless auto-apply is on.
   - A run waits in `awaiting_review`, and approving a change set continues it.
   - A change set applied in a team run does not supersede the team's other change
     sets.
   - Conflicts (the file changed since the proposal) are redone once against the
     current files.
6. **Integrate, verify, review, repair.** Integration confirms that every change set
   is applied. Then QA, then review. Failures or blockers start a repair round,
   bounded by `max_repair_rounds`.
7. **Deliver.** A summary message with each task's result, the changed files, the
   real check result, the acceptance criteria (marked unverified when no test
   covered them) and the model request counts.

**Records**

- `team_tasks`: role, provider, routing and activation reasons, dependencies,
  status, result, change set.
- `team_messages`: typed messages such as assignment, proposal, rejection,
  escalation, approval, test failure, review finding, question and handoff.
- `team_memory`: requirements, architecture and plan proposals (unverified),
  implementation and test results (verified), findings, known issues.

**Controls**

- *Claude gate.* With "ask before sending", a run asks once, listing every planned
  Claude use. Allow covers this run only, within `max_claude_calls`. Decline moves
  those tasks to Qwen.
- *Budgets.* `max_model_calls` and `max_claude_calls` per run.
- *Zero-paid mode.* Claude is never chosen automatically.
- *Privacy.* Claude prompts are built from a cloud-safe copy of the context, so
  excluded files are absent and private lines are scrubbed.
- *Pause and cancel.* Switching TEAM ROLES off pauses at the next checkpoint and
  keeps finished work; switching it on continues from the saved graph without
  duplicating tasks. Cancel closes the graph.
- *Restart.* After a restart, tasks that were running become `planned`, and Resume
  continues the run.

### Steps and activity events

Every unit of work inside a task is stored as a **step** (`steps` table): the
analysis, each Qwen attempt, each Claude request, Enoy's check of a proposal,
your approval, and each check run. A step records who does it (`qwen`, `claude`,
`system` = Enoy itself, `user`), the stage, the reason it was assigned (taken
from the routing or escalation decision at that moment), its status
(queued / working / waiting / completed / failed / cancelled / skipped /
interrupted), the step it followed, its files, result and error.

The orchestrator also writes structured **activity events** (`activity` table) at
the same points: `task.created`, `task.assigned`, `task.escalated`,
`step.started/completed/failed`, `backend.request`, `approval.requested/resolved`,
`patch.proposed`, `validation.failed`, `tool.started/completed` (file writes),
`test.started/completed`, `task.completed/failed/cancelled`, `task.interrupted`,
`backend.connected/disconnected`, `dispatch.paused/resumed`. They are pushed to the
UI over the existing event stream and stay in SQLite across restarts.

Safety nets keep the panel truthful: when a task ends, any step still open is
closed as cancelled; at startup, steps that were working are marked interrupted;
"Cancelled" is shown only after the task has actually stopped. *Pause new tasks*
queues new tasks and follow-ups (they survive a restart); it does not stop work
already running.

### Workflows

| Workflow | What happens |
|---|---|
| `escalate` (default) | Qwen first; Claude only when the rules above say so |
| `local_only` | Never Claude |
| `local_then_review` | Qwen implements; Claude reviews the diff; on `CHANGES_NEEDED` Qwen revises (bounded by `max_review_rounds`) |
| `claude_plans` | Claude writes a plan; Qwen implements it |
| `compare` | Qwen and Claude both propose; you approve one, the other is superseded |

## Shared context between the two models

The models do not share memory. Enoy passes state explicitly:

- Project instructions (`ENOY.md`, `.enoy/instructions.md`, `CLAUDE.md`, `AGENTS.md`).
- A project summary (made locally by Qwen, cached by file-list hash).
- Short summaries of recent tasks in the project.
- The task's own state: plan, decisions, failed approaches, check results.
- For Claude follow-ups, the Claude Code session is resumed (`--resume`) and only
  files whose hash changed are sent again.

The **continuation brief** (`context.continuation_brief`) is built from these
records, not from any model's hidden history. It is used to resume interrupted
tasks and for the manual hand-off to Claude Code.

## Security model

- File access goes through `Workspace.resolve`: no absolute paths, no `..`, no
  alternate data streams or device names, and symlinks/junctions may not lead
  outside the project.
- Secret-looking files (`.env*`, keys, certificates, credential stores) are never
  read, indexed, shown, or sent. Text is redacted before it reaches a model, the
  database, or the log. A proposal that would write `[REDACTED]` over real content
  is rejected.
- Commands run without a shell, so pipes, redirection and chaining are impossible;
  known destructive, install, network and admin programs need explicit approval or
  are blocked. **This is not an operating-system sandbox**: an approved command runs
  with your normal Windows permissions and can use the network.
- Claude Code runs with no tools in an empty folder, so it sees only what Enoy
  AI sends and cannot touch your files.
- The local API listens on 127.0.0.1 only and requires a random per-launch token and
  a localhost Host header. The UI uses a Content Security Policy that blocks inline
  and external scripts, and all model output is HTML-escaped.

## Data locations

`%LOCALAPPDATA%\Enoy\` holds `config.json`, `enoy.db`, `logs\`,
`config-backups\`, `file-backups\` and the browser profile for the window.
Set `ENOY_HOME` to move it.
