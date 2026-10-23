"""Context assembly: which files and facts each backend is given.

Everything that leaves Enoy for a model is built here, redacted, and
recorded by the orchestrator. Claude gets a compact brief; files it already
received unchanged in the same Claude session are not sent again.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
You are the local coding assistant inside Enoy, working on the
user's project "{project}". Be accurate and concise. Base answers on the files
shown; if something you need is not shown, say which file you need instead of
guessing. If you are unsure, say so plainly."""

EDIT_SYSTEM = """\
Your job in this step is to propose concrete file changes for the task. Keep
changes minimal and consistent with the existing style. Briefly explain the
change first, then give the blocks.

""" + FORMAT_INSTRUCTIONS

CLAUDE_SYSTEM = """\
You are the specialist reviewer and problem-solver in Enoy.
A local model (Qwen 9B via Ollama) handles routine work; you are consulted for
work that needs stronger reasoning. You cannot open files or run commands: you
only see what is in this message. Ask for a specific file if you need it.
Be direct and concise."""

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


GENERAL_SYSTEM = """\
You are a helpful AI assistant in Enoy, running on the user's own
PC. No project folder is open, so you cannot see or change files. Be accurate,
clear and concise. Use Markdown, with code in fenced blocks. If you are unsure,
say so plainly."""


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


def local_system_prompt(ws: Workspace, pack: ContextPack, kind: str) -> str:
    if is_general(ws):
        return "\n\n".join(x for x in (GENERAL_SYSTEM, instructions_block(pack)) if x)
    parts = [BASE_SYSTEM.format(project=ws.name), project_header(ws, pack)]
    if pack.facts.get("roblox"):
        parts.append(LUAU_NOTES)
    if kind == "edit":
        parts.append(EDIT_SYSTEM)
    return "\n\n".join(parts)


def task_message(objective: str, pack: ContextPack, extra: str = "", only: set[str] | None = None) -> str:
    parts = [f"Task:\n{objective.strip()}"]
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
