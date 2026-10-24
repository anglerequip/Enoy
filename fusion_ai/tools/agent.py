"""Workspace tools for agent mode: look around, read, search, edit, write and run.

These run inside the open project folder only (Workspace.resolve refuses
anything outside it, secrets files, binaries and very large files). Edits are
syntax-checked before they touch disk. With "apply" on (the default in agent
mode) each edit is written at once with a backup, and the whole turn becomes
one change set you can undo; with it off, edits are staged and shown as one
proposal to apply or discard at the end. Commands go through Enoy's command
policy: allowlisted ones run, others are handed back for your approval,
blocked ones never run.
"""

from __future__ import annotations

import fnmatch
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import commands as cmdpolicy
from .. import patches
from ..project import PathError, Workspace, sha256_text
from .registry import Tool, ToolContext, ToolError

MAX_LIST = 300
MAX_READ_LINES = 400


@dataclass
class AgentFiles:
    """The project as the agent sees it, with every change it made this turn."""
    ws: Workspace
    apply: bool
    backup_dir: Path
    run: Callable[[str], dict] | None = None   # Runs an allowlisted command, returns {status, exit_code, output}.
    changes: dict[str, dict] = field(default_factory=dict)  # path -> {orig, orig_hash, now}
    suggested: list[str] = field(default_factory=list)      # Commands that need your approval.

    def _norm(self, path: str) -> str:
        try:
            rel = self.ws.rel(self.ws.resolve(path))
        except PathError as exc:
            raise ToolError("denied", str(exc)) from exc
        if self.ws.is_sensitive(rel):
            raise ToolError("denied", f"'{rel}' looks like a secrets file; Enoy does not read or change these.")
        if self.ws.is_excluded(rel):
            raise ToolError("denied", f"'{rel}' is in an excluded folder (for example .git or node_modules).")
        return rel

    def read(self, path: str) -> str | None:
        rel = self._norm(path)
        if rel in self.changes:
            return self.changes[rel]["now"]
        try:
            return self.ws.read_text(rel)[0]
        except FileNotFoundError:
            return None
        except PathError as exc:
            raise ToolError("denied", str(exc)) from exc

    def save(self, path: str, content: str) -> dict:
        rel = self._norm(path)
        problems, warnings = patches.check_syntax(rel, content)
        if problems:
            raise ToolError("invalid", "Not written, the result would not be valid: " + "; ".join(problems[:5]))
        before = self.read(rel)
        if rel not in self.changes:
            self.changes[rel] = {"orig": before, "orig_hash": None if before is None else sha256_text(before), "now": before}
        if self.apply:
            current = self.ws.current_hash(rel)
            f = {"path": rel, "action": "create" if current is None else "modify", "old_hash": current,
                 "old_content": before if current is not None else None, "new_content": content,
                 "new_hash": sha256_text(content)}
            try:
                patches.apply_files(self.ws, [f], self.backup_dir)
            except patches.ConflictError as exc:
                raise ToolError("failed", str(exc)) from exc
        self.changes[rel]["now"] = content
        added = len(content.splitlines()) - len((before or "").splitlines())
        return {"path": rel, "written": self.apply, "staged": not self.apply, "lines": len(content.splitlines()),
                "line_change": added, "warnings": warnings[:3]}

    def changeset_files(self) -> list[dict]:
        out = []
        for rel, c in self.changes.items():
            if c["now"] == c["orig"]:
                continue
            out.append({"path": rel, "action": "create" if c["orig"] is None else "modify",
                        "old_hash": c["orig_hash"], "new_hash": sha256_text(c["now"]),
                        "old_content": c["orig"], "new_content": c["now"],
                        "diff": patches.make_diff(rel, c["orig"], c["now"])})
        return out


def _files(ctx: ToolContext) -> AgentFiles:
    files = (ctx.services or {}).get("files")
    if files is None:
        raise ToolError("unavailable", "No project folder is open, so there are no files to work with.")
    return files


def _list_files(args: dict, ctx: ToolContext) -> dict:
    files = _files(ctx)
    base = (args.get("path") or ".").strip().strip("/") or "."
    pattern = args.get("pattern") or "*"
    names = sorted(files.ws.index)
    if base != ".":
        names = [n for n in names if n == base or n.startswith(base.rstrip("/") + "/")]
    names = [n for n in names if fnmatch.fnmatch(n.rsplit("/", 1)[-1], pattern) or fnmatch.fnmatch(n, pattern)]
    created = [p for p, c in files.changes.items() if c["orig"] is None and p not in names]
    names = sorted(set(names) | set(created))
    return {"root": files.ws.name, "count": len(names), "files": names[:MAX_LIST],
            "truncated": len(names) > MAX_LIST}


def _read_file(args: dict, ctx: ToolContext) -> dict:
    files = _files(ctx)
    text = files.read(args["path"])
    if text is None:
        raise ToolError("failed", f"{args['path']} does not exist. Use list_files or search_code to find the file.")
    lines = text.splitlines()
    start = max(1, int(args.get("start_line") or 1))
    end = int(args.get("end_line") or 0) or min(len(lines), start + MAX_READ_LINES - 1)
    end = min(end, len(lines), start + MAX_READ_LINES - 1)
    body = "\n".join(f"{n:>4}| {lines[n - 1]}" for n in range(start, end + 1)) if lines else ""
    return {"path": files._norm(args["path"]), "lines": f"{start}-{end} of {len(lines)}", "content": body,
            "more": end < len(lines)}


def _search_code(args: dict, ctx: ToolContext) -> dict:
    files = _files(ctx)
    limit = int(args.get("max_results") or 40)
    hits = files.ws.search(args["query"], max_results=limit)
    out = {"query": args["query"], "matches": [{"path": h["path"], "line": h["line"], "text": h["text"]} for h in hits]}
    # Models often search several words at once ("ollama ready qwen ready"). When the exact phrase finds
    # little, lines holding most of the words follow, best first.
    words = sorted(set(re.findall(r"[\w.-]{3,}", args["query"].lower())))
    if len(hits) < limit and len(words) > 1:
        need = max(2, (len(words) + 1) // 2)
        seen = {(h["path"], h["line"]) for h in hits}
        extra = []
        for rel, info in files.ws.index.items():
            if info.size > 1_000_000:
                continue
            try:
                text = (files.ws.root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                low = line.lower()
                score = sum(w in low for w in words)
                if score >= need and (rel, n) not in seen:
                    extra.append((-score, rel, n, line.strip()[:200]))
        extra.sort()
        out["matches"] += [{"path": p, "line": n, "text": t} for _, p, n, t in extra[:limit - len(hits)]]
        if extra and not hits:
            out["note"] = "No line has the exact phrase; these lines contain most of its words."
    return out


def _edit_file(args: dict, ctx: ToolContext) -> dict:
    files = _files(ctx)
    current = files.read(args["path"])
    if current is None:
        raise ToolError("failed", f"{args['path']} does not exist. Use write_file to create it.")
    new, errors = patches._apply_edits(current, [patches.Edit(args["old_text"], args["new_text"])], args["path"])
    if errors:
        raise ToolError("invalid", errors[0] + " Read the file again and copy old_text exactly, including indentation.")
    return files.save(args["path"], new)


def _write_file(args: dict, ctx: ToolContext) -> dict:
    return _files(ctx).save(args["path"], args["content"])


def _run_command(args: dict, ctx: ToolContext) -> dict:
    files = _files(ctx)
    command = args["command"].strip()
    policy = cmdpolicy.classify(command, ctx.cfg["permissions"]["command_allowlist"])
    if policy.level == "blocked":
        raise ToolError("denied", "Blocked by Enoy's command policy: " + " ".join(policy.reasons))
    if policy.level != "allowlisted":
        if command not in files.suggested:
            files.suggested.append(command)
        raise ToolError("needs_approval", f"`{command}` needs the user's approval ({' '.join(policy.reasons)}). "
                                          "It was not run. Tell the user what it does and why; they can run it with one click.")
    if files.run is None:
        raise ToolError("unavailable", "Commands cannot run here.")
    if not files.apply and files.changes:
        note = "Your edits are staged, not on disk yet, so this ran against the files as they were."
    else:
        note = ""
    result = files.run(command)
    out = result.get("output", "")
    return {"command": command, "status": result.get("status"), "exit_code": result.get("exit_code"),
            "output_tail": out[-4000:], "note": note}


def _tool(name, desc, fn, params, required, permission, output, timeout=30, writes=False) -> Tool:
    return Tool(name=name, description=desc, run=fn, output=output, permission=permission, timeout_s=timeout,
                parameters={"type": "object", "properties": params, "required": required},
                reads_files=True, writes_files=writes, needs_workspace=True)


AGENT_TOOLS = [
    _tool("list_files", "List the project's files (optionally under a folder or matching a pattern like *.py).",
          _list_files, {"path": {"type": "string", "description": "Folder inside the project, default the root.", "maxLength": 300},
                        "pattern": {"type": "string", "description": "File name pattern, e.g. *.py", "maxLength": 100}},
          [], "files.read", "count, files"),
    _tool("read_file", "Read a project file with line numbers (up to 400 lines per call; use start_line for more).",
          _read_file, {"path": {"type": "string", "maxLength": 300},
                       "start_line": {"type": "integer", "minimum": 1, "maximum": 1000000},
                       "end_line": {"type": "integer", "minimum": 1, "maximum": 1000000}},
          ["path"], "files.read", "path, lines, content (numbered), more"),
    _tool("search_code", "Find text in the project's files and file names (case-insensitive).",
          _search_code, {"query": {"type": "string", "maxLength": 200},
                         "max_results": {"type": "integer", "minimum": 1, "maximum": 100}},
          ["query"], "files.read", "matches: path, line, text"),
    _tool("edit_file", "Change part of a file: old_text must match the file exactly once (copy it from read_file "
          "without the line numbers); it is replaced by new_text. Read the file first.",
          _edit_file, {"path": {"type": "string", "maxLength": 300},
                       "old_text": {"type": "string", "maxLength": 20000},
                       "new_text": {"type": "string", "maxLength": 40000}},
          ["path", "old_text", "new_text"], "files.write", "path, written or staged, line_change", writes=True),
    _tool("write_file", "Create a new file, or replace a whole file, with the given content.",
          _write_file, {"path": {"type": "string", "maxLength": 300},
                        "content": {"type": "string", "maxLength": 200000}},
          ["path", "content"], "files.write", "path, written or staged, lines", writes=True),
    _tool("run_command", "Run a command in the project folder, e.g. the tests (python -m pytest -q, npm test). "
          "Allowed commands run at once; others are handed to the user for approval.",
          _run_command, {"command": {"type": "string", "maxLength": 500}},
          ["command"], "commands", "status, exit_code, output_tail", timeout=600),
]
