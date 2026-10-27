"""Folder tools: find a folder on this PC, and ask the user to move the chat there.

Enoy works inside one folder per chat. When a request is about code somewhere else (most often
Enoy's own source code: "these status dots", "this app"), the agent can look for the folder
and ask to move the chat. Nothing moves without the user's click: switch_folder only records
the request, and Enoy shows it as a card with "Move and continue" / "Stay here".

find_folders reads folder names only (never file contents) in the places code usually lives,
skipping Windows, program and cache folders, with a time limit.
"""

from __future__ import annotations

import os
import re
import string
import time
from dataclasses import dataclass, field
from pathlib import Path

from .registry import Tool, ToolContext, ToolError

SKIP = {"windows", "program files", "program files (x86)", "programdata", "$recycle.bin", "system volume information",
        "appdata", "node_modules", ".git", ".venv", "venv", "__pycache__", "site-packages", ".cache", "recovery",
        "perflogs", "msocache", "intel", "amd", "nvidia", "drivers"}
MARKERS = (".git", "package.json", "pyproject.toml", "requirements.txt", "setup.py", "Cargo.toml", "go.mod",
           "default.project.json")
MAX_RESULTS = 15
TIME_LIMIT_S = 3.0


@dataclass
class FolderService:
    """What the folder tools know and what they asked for in this turn."""
    current: str                                    # This chat's folder ("" for a general chat).
    known: list[tuple[str, str]] = field(default_factory=list)   # (name, path) of folders opened in Enoy.
    source: str = ""                                # Enoy's own source code, when it runs from source.
    requested: dict | None = None                   # {"path", "reason"} once switch_folder was called.


def enoy_source() -> str:
    """Enoy's own source folder when it runs from source (the folder holding enoy/ui), else ""."""
    root = Path(__file__).resolve().parents[2]
    return str(root) if (root / "enoy" / "ui" / "app.js").is_file() else ""


# A change asked of Enoy itself: a change word together with something only Enoy has.
_ENOY = re.compile(r"\benoy'?s?\b|\bthis (app|application|program|window)\b|\b(ollama|qwen|claude) (ready|offline)\b"
                   r"|\bstatus (dots?|lights?|indicators?)\b", re.I)
_CHANGE = re.compile(r"\b(redesign|restyle|change|fix|add|make|improve|edit|update|remove|move|rename|rebuild|"
                     r"modify|tweak|style|ändra|fixa|lägg till|gör|förbättra|ta bort|byt)\b", re.I)


def about_enoy_change(text: str) -> bool:
    """True for a request to change Enoy itself ("redesign the qwen ready dot", "add dark mode to this app")."""
    return bool(_ENOY.search(text or "")) and bool(_CHANGE.search(text or ""))


def markers(path: Path) -> list[str]:
    found = []
    for m in MARKERS:
        try:
            if (path / m).exists():
                found.append(m)
        except OSError:
            pass
    return found


def search_roots() -> list[tuple[Path, int]]:
    """(folder, depth) to scan: the user's own folders deeper, drive roots shallower."""
    home = Path.home()
    roots = [(home / n, 3) for n in ("Desktop", "Documents", "Downloads", "source", "repos", "projects", "dev",
                                     "code", "OneDrive")]
    roots.append((home, 1))
    if os.name == "nt":
        roots += [(Path(f"{d}:\\"), 2) for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
    return [(p, d) for p, d in roots if p.is_dir()]


def scan(name: str, roots: list[tuple[Path, int]], deadline: float) -> list[Path]:
    """Folders whose name contains `name` (case-insensitive), breadth-first, until the deadline."""
    want = name.lower()
    hits: list[Path] = []
    seen: set[str] = set()
    queue = [(p, 0, d) for p, d in roots]
    while queue and time.monotonic() < deadline and len(hits) < MAX_RESULTS * 2:
        folder, depth, limit = queue.pop(0)
        key = str(folder).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            try:
                if not e.is_dir(follow_symlinks=False) or e.name.startswith(".") or e.name.lower() in SKIP:
                    continue
            except OSError:
                continue
            if want in e.name.lower():
                hits.append(Path(e.path))
            if depth + 1 < limit:
                queue.append((Path(e.path), depth + 1, limit))
    return hits


def _service(ctx: ToolContext) -> FolderService:
    svc = (ctx.services or {}).get("folders")
    if svc is None:
        raise ToolError("unavailable", "Folder tools are not available here.")
    return svc


def _describe(path: Path, svc: FolderService) -> dict:
    out = {"path": str(path), "name": path.name, "markers": markers(path)}
    if svc.source and os.path.normcase(str(path)) == os.path.normcase(svc.source):
        out["note"] = "Enoy's own source code (this app)"
    if svc.current and os.path.normcase(str(path)) == os.path.normcase(svc.current):
        out["note"] = "the folder this chat already uses"
    return out


def _find_folders(args: dict, ctx: ToolContext) -> dict:
    svc = _service(ctx)
    name = (args.get("name") or "").strip()
    results: list[Path] = []
    if svc.source:
        results.append(Path(svc.source))
    results += [Path(p) for _, p in svc.known if Path(p).is_dir()]
    if name:
        results = [p for p in results if name.lower() in p.name.lower()
                   or (svc.source and str(p) == svc.source and name.lower() in ("enoy",))]
        results += scan(name, search_roots(), time.monotonic() + TIME_LIMIT_S)
    unique, keys = [], set()
    for p in results:
        k = os.path.normcase(str(p))
        if k not in keys:
            keys.add(k)
            unique.append(p)
    return {"query": name or "(known folders)", "count": len(unique[:MAX_RESULTS]),
            "folders": [_describe(p, svc) for p in unique[:MAX_RESULTS]],
            "enoy_source": svc.source or "Enoy is not running from source on this PC."}


def check_target(path: str, current: str = "") -> Path:
    """The folder a chat may move to, or ToolError. Never a drive root, Windows or the user folder itself."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ToolError("invalid", "Give the full path of the folder, e.g. C:\\Projects\\my-app.")
    try:
        p = p.resolve()
    except OSError as exc:
        raise ToolError("invalid", f"Cannot use {path}: {exc}") from exc
    if not p.is_dir():
        raise ToolError("invalid", f"{p} is not a folder on this PC. Use find_folders to find the right one.")
    if p.parent == p or p == Path.home() or p.name.lower() in SKIP or any(
            part.lower() in ("windows", "program files", "program files (x86)") for part in p.parts[:2]):
        raise ToolError("denied", f"{p} is a system or top-level folder; pick the project folder itself.")
    if current and os.path.normcase(str(p)) == os.path.normcase(current):
        raise ToolError("invalid", "This chat already works in that folder.")
    return p


def _switch_folder(args: dict, ctx: ToolContext) -> dict:
    svc = _service(ctx)
    p = check_target(args["path"], svc.current)
    svc.requested = {"path": str(p), "reason": (args.get("reason") or "").strip()[:300]}
    return {"requested": str(p), "next": "Enoy now asks the user to move this chat there. Stop using tools and "
                                        "reply in one or two sentences: which folder and why. Once the user agrees, "
                                        "the work continues in that folder."}


FIND_FOLDERS = Tool(
    name="find_folders",
    description="Find folders on this PC by name, e.g. a project's folder or Enoy's own source code. Without a "
                "name it lists Enoy's source and the folders opened in Enoy before. Reads folder names only.",
    parameters={"type": "object", "properties": {
        "name": {"type": "string", "description": "Part of the folder name, e.g. 'enoy' or 'my-website'.",
                 "maxLength": 100}}, "required": []},
    run=_find_folders, output="folders: path, name, markers (.git, package.json...), note", permission="files.read",
    reads_files=True, timeout_s=10)

SWITCH_FOLDER = Tool(
    name="switch_folder",
    description="Ask the user to move this chat to another folder, when the request is about code that is not in "
                "this chat's folder (for example Enoy's own source code). The user confirms; then the work continues "
                "there. Give the full path.",
    parameters={"type": "object", "properties": {
        "path": {"type": "string", "maxLength": 400},
        "reason": {"type": "string", "description": "One sentence for the user: why this folder.", "maxLength": 300}},
        "required": ["path", "reason"]},
    run=_switch_folder, output="requested path; Enoy asks the user", permission="files.read")

FOLDER_TOOLS = [FIND_FOLDERS, SWITCH_FOLDER]
