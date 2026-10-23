# Enoy: application audit (2026-10-23)

Written after building the image, Projects/Artifacts, redesign, rendering, memory and PC
access features. It is based on the code (13,262 lines across 31 modules, 18 test files)
and on what was measured on this PC, not on guesses. Items are ordered by how much they
matter.

## Summary

Enoy works end to end with Qwen on this PC:

- chat, images, projects, artifacts, routing, the activity panel, memory limits and
  read-only PC access;
- 315 unit tests and 2 real-Ollama tests pass.

The three biggest risks are not code quality but process and verification:

1. ~~there is **no version control**~~ (fixed: Git repository with history since 2026-10-23);
2. **none of the Claude paths added on 2026-10-23 has run against real Claude**;
3. the **default memory mode blocks the default model**.

## Critical

1. **Fixed: no Git repository.** The project is now under Git. Before that, the project folder is not under version control. Every change
   made in this session (thousands of lines) exists only on disk, with no history and no
   way to roll back one feature. *Fix:* `git init`, commit the current state, and commit
   per feature from now on. This is the single most valuable thing to do next.
2. **Unverified Claude paths.** These have only run against a fake Claude Code CLI:
   - images (stream-json input);
   - Opus 5.5 at high effort;
   - project instructions and knowledge in Claude prompts;
   - team runs with Claude.

   The CLI argument combination was checked locally, but a format mistake would only show
   up on your first real request. *Fix:* one approved test request per path, sent from the
   approval card so you see what is sent.
3. **The default memory mode blocks the default model.** Chill (700 MiB) is the default
   you asked for, but Qwen 3.5 9B needs at least about 1,075 MiB. Until you choose Medium
   or Full, every local answer is blocked (with a clear explanation and a settings button).
   *Fix options:*
   - a first-run choice that shows the measured need;
   - or a smaller default model for Chill, for example a 1.5–3B model. That needs a
     download, which only you can approve.

## High

4. **The memory supervisor acts on the shared Ollama.** It measures and unloads Ollama's
   runner, which every program using Ollama shares. During testing it stopped another
   client's request; that was the dev preview stopping the integration test. *Fixed:* the
   Memory tab now says so. Optionally, only enforce
   while Enoy itself has a request running or has loaded the model.
5. **Not a hard cap.** Windows cannot cap a process Ollama starts without making it crash.
   The supervisor reacts within about one second, so a fast spike can briefly exceed the
   limit. A stronger option exists: Enoy could run its own `ollama serve` on a private
   port, inside a job object with a memory limit, with `OLLAMA_MAX_LOADED_MODELS=1` and
   `OLLAMA_NUM_PARALLEL=1`. It needs testing to see how llama-server behaves when it hits
   the cap.
6. **`orchestrator.py` is 1,971 lines** and owns routing, Claude approval, escalation, team
   hand-off, projects, PC requests and memory hooks. Each new feature lands there, which
   makes changes risky. *Fix:* split it into focused services (Claude gate, project
   service, PC handler, local runner), each with its own tests. The existing tests are
   good enough to make that refactor safe.
7. **No linting, type checking or JS tests in CI.** `ruff`, `mypy` and `eslint` are not set
   up, and only the Markdown renderer has JS tests (Node). The UI was verified by scripted
   checks in a browser preview, not by repeatable end-to-end tests. *Fix:* add ruff and
   mypy to the dev venv and `run-tests.cmd`. Add a few Playwright smoke tests (send,
   stream, approve, artifact save) if you accept the extra dependency.

## Medium

8. **Claude answers do not stream.** Opus at high effort can think for a long time with
   only a spinner. Claude Code supports `--include-partial-messages` with stream-json
   output, which would give live text. That changes the verified Claude path, so it needs
   one approved real test.
9. **Images skip the secret check.** Text is redacted before it goes to Claude; images
   (for example a screenshot of an API key) are not, and the approval card only says so.
   *Fix:* optional local OCR (Windows has a built-in OCR API) to warn about visible keys
   before sending.
10. **Fixed: PC request results stay in the chat history.** A window list can contain private
    titles, such as a Discord channel name. If you later continue that chat with Claude,
    those earlier messages can be part of the conversation sent. *Checked:* Claude's own requests never contained them; the one path
    that could carry one was the brief for *Continue in Claude Code*, which now leaves PC
    results out.
11. **Project knowledge is keyword-only and text-only.** No PDF or Word, and no semantic
    search. It works well for exact terms (it found "smoked paprika" in the test) but
    misses paraphrases. *Fix:* local embeddings through Ollama (`nomic-embed-text`, about
    270 MB, which you would need to approve), stored in SQLite.
12. **Startup of very long chats.** A 300-message chat takes about 86 ms to open the first
    time (richer Markdown and highlighting). Re-renders are fast (3 ms). Above about 1,000
    messages, load older messages on scroll instead of all at once.
13. **Fixed: Ollama keep-alive.** `keep_alive = 15m` keeps about 1.1–1.5 GB of RAM and 5+ GB of
    VRAM in use after each answer. That is fine in Full mode, but it fights the point of
    Medium and Chill. Now the model is unloaded 1 minute (Chill) or 5 minutes (Medium)
    after an answer; Full keeps the configured value.
14. **CSS has two layers.** The redesign was added as a second layer over the original
    rules to avoid breaking screens. *Fix:* merge the two into one token-based sheet.

## Low

15. **The logo is a hand-traced approximation.** The original image file was not available
    on disk. Replace `fusion_ai/ui/logo.svg` with the original artwork and run
    `launch\make_icon.py`.
16. **"Authorized automation" equals "Assist".** Clicking, typing, form filling and logins
    are not built (honestly marked as unavailable). If you want them, a reviewed design
    comes first: UI Automation through `pywinauto`, per-app scopes, and a confirmation card
    for consequential steps.
17. **Images can grow without limit.** They are stored by hash in
    `%LOCALAPPDATA%\FusionAI\images`, and there is no cleanup for images whose chats are
    gone. *Fix:* a periodic sweep of unreferenced files.
18. **Internal names still say Fusion.** The package, data folder and token header still do,
    which only matters to developers. Renaming the data folder would need a migration.

## What is in good shape

- **Safety model.** Every file change needs your approval, with backup and undo. Claude
  requests are previewed exactly and need your OK. Local-only works at three levels.
  Secrets are redacted. PC actions are checked in the backend before each step and logged.
- **Honesty.** Statuses come from real checks, not hard-coded values. Unavailable features
  say so.
- **Tests.** 315 fast unit tests cover the routing, privacy, projects, artifacts, memory
  and PC rules, including several real bugs they caught this session:
  - project folders colliding;
  - a list-merging Markdown bug;
  - a stray `ollama.exe` being counted;
  - a memory stop reported as a user cancel.
- **Rendering.** Streaming updates take under 1 ms each. Finished messages are not rebuilt,
  and the streamed and final HTML are identical.

## Suggested order

1. `git init` and a first commit.
2. Choose a memory mode, or approve a small model for Chill.
3. Approve one real Claude test per new path (images, projects, Opus/high).
4. Add ruff and mypy, then split `orchestrator.py`.
5. Claude streaming, image secret warning, and history exclusion for PC results.

## Update (later on 2026-10-23)

- **Fixed:**
  - Sending in Code mode with no folder open did nothing.
  - The taskbar showed Python instead of Enoy.
  - Chats could not be renamed or deleted.
  - Stopping an answer threw away the text written so far.
- **New Memory tab statuses:** *Model loading blocked* and *Hard cap unavailable*.
- **Measured:** Qwen 3.5 9B at a 16k context reached 2,018 MiB in Full mode, more than the
  1,515 MiB measured earlier. Medium mode handles this by lowering the context, and the
  supervisor unloaded it when the preview was switched to Medium.
- **Still open, not built:**
  - Clicking and typing in other apps.
  - Browser form filling and logins.
  - Screenshots.

  These are shown as unavailable in the app. They need a reviewed design and a new
  dependency (UI Automation through `pywinauto`, or a browser driver), so they were not
  added without your decision.

## Update: memory limits 1,200 / 1,600 / 1,900 MiB (2026-10-23)

- **Limits:** Chill 1,200 MiB, Middle (shown as Medium before) 1,600 MiB, Full 1,900 MiB.
  No mode may reach 2,000 MiB; the app refuses to start if one does. Items 3 and 5 above
  are superseded: Qwen 3.5 9B now fits all three modes, and there is a Windows cap.
- **New:** per-mode context, answer-length and batch caps; one model in memory at a time;
  free-RAM and measurement checks before loading; a Windows hard working-set cap on
  Ollama's runner; a stop point 4 % under each limit; checks four times a second while
  Qwen works; the old *enforce* setting no longer switches enforcement off.
- **Still not covered:** the Windows cap starts up to a quarter second after the runner,
  covers RAM only (not commit or the page file) and not Ollama's server process. Two
  runners are each capped and the extra one is unloaded, so for a moment their sum can
  pass the limit. Ollama's server-wide settings are not changed.
