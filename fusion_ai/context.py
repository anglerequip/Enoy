"""Context assembly: which files and facts each backend is given.

Everything that leaves Enoy for a model is built here, redacted, and
recorded by the orchestrator. Claude gets a compact brief; files it already
received unchanged in the same Claude session are not sent again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath

from . import knowledge
from .patches import FORMAT_INSTRUCTIONS
from .project import PathError, Workspace
from .redact import redact

LUAU_NOTES = """\
Roblox/Luau notes: use `local` variables, `game:GetService()` for services, and
type annotations where the project already uses them. Server code must not
trust values sent by clients through RemoteEvents/RemoteFunctions; validate
them on the server. Keep ModuleScripts returning a single table."""

BASE_SYSTEM = """\
You are Qwen, the coding assistant inside Enoy, running on the user's PC and
working in their project "{project}".

How to work:
- Act on the request. When the user asks for a change, make it. Do not answer
  with questions or with a description of what you would do.
- Work out what you can from the files shown and the file list. If a detail is
  missing, pick the most likely option, say in one line what you assumed, and
  carry on.
- Ask the user a question only when the request has two very different meanings
  and a wrong guess would waste their time. Then ask one short question.
- Never ask the user to paste files from this project or to tell you its
  language or framework. Look at the file list instead.
- Be accurate and concise. If you are unsure of a fact, say so in a few words
  rather than inventing it."""

READ_INSTRUCTIONS = """\
To see a project file that is not shown, reply with only lines like these and
nothing else:
<<<READ path/to/file.ext>>>
<<<READ path/to/file.ext | text to find>>>
The second form sends only the parts of the file around that text: use it for
long files and for files shown cut off. Enoy sends you what you asked for and you
continue. Ask for at most {n} files, only paths from the file list, and never
for a file that is already shown in full. Never guess what a file contains:
an EDIT block only works when its SEARCH lines are copied from a file you saw."""

NO_MORE_READS = "You cannot ask for more files now. Work with the files shown."

EMPTY_FOLDER = """\
The project folder ({path}) is empty: it has no files. If the user refers to
existing code ("this app", "the header", "my script"), tell them in one or two
sentences that this folder is empty and that they can open the folder that holds
the code with "Open folder…" in Enoy. Then stop. If they ask for something new,
create it."""

CLAUDE_EMPTY_FOLDER = """\
The project folder ({path}) is empty: it has no files. If the user asks to change
existing code (Enoy itself, "this app", "my script"), do not plan or write changes:
say in one or two sentences that this folder is empty and that they can open the
folder that holds the code with "Open folder…" in Enoy. Only create new files here
when the user asks for something new."""

CONVERSATION_NOTE = """\
Conversation so far in this chat (oldest first). These messages were already
handled: do not answer them again unless the latest message asks. Answers from Qwen
may use live data that Enoy fetched with its tools (weather, web pages, this PC);
trust those and never tell the user that live data is unavailable."""

EDIT_SYSTEM = """\
Your job in this step is to make the change. Keep it minimal and consistent with
the existing style. Say in one to three sentences what you changed, then give
the blocks. Only give a FILE block for a file you have seen in full; for a file
marked (truncated), use an EDIT block.

""" + FORMAT_INSTRUCTIONS

MAX_READ_FILES = 4
EXCERPT_LINES = 12  # Lines shown before and after each match in a "path | text" read.
READ_REQUEST = re.compile(r"^[ \t]*<<<READ\s+(.+?)\s*>*[ \t]*$", re.M)  # Qwen sometimes drops a closing ">".
CHARS_PER_TOKEN = 3  # Code runs at a little over 3 characters per token; counting low keeps the prompt inside the window.

CLAUDE_SYSTEM = """\
You are the specialist reviewer and problem-solver in Enoy.
A local model (Qwen 9B via Ollama) handles routine work; you are consulted for
work that needs stronger reasoning. You cannot open files or run commands: you
only see what is in this message. Ask for a specific file if you need it.
Be direct and concise."""

CLAUDE_LEAD_SYSTEM = """\
You lead Enoy, an AI workspace on the user's PC. The user talks to you. Behind
you is a small team: Qwen (a 9B model running locally, good at clear, routine,
precisely specified code changes) and roles that Enoy runs as code: QA runs the
project's real tests, a reviewer checks the diff, a debugger fixes failures.
You cannot open files or run commands: you see only this message. Be direct,
concise and creative where it helps."""

LEAD_INSTRUCTIONS = """\
Decide how to handle the user's latest message; earlier messages in this chat are
already handled, so do not repeat their answers. Start your reply with exactly one line:
MODE: answer    (you answer yourself: questions, explanations, brainstorming, advice, short snippets)
MODE: clarify   (a wrong guess would be costly: ask at most two short questions)
{handoff_mode}MODE: delegate  (files in the project must change) {delegate_note}
Then write your message to the user in Markdown. For delegate, say in one or two
sentences what the team will do, then add the plan as one JSON block:
```json
{{"acceptance": ["checkable criterion", ...],
 "architecture": "short notes: modules, interfaces, risks",
 "tasks": [{{"id": "T1", "title": "...", "description": "the exact change: what, where and how",
            "files": ["path/in/project"], "depends_on": [], "specialty": "",
            "model": "qwen", "why": "why this task and this model",
            "expected_output": "...", "constraints": ["..."]}}]}}
```
Plan rules: 1 to {max_tasks} tasks, as few as keep each one focused. Give every
clear, routine task to Qwen ("model": "qwen") and describe it so precisely that a
small model needs no guessing. Use "model": "claude" only for a part that needs
your reasoning. Tasks that change the same file depend on each other. Name only
files that exist or must be created. Do not delegate questions or brainstorming.{style_note}"""

LEAD_HANDOFF_MODE = "MODE: handoff   (Qwen writes the reply from your instructions)\n"

CLAUDE_FIRST_NOTE = """

How this chat works (Claude first): you talk with the user and decide; Qwen, the
local model, does most of the writing. Aim for Qwen to write at least {share}% of
all text in this chat. Choose:
- answer only for a short reply (a greeting, a quick fact, a decision, under about
  120 words) or when the reply needs your reasoning: subtle bugs, architecture,
  security, tricky maths, judging trade-offs.
- handoff for everything else Qwen can write from clear instructions: code
  snippets, explanations, lists, summaries, drafts, and lookups (Enoy gives Qwen
  tools for the weather, the web, this PC and the project's files). After the MODE
  line write at most one short sentence to the user, then the instructions as one
  block of 3 to 8 short lines (every word you write counts against the user's
  Claude plan; Qwen's words do not):
```qwen
what to produce, its format and length, the key points, one pitfall to avoid.
Do not write the answer itself here.
```
- delegate when project files must change, giving the clear tasks to Qwen.
Qwen's reply goes to the user as it is, labeled Qwen, so put everything it needs in
the instructions; it also sees the recent chat."""

LEAD_NO_DELEGATE = "Not possible in this chat: no project folder is open, so never choose delegate."

LEAD_REVIEW_INSTRUCTIONS = """\
Your team finished the work you planned. Below are the results Enoy recorded:
tasks, changed files with diffs, real test runs and review findings. Check them
against the user's request and the acceptance criteria. Then write your answer to
the user: what was done, what was tested (only what the results show), and anything
that still needs attention. Do not claim checks that did not run.
End with exactly one line:
NEXT: done
or, only if a clear defect remains that the team can fix:
NEXT: fix <one precise instruction for the developer>"""

LEAD_MODE = re.compile(r"^\s*MODE:\s*(answer|clarify|delegate|handoff)\b[^\n]*(\n|$)", re.I)
LEAD_PLAN = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.S)
LEAD_HANDOFF = re.compile(r"```qwen[^\n]*\n(.*?)```", re.S)
LEAD_NEXT = re.compile(r"^\s*NEXT:\s*(done|fix)\b:?\s*(.*)$", re.I | re.M)


def parse_lead(text: str) -> tuple[str, str, str | None]:
    """Claude's lead reply: (mode, message to the user, plan JSON or Qwen's instructions, or None).

    Without a MODE line it is an answer."""
    m = LEAD_MODE.match(text)
    mode = m.group(1).lower() if m else "answer"
    body = text[m.end():] if m else text
    plan = None
    fence = LEAD_PLAN.search(body)
    if mode == "delegate" and fence:
        plan = fence.group(1)
        body = body[:fence.start()] + body[fence.end():]
    if mode == "handoff":  # plan = Qwen's instructions; without the block, the whole reply is the instructions.
        block = LEAD_HANDOFF.search(body)
        if block:
            plan = block.group(1).strip()
            body = body[:block.start()] + body[block.end():]
        else:
            plan, body = body.strip(), ""
    return mode, body.strip(), plan


def parse_lead_review(text: str) -> tuple[str, str, str]:
    """Claude's final check: (message, "done" or "fix", fix instruction)."""
    found = list(LEAD_NEXT.finditer(text))
    if not found:
        return text.strip(), "done", ""
    last = found[-1]
    message = (text[:last.start()] + text[last.end():]).strip()
    return message, last.group(1).lower(), last.group(2).strip()


def memory_block(rows: list[dict], max_chars: int = 3000) -> str:
    """Shared project memory for a prompt, newest first, marked verified (from real results) or not."""
    lines, used = [], 0
    for r in rows:
        tag = "verified" if r.get("verified") else "suggestion, not verified"
        line = f"- [{r['category'].replace('_', ' ')}, {tag}, from {r.get('source') or 'unknown'}] " \
               + " ".join(str(r["content"]).split())[:400]
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return ("Project memory (earlier work in this project; verified items come from real results):\n"
            + "\n".join(lines)) if lines else ""


REVIEW_INSTRUCTIONS = """\
Review the proposed change below against the task. Reply with:
VERDICT: APPROVE or VERDICT: CHANGES_NEEDED
then a short list of concrete problems (bugs, missed requirements, risks), most
important first. Do not restate the diff. If changes are needed, describe
exactly what to change so a smaller model can implement it."""

PLAN_INSTRUCTIONS = """\
Write a short, concrete implementation plan for the task that a smaller model
will follow. Name the files to change and what to change in each, in order.
Point out pitfalls. Do not write the full code."""


@dataclass
class FileContext:
    path: str
    content: str
    sha: str
    truncated: bool


@dataclass
class ContextPack:
    files: list[FileContext] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    instructions: list[tuple[str, str]] = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    history: str = ""
    project_summary: str = ""
    user_instructions: str = ""                   # Written by the user on the project page.
    knowledge: list = field(default_factory=list)  # knowledge.Snippet excerpts for this question.
    system_facts: str = ""                         # Measured hardware facts, when the user asked about their PC.

    def files_text(self, only: set[str] | None = None) -> str:
        parts = []
        for f in self.files:
            if only is not None and f.path not in only:
                continue
            note = " (truncated)" if f.truncated else ""
            parts.append(f"=== {f.path}{note} ===\n{f.content}")
        return "\n\n".join(parts)

    def manifest(self) -> list[dict]:
        return [{"path": f.path, "sha": f.sha[:12], "chars": len(f.content), "truncated": f.truncated} for f in self.files]


def gather(ws: Workspace, objective: str, ctx_settings: dict, extra_paths: list[str] | None = None,
           history: str = "", project_summary: str = "") -> ContextPack:
    pack = ContextPack(history=history, project_summary=project_summary)
    pack.instructions = ws.instructions()
    facts = ws.detect()
    pack.facts = facts
    ranked = [p for p, _ in ws.relevant_files(objective, ctx_settings["max_files"])]
    ordered: list[str] = []
    for p in (extra_paths or []) + ranked:
        if p not in ordered:
            ordered.append(p)
    budget = ctx_settings["max_chars"]
    per_file = ctx_settings["max_file_chars"]
    for rel in ordered[: ctx_settings["max_files"] + len(extra_paths or [])]:
        try:
            text, sha = ws.read_text(rel)
        except (PathError, FileNotFoundError, OSError) as exc:
            pack.skipped.append(f"{rel}: {exc}")
            continue
        truncated = len(text) > per_file
        if truncated:
            text = text[:per_file] + "\n... [rest of file not shown] ..."
        if len(text) > budget:
            pack.skipped.append(f"{rel}: context budget used up")
            continue
        budget -= len(text)
        pack.files.append(FileContext(rel, redact(text), sha, truncated))
    return pack


def prompt_budget(num_ctx: int, num_predict: int | None) -> int:
    """Characters of prompt that fit the local model's context window, leaving room for its answer."""
    reserve = max(512, num_ctx // 3 if num_predict is None else min(num_predict, num_ctx // 3))
    return max(0, num_ctx - reserve) * CHARS_PER_TOKEN


def fit_files(pack: ContextPack, chars: int) -> ContextPack:
    """Copy of the pack with only as much file content as `chars` allows. Files that do not fit are cut
    (the first one that overflows, if enough room is left) or left out and listed as skipped."""
    kept, skipped = [], list(pack.skipped)
    for f in pack.files:
        size = len(f.path) + 20 + len(f.content)
        if size <= chars:
            kept.append(f)
            chars -= size
        elif chars >= 1500:
            cut = f.content[: chars - len(f.path) - 60] + "\n... [rest of file not shown] ..."
            kept.append(replace(f, content=cut, truncated=True))
            chars = 0
        else:
            skipped.append(f"{f.path}: does not fit the local model's context window")
            chars -= len(skipped[-1]) + 2
    return replace(pack, files=kept, skipped=skipped)


def file_list(ws: Workspace, max_chars: int) -> str:
    """The project's files grouped by folder, cut to `max_chars`, so the model knows what exists."""
    groups: dict[str, list[str]] = {}
    for rel in sorted(ws.index):
        p = PurePosixPath(rel)
        groups.setdefault("" if str(p.parent) == "." else str(p.parent) + "/", []).append(p.name)
    total = sum(len(v) for v in groups.values())
    lines, used, shown = [], 0, 0
    for folder, names in groups.items():
        line = folder or "(top level)"
        line += ": "
        taken = []
        for name in names:
            if used + len(line) + len(", ".join(taken + [name])) > max_chars:
                break
            taken.append(name)
        if not taken:
            break
        lines.append(line + ", ".join(taken))
        used += len(lines[-1]) + 1
        shown += len(taken)
        if len(taken) < len(names):
            break
    if shown < total:
        lines.append(f"... and {total - shown} more files")
    return "\n".join(lines)


def read_requests(text: str) -> list[str]:
    """What the model asked to see with <<<READ path>>> or <<<READ path | text>>> lines, in order, without repeats."""
    return list(dict.fromkeys(p.strip().strip("\"'`") for p in READ_REQUEST.findall(text)))


def strip_reads(text: str) -> str:
    return READ_REQUEST.sub("", text).strip()


def excerpt(text: str, needle: str, max_chars: int) -> str | None:
    """The parts of `text` around each line containing `needle` (any case), or None if it does not occur."""
    lines = text.split("\n")
    low = needle.lower()
    spans: list[list[int]] = []
    for i, line in enumerate(lines):
        if low not in line.lower():
            continue
        a, b = max(0, i - EXCERPT_LINES), min(len(lines), i + EXCERPT_LINES + 1)
        if spans and a <= spans[-1][1]:
            spans[-1][1] = b
        else:
            spans.append([a, b])
    if not spans:
        return None
    out = f'[Only the parts around "{needle}" are shown.]'
    for a, b in spans:
        part = f"\n... [lines {a + 1}-{b}] ...\n" + "\n".join(lines[a:b])
        if len(out) + len(part) > max_chars:
            return out + "\n... [more matches not shown] ..."
        out += part
    return out


def add_files(ws: Workspace, pack: ContextPack, requests: list[str], per_file: int) -> tuple[list[str], list[str]]:
    """Read what the model asked for into the front of the pack: whole files, or the parts around a text
    ("path | text"). An excerpt replaces what was shown of that file before. Returns (added, refusals)."""
    added, refused, new = [], [], []
    for req in requests[:MAX_READ_FILES]:
        rel, _, needle = (x.strip() for x in req.partition("|"))
        try:
            text, sha = ws.read_text(rel)
            norm = ws.rel(ws.resolve(rel))
        except FileNotFoundError:
            refused.append(f"{rel}: no such file")
            continue
        except (PathError, OSError) as exc:
            refused.append(f"{rel}: {exc}")
            continue
        if needle:
            part = excerpt(text, needle, per_file)
            if part is None:
                refused.append(f'{norm}: "{needle}" does not occur in it')
                continue
            pack.files[:] = [f for f in pack.files if f.path != norm]
            new = [f for f in new if f.path != norm]
            new.append(FileContext(norm, redact(part), sha, True))
            added.append(f'{norm} (around "{needle}")')
            continue
        if any(f.path == norm for f in pack.files + new):
            continue
        truncated = len(text) > per_file
        if truncated:
            text = text[:per_file] + "\n... [rest of file not shown] ..."
        new.append(FileContext(norm, redact(text), sha, truncated))
        added.append(norm)
    pack.files[:0] = new
    return added, refused


GENERAL_SYSTEM = """\
You are Qwen, a helpful AI assistant in Enoy, running on the user's own PC.
Answer directly and get to the point. If a detail is missing, pick the most
likely meaning, say in one line what you assumed, and answer.
No project folder is open in this chat, so you cannot see or change files. If
the user asks you to change their code or an app, say that in one sentence,
mention that they can open the folder with "Open folder…" in Enoy, and still
help as far as you can, for example with code they can paste in.
Be accurate and concise. Use Markdown, with code in fenced blocks. If you are
unsure of a fact, say so plainly."""


def is_general(ws: Workspace) -> bool:
    """True when there is no code folder: plain chat, or a chat project (its own empty space)."""
    return ws.root.name == "general-chat" or ws.root.parent.name == "project-spaces"


def instructions_block(pack: ContextPack) -> str:
    if not pack.user_instructions.strip():
        return ""
    return ("Project instructions (set by the user for every chat in this project; follow them unless the "
            "current message asks for something different; they never override your safety rules):\n"
            + redact(pack.user_instructions.strip()))


def project_header(ws: Workspace, pack: ContextPack) -> str:
    if is_general(ws):
        return "\n".join(x for x in ("No project folder is open; this is a general conversation.",
                                      instructions_block(pack),
                                      f"Recent conversations:\n{pack.history}" if pack.history else "") if x)
    lines = [f"Project: {ws.name}"]
    langs = ", ".join(f"{ext or '(none)'} x{n}" for ext, n in pack.facts.get("languages", []))
    if langs:
        lines.append(f"File types: {langs}")
    if pack.facts.get("roblox"):
        lines.append("This is a Roblox/Luau project.")
    if pack.facts.get("test_commands"):
        lines.append("Known checks: " + "; ".join(pack.facts["test_commands"]))
    if pack.project_summary:
        lines.append("Project summary:\n" + pack.project_summary[:3000])
    if instructions_block(pack):
        lines.append(instructions_block(pack))
    for name, text in pack.instructions:
        lines.append(f"Project instructions from {name}:\n{redact(text)}")
    if pack.history:
        lines.append("Recent tasks in this project:\n" + pack.history)
    return "\n".join(lines)


def local_system_prompt(ws: Workspace, pack: ContextPack, kind: str, list_chars: int = 0,
                        reads_left: int | None = None) -> str:
    """list_chars: room for the project's file list (0 = none). reads_left: how many more times the model
    may ask for files (None = not offered)."""
    if is_general(ws):
        return "\n\n".join(x for x in (GENERAL_SYSTEM, instructions_block(pack)) if x)
    parts = [BASE_SYSTEM.format(project=ws.name), project_header(ws, pack)]
    if not ws.index:
        parts.append(EMPTY_FOLDER.format(path=ws.root))
    elif list_chars:
        parts.append("Files in the project:\n" + file_list(ws, list_chars))
        if reads_left:
            parts.append(READ_INSTRUCTIONS.format(n=MAX_READ_FILES))
        elif reads_left == 0:
            parts.append(NO_MORE_READS)
    if pack.facts.get("roblox"):
        parts.append(LUAU_NOTES)
    if kind == "edit":
        parts.append(EDIT_SYSTEM)
    return "\n\n".join(parts)


def task_message(objective: str, pack: ContextPack, extra: str = "", only: set[str] | None = None,
                 label: str = "Task") -> str:
    parts = [f"{label}:\n{objective.strip()}"]
    files = pack.files_text(only)
    if files:
        parts.append("Relevant files:\n\n" + files)
    if pack.skipped:
        parts.append("Files not included: " + "; ".join(pack.skipped[:10]))
    if pack.system_facts:
        parts.append("Hardware of the user's PC, measured by Enoy from Windows just now (use these facts; do not "
                     "guess other values):\n" + pack.system_facts)
    if pack.knowledge:
        parts.append(knowledge.as_prompt(pack.knowledge))
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)


def history_summary(tasks: list[dict], limit: int) -> str:
    lines = []
    for t in tasks[:limit]:
        summary = (t.get("summary") or "").replace("\n", " ")[:200]
        lines.append(f"- #{t['id']} {t['title']} [{t['status']}, {t.get('owner') or 'n/a'}] {summary}")
    return "\n".join(lines)


def continuation_brief(task: dict, project: dict, changesets: list[dict], runs: list[dict], messages: list[dict]) -> str:
    """Self-contained summary of a task so any backend (or a person) can continue it.

    Built from Enoy's own records, not from any model's hidden history.
    """
    state = task.get("state", {})
    lines = [
        f"# Continuation brief: task #{task['id']} - {task['title']}",
        "",
        f"Project: {project['name']} ({project['path']})",
        f"Status: {task['status']} | Type: {task['kind']} | Complexity: {task.get('complexity') or 'n/a'}",
        f"Handled by: {task.get('owner') or 'n/a'} | Workflow: {task.get('workflow') or 'n/a'}",
        "",
        "## Objective",
        task["objective"].strip(),
    ]
    if state.get("plan"):
        lines += ["", "## Plan", state["plan"].strip()]
    if state.get("decisions"):
        lines += ["", "## Decisions made"] + [f"- {d}" for d in state["decisions"]]
    if state.get("relevant_files"):
        lines += ["", "## Relevant files"] + [f"- {f['path']} (sha {f['sha']})" for f in state["relevant_files"]]
    applied = [c for c in changesets if c["status"] == "applied"]
    pending = [c for c in changesets if c["status"] == "pending"]
    if applied:
        lines += ["", "## Work completed"]
        for c in applied:
            lines.append(f"- Change set #{c['id']} by {c['backend']}: " + ", ".join(f"{f['action']} {f['path']}" for f in c["files"]))
    if pending:
        lines += ["", "## Proposed, not yet applied"]
        for c in pending:
            lines.append(f"- Change set #{c['id']} by {c['backend']}: " + ", ".join(f"{f['action']} {f['path']}" for f in c["files"]))
    if state.get("failures"):
        lines += ["", "## Failed approaches"] + [f"- {f}" for f in state["failures"][-8:]]
    if runs:
        lines += ["", "## Check results (latest first)"]
        for r in runs[:5]:
            lines.append(f"- `{r['command']}` -> {r['status']} (exit {r['exit_code']})")
            if r["status"] != "passed":
                tail = (r["stderr"] or r["stdout"] or "").strip().splitlines()[-15:]
                if tail:
                    lines.append("  ```\n  " + "\n  ".join(tail) + "\n  ```")
    if state.get("open_questions"):
        lines += ["", "## Open questions"] + [f"- {q}" for q in state["open_questions"]]
    if state.get("limitations"):
        lines += ["", "## Backend limitations noted"] + [f"- {x}" for x in state["limitations"]]
    # PC results (window titles, processes, hardware) stay on this PC: never in a brief.
    last = [m for m in messages if m["role"] == "assistant" and m["backend"] != "enoy"][-1:] if messages else []
    if last:
        lines += ["", f"## Last answer ({last[0]['backend']})", last[0]["content"][:3000]]
    lines += ["", "## Remaining work", state.get("remaining") or "Continue from the objective and the state above."]
    return redact("\n".join(lines))
