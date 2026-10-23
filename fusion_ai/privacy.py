"""What may leave this PC, and in what form.

Claude only ever receives a package built here: the task's relevant files minus
anything excluded, with secrets replaced. Files that Settings mark as private
for the cloud may still be read by Qwen locally, so their content could come
back inside Qwen's answer, a diff, or command output. scrub() removes any line
of a private file that reappears in text bound for the cloud.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .redact import redact

REDACTED = "[REDACTED]"
MIN_LINE = 12  # Shorter lines (braces, "return x") are too common to identify a file.


def cloud_excluded(rel: str, globs: list[str]) -> bool:
    """True when a project path must never be sent to a cloud model."""
    path = rel.replace("\\", "/")
    while path.startswith("./"):  # Strip the prefix only; a leading dot is part of names like .secrets/.
        path = path[2:]
    name = PurePosixPath(path).name
    for pattern in globs:
        pattern = pattern.strip().replace("\\", "/")
        if not pattern:
            continue
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(name, pattern):
            return True
        if pattern.endswith("/**") and (path + "/").startswith(pattern[:-2]):
            return True
        if "/" not in pattern.rstrip("/") and fnmatch.fnmatch(path.split("/")[0], pattern.rstrip("/")):
            return True  # A bare folder name such as "secrets" or "private/".
    return False


def local_only_reason(cfg: dict, project: dict | None, task_state: dict | None = None) -> str:
    """Why a task may not use the cloud, or "" when it may."""
    if cfg.get("privacy", {}).get("cloud_policy") == "never":
        return "Settings say never to send code to Claude"
    if project and project.get("local_only"):
        return "this project is set to local-only"
    if task_state and task_state.get("local_only"):
        return "this chat is set to stay on your PC"
    return ""


@dataclass
class CloudCheck:
    """Result of the privacy check that runs before every Claude request."""
    sent_files: list[str] = field(default_factory=list)
    excluded_files: list[str] = field(default_factory=list)
    scrubbed_lines: int = 0
    redactions: int = 0

    def summary(self) -> str:
        bits = [f"{len(self.sent_files)} file(s) allowed"]
        if self.excluded_files:
            bits.append(f"{len(self.excluded_files)} private file(s) left out ({', '.join(self.excluded_files[:4])})")
        if self.scrubbed_lines:
            bits.append(f"{self.scrubbed_lines} line(s) quoting private files removed")
        bits.append(f"{self.redactions} secret value(s) replaced with {REDACTED}" if self.redactions else "no secrets found")
        return "Privacy check: " + "; ".join(bits) + "."


def private_lines(texts: list[str]) -> set[str]:
    return {ln.strip() for t in texts for ln in t.splitlines() if len(ln.strip()) >= MIN_LINE}


def scrub(text: str, private: set[str]) -> tuple[str, int]:
    """Drop lines of text that repeat a line from a private file. Returns (text, removed)."""
    if not private or not text:
        return text, 0
    out, removed = [], 0
    for line in text.splitlines():
        core = line.strip().lstrip("+-> ").strip()  # Also catches diff and quote prefixes.
        if len(core) >= MIN_LINE and (core in private or line.strip() in private):
            removed += 1
            continue
        out.append(line)
    return "\n".join(out), removed


def finalize(prompt: str, private: set[str], check: CloudCheck) -> str:
    """Last step before a cloud request: scrub private lines, then redact secrets."""
    text, removed = scrub(prompt, private)
    check.scrubbed_lines += removed
    text = redact(text)
    # Counts every replacement in the package, including those made when files were read.
    check.redactions = text.count(REDACTED)
    return text
