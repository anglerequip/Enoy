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

**Memory mode** caps the RAM the local model may use, so Windows stays responsive. The
numbers are MiB (1 MiB = 1,048,576 bytes):

| Mode | Limit | Requests start below | Request stopped at | Context up to | Answers up to | Prompt batch |
|---|---|---|---|---|---|---|
| **Chill** (default) | 1,200 MiB | 1,080 MiB | 1,152 MiB | 4,096 tokens | 2,048 tokens | 256 |
| **Middle** | 1,600 MiB | 1,440 MiB | 1,536 MiB | 8,192 tokens | 4,096 tokens | 256 |
| **Full** | 1,900 MiB | 1,710 MiB | 1,824 MiB | 16,384 tokens | your setting | Ollama's default |

No mode can allow 2,000 MiB or more; the app refuses to start if the code is ever changed
to do so. The "start below" point is 10 % under the limit (`memory.margin_percent`); the
stop point is 4 % under it, to leave room for the reaction time. A mode only ever lowers
your own settings.

**What is measured.** The limit applies to the *private working set*, the "Memory" column
in Task Manager, of Ollama's server plus its model runner (`llama-server.exe`). Each
process is counted once. Shown next to it but not counted: private commit, total working
set (which includes shared pages of the model file), GPU memory (reported by Ollama) and
Enoy's own memory.

**Measured on this PC** (Qwen 3.5 9B, Q4_K_M, RTX 3070, prompt filling most of the context):

| Context | Private working set | With prompt batch 256 |
|---|---|---|
| 2,048 | 1,029 MiB | |
| 4,096 | 1,082 MiB | 1,055 MiB |
| 8,192 | 1,292 MiB | 1,199 MiB |
| 16,384 | 1,521 MiB | |

From 8k on, part of the model no longer fits on the GPU and moves to RAM. So Qwen runs in
all three modes: Chill at up to 4k, Middle at 8k, Full at 16k. One earlier run at 16k
reached 2,018 MiB; Full guards against that with the measures below. These are
measurements, not guarantees: other programs using the GPU can push more of the model
into RAM.

In the Memory tab each mode card says whether the configured model fits and at which
context, and whether that number was measured or estimated. *Measured model sizes* lists
the readings, with **Forget measurements** to start over (for example after closing a game
that was using the GPU).

**How it is enforced:**

1. **Before a request.** The context, answer length and batch size are capped for the mode.
   Other models Ollama has loaded are unloaded first, so only one model holds memory. The
   request's memory is estimated from earlier measurements (or the model file size) and a
   smaller context is used if needed. If nothing fits, if Ollama's memory cannot be
   measured, or if Windows has less than 512 MiB to spare, the request is refused with an
   explanation: the model, its quantization, and what to change (another mode, a smaller
   context, a smaller model or quantization). It is never sent to Claude instead.
2. **Windows hard cap** (on by default; switch: *Windows RAM cap on Qwen*). Ollama's model
   runner gets a Windows hard working-set maximum: the stop point minus the server's own
   memory. Windows keeps the runner's RAM under it and moves the rest to the page file, so
   Qwen keeps working but may slow down. Tested: a 16k context that uses 1,521 MiB
   uncapped stayed at 1,103 MiB under an 1,100 MiB cap and answered normally. If the
   runner sits at the cap, the next request uses a smaller context.
3. **While Qwen works**, memory is checked four times a second. At the stop point the
   request is stopped and the model unloaded. If memory stays high for 8 seconds, Ollama's
   runner process is ended as a last resort. Nothing else is ever touched. This cannot be
   switched off.

**Hard or best-effort?** The Windows cap is enforced by Windows, but only on the runner's
RAM: it starts up to a quarter second after the runner does, does not limit commit or page
file use, and does not cover Ollama's server process (a few MiB, monitored). A Windows
job-object limit was not used: it caps commit, and the runner commits about 8,300 MiB
while holding about 1,000 MiB in RAM, so it would fail at load. Every action is logged in
Activity.

Ollama's server-wide settings (`OLLAMA_NUM_PARALLEL`, `OLLAMA_MAX_LOADED_MODELS`,
`OLLAMA_KV_CACHE_TYPE`) are not set on this PC and Enoy does not change them. Enoy sends
one request at a time and unloads extra models itself.

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

## Claude leads (on by default)

When Claude is available, it reads each new message first and decides what happens.
Its reply starts with one of three modes:

- **answer:** Claude answers directly (questions, explanations, short replies).
- **clarify:** the request is genuinely ambiguous, so Claude asks one short question.
- **delegate:** Claude writes a plan of small, bounded tasks. Each task names the
  files, why it exists, what output is expected, constraints, and which model should
  do it (usually Qwen).

A delegated plan runs on the team:

1. **Qwen** writes the code locally, task by task.
2. **QA** runs the project's real tests. A task only counts as done when the checks
   pass; a failure is recorded as a failure.
3. The **reviewer** checks the diff.
4. **Claude** does a final review of the results and replies `done` or asks for one
   fix round (at most *Fix rounds Claude may order*, default 1). After that the run ends and the
   result is reported as it is.

Settings → *Claude leads*:

| Setting | Default | What it does |
|---|---|---|
| Claude leads new messages | on | Off: the router sends each message to Qwen or Claude as before. |
| Claude checks the team's results | on | One more Claude request per delegated task. Off: Enoy's own summary is the answer. |
| Fix rounds Claude may order | 1 | How many fix rounds Claude may ask for after its check. |

Things to know:

- **Ask before sending still applies.** Every Claude request (the first read and the
  final review) waits for *Send to Claude* when that safety setting is on.
- Picking a model by hand, a chat kept on your PC, or *zero paid mode* skips the
  lead. Qwen then works as described under routing above.
- If Claude is unavailable, Enoy follows *If Claude is needed but unavailable* in Settings:
  **local** lets Qwen continue alone and says so; **stop** stops and tells you why.
  Enoy never shows a Qwen answer as Claude's.
- **Your uncommitted work is protected.** If the project is a git repository, Enoy
  notes which files you had changed before the run. A proposed change to one of those
  files waits for your review instead of being applied automatically.
- **Activity** shows the lead's state, each task's spec and verification result, and
  buttons to *Pause delegation*, *Retry* or *Cancel* a task, or move it to the other
  model.
- Plans, checkpoints and test results go into the chat's shared memory. Items that
  were not verified are marked as suggestions, so the next model does not treat them
  as facts.
- Enoy has no GitHub sync; it only reads `git status`, never commits or pushes.
- Tested with the real Qwen model and a scripted stand-in for Claude. **No real Claude
  request has been sent through this mode yet**, so treat the first real run as a test.

## Qwen as an agent (on by default)

Talk to Qwen the way you would talk to Claude Code. In a project folder Qwen works in
a loop with tools until the job is done: `list_files`, `read_file`, `search_code`,
`edit_file`, `write_file`, `run_command` (tests and other allowlisted commands), plus
the PC, weather and web tools below. It reads before it edits, runs the tests after,
and fixes what fails.

- Edits are written at once with a backup; each message's edits are one change set
  that **Undo** restores. Turn *Agent edits are applied right away* off to get one
  proposal to apply or discard instead.
- Guardrails that stay on: only files inside the project folder; secrets files (.env,
  keys) are never read or written; edits that would break the syntax are refused;
  commands outside the allowlist are handed to you to approve, blocked ones never run.
- Settings → Tools: agent on/off, apply right away, steps per message (default 14).
- In **Auto** mode Claude still leads real coding work; pick **Qwen** in the model
  menu to work with Qwen directly. Small talk always goes straight to Qwen.
- Qwen 3.5 9B is a small model: tested live, it added functions with tests, ran pytest
  and reported the real result, but expect more slips than with Claude.

## Tools: your PC, weather and the web

Models can ask Enoy to run tools. Enoy checks the permission, runs the tool and gives
the real result back, errors included. Each call is listed in **Activity**, and answers
end with the sources Enoy actually fetched (or "Read from Windows by Enoy").

| Tool | What it does | Needs |
|---|---|---|
| `get_hardware_inventory`, `get_cpu_info`, `get_gpu_info`, `get_memory_info`, `get_storage_info`, `get_motherboard_info`, `get_windows_info`, `get_system_summary` | Reads this PC from Windows (CIM/WMI, registry, nvidia-smi). Read-only, no administrator rights. | The "hardware" switch in Memory & PC |
| `check_local_model_fit` | RAM/VRAM free and total, the configured Qwen model and its size, Enoy's memory mode | same |
| `get_weather` | SMHI open data (Nordic countries, plus the nearest station's latest reading), MET Norway elsewhere. Place names via OpenStreetMap Nominatim. | Online tools on |
| `web_search` | Brave Search (your key), your SearXNG, or Wikipedia (background only, not news) | Online tools on |
| `fetch_page` | Reads a public web page as text. Local and private addresses (localhost, 192.168.x.x) are refused. | Online tools on |

How each model uses them:

- **Qwen** gets only the tools that fit the message (hardware questions get the
  hardware tools, live questions the online ones) through Ollama's native tool
  calling, and may call them up to *Tool rounds per answer* times (default 5).
- **Claude** gets Claude Code's own **WebSearch** and **WebFetch** for questions about
  current information, and nothing else on your PC. For questions about this PC, Enoy
  reads the hardware first and puts the facts in Claude's request. Letting Claude call
  Enoy's tools itself (`tools.claude_enoy_mcp`, off by default) runs Claude Code without
  `--safe-mode`, because safe mode also blocks Enoy's tool server.
- When Qwen's search can only reach Wikipedia, questions that need web research
  (news, releases, documentation) go to Claude. Weather stays with Qwen.

Settings → **Tools**: turn tools or single tools off, choose the search provider, save a
Brave key (stored in Windows Credential Manager, never in Enoy's files), set a SearXNG
address. Nothing a tool returns is treated as an instruction.

Tested for real: all hardware questions and the Trollhättan weather with real Qwen;
one real Claude request used WebFetch for live weather.

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
  change your files. For live questions it gets only WebSearch and WebFetch. Enoy
  never touches Claude's tokens or cookies and does not use paid API keys.

## The window

In the desktop app the Windows title bar is replaced by Enoy's own header. The window
is still a normal Windows window: drag it by the header, double-click the header to
maximize, right-click it for the system menu, snap it to screen edges, resize it from
any edge, and use Win+arrow keys, Alt+Space and Alt+F4. Minimize, maximize and close
sit at the top right. Set `ENOY_NATIVE_FRAME=1` to get the standard title bar back.
Hovering the maximize button does not show the Windows 11 snap layouts menu (Win+Z
still does).

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
