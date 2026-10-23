# Verification report

Date: 2026-10-22. Machine: Windows 11 Home (build 26200), AMD Ryzen 7 3700X,
15.9 GB RAM, NVIDIA RTX 3070 8 GB (driver 595.95), Python 3.12.10, Ollama 0.40.8
(`qwen3.5:9b`, Q4_K_M, 6.6 GB, reports completion/vision/tools/thinking),
Claude Code 2.1.308 (signed in, Pro plan).

No earlier Enoy prototype was found on this machine (searched Desktop,
Documents, Downloads, OneDrive and C:\ two levels deep), so nothing was replaced.

## 1. Automated tests: 149 passed

`python -m pytest -q -m "not integration"` → **149 passed** (about 22 s).
All model and Claude calls in these tests are fakes; nothing is charged.

| Required area | Tests |
|---|---|
| Configuration loading and validation | `test_config.py` (defaults, bad values, unknown keys, corrupt file left untouched, backup on save, strict validation) |
| Backend selection, local-first routing, manual override | `test_routing.py` |
| Missing Ollama service, missing model, timeouts, malformed/truncated responses, out-of-memory, cancel mid-stream | `test_backends.py` (fake Ollama HTTP server) |
| Claude unavailable, not signed in, usage limit, garbage output, timeout, disabled, not installed, no tools passed, tokens not exposed | `test_backends.py` (fake Claude Code CLI) |
| Escalation after local failure, retry limits, loop prevention, review rounds, plan/compare workflows, confirmation gate, decline, Claude limit handling, resend avoidance | `test_orchestrator.py` |
| Project file retrieval, search, ignored folders | `test_project.py` |
| Path traversal (`..`, absolute, drive, UNC, ADS, device names, junction escape) | `test_project.py` |
| Secret redaction (prompts, stored transmissions, command output, env vars) | `test_redact.py`, `test_commands.py`, `test_orchestrator.py` |
| Patch validation, diff generation, apply/revert, conflict detection, CRLF | `test_patches.py` |
| File modification approval (nothing written before approval), concurrent user edit | `test_orchestrator.py` |
| Test execution reporting (exit codes, output, timeout, cancel), command policy | `test_commands.py`, `test_orchestrator.py` (real pytest run in the fix loop) |
| Task persistence and recovery after restart | `test_orchestrator.py::test_task_state_survives_restart_and_resumes` |
| Application startup, API token and Host checks | `test_server.py` |

Bugs found and fixed while testing: socket timeouts were misreported as "Ollama
unavailable"; on Windows a blocked network read could not be cancelled until the next
chunk arrived (reads now run on a separate thread); declining Claude after a change was
already applied marked the task as failed and left Claude shown as the owner.

## 2. Real local integration tests (Ollama + Qwen): 2 passed

`python -m pytest -m integration -s` → **2 passed** (114 s).

- Qwen answered "READY" in 22.3 s (17.4 s of it loading the model).
- Given "Add a multiply(a, b) function to calculator.py", Qwen produced a valid
  edit on the first attempt (687 prompt tokens, 270 output tokens, 90.8 s). It passed
  validation, was applied through the approval path, and the resulting module
  returned `multiply(6, 7) == 42` with existing functions intact.

## 3. Manual checks performed in the running app

Done in the browser pane (server mode, separate test data folder) and as the real
desktop window:

- Opened a project; explorer hides `node_modules`; `.env` is listed but refused.
- Real Qwen task through the UI: "Add a removeItem(itemId, amount) method to the
  Inventory module…" → Qwen proposed a correct Luau change (302 output tokens, 31 s),
  shown as a diff on Changes → approved → file written with tabs preserved, backup
  saved in `file-backups\task-1\…`.
- Checks tab ran `python -m pytest -q`. It failed truthfully with exit code 1
  ("No module named pytest", because that project had no venv and the system Python
  lacks pytest). Checks now prefer a project's own `.venv`.
- *Ask Claude* prepared a request and showed the gate (4,519 characters, 4 files) and
  the exact preview. **Nothing was sent.** The pending request survived a server
  restart; *Keep it local* then completed the task.
- Agents, History, Settings (save + config backup) and Diagnostics show real data.
- `Setup Enoy.cmd` / `launch\setup.ps1`: ran on this folder (created the Desktop
  and Start menu shortcuts) and from scratch on a copy (new `.venv`, pinned packages).
- The Desktop shortcut started the app in a native window titled
  "Enoy" (pywebview with WebView2), and the home screen rendered with
  live backend status.

## 4. Status of each requirement

| Item | Status |
|---|---|
| Ollama + Qwen working | **Verified** with real requests |
| Desktop app + shortcut | **Verified** (native window opened from the shortcut) |
| Project open, inspect files, propose change, diff, approve/reject, apply, revert | **Verified** (UI with real Qwen; revert/reject in automated tests) |
| Safe test run and reporting | **Verified** |
| Task history survives restart | **Verified** (test + pending Claude request kept across a real restart) |
| Local-first routing, retries, escalation rules | **Verified in automated tests**; escalation with a real Claude call **not** run |
| Missing services and model errors | **Verified in automated tests**; Diagnostics shows the real state |
| Claude connection | **Verified** on 2026-10-22 with one real request, approved by you, through the running app's *Test Claude* endpoint: reply "READY" in 14.5 s. Claude Code reported 2 input and 4 output tokens, plus **46,552 tokens of context written to cache** (see section 5b). Status now shows "Last real request succeeded" |
| Claude review / plan / compare workflows | Implemented, verified with fake Claude only |
| Hand-off to Claude Code (new console with the brief) | Implemented; brief generation tested; opening the console **not** tested |
| Native folder picker (Open folder…) | Implemented with pywebview's dialog; **not** clicked during testing (typed path tested) |
| Edge app-mode and browser fallbacks | Implemented, **not** exercised (pywebview worked) |
| Single-instance message | Implemented, **not** exercised |
| Standalone installer / .exe | **Not built.** The app runs from this folder with its `.venv`; setup creates the shortcuts |

## 5. Performance note (measured)

Qwen generated **3.2–3.7 tokens/s** at 8k and 16k context in a benchmark and about
10 tokens/s in the UI run. Ollama placed 84% of the model on the GPU: other running
programs (Discord, Spotify, Chrome, Steam, Medal's recorder, Edge) used about 1.9 GB
of the 8 GB of VRAM, so the rest ran on the CPU. Lowering the context from 16k to 8k
did not change the GPU share. Closing those programs before a session is the most
effective fix; Diagnostics shows the current GPU share and this advice.

## 5b. Claude request overhead (measured)

The first test request carried about 46.5k tokens of context that Enoy did not
send, although Enoy replaces Claude Code's system prompt and disables all its
tools. The extra context came from your Claude Code setup that loads on every start
(global CLAUDE.md, memory, plugins, skills, MCP server definitions).

Enoy now starts Claude Code with `--safe-mode` by default (Settings > Claude >
*Skip my Claude Code customizations*). A second real test request, approved by you,
measured the difference:

| | Without `--safe-mode` | With `--safe-mode` |
|---|---|---|
| Reply | READY | READY |
| Time | 14.5 s | 5.8 s |
| Input / output tokens | 2 / 4 | 2 / 4 |
| Context written to cache | 46,552 tokens | **850 tokens** |

That is about 98% less overhead per Claude request, and sign-in still works.

## 5c. Chat-style interface (rebuilt 2026-10-22)

The seven-screen layout was replaced by a single chat view (sidebar, model menu,
conversation, composer). Changes, checks and the Claude question are now cards in the
conversation; history is the chat list; diagnostics live in Settings.

- Real Qwen chats run through the new UI: a plain question in *Just chat* mode
  (streamed with Markdown) and a code change in the demo project, shown as a change
  card. The change was applied through the same API call as the *Apply changes*
  button, and the card then showed *Changes applied*, *Run tests* and *Undo*.
- Screens checked in headless Edge screenshots (light and dark) and by running the
  page script against the live server: the empty start screen, three chats, and all
  five Settings tabs render without script errors.
- Not clicked by hand: the buttons inside cards, menus and Settings (the browser pane
  was hidden). Each one calls an API endpoint that the automated tests cover.
- Automated tests: **152 passed** (adds plain-chat mode and attachments on follow-ups).

## 5d. Activity panel (added 2026-10-23)

Automated: `python -m pytest -q -m "not integration"` → **169 passed** (17 new: 16 in
`tests/test_activity.py`, 1 in `tests/test_server.py`). All model calls in them are fakes.

| Required scenario | Test |
|---|---|
| Task handled entirely by Qwen | `test_qwen_only_task_is_recorded_step_by_step` |
| Escalation Qwen → Claude (fake Claude) | `test_escalation_records_reason_and_handoff` |
| Waiting for approval, then completed | `test_waiting_for_approval_then_applied` |
| Failed task | `test_failed_task_shows_error_on_qwen_card` |
| Claude gate waiting / declined | `test_claude_gate_shows_waiting_and_decline_is_recorded` |
| Unavailable backends, both offline | `test_backends_offline_dashboard_still_works`, `test_task_with_unavailable_model_is_not_left_running` |
| Restart and history recovery | `test_restart_keeps_history_and_marks_interrupted`, `test_pause_queues_new_tasks_across_restart` |
| Dashboard updates while a task runs | `test_dashboard_updates_while_a_task_runs` |
| Monitoring makes no model requests (Claude or Qwen) | `test_monitoring_never_calls_a_model`, `test_activity_endpoints` |
| Cancel, checks, explain, workflow order | `test_cancel_reports_what_was_stopped`, `test_check_run_is_a_testing_step`, `test_explain_*`, `test_workflow_order_follows_real_steps` |

Real Qwen integration tests: **2 passed** (17.9 s), run after the change.

By hand in the dev copy of the app (separate test data folder, real Qwen, no Claude
requests): a question (Qwen card switched to *Working* with task, reason and a live
text counter, then back to *Available*; all stages marked); a code change (patch check,
*Your approval* waiting, *Needs you* badge, *Review changes* → *Apply changes* from the
panel); a check run that failed truthfully (`No module named pytest`, the demo project
has no pytest); *Explain workflow* (real local Qwen request, 4.7 s, accurate summary);
*Pause new tasks* (new task stayed queued, Qwen received nothing) and *Resume*; *Cancel*
on a running Qwen request (task shown as cancelled only after it stopped); *View log*;
narrow window (panel covers the width, no sideways scrolling).

Not tested by hand: *Send to Claude…* from the panel (it would use your Claude plan; the
same endpoint is covered by fake-Claude tests), and the panel in the installed desktop
window, which needs a restart of Enoy to load the new backend code.

## 5e. Smart task routing (added 2026-10-23)

Automated: `python -m pytest -q -m "not integration"` → **201 passed**. 32 are new
in `tests/test_smart_routing.py` (fake Qwen and fake Claude only); five existing tests
were updated for the new default mode and wording.

| Required scenario | Test |
|---|---|
| 1 Code explanation → Qwen | `test_simple_explanation_routes_to_qwen` |
| 2 Small isolated coding task → Qwen | `test_small_isolated_coding_tasks_route_to_qwen` |
| 3 Complex multi-file feature → Claude directly | `test_complex_multi_file_feature_routes_directly_to_claude` |
| 4 Cross-module bug → Claude directly | `test_cross_module_bug_routes_directly_to_claude`, `test_short_sentence_can_still_be_complex` |
| 5 Qwen escalates after repeated failure | `test_repeated_failure_escalates_with_handoff_package`, `test_same_error_after_fix_escalates` |
| 6 Passing tests do not escalate | `test_passing_checks_do_not_escalate` |
| 7 / 8 Manual Qwen / Claude override | `test_manual_qwen_overrides_automatic_routing`, `test_manual_claude_overrides_automatic_routing` |
| 9 Local-only project never sends code | `test_local_only_project_never_sends_code_to_claude`, `test_per_chat_local_only_and_global_never` |
| 10 Secrets redacted before the request | `test_secrets_are_redacted_before_the_request_is_built` |
| 11 Excluded files absent from cloud context and handoff | `test_excluded_files_stay_out_of_cloud_context_and_handoff` |
| 12 Provider outage, no false success | `test_provider_outage_*`, `test_claude_failure_mid_task_is_not_success`, `test_claude_failure_falls_back_to_qwen_once` |
| 13 Retry limits stop model switching | `test_model_switch_limit_stops_alternation` |
| 14 Activity events match execution | `test_activity_shows_the_routing_sequence` |
| 15 Statistics from real records, incomplete tasks excluded | `test_distribution_stats_use_real_records` |
| 16 Preferences persist after restart | `test_routing_preferences_persist`, `test_project_local_only_persists` |
| 17 Concurrent tasks cannot overwrite each other | `test_concurrent_proposals_cannot_overwrite_each_other` |

A bug found by these tests and fixed: the "same error again" check compared the last
output line, which for pytest includes the run time and therefore never matched.

By hand in the dev copy (test data folder, real Qwen, **no Claude request sent**):

- **Settings.** Switching the preset to *Smart routing* saved mode `auto`; changing
  *How readily to use Claude* saved and restored; the model-share line showed real
  counts (6 finished tasks, all Qwen); the cloud privacy section rendered.
- **A hard task.** "Players sometimes lose items when two trades happen at once…" was
  classified as concurrency, high complexity, confidence high, and routed straight to
  Claude. Qwen made no attempt. The privacy check logged "2 files allowed, no secrets
  found", and the request waited for approval.
- **Keep with Qwen.** Clicking it handed the task to Qwen with a recorded reason, and
  nothing was sent. Before this fix, declining stopped the task even though the button
  says "Keep with Qwen".
- **Local-only project.** With *Keep this project on my PC* on, the 🔒 note appeared,
  and an architecture request stayed on Qwen. Its reason says Claude would have helped
  but the project is local-only. The task correctly showed "Waiting for Qwen to finish
  another request".

Not verified:

- A real Claude request through the router. It would use your Claude plan; fake-Claude
  tests cover the path.
- The `claude.model` setting. It is still empty, which means Claude Code's default
  model; it was not changed to Opus 5.5 without asking you.

## 5f. TEAM ROLES (added 2026-10-23)

Automated: `python -m pytest -q -m "not integration"` → **232 passed**. 31 are new in
`tests/test_team.py`, all with fake Qwen and fake Claude; the QA role runs the real
pytest on a copy of the sample project.

They cover:

- **Orchestration and planning:** minimal orchestration for simple tasks; questions
  bypass the team; the architect is activated for complex work; dependency order;
  shared files serialized; parallel batches (different models, disjoint files).
- **Plan validation:** cycles, duplicates, unsafe paths, the task limit, an unusable
  plan falling back to one task, specialists only when justified.
- **Verification and repair:** test-based completion with a repair round; the repair
  limit ending in an honest failure; the debugger on Claude after a repeated failure;
  developer escalation after invalid output; leaked credentials blocked by review;
  model review only for larger changes; checks that cannot run reported, not repaired.
- **Approvals:** changes waiting for approval; Claude asked once per run (allow and
  decline).
- **TEAM ROLES switch:** OFF bypasses orchestration; each chat keeps its own setting;
  switching off pauses and on resumes without duplicates.
- **Lifecycle:** cancel closes the graph; an interrupted run resumes after a restart.
- **Privacy and limits:** local-only projects; zero-paid mode (a manual Claude pick
  still works); run budgets; a Qwen outage reported; excluded files never reaching
  Claude.
- **Records:** messages, memory and activity; persistence; the Activity team board
  matching the real run.

Bugs found by these tests and fixed before shipping:

- **Path check:** a plan path such as `../../etc/passwd` passed because `lstrip("./")`
  removed characters, not the `./` prefix.
- **Privacy matcher:** the same mistake dropped the leading dot from `.secrets/...`.
- **Excluded files in team prompts:** the architect's and developers' Claude prompts
  included excluded files' content. They are now built from a cloud-safe copy of the
  context.

By hand in the dev copy (test data, real Qwen, **no Claude request**):

- **The switch:** the TEAM ROLES button turned on for a new chat.
- **A request:** "Add a modulo(a, b) function to calculator.py…" became a one-task
  plan with no architect.
- **The developer:** Qwen's first proposal was rejected with the exact reason, and
  its second was valid.
- **Approval:** the run waited for review; *Review changes → Apply* in the Activity
  panel continued it.
- **QA:** it ran the real check, which could not run (`No module named pytest` in that
  project's `.venv`). This was reported as not verified, with no repair rounds.
- **Review and result:** the deterministic review found nothing, and the run finished
  as done but unverified.
- **Persistence:** the setting stayed per chat after a page reload (chat 10 ON, chat
  8 OFF, new chat OFF).

Not verified:

- A team run with real Claude requests.
- Two real tasks running at the same time (covered by the scheduler test only).
- The architect role with real Qwen. Qwen 9B's JSON plans may often be rejected and
  fall back to a single task.

## 5g. Chat / Code switch and images (added 2026-10-23)

Automated: `tests/test_images.py` has 20 tests. They cover:

- Storage and format detection by content: GIF, SVG and EXE are refused, and oversize uploads are refused.
- Path-trick ids.
- Qwen receiving the picture, including in a follow-up.
- A Qwen model without vision handing the image to Claude.
- A local-only chat never sending an image to Claude.
- The Claude gate listing images before anything is sent.
- TEAM ROLES answering image messages directly.
- The Ollama `images` field.
- Claude Code stream-json input and output with a fake CLI.
- Upload and download through the API, including the token check.

The full suite passed 253 tests.

Live in the dev preview with real Qwen:

- Switching Chat → Code → Chat changed folder, Files and TEAM ROLES visibility as intended.
- A generated WebP was converted to PNG in the page and uploaded.
- Qwen 3.5 9B answered "The image shows a blue background with a yellow circle centered above the white text 'HELLO 42'". That is correct.

Not verified:

- Claude with an image. Claude Code accepts `--input-format stream-json --output-format stream-json --verbose`; this was checked by its own argument validation, with no prompt sent. A real request needs your approval.
- Drag and drop from Explorer into the desktop window. Only paste/attach was exercised in the browser preview.

## 5h. Projects and Artifacts (added 2026-10-23)

Automated: `tests/test_projects_artifacts.py` has 27 tests. They cover:

- Project create, rename, describe and instructions.
- Validation and duplicate names.
- Instructions isolated per project, with plain chats unchanged.
- Folder-project instructions.
- Move, reopen and continue chats.
- Delete semantics for both kinds.
- Document validation: types, size, secrets files, binary and empty files.
- Cross-project file access refused.
- Retrieval relevance and redaction.
- Private documents and cloud exclusions kept from Claude, and a local-only project.
- Artifact versions, stale-save refusal and restore.
- A failed save rolled back.
- Code-block extraction.
- Export names and types, including invalid JSON refused.
- An artifact surviving its chat.
- A full API flow, including that saving from a chat makes no model call.

The full suite passed 280 tests.

Bug found by these tests and fixed: two chat projects created in the same millisecond shared a folder and merged. Folders now carry a random suffix.

Live in the dev preview with real Qwen:

- The New project form was submitted twice; one project was created.
- A Markdown document was uploaded.
- The question "How much smoked paprika goes into the dough?" in that project gave "The dough contains 12 grams of smoked paprika per kilo of flour. PIZZA". The instruction asked to end with PIZZA, and the logged prompt contained both the instructions and the document excerpt.
- *Save as artifact*, then edit and save, produced version 2. A stale save was refused.
- An HTML artifact with a script rendered statically: the script did not run and the parent page was not reachable.
- Invalid JSON export was refused.

Not verified:

- The native *Save as* dialog for export in the desktop window. The dev preview has no window, so it used the browser download path.
- Uploading documents through the desktop window's file picker.

## 5i. Rendering, redesign and the Enoy name (2026-10-23)

**Root causes of the jagged answers**, found in the code and measured before any change:

1. The streaming answer's `innerHTML` was rebuilt from the full text on every frame. Every element was recreated, so text selection and hover were lost, and code blocks and their buttons were rebuilt constantly.
2. Every task event rebuilt the whole thread and re-parsed every message's Markdown. That took 49 ms for 300 messages, a visible stutter, and images reloaded each time.
3. When an answer finished, the streamed text was cleared before the saved message loaded. The typing dots appeared briefly, then the whole thread was rebuilt.
4. The old Markdown parser had no tables, links, blockquotes, rules or real nested lists, so those showed as raw `|`, `[` and `>` characters. An unclosed `**` or backtick showed raw markers that flipped to formatting later.

**Fixes:**

- `ui/markdown.js` is a block-based renderer. Each block (paragraph, list, code, table…) is one element. While streaming, only the block still being written is redrawn; finished blocks keep their DOM nodes.
- A dangling `**`, backtick or half-written link is closed for display only. The stored text is never changed.
- The streaming and final renders use the same code. A test checks the streamed HTML equals the final HTML exactly.
- The thread is patched by key, so unchanged items keep their nodes.
- At the end of a stream, the streamed text stays on screen until the saved message arrives; then both swap in one frame.
- Tokens are applied at most once per frame, with no timers or artificial delay.
- Syntax highlighting covers Python, JS/TS/JSON, Lua/Luau, C-family, shell and SQL. It changes colour only: the copied text equals the source.
- Long code lines scroll inside the block.
- Startup no longer waits for the provider status checks.

**Measured in the dev preview (Edge/Chromium, same machine):**

| | Before | After |
|---|---|---|
| Re-render a 300-message chat after an event | 49.0 ms | 3.1 ms |
| Add one message to a 300-message chat | 49 ms (full rebuild) | 8.9 ms |
| First open of a 300-message chat | 49.0 ms | 85.9 ms (richer Markdown and highlighting; one-off) |
| Streaming update, 3.3k-character answer | rebuilds all nodes | median 0.5 ms, p95 0.9 ms, max 1.3 ms, finished blocks untouched |
| Page start to first full render | not measured | 84–101 ms |

**Tests:**

- `tests/js/markdown.test.js` has 18 checks, run by `tests/test_markdown_js.py` with Node. They cover:
  - escaping and Unicode/emoji;
  - headings, nested lists, list start numbers and a bullet list followed by a numbered one (a bug found and fixed);
  - several code blocks with exact source, and an unclosed fence while streaming;
  - tables with alignment, links (`javascript:` refused) and inline code;
  - quotes and rules, plus dangling markers;
  - chunk sizes 1, 2, 3 and 7 with no lost or duplicated text and an identical final render;
  - Enoy change blocks and highlighting accuracy.
- Live checks:
  - A synthetic stream split mid-word, mid-marker and mid-fence produced identical final HTML.
  - Following works at the bottom, and the view stays put when you scroll up.
  - A real Qwen answer with a heading, nested list, table and code rendered correctly; all 262 streamed characters equal the saved message.
- The full suite passed 281 tests.

**Redesign:**

- Shared tokens: colours, fonts, radii, spacing, motion and z-index.
- SVG icons instead of emoji in the sidebar and composer.
- Refined sidebar, welcome screen, composer and code blocks.
- Focus rings and reduced-motion support.
- `theme-boot.js` applies a saved light or dark theme before first paint. Inline scripts are blocked by the CSP.
- The desktop window background now matches the dark theme.

**Name and logo:**

- The app is called **Enoy** in the window title, UI, prompts, shortcuts and docs.
- The logo is `ui/logo.svg`, a hand-traced vector of the red mark you sent. The original image file was not available on disk, so the shape is a close approximation, not a pixel copy.
- `launch/enoy.ico` is rendered from it by `launch/make_icon.py`, using headless Edge.
- The Desktop and Start menu shortcuts are now "Enoy". The old "Fusion AI Workspace" ones were removed after checking they started this app.

**Not done or not verified:**

- Claude answers still arrive in one piece rather than streaming: Claude Code's JSON mode has no live text. Real streaming would need `--include-partial-messages`, an untested change to the verified Claude path, so it was left out.
- The new look was not checked in the real desktop window, only the browser preview. A restart is needed.
- No profiler trace of frame pacing was recorded, only the timings above.

## 5j. Memory modes, hardware inventory and PC access (2026-10-23)

**Runtime inspected:**

- Ollama 0.40.8 runs as the tray app (`ollama app.exe`), which starts `ollama.exe serve`.
- `ollama serve` starts the model runner `llama-server.exe`.
- Enoy does not start any of them.

**Metric:** private working set, the same number as Task Manager's "Memory" column. It is
read with `GetProcessMemoryInfo` (`PROCESS_MEMORY_COUNTERS_EX2`) and summed over the server
and its runner. Private memory is per process, so nothing is counted twice. Commit and VRAM
are reported separately.

Measured with `qwen3.5:9b`:

| Context | Private working set | Private commit | On GPU |
|---|---|---|---|
| 16,384 tokens | 1,515 MiB | 8,689 MiB | 5,338 of 6,378 MiB |
| 4,096 tokens | 1,075 MiB | 8,257 MiB | all |

**Enforcement:** a best-effort supervisor, not a hard cap. A Windows job-object limit on a
process Ollama starts itself would make its allocations fail mid-request, so none is applied.

**Live checks** in the dev preview against the real Ollama:

- **Startup in Chill:** the model, loaded at 975 MiB, was unloaded through Ollama's API.
- **A chat in Chill** was blocked: "needs about 1,056 MiB … Chill mode allows 700 MiB". There was no retry and no Claude call.
- **In Medium:**
  - The context was reduced from 16,384 to 8,192 tokens, with a note in the chat.
  - Qwen answered with a measured peak of 1,176 MiB. A later load peaked at 1,464 MiB, under the 1,500 MiB ceiling.
  - The mode was then switched to Chill during a running answer. The request stopped at 795 MiB and the model was unloaded within about a second. The chat now reports this as a memory stop, not "Cancelled by you"; this bug was found live and fixed.
- **PC requests:**
  - "Which windows are open?" listed 8 real windows, with no model call.
  - "open youtube.com" in Observe was refused, naming the Assist profile.
  - "open main.py" was treated as a normal chat.
  - "What GPU and how much RAM do I have?" was answered by Qwen from the measured inventory: RTX 3070, 8 GB VRAM, 15.9 GB RAM.

**Automated:** `tests/test_memory_pc.py` has 34 tests. They cover:

- Mode limits and units; Chill as the default and persistence.
- Process selection: Ollama's server and runner only; not other programs, conhost or a stray client.
- Measurement failure; blocking an incompatible model; shrinking the context; measured profiles beating estimates.
- The warning threshold and ceiling: stop the request, then unload, in that order.
- The last resort ending only the runner; enforcement off.
- Status reporting "Hard cap unavailable".
- No Claude escalation after a block; a memory stop not reported as a user cancel.
- Least privilege; unimplemented capabilities refused in every profile.
- Intent detection, including normal coding sentences that must not trigger; URL safety (`javascript:`, `file:` and credentials in the address are refused).
- Profile-gated actions with the browser and programs mocked; approved programs only; window actions only for real windows.
- Hardware facts in the prompt; the read permission switch.
- The real inventory of this PC.
- API confirmation for raising access, with the generic config endpoint closed as a back door; app-path validation; hardware 403.

The full suite passed 315 tests.

**Not verified or not built:**

- Opening a real browser tab, starting a real program, and minimizing or focusing a real window. These were checked only with mocks, to avoid touching your desktop while you were away.
- Clicking and typing in other apps, reading other apps' controls, browser form filling and logins, and screenshots. These are not built and are shown as "Not available".
- The terminate-the-runner last resort was exercised only in tests, not against the real Ollama.

## 6. Needs you

1. **Memory modes** are now Chill 1,200, Middle 1,600 and Full 1,900 MiB, and Qwen 3.5 9B
   runs in all three (see "Memory limits, second round" below). Nothing to choose unless you
   want a larger context than Chill's 4,096 tokens.
2. **Claude has not been tested with the new paths.** That covers images (stream-json input),
   Opus 5.5 at high effort, project instructions and knowledge, and team runs. Each needs one
   real request, which uses your plan, so none was sent. The approval card shows exactly what
   would be sent.
3. **PC access is Observe by default.** Switch to Assist to let Enoy open pages, approved
   programs and windows; add programs under *Approved programs*. Real desktop actions were
   only tested with mocks.
4. **The logo is a hand-traced vector of the image you sent;** the original file was not on
   disk. To use the exact artwork, save it as SVG at `fusion_ai/ui/logo.svg` (or tell Claude
   where the PNG is) and run `launch\make_icon.py`.
5. Luau checking is a bracket-balance check only. Installing `selene` or `stylua`
   (Rokit/Aftman) gives real Luau checks, which Enoy will suggest as checks.

## Memory limits, second round (2026-10-23)

Chill 1,200 MiB, Middle 1,600 MiB, Full 1,900 MiB; none may reach 2,000 MiB. Everything
below ran on this PC with local Qwen 3.5 9B only; no Claude request was sent.

**Measured directly through Ollama** (private working set of Ollama's server and runner,
prompt filling most of the context):

| Context | Prompt batch 512 | Prompt batch 256 | Private commit | On the GPU |
|---|---|---|---|---|
| 2,048 | 1,029 MiB | | 8,267 MiB | all |
| 4,096 | 1,082 MiB | 1,055 MiB | 8,386 MiB | all |
| 8,192 | 1,292 MiB | 1,199 MiB | 8,509 MiB | 5,354 of 6,106 MiB |
| 16,384 | 1,521 MiB | | 8,787 MiB | 5,338 of 6,378 MiB |

**Windows hard cap evaluated.** A hard working-set maximum (SetProcessWorkingSetSizeEx) on
the runner: at 16k with an 1,100 MiB cap the answer completed in 20.2 s (23.7 s uncapped)
and memory stayed at 1,103 MiB; at 4k with a 900 MiB cap, 10.9 s and 899 MiB. Windows
accepted it without administrator rights. A job-object limit was not tried on the runner:
it caps commit, which is about 8,300 MiB.

**In the app (dev preview, Activity log checked):**

- Chill: a chat ran at a 4,096-token context (capped from 16,384, logged), peaked at
  1,004 MiB; the runner was capped at 1,135 MiB.
- Middle: a prompt filling 8k peaked at 1,200 MiB; Ollama reported 5,298 of 6,030 MiB on
  the GPU.
- Switching to Chill with that model still loaded: Windows trimmed it from 1,200 to
  1,095 MiB at once; nothing was unloaded.
- Full: a prompt of about 14,000 tokens at 16k peaked at 1,520 MiB; cap 1,806 MiB.
- The cap switch: off removed the cap from the running runner (Windows reported it as soft),
  on restored it.
- *Forget measurements* cleared three old readings (1,622 MiB at 8k and 1,747 MiB at 16k
  from an earlier, heavier run) that had been holding Middle and Full to smaller contexts.

**Found and fixed during the live check:**

- Old peak readings were kept forever, and small contexts were extrapolated from them, so
  Chill and Middle would have refused Qwen. Now each new load replaces the reading, and a
  smaller context is never estimated above the smallest measured one.
- A runner already over the limit (left loaded by the integration tests) was unloaded on
  the reading taken just before its cap applied. Now Enoy re-measures after setting a cap.
- Ollama's short GPU-check runner exits before it can be capped and was reported as a
  refused cap. Now only runners holding a model are reported.

**Not verified:** the last resort (ending the runner) against the real Ollama; behaviour
when another program fills the GPU and pushes more of the model into RAM.
