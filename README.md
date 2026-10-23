# Enoy

*(formerly Fusion AI Workspace. Internal names such as the `fusion_ai` package and the `%LOCALAPPDATA%\FusionAI` data folder are unchanged, so existing chats and settings carry over.)*

A Windows desktop workspace for AI-assisted development. **Qwen 3.5 9B runs locally
through Ollama and handles routine work by default.** Claude, through your existing
Claude Code sign-in, is an optional specialist for hard problems. You approve every
file change, and you see everything that is sent to Claude before it goes.

## Start it

1. Double-click **`Setup Enoy.cmd`** (once). It creates a private Python
   environment in `.venv`, installs the pinned packages, and puts a
   **Enoy** shortcut on your Desktop and in the Start menu.
2. Make sure **Ollama** is running (it normally starts with Windows).
3. Open **Enoy** from the Desktop or Start menu.

Requirements: Windows 10/11, Python 3.11+, Ollama with `qwen3.5:9b`. Claude Code is
optional. Setup never downloads models; it tells you the command if one is missing.

## Daily use

It works like any AI chat app: type a message and press **Enter** (Shift+Enter for a
new line). Each answer shows who wrote it: **Qwen** (on your PC) or **Claude**.

- **Left sidebar**: the **Chat / Code** switch at the top, *New chat*, and your chats
  by date. *Chat* is for plain questions with no folder. *Code* works on a project
  folder; the folder switcher (with *Open folder…*) appears there.
- **Images**: attach up to 5 per message with **＋ → Add images**, by pasting, or by
  dropping them on the chat. Qwen 3.5 reads them on your PC. If Claude is asked, the
  approval card shows the images too; images go to Claude only after your OK and are
  never sent from a local-only chat. Pictures are stored once in
  `%LOCALAPPDATA%\FusionAI\images`, converted to PNG/JPEG and shrunk to 1568 px if
  larger. TEAM ROLES works from text only, so messages with images are answered directly.
- **Model menu** (in the message box, next to Send): *Auto* (Qwen first, Claude when
  needed), *Qwen* or *Claude*. Your pick always wins.
- **Code mode** shows the folder in the message box too. If no folder is open yet, type
  your message and press Send: Enoy asks for the folder, then sends it there.
- **Stop** (the square button while an answer is being written) keeps the text written so
  far, marked as stopped.
- **Inside the chat**: proposed code changes appear as a card with *Apply changes* /
  *Discard* and a diff you can expand; after applying you get *Run tests* and
  *Undo*. If Claude is needed, a card asks first and shows what would be sent.
  A failed test gets a *Fix it* button.
- **＋ button**: add a project file to the chat, run tests or a command, or summarize
  the project.
- **Files** (top right): browse and search the project, preview a file, *Add to chat*.
- **Chat title** (top left, in a chat) opens the chat menu: rename, move to a project,
  activity log, copy a summary, continue in Claude Code, delete. Rename and delete are
  also in the **⋯** button that appears when you point at a chat in the sidebar.
  Deleting a chat removes its messages and log; files on disk, applied changes and
  their backups, saved artifacts and the record of what was sent to Claude stay.
- **Settings** (bottom left): theme, who answers, models, safety rules, status and
  help (hardware, GPU share, logs), and everything that was sent to Claude.
- **Activity** (top right, or Ctrl+Shift+A): a side panel next to the chat that shows
  what Qwen and Claude are doing right now, why each one was picked, the task's
  workflow stage by stage, what waits for you, and a filterable timeline. From there
  you can review and apply changes, cancel, retry, view a task's log, and pause new
  tasks. It reads saved task state only, so it never sends a model request and keeps
  working when both models are offline. *Explain workflow* is the one exception you
  trigger yourself: one short local Qwen request that summarizes the step log.

## Projects and Artifacts

**Projects** (sidebar → *Projects*) keep related chats, instructions, documents and artifacts
together. There are two kinds:

- **Chat project**: no folder. Create it with *New project*. Its chats appear in Chat mode
  with the project shown at the top of the sidebar.
- **Code folder**: create it with *Open folder…*. Existing folders appear here automatically.

A project page has:

- **Instructions**, saved with *Save instructions*. They are added once to the system prompt
  of every chat in that project only, for Qwen and for Claude. Your message in a chat wins
  when it asks for something different.
- **Documents.** Text formats only: Markdown, text, CSV, JSON, YAML, HTML and source code,
  up to 1 MB each and 100 per project. PDF and Word are not supported. For each question a
  keyword search picks at most 6 matching passages, about 6,000 characters, with their file
  names. Secrets are redacted. A **Private** document is used with Qwen but never sent to
  Claude. Files matching your cloud exclusions are treated the same way. A local-only
  project or chat sends nothing to Claude.
- **Chats** and **Artifacts** for the project.
- **Deleting** works differently for each kind:
  - A chat project: its chats move to plain chats and its documents are deleted.
  - A code folder: it is only removed from the list. Nothing on disk is touched, and
    reopening the folder brings back its chats.
  - Either way, artifacts stay in the library without the project link.
- **Move to project…** (chat ⋯ menu) moves a chat between plain chats and chat projects,
  with its full history.

**Artifacts** (sidebar → *Artifacts*) are saved work products: Markdown documents, text,
code, HTML, SVG, JSON and CSV. You can create them in three ways:

- *Save as artifact* under an answer.
- *Save* on a code block.
- *New artifact*.

Saving copies the text exactly and never calls a model. An artifact opens in a panel next
to the chat:

- **Preview / Edit.** *Save* and *Cancel*, with an unsaved-changes marker. Ctrl+S saves.
- **Copy** and **Export to a file.** The file gets the right extension: `.md`, `.txt`,
  `.py`/`.lua`/…, `.html`, `.svg`, `.json` (only if it is valid JSON), or `.csv`.
- **Version history.** Every save is a version, and *Restore* adds the old one as a new
  version.
- **Project link** and **Delete**, which asks for confirmation first.

A save based on an outdated version is refused rather than overwriting newer work. HTML is
previewed in a sandboxed frame with scripts and forms switched off. SVG is shown as an
image, so scripts inside it do not run. Export the file to run it in a browser. Artifacts
stay when the chat they came from is gone.

## Memory modes and PC access (Settings → Memory & PC)

**Memory mode** caps the RAM the local model may use, so Windows stays responsive:

- **Chill**: 700 MiB, the default.
- **Medium**: 1,500 MiB.
- **Full**: 2,800 MiB.

The number is the *private working set*, the "Memory" column in Task Manager, of Ollama's
server plus its model runner (`llama-server.exe`). GPU memory is shown separately and not
counted. Measured on this PC, Qwen 3.5 9B needs about **1,075 MiB at a 4k context and
1,515 MiB at 16k**, so:

- In **Chill** it is blocked, with an explanation.
- In **Medium** Enoy lowers the context (the chat says so).
- In **Full** it runs as configured.

How it is enforced:

- Every local request is checked first. Requests that cannot fit are blocked or given a
  smaller context; the limit is never raised.
- While the model is loaded, its memory is checked every second.
- Above the ceiling the request is stopped and the model is unloaded through Ollama's API.
  If memory stays high, Ollama's runner process is ended as a last resort. Nothing else is
  ever touched.

This is a supervisor, **not a hard cap**. Ollama starts its runner itself, so Windows cannot
cap it without crashing it, and a fast spike can exceed the limit for about a second.
Blocked requests never move to Claude automatically.

**PC access** works from a chat:

- **Observe** (default): read hardware, running programs and open windows. For example
  "what GPU do I have?", "which windows are open?" or "what is using my RAM?".
- **Assist**, which you must confirm: also opens http(s) pages in your browser, starts
  programs on your approved list, and focuses, minimizes, maximizes or restores windows.
  For example "open youtube.com" or "minimize spotify".
- **Authorized automation**: currently the same actions as Assist.

Clicking and typing in other apps, form filling, logins, screenshots and system settings
are **not implemented**, and they are shown as unavailable, never faked. Every action and
refusal is logged in Activity. Hardware answers come from Windows itself (registry,
GlobalMemoryStatusEx, nvidia-smi, disk usage) and are read-only.

## How routing works (short version)

- Default mode **Smart routing** (`auto`). Before any model starts, Enoy
  classifies the request by what it asks: an explanation, prose, a small function,
  a Luau script, a bug fix, a multi-file feature, architecture, a concurrency or
  security problem. It also estimates how far the change reaches and how costly a
  mistake would be. These are fixed rules; no model is asked.
  - **Qwen** gets clear routine work.
  - **Claude** gets work that clearly benefits from stronger reasoning, straight away.
  - **Borderline tasks** stay with Qwen when a mistake is cheap and easy to verify.
- After Qwen starts, real outcomes decide escalation:
  - its proposal fails Enoy's checks twice;
  - the same error comes back after a fix;
  - checks keep failing;
  - the change turns out larger than expected and an attempt fails.

  Claude receives a structured handoff summary, not the whole conversation. Model
  switches per chat are capped, so the two models never hand work back and forth
  without end.
- The target is 70% Qwen / 30% Claude over recent finished tasks. It is only a
  comparison point, shown in Activity and Settings with an explanation. It never
  forces a model.
- Before any Claude request, Enoy shows the file list and size and waits for
  *Send to Claude* (you can preview the exact text, or keep it with Qwen). If you
  keep it with Qwen, or Claude fails, Qwen takes the task over once.
- **Privacy.** Claude only gets the few relevant files, never the whole project.
  - Files listed under *Settings → Safety → What may go to Claude* are never sent,
    and neither are lines that quote them.
  - Secrets are replaced with `[REDACTED]` before the request is built.
  - To keep everything on your PC, use *Keep this project on my PC* in the project
    menu or *Keep this chat on my PC* in the ＋ menu. The 🔒 note under the message
    box shows when this applies.
- Other workflows: *local_only*, *local_then_review* (Claude reviews Qwen's diff),
  *claude_plans* (Claude plans, Qwen implements), *compare* (both propose).

## TEAM ROLES (per chat)

The **Team** button in the message box (Code mode) switches a chat between:

- **OFF:** one model answers directly. This is the default, and it's the lightest
  option for questions and small edits.
- **ON:** code changes are handled by a small team.

**Roles**

- **Orchestrator.** Plain code, no model. It plans and tracks the work.
- **Architect.** Added only for features, multi-module work or high complexity.
- **Developers.** They write the code. Optional specialists (UI, database, API,
  Luau, docs, performance, accessibility) are added only when the request or plan
  mentions that area.
- **QA.** Runs the project's real, allowlisted checks.
- **Reviewer.** Automatic checks for secrets, unsafe calls and unfinished code; a
  model review for larger or risky changes.
- **Debugger.** Steps in after repeated failures.

**How a run works**

- Each piece of work goes to Qwen or Claude through the smart router.
- If Settings ask before using Claude, the run asks once, for that run only.
- Tasks that touch the same file never run at the same time.
- File changes follow your approval setting.
- A run is "done" only after its checks ran. A run without working checks says
  "not verified by tests".
- Questions are always answered directly, even with TEAM ROLES on.
- Switching TEAM ROLES off pauses a run safely; switching it on continues it.

Limits (tasks, parallel work, repair rounds, model and Claude requests per run) and
*Zero-paid mode* are in *Settings → Who answers*. The Activity panel shows the team:
each role's task, model and reason, dependencies, messages between roles, checks
and review findings.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Safety

- File access stays inside the project folder; `.env`, keys and credential files are
  never read or sent; secrets are redacted from prompts, logs and the database.
- Changes are applied only after approval, all-or-nothing, with backups, and only if
  the files have not changed since the proposal.
- Commands run without a shell, after a policy check. Installs, deletions, network
  tools and anything not on the allowlist ask first; admin/system tools are blocked.
  This is not an OS sandbox: an approved command runs with your normal permissions.
- Claude Code is called with no tools, from an empty folder, so it cannot read or
  change your files. Enoy never touches Claude's tokens or cookies and does not
  use paid API keys.

## Development

```bash
launch\dev.cmd
```

```bash
.venv\Scripts\python.exe -m pytest -q
```

```bash
.venv\Scripts\python.exe -m pytest -m integration -s
```

The second command runs the automated tests (all model and Claude calls are faked).
The third runs real tests against your local Ollama. No test calls Claude.

Docs: [Architecture](docs/ARCHITECTURE.md) ·
[Troubleshooting](docs/TROUBLESHOOTING.md) ·
[Backup and recovery](docs/RECOVERY.md) ·
[Verification report](docs/VERIFICATION.md)

A small sample project for trying the app is in `fixtures/sample_project`.
