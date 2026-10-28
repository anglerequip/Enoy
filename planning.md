# Enoy: AI development orchestration ("Build" workspace) — assessment and plan

Status: **approved 2026-10-28** with the recommended answers to every question in §12 (see "Decisions" at the end).
Work happens on branch `feature/build`; it is pushed to `main` (which publishes a release) once at the end.
Inspected: `C:\ClaudeGENERELLT\enoy-workspace` at commit `c4aba02` (Enoy 0.6.14), 2026-10-28.

---

## 1. Repository architecture assessment

| Area | What Enoy actually uses |
|---|---|
| Runtime | Python 3.12, standard library only plus `pywebview 5.4` (WebView2 window) and its Windows dependencies (`pythonnet`, `clr_loader`). No web framework: `enoy/server.py` has its own small router. |
| Frontend | Plain JavaScript and CSS, with no framework or build step: `enoy/ui/app.js` (2,010 lines), `projects.js`, `activity.js`, `markdown.js`, `styles.css`, `index.html`. The sidebar has Chat/Code modes, Projects, Artifacts, Activity and Settings. Modals, drawers and toasts are shared. |
| Backend API | Localhost HTTP API, about 90 routes, protected by a per-launch token and a check that the Host header is localhost. |
| Database | SQLite at `%LOCALAPPDATA%\Enoy\enoy.db` (`storage.py`, 1,032 lines). Migrations only add columns (`ALTER TABLE ADD COLUMN`). Updates make a backup before migrating. |
| Config | `%LOCALAPPDATA%\Enoy\config.json`, checked against a typed schema (`config.py`). Keeps 15 backups. |
| Secrets | `enoy/tools/keystore.py` stores keys in Windows Credential Manager (`Enoy/<name>`). The UI can only set, clear or ask "is it set". Today the only allowed name is `brave_search`. |
| AI providers | Exactly two, both through signed-in command-line clients: **Codex CLI** (ChatGPT plan) and **Claude Code CLI** (Claude Pro plan). There is **no HTTP API provider and no local model.** Qwen/Ollama was removed in commit `e67534e`, and `backends/base.py` still keeps `LOCAL = "qwen"` only as a historical label. |
| Backend contract | `Backend.status()`, plus `Backend.chat(ChatRequest) -> ChatResult`. Failures raise `BackendError` with a kind: `unavailable`, `timeout`, `limit`, `malformed`, `cancelled` or `failed`. |
| Orchestration | `coordinator.py`: ChatGPT writes a JSON task plan. `team.py` (`TeamRunner`, 1,284 lines) runs it with roles (architect, developer, qa, reviewer), a `team_tasks` table (plan_key, acceptance, files, depends_on, provider, status, attempts, spec), cycle checks, budgets (tasks, model calls, Claude calls, repair rounds), Claude plan review, QA through allowlisted commands, a review gate and repair rounds. |
| Editing | `patches.py`: validated patch blocks, atomic writes, backups, change sets you can approve, reject or revert, a lost-content check and syntax checks. Native CLI agents can also edit directly when the access policy allows it. |
| Commands | `commands.py`: no shell. Every command is classified as blocked, high_risk, approval or allowlisted. Pipes and redirection, admin tools, credential paths and encoded PowerShell are blocked. Commands run in an environment with secrets removed (`safe_env`), with a timeout, and the whole process tree is killed on cancel. |
| Workspace safety | `project.py`: paths cannot escape the project. Sensitive files (`.env`, `*.pem`, `*.key` and similar) and excluded folders are hidden. Instruction files are read in this order: `ENOY.md`, `.enoy/instructions.md`, `CLAUDE.md`, `AGENTS.md`. `planning.md` is not read. |
| Privacy | `privacy.py` and `redact.py`: a cloud policy, a local-only switch per chat or project, private files and secret redaction. The `transmissions` table records what was sent to the cloud. |
| Git | `gitinfo.changed_files()` and a "your uncommitted files" snapshot. **No commits, branches or worktrees.** |
| Recovery | On startup, running tasks are marked `interrupted` and `Orchestrator.resume()` can continue them. |
| Usage | Plan-limit windows are read from both CLIs (`usage.py`). There is no money or cost tracking. |
| Claude tools | A stdio MCP server (`tools/mcp_server.py`) gives Claude read-only `list_files`, `read_file`, `search_code` and `find_folders`. |
| Tests | 364 pytest tests. 4 were already failing before this work (approval flow, image approval, permission-modes API). CI runs only an offline subset. |
| Release | Every push to `main` triggers GitHub Actions: version `0.6.<run>`, PyInstaller and Inno Setup installer, smoke test, then a published GitHub release. The updater verifies checksums. |

## 2. Existing features relevant to this integration (reuse inventory)

- **Backend protocol and the two CLI adapters.** New API adapters plug in beside them.
- **Planning, task data and dependencies** (`coordinator.py`, `team.parse_plan`, `_check_cycles`), and file-disjoint batch selection (`_pick_batch`).
- **Budgets and per-provider caps** (`team.*` config).
- **Agent file tools** (`tools/agent.py`, `AgentFiles`: list, read, search, edit, write, run_command, routed through the command policy).
- **Command policy and runner** (`commands.py`). Kept unchanged and reused as the only way agents run commands.
- **Patch validation helpers** (`check_syntax`, `lost_content`, `_write_atomic`) and the `UNFINISHED` marker check.
- **Credential Manager key store, redaction, `safe_env`, privacy filters, transmissions log.**
- **Activity log and live streaming UI** (`activity.js`), modals and drawers, Settings tabs.
- **Read-only MCP file tools**, which give a Claude planner real repository access.
- **Interrupted-task recovery, config backups, database backup on update, release pipeline.**

## 3. Gap analysis

| Needed | Exists? | Work |
|---|---|---|
| HTTP API providers (DeepSeek, Fireworks, Anthropic API, OpenAI-compatible) | No | Two adapters: OpenAI-compatible and Anthropic Messages |
| Local model runtime | Removed | Ollama through the OpenAI-compatible adapter (`/v1`), detection only, never downloads models |
| Model registry and role assignment (planner, implementer, reviewer) | Partly (per-CLI model fields) | New `providers`, `models` and `roles` config |
| Secure keys for many providers | Partly (one fixed name) | Generalise the key store names |
| Connection test and model discovery | Claude CLI test only | Per provider; listing models costs no tokens |
| Spending limits and cost ledger | No | `model_calls` ledger, price table, check before each request |
| Strict plan schema (allowed files, tests first, verification commands, edge cases, failure conditions) | Partial (title, description, acceptance, files, deps) | New validated schema |
| Plan versions, approval lock, immutable archive | No | New tables, plus a hashed JSON archive |
| `planning.md` sync, and reading `CLAUDE.md` / `planning.md` | `CLAUDE.md` read only | Renderer, safe writer, archive |
| Test-first protocol with **technically frozen tests** | No | Two-phase run, file hashes, test commit, write refusal |
| Git worktree isolation, per-task branches, merge and conflict detection | No | New git service |
| Fresh, minimal context per task | Partial (briefs) | Task context packer |
| Independent review that cannot be overridden by the implementer | Partial (Claude review) | Fixed checks plus an optional review by a different model |
| Scheduler: manual, then automatic sequential, then optional parallel | Partial (team batches) | New scheduler with atomic task claims |
| Offline handling of pending tasks | Partial (interrupted) | Network-wait state, no failure counted while offline |
| UI: Build workspace (Projects, Plan, Tasks, Agents, Providers, Monitor) | No | New nav page plus a Settings tab |

## 4. Recommended architecture and integration points

**Principle:** add a separate **Build** workflow next to the existing chat and team flow, which stays unchanged. Reuse the modules listed above. Make any rule that matters for safety a check in code, not an instruction in a prompt.

New modules, all under `enoy/`:

| Module | Responsibility |
|---|---|
| `backends/http.py` | Shared HTTP client (urllib): timeouts, cancel, retry and back-off on 429/5xx, error-kind mapping, keys removed from errors |
| `backends/openai_compat.py` | DeepSeek, Fireworks, Ollama `/v1`, custom endpoints: chat, tool calls, usage, model listing |
| `backends/anthropic_api.py` | Anthropic Messages API (and DeepSeek's Anthropic-compatible endpoint) |
| `providers.py` | Provider and model registry, status, connection test, discovery, concurrency semaphores |
| `spend.py` | Cost ledger, price table, daily and monthly limits, check before each request |
| `devplan.py` | Plan schema, validator, planner prompts, versions, approval |
| `devstore.py` | Storage for plans, tasks, runs and frozen files (same SQLite file); state machine; atomic task claims |
| `planfile.py` | `planning.md` renderer, safe writer, archive |
| `gitwork.py` | Worktrees, branches, commits, merges, conflicts, cleanup (git through an argument list, no shell, `safe_env`) |
| `devagent.py` | Tool loop for API models; scoped tools; test phase and implementation phase |
| `devgate.py` | Fixed verification gate plus optional model review |
| `devrunner.py` | Scheduler: ready set, claims, retries, offline waits, cancel, merges |
| `ui/build.js` (+ CSS in `styles.css`) | Build workspace UI |

**Execution flow for one task:**

1. Claim the task.
2. Create a new worktree at `%LOCALAPPDATA%\Enoy\worktrees\…` on branch `enoy/p<plan>/<task>-a<attempt>`, starting from the integration branch.
3. **Test phase.** The agent may write only the task's test files. Enoy runs the verification command, requires the expected failure, then commits "tests: Tn" and records a SHA-256 hash of every test file.
4. **Implementation phase.** The agent may write only the task's allowed files, minus the frozen tests. A write to a frozen file is refused by the tool itself.
5. **Gate.** Enoy (not the agent):
   - checks the diff stays in scope;
   - re-hashes the frozen tests;
   - re-runs the frozen tests and the regression commands;
   - runs the syntax, lost-content and unfinished-marker checks.
6. **Optional review** by a different model.
7. Merge into the integration branch `enoy/plan-<id>` and record the commit ID and evidence.
8. Update `planning.md`.
9. Remove the worktree. The next task starts with a fresh context.

Your own branch is touched only when you click **Merge into <branch>**.

## 5. Provider configuration strategy

Endpoints were checked against official docs on 2026-10-28. They will be checked again at implementation time.

| Provider | Endpoint | Auth | Models | Notes |
|---|---|---|---|---|
| DeepSeek | `https://api.deepseek.com` (OpenAI-compatible); `https://api.deepseek.com/anthropic` | `Authorization: Bearer` | Docs name `deepseek-flash` and `deepseek-v4-pro` | The pricing page could not be read. Prices are entered or confirmed by you, never hardcoded. A list-models endpoint is not documented, so discovery falls back to manual entry. |
| Fireworks | `https://api.fireworks.ai/inference/v1` (OpenAI-compatible) | `Authorization: Bearer` | Format `accounts/fireworks/models/<name>`; docs show GLM (`glm-5p3`, `glm-5p3-flash`) and Kimi | Listing is `GET /v1/accounts/{account_id}/models`, which needs the account ID |
| Anthropic API | `https://api.anthropic.com/v1/messages`, `GET /v1/models` | API key and `anthropic-version` header | Discovered with `GET /v1/models` | **Billed separately from your Pro plan.** Off until you authorise it. |
| Claude Code CLI | existing | your Pro sign-in | existing | Planner with no extra charges (uses Pro plan limits) |
| Codex CLI | existing | your ChatGPT sign-in | existing | Planner or reviewer with no extra charges |
| Ollama (local) | `http://127.0.0.1:11434/v1` (+ `/api/tags` to list) | none | Only what is already installed | Works offline. Never downloads a model without asking. |

**Rules for every provider:**
- The connection test lists models (no tokens used). A paid one-token chat test runs only after you click and confirm a dialog that shows it costs money.
- No model name, endpoint or key is hardcoded. Defaults ship disabled and empty.
- Each model has editable prices per million tokens (input, cached input, output). Where a provider reports cost, that number is shown. Otherwise the figure is labelled **estimate**.
- Limits are per provider: daily and monthly spending caps, plus a maximum number of requests at once. Before each request, Enoy reserves the largest possible cost (prompt plus max output at the configured price). If that would break a cap, the request is refused.

## 6. Offline-first strategy

- The UI, plans, tasks, `planning.md`, logs, settings and history all stay local in SQLite and JSON. They work with no connection.
- Each provider is labelled **Local** or **Cloud**. Cloud actions are disabled with a reason ("No connection" or "No key set") instead of failing silently.
- If a request fails because the network is unavailable, the task moves to `waiting_network`. No attempt is counted, its worktree is kept, and it resumes when the connection test passes again.
- **Restart:** running work is marked interrupted, claims are released, and worktrees are kept.
- **No duplicate work:** a task counts as complete only once its commit is on the integration branch. On resume, Enoy checks git first and records completion without re-running if the commit is already there.
- With Ollama and an installed model, the whole workflow can run offline.

## 7. Security and isolation design

- **Implementers never get a native CLI agent with its own shell.** They work through Enoy's tool loop, so every rule is enforced in Enoy's code:
  - Reads stay inside the worktree, with sensitive files hidden.
  - Writes are limited to the current phase's allowed files.
  - Commands go through `commands.classify`. When unattended, only allowlisted commands and the task's approved verification commands run. Anything else stops the task as a blocker, or waits for your approval in manual mode.
- **Planners are read-only by construction.** They get no write tools and no native edits. Any code or patch blocks in their output are rejected by the plan validator.
- **Secrets:**
  - Keys live only in Credential Manager.
  - Keys are never put into the environment of child processes (`safe_env` already strips variables that look like secrets).
  - Outgoing context goes through the existing redaction and privacy filters, and every API call is logged in `transmissions`.
  - A test checks that the content of a `.env` file never leaves the PC.
- **Instructions inside a repository** (`CLAUDE.md`, `planning.md`) are passed to models as project guidance. They never change Enoy's permissions.
- **Rollback:** every task is a separate branch and commit. Your branch changes only on an explicit merge, and the merge can be undone with `git revert`. Worktrees with uncommitted changes are never deleted without asking.
- **Cancel** kills the agent loop and the command process tree.
- **Known limit:** Windows cannot block network access for one process without admin firewall rules. Commands containing URLs, or install commands, are already high-risk and are not run unattended.

## 8. UI integration proposal

- A new sidebar entry, **Build**, next to Projects and Artifacts, using the same button style and icon size. Its page has tabs:
  - **Project:** pick a git repository; shows branch, clean or dirty state, instruction files found, test commands.
  - **Plan:** goal box, planner model picker, Generate, versions with differences, comments, Approve or Request changes.
  - **Tasks:** checklist with dependencies and status badges. A drawer shows the full spec, tests, commands, acceptance, evidence and commit. Buttons: Run, Stop, Retry, Mark for revision.
  - **Agents:** active runs, live log (reusing `activity.js` streaming), results and diffs.
  - **Monitor:** test results, tokens and cost per plan and model, failures, changed files.
- **Settings → Providers** (a new tab):
  - Providers with Local or Cloud badges.
  - Key field (password input, never shown back).
  - Test, Discover models, role assignment (planner, implementer, reviewer), limits.
- Offline: cloud buttons are greyed out with the reason. Local data stays fully browsable.

## 9. Test strategy

- **Unit tests for every new module**, using fake HTTP servers (`http.server` in a thread) and temporary git repositories. No network.
- **Tamper tests:**
  - an agent edits a frozen test;
  - an agent writes outside its allowed files;
  - an agent claims success while the tests fail;
  - a planner returns code.
- **Recovery tests:** restart in the middle of a task; offline in the middle of a task; resume after a merge has already happened.
- **Simulated end-to-end:** a scripted OpenAI-compatible fake model drives the full flow on a temporary repository, then a simulated restart and a simulated offline period. Clearly labelled **simulated**.
- **Live tests:** only with your keys and your approval, each request listed beforehand. Results are labelled **live** with model and token counts.
- **CI:** new offline test files are added to the release workflow's test list.
- **UI:** checked in the browser preview, including dark mode and a narrow window.

## 10–11. Implementation checklist with acceptance criteria

Each task fits one fresh session. "Verify" is the exact command. Every task also has to keep the existing suite at the baseline: 360 passing and the 4 known failures unchanged, unless the task fixes them.

### Phase 0: Baseline
- [x] **T0.1 Fix the 4 pre-existing test failures.** *Done:* the tests were out of date; the code was not broken. Commit `090172b` intentionally retired "Ask before sending to Claude" and added `agent.self_check`. Three tests now check the current behaviour and one checks the new setting. Production code is unchanged. Full suite: 363 passed, 0 failed.
  *Original spec:* Files: tests named in §1 and the code they cover (`team.py` plan-review confirmation, the image approval path, the `server.py` permission-modes API). Tests first: the existing failing tests are the specification. Verify: `python -m pytest -q`. Accept: 0 failures. Production behaviour changes only where a test shows a real bug. Blocker: a test's expectation contradicts a decision you made later. If so, ask you.

### Phase 1: Data and persistence
- [x] **T1.1 Schema.** *Done:* 6 tables added to `SCHEMA`. SQLite triggers refuse to change or delete an approved plan version or a frozen test fingerprint. Tests: a 0.6.14 database keeps every row; opening it twice is harmless; both locks hold.
  *Original spec:* Files: `enoy/storage.py`, `tests/test_devstore.py`.
  - Adds the tables `dev_plans`, `dev_plan_versions`, `dev_tasks`, `dev_runs`, `dev_frozen_files` and `model_calls`.
  - Additive only: nothing existing is altered.
  - Tests first: a copy of a 0.6.14 database migrates with every row count unchanged; running the migration twice is harmless.
  - Verify: `python -m pytest -q tests/test_devstore.py tests/test_migration.py`. Accept: those tests pass; existing tests unchanged.
- [x] **T1.2 Store API and state machine.** *Done:* `enoy/devstore.py`. Plan versions get a SHA-256 fingerprint. Approving a version locks it and creates its tasks: tasks with no dependencies start as `ready`, the rest as `pending` (the spec's `draft` is called `pending`), and `refresh_ready` releases them. A claim token comes from one conditional UPDATE: 8 threads, 1 winner. A stale token cannot change a task. `completed` needs a commit ID. Also covers runs and frozen files.
  *Original spec:* Files: `enoy/devstore.py`, `tests/test_devstore.py`.
  - Task states: `draft → ready → claimed → testing → implementing → verifying → reviewing → merging → completed`, plus `blocked`, `failed`, `waiting_network`, `interrupted`, `cancelled`.
  - The claim is a single conditional UPDATE.
  - Tests first: an illegal transition raises an error; two claims at the same time give exactly one winner; an approved plan version cannot be updated.
  - Accept: tests pass.
- [x] **T1.3 Startup recovery.** *Done:* `DevStore.recover()` runs in `app.build()` after `mark_interrupted`. Runs that were running become `interrupted` with a reason, active tasks become `interrupted` with their claim released, and completed and pending tasks are untouched. Full suite: 377 passed.
  *Original spec:* Files: `enoy/app.py` (one call), `enoy/devstore.py`.
  - Tests first: runs left running become `interrupted`; claims are released; completed tasks stay completed.
  - Accept: tests pass, and `storage.mark_interrupted` behaviour is unchanged.

### Phase 2: Providers
- [x] **T2.1 Key store names.** *Done:* `provider:<id>` key names are accepted and the Brave key works as before. The Windows calls are split into `_read`/`_write`/`_remove` so tests never touch the real Credential Manager. Key values never appear in a status or an error.
  *Original spec:* Files: `enoy/tools/keystore.py`, `tests/test_keystore.py`.
  - Allow `provider:<id>` where the ID matches `[a-z0-9_-]{1,40}`. `brave_search` still works.
  - Tests first: invalid names are rejected, and a key value never appears in `status()` or in an exception message. The Windows API is mocked.
- [x] **T2.2 Registry config.** *Done:* New `build` section with providers, models, roles and `parallel`. DeepSeek, Fireworks and Anthropic (API version `2026-10-21`) default to off with US$1/day and US$10/month limits. Ollama is local, off and has no limits. The planner is `claude-cli` and the reviewer `codex-cli`. Validation refuses: secret-like fields, plain `http` to anything but this PC, unknown providers, and roles that point at a model not allowed for that role. Old config files load unchanged.
  *Original spec:* Files: `enoy/config.py`, `tests/test_config.py`.
  - Adds `providers`, `models` and `roles` to the schema, with no secrets and with defaults disabled and empty.
  - Tests first: old config files load unchanged; a config containing a field named like a key is rejected; a role that points to an unknown model is reported.
- [x] **T2.3 HTTP client.** *Done:* `enoy/backends/http.py`, standard library only. Handles retries, Retry-After, cancel, and maps errors to kinds: `auth`, `limit` (429/402), `unavailable`, `timeout`, `malformed`, `failed`. Keys are removed from error text. Note: on Windows a connection to a closed port only times out, so any connect-stage failure counts as `unavailable` and only a slow answer counts as `timeout`.
  *Original spec:* Files: `enoy/backends/http.py`, `tests/test_http_client.py`.
  - Tests first, against a fake server: timeout → `timeout`; 401/403 → `auth`; 429 with Retry-After → retried and then `limit`; 5xx → retried; connection refused → `unavailable`; cancel stops the request; the key never appears in the error text.
- [x] **T2.4 OpenAI-compatible adapter.** *Done:* Chat, tool calls (invalid argument JSON is reported, not guessed), image data URLs and model listing. Usage is normalised to uncached input, cached input and output tokens. Each assistant turn is sent back exactly as the provider returned it, because DeepSeek requires `reasoning_content` back during tool use (checked in its docs, 2026-10-28). `temperature` is never sent.
  *Original spec:* Files: `enoy/backends/openai_compat.py`, tests.
  - Chat, tool calls, usage (including DeepSeek cache-hit fields when present), `list_models()`.
  - Tests first, using recorded response shapes in a fake server.
- [x] **T2.5 Anthropic Messages adapter.** *Done:* Raw HTTP, not the SDK, so that Enoy stays standard-library only and every provider shares one client. Headers: `x-api-key`, `anthropic-version: 2026-10-21`. No `temperature`/`top_p`/`top_k` and no forced `tool_choice`. Thinking blocks are sent back unchanged, and all tool results of a turn go in one user message. A refusal raises an error rather than returning an answer. Model listing follows every page. Still off by default.
  *Original spec:* Files: `enoy/backends/anthropic_api.py`, tests.
  - Same coverage as T2.4, in the Messages format (tool_use and tool_result blocks), plus `GET /v1/models`.
  - The headers are checked against the docs again when this task is done.
- [x] **T2.6 Provider service.** *Done:* `enoy/providers.py`. Status per provider: disabled, no_key, no_connection, key_refused, ready or verified. The free connection test lists models. A paid test needs `confirm=True`, and only a successful real request marks a provider verified. Concurrency limits are per provider. `complete()` is the only metered request path.
  *Original spec:* Files: `enoy/providers.py`, tests.
  - Status per provider (no key / no connection / ready / verified); a connection test that costs nothing; discovery; a per-provider concurrency limit.
  - Tests first: offline maps to "No connection"; `verified` is set only after a real successful response; a "paid test" call without the confirm flag is refused.
- [x] **T2.7 Spend ledger and limits.** *Done:* `enoy/spend.py`. Before each request it reserves the most the request could cost, and checks it against the daily and monthly caps inside one transaction. When a request ends, the actual cost is calculated from the prices saved with the reservation. A cost reported by the provider wins. Local requests cost 0. A missing price blocks the request whenever a cap is set. A failed request without usage costs 0. After a restart, reservations that were cut off count as spent.
  *Original spec:* Files: `enoy/spend.py`, tests.
  - Tests first: a request is refused when the reserved maximum cost would cross the daily or monthly cap; the actual cost is recorded from usage; missing prices give "cost unknown" and are not treated as zero for the cap (a request with unknown cost is blocked when a cap is set).
- [x] **T2.8 Provider API routes.** *Done:* Five routes: `GET /api/build/providers`, plus `key`, `test`, `paid-test` (which needs `confirm: true`) and `roles`. A test scans every response, the logs and 5 database tables for a sentinel key. Full suite: 439 passed.
  *Original spec:* Files: `enoy/server.py`, `tests/test_server_providers.py`.
  - `GET /api/build/providers`, `POST …/key`, `POST …/test`, `POST …/discover`, `POST /api/build/roles`.
  - Tests first: no response, log or activity row ever contains the key (a sentinel key is scanned for).

### Phase 3: Planning engine
- [ ] **T3.1 Plan schema and validator.** Files: `enoy/devplan.py`, `tests/test_devplan.py`.
  - Required fields per task: id, objective, depends_on, allowed_files, test_files, spec, inputs/outputs, edge_cases, tests (name and expected failure), verify_commands, regression_commands, acceptance, completion, blockers.
  - Rejects: cycles, unknown dependencies, paths outside the repo or sensitive paths, test files outside `test_files`, commands that are not `allowlisted` or `approval` under `commands.classify`, production code blocks, and more than N allowed files.
  - Tests first: one case for each rejection.
- [ ] **T3.2 Planner context.** Files: `enoy/devplan.py`, `enoy/project.py` (read `planning.md` separately; `INSTRUCTION_FILES` unchanged).
  - Tests first: the context includes instruction files, the result of `detect()` and a tree summary; it excludes sensitive files; it stays within the size limit.
- [ ] **T3.3 Planner service.** Files: `enoy/devplan.py`.
  - Calls the planner-role model with read-only tools. When validation fails, it sends one repair round with the errors. It stores a draft version.
  - Tests first, with a scripted backend: the request carries no write tools; invalid output is repaired once and then reported.
- [ ] **T3.4 Revision and approval.** Tests first: a comment creates version N+1; approving locks the version (hash stored); only tasks of the approved version can become `ready`.

### Phase 4: `planning.md`, `CLAUDE.md` and archive
- [ ] **T4.1 Renderer.** Files: `enoy/planfile.py`, golden-file test.
  - Open tasks show their full spec. Completed tasks show `[x]` with ID, summary, commit and evidence only.
  - There is a marker line with the plan ID and a hash. Output is byte-for-byte the same for the same input.
- [ ] **T4.2 Archive.** Each approved version is written as JSON with a `.sha256` file, in the data folder (and in the repo if you choose that, see Q4). Tests first: tampering is detected when loading.
- [ ] **T4.3 Safe writer.** The file is written only if it is absent, or marked by Enoy and unchanged since Enoy's last write. Otherwise Enoy shows a diff and asks. The write is atomic. Tests first: a file you edited is never overwritten without confirmation.
- [ ] **T4.4 `CLAUDE.md` generation.** The planner drafts it, you see a preview, and it is written only if absent or you confirm, with a backup. Tests first: an existing `CLAUDE.md` is untouched without confirmation.

### Phase 5: Git isolation
- [ ] **T5.1 Git service.** Files: `enoy/gitwork.py`, `tests/test_gitwork.py`, using real temporary repositories.
  - Detect a repository; integration branch from HEAD; task worktree and branch; commit as "Enoy agent"; diff names; merge with conflict detection (abort on conflict); remove a worktree.
  - Tests first: a conflict leaves both branches intact; your working branch is never changed.
- [ ] **T5.2 Cleanup.** Tests first: `prune` runs; a worktree with uncommitted changes is not removed without confirmation.
- [ ] **T5.3 Final merge into your branch.** An explicit action. It refuses when your working tree has changes in overlapping files. Tests first.

### Phase 6: Implementer execution
- [ ] **T6.1 Scoped tools.** Files: `enoy/devagent.py` (wraps `AgentFiles`), tests.
  - Tests first: writing outside the allowed files is refused; writing a frozen test is refused; reading `.env` is refused; a high-risk command is refused when unattended.
- [ ] **T6.2 API tool loop.** Limits on steps, tokens and spend (checked every round), plus cancel. Tests first: a scripted fake model; the loop stops at each limit with a clear reason.
- [ ] **T6.3 Context packer.** Tests first: the context contains this task only, plus completion summaries of its dependencies. It contains no chat history and no other task's spec, and it stays within size.
- [ ] **T6.4 Test phase.** The run must fail for the expected reason: named tests fail, with no syntax, import or collection error. Then commit and freeze the hashes. Tests first: a test that passes before the change counts as a blocker; a test that errors on collection counts as a blocker.
- [ ] **T6.5 Implementation phase.** Tests first: tampering with a frozen test means rejection, even when the tests pass; a failing frozen test means a retry, then `failed`.
- [ ] **T6.6 Blocker reporting.** `report_blocker` tool. "Test spec wrong" sets `blocked` and asks for a plan revision. Tests first.

### Phase 7: Verification and review
- [ ] **T7.1 Fixed gate.** Scope, hashes, a fresh re-run of the tests, syntax and lost-content checks, unfinished markers. Tests first: one failing case for each check.
- [ ] **T7.2 Model review.** The reviewer model must differ from the implementer (enforced). The review cannot override a failed gate. Tests first.
- [ ] **T7.3 Completion recorder.** Merge, commit ID, evidence, `planning.md` update. If the commit is already merged, completion is recorded without re-running. Tests first.

### Phase 8: Scheduler
- [ ] **T8.1 Manual and automatic sequential runs.** Retries with a new worktree each time; `waiting_network` when offline; stop and cancel. Tests first.
- [ ] **T8.2 Parallel runs (off by default).** Disjoint files, concurrency limits, spend reserved for every task in the batch, merges one at a time, a conflict sends the task back to ready for a new attempt. Tests first, with real temporary git and fake agents. It is enabled only after T12.1 passes.

### Phase 9: UI
- [ ] **T9.1** Build nav entry and Project tab.
- [ ] **T9.2** Plan tab.
- [ ] **T9.3** Tasks tab and drawer.
- [ ] **T9.4** Agents and Monitor tabs.
- [ ] **T9.5** Settings → Providers tab.

Each UI task has a JS test where logic exists (`tests/js`), plus a preview check: light and dark mode, narrow window, offline state.

### Phase 10: Local models
- [ ] **T10.1 Ollama.** Detect the service; list installed models; Local badge; works offline. It never pulls a model. Tests with a fake `/api/tags`. The memory guard follows your answer to Q3.

### Phase 11: Security hardening
- [ ] **T11.1** Redaction, privacy filters and transmission logging on every new provider path. Test: a `.env` sentinel never appears in any captured request.
- [ ] **T11.2** Child-process environment: no provider key and no variable that looks like a secret. Test.
- [ ] **T11.3** Audit and cancel: every Build action appears in Activity, and cancel kills the process tree. Test.

### Phase 12: End-to-end and release
- [ ] **T12.1 Simulated end-to-end** on a temporary repository with a fake provider: register, plan, approve, `planning.md`, one task with tests first, gate, merge, the next task in a fresh context, restart and persistence, offline wait and resume. Labelled simulated.
- [ ] **T12.2 Live checks** (only with keys and your approval, each request listed first): DeepSeek implements one task in a test repository; the Claude CLI plans once (Pro plan); the Fireworks connection test; the Anthropic API only if authorised.
- [ ] **T12.3 Docs and release.** `docs/ARCHITECTURE.md`, README, and the CI test list; push to `main`, which publishes the release.

## 12. Questions and decisions needed before implementation

1. **Planner model.** I recommend Claude Opus 5.5 through the existing Claude Code CLI (your Pro plan, no extra API charges), with ChatGPT through Codex as the fallback. Should the Anthropic API key path be built but left **off** until you authorise its separate billing?
2. **Keys and caps.** Which keys will you add (DeepSeek, Fireworks, Anthropic)? Starting caps: I suggest US$1 a day and US$10 a month per provider, with live tests needing your OK.
3. **Local models.** Ollama support was removed in `e67534e`. Should I bring it back as an optional local provider? Do the old memory limits (Chill 1,200, Middle 1,600, Full 1,900 MiB) apply again, which means bringing back the memory guard?
4. **Where `planning.md` and the archive live.**
   - Proposal: `planning.md` in the project root, not committed automatically.
   - The archive goes in Enoy's data folder.
   - Optionally also `.enoy/plans/` in the repository.
5. **Repositories without git.** Refuse, or offer `git init` (with your OK)? I recommend offering it.
6. **Default reviewer.** Fixed checks always run. I recommend ChatGPT through Codex as the default reviewing model, since it costs no API charges and is a different model from the implementer.
7. **Unattended commands.** Only allowlisted commands and the plan's approved verification commands; everything else stops the task. Is that acceptable?
8. **Existing chat and team flow.** Keep it unchanged, with Build separate (recommended)?
9. **Baseline.** Fix the 4 old failing tests first (T0.1)?
10. **Releases.** Every push to `main` publishes a release. I will work on branch `feature/build`, then push to `main` once at the end (or once per phase, if you prefer).
11. **Live test budget.** How many Claude CLI requests and how much API spending may the end-to-end live checks use?

## Decisions (approved 2026-10-28)

1. Planner: Claude through the Claude Code CLI (Pro plan); ChatGPT through Codex as fallback. The Anthropic API adapter is built but **off** until separate billing is authorised.
2. No API keys supplied yet. Default caps per provider: US$1/day, US$10/month. Every live test needs approval first.
3. Ollama returns as an optional local provider. It never downloads models. Enoy's standing rule still applies: Ollama memory use must stay under 2,000 MiB (Chill 1,200, Middle 1,600, Full 1,900 MiB). The memory guard is decided in T10.1.
4. `planning.md` sits in the project root and is not committed automatically. The archive goes in Enoy's data folder.
5. For a folder without git, Enoy offers `git init` and runs it only after the user agrees.
6. Fixed checks always run. The default reviewer model is ChatGPT through Codex.
7. Unattended runs may only run allowlisted commands and the plan's approved verification commands. Any other command stops the task.
8. The existing chat and team workflow is unchanged. Build is a separate workspace.
9. Fix the 4 tests that were already failing first (T0.1).
10. Work on `feature/build`. Push to `main` once at the end.
11. Live-test budget: ask before each live request.
