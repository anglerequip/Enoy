"""Read-only Git facts about a project folder: which files have uncommitted changes.

Enoy never commits, pushes or changes Git state. It only asks `git status` so
team runs can leave your own uncommitted work alone.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def changed_files(root: Path) -> list[str] | None:
    """Project-relative paths with uncommitted changes (modified, added, deleted, untracked), or None when the
    folder is not a Git repository or Git is not installed."""
    git = shutil.which("git")
    if not git:
        return None
    try:
        out = subprocess.run([git, "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                             capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        top = subprocess.run([git, "-C", str(root), "rev-parse", "--show-prefix"], capture_output=True, timeout=10,
                             text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        top = ""
    entries = out.stdout.decode("utf-8", errors="replace").split("\0")
    paths: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1  # A rename or copy is followed by its old path.
        # git prints paths relative to the repository root; keep those inside this folder, relative to it.
        if top and not path.startswith(top):
            continue
        paths.append(path[len(top):] if top else path)
    return sorted(set(paths))
