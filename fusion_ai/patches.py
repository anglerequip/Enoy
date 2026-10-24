"""Proposed file changes: parsing model output, validation, diffs, apply, revert.

Models describe changes in a small block format (see FORMAT_INSTRUCTIONS).
Full-file and search/replace blocks are far more reliable for a 9B local model
than hand-written unified diffs; Enoy computes the real diff itself.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .project import PathError, Workspace, sha256_bytes
from .redact import REDACTED

FORMAT_INSTRUCTIONS = """\
When you change files, use ONLY these blocks. Paths are relative to the project root.

Create a file, or replace a whole file:
<<<FILE path/to/file.ext
complete file content
>>>END

Edit part of an existing file (preferred for files longer than about 150 lines).
SEARCH must copy the existing lines exactly, including indentation. Use several
SEARCH/REPLACE pairs inside one EDIT block if needed:
<<<EDIT path/to/file.ext
<<<SEARCH
exact existing lines
===
replacement lines
>>>REPLACE
>>>END

Delete a file:
<<<DELETE path/to/file.ext>>>

Suggest a command the user may run to check the change (it is NOT run automatically):
<<<RUN python -m pytest -q>>>

Do not wrap these blocks in Markdown code fences. Write a short explanation
before the blocks.
"""

_FILE_START = re.compile(r"^<<<FILE\s+(.+?)\s*$")
_EDIT_START = re.compile(r"^<<<EDIT\s+(.+?)\s*$")
_DELETE = re.compile(r"^<<<DELETE\s+(.+?)\s*>>>\s*$")
_RUN = re.compile(r"^<<<RUN\s+(.+?)\s*>>>\s*$")
_SEPARATOR = re.compile(r"^(===\s*(REPLACE)?|>>>REPLACE)$")  # Not "=====": that can be file content.


def _block_start(stripped: str) -> bool:
    return bool(_FILE_START.match(stripped) or _EDIT_START.match(stripped) or _DELETE.match(stripped)
                or _RUN.match(stripped))


@dataclass
class Edit:
    search: str
    replace: str


@dataclass
class ProposedChange:
    action: str                 # "write", "edit" or "delete" (as written by the model)
    path: str
    content: str | None = None  # For "write".
    edits: list[Edit] = field(default_factory=list)


@dataclass
class ParseResult:
    changes: list[ProposedChange]
    commands: list[str]
    explanation: str
    errors: list[str]


def _strip_fence(text: str) -> str:
    lines = text.split("\n")
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
    return "\n".join(lines)


def parse(output: str) -> ParseResult:
    """Extract change blocks and suggested commands from model output."""
    lines = output.replace("\r\n", "\n").split("\n")
    changes: list[ProposedChange] = []
    commands: list[str] = []
    errors: list[str] = []
    explanation: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        m_file = _FILE_START.match(stripped)
        m_edit = _EDIT_START.match(stripped)
        m_del = _DELETE.match(stripped)
        m_run = _RUN.match(stripped)
        if m_del:
            changes.append(ProposedChange("delete", m_del.group(1).strip()))
            i += 1
        elif m_run:
            commands.append(m_run.group(1).strip())
            i += 1
        elif m_file:
            path = m_file.group(1).strip()
            body: list[str] = []
            i += 1
            closed = False
            while i < len(lines):
                if lines[i].strip() == ">>>END":
                    closed = True
                    break
                body.append(lines[i])
                i += 1
            if not closed:
                errors.append(f"FILE block for '{path}' is missing its >>>END line (output may be truncated).")
            else:
                content = _strip_fence("\n".join(body))
                if content and not content.endswith("\n"):
                    content += "\n"
                changes.append(ProposedChange("write", path, content=content))
            i += 1
        elif m_edit:
            path = m_edit.group(1).strip()
            edits: list[Edit] = []
            i += 1
            closed = False
            # Qwen's common slips are accepted where the meaning stays unambiguous: "===REPLACE" or ">>>REPLACE"
            # as the separator, a pair ended by >>>END or the next <<<SEARCH, and a block ended by the next block.
            # A pair or block cut off by the end of the output is still an error (the answer may be truncated).
            while i < len(lines):
                s = lines[i].strip()
                if s == ">>>END":
                    closed = True
                    i += 1
                    break
                if _block_start(s):
                    closed = bool(edits)  # The next block began; this one was complete.
                    break
                if s == "<<<SEARCH":
                    search: list[str] = []
                    replace: list[str] = []
                    i += 1
                    while i < len(lines) and not _SEPARATOR.match(lines[i].strip()):
                        search.append(lines[i])
                        i += 1
                    if i >= len(lines):
                        errors.append(f"EDIT block for '{path}' has an unterminated SEARCH/REPLACE pair.")
                        break
                    separator = lines[i].strip()
                    i += 1
                    while i < len(lines):
                        s = lines[i].strip()
                        if s in (">>>END", "<<<SEARCH") or _block_start(s) or (s == ">>>REPLACE" and separator != s):
                            break
                        replace.append(lines[i])
                        i += 1
                    if i >= len(lines):
                        errors.append(f"EDIT block for '{path}' has an unterminated SEARCH/REPLACE pair.")
                        break
                    if lines[i].strip() == ">>>REPLACE":
                        i += 1
                    edits.append(Edit("\n".join(search), "\n".join(replace)))
                    continue
                i += 1
            if not closed:
                if not any(e.startswith(f"EDIT block for '{path}'") for e in errors):
                    errors.append(f"EDIT block for '{path}' is missing its >>>END line (output may be truncated).")
            elif not edits:
                errors.append(f"EDIT block for '{path}' contains no SEARCH/REPLACE pairs.")
            else:
                changes.append(ProposedChange("edit", path, edits=edits))
        else:
            if not changes:
                explanation.append(line)
            i += 1
    return ParseResult(changes, commands, "\n".join(explanation).strip(), errors)


# Validation ---------------------------------------------------------------------

@dataclass
class ValidatedFile:
    path: str
    action: str              # "create", "modify" or "delete"
    old_hash: str | None
    new_hash: str | None
    old_content: str | None
    new_content: str | None
    diff: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class ValidationResult:
    files: list[ValidatedFile]
    errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.files)

    def summary(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


def make_diff(path: str, old: str | None, new: str | None) -> str:
    old_lines = (old or "").splitlines(keepends=True)
    new_lines = (new or "").splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile="/dev/null" if old is None else f"a/{path}",
        tofile="/dev/null" if new is None else f"b/{path}",
    )
    out = []
    for line in diff:
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def _apply_edits(original: str, edits: list[Edit], path: str) -> tuple[str | None, list[str]]:
    text = original
    errors = []
    for n, edit in enumerate(edits, 1):
        if not edit.search.strip():
            errors.append(f"{path}: edit {n} has an empty SEARCH section.")
            continue
        count = text.count(edit.search)
        if count == 1:
            text = text.replace(edit.search, edit.replace, 1)
            continue
        if count > 1:
            errors.append(f"{path}: edit {n} SEARCH text appears {count} times; it must be unique.")
            continue
        # Retry ignoring trailing whitespace on each line.
        located = _fuzzy_locate(text, edit.search)
        if located is None:
            errors.append(f"{path}: edit {n} SEARCH text was not found in the current file.")
            continue
        start, end = located
        text = text[:start] + edit.replace + text[end:]
    return (text if not errors else None), errors


def _fuzzy_locate(text: str, search: str) -> tuple[int, int] | None:
    target = [l.rstrip() for l in search.split("\n")]
    while target and not target[-1]:
        target.pop()
    if not target:
        return None
    lines = text.split("\n")
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    matches = []
    for i in range(len(lines) - len(target) + 1):
        if all(lines[i + k].rstrip() == target[k] for k in range(len(target))):
            matches.append(i)
    if len(matches) != 1:
        return None
    i = matches[0]
    end_line = i + len(target) - 1
    return offsets[i], offsets[end_line] + len(lines[end_line])


# "/* ... existing content unchanged ... */", "// rest of the file", "# ... remaining code ...": a model
# that writes this in place of real code would delete that code.
_ELIDED = re.compile(r"(\.\.\.|…)[^\n]{0,40}\b(existing|unchanged|rest of|remaining|omitted|same as before)\b"
                     r"|\b(rest of|remaining|existing) (the )?(content|code|file|styles|functions|lines)\b[^\n]{0,30}"
                     r"\b(unchanged|omitted|here|as before|stays)\b", re.I)


def lost_content(path: str, before: str | None, after: str, whole_file: bool = True) -> str:
    """Why this change would silently delete code, or "". Checked for every proposal and every agent write."""
    if before is None:
        return ""
    new_marks = [m.group(0) for m in _ELIDED.finditer(after) if m.group(0) not in before]
    if new_marks:
        return (f"{path}: the change contains a placeholder ('{new_marks[0][:60]}') instead of the real code, "
                "which would delete that code. Use EDIT blocks that change only the lines you mean.")
    old_n, new_n = len(before.splitlines()), len(after.splitlines())
    if whole_file and old_n >= 60 and new_n < old_n // 2:
        return (f"{path}: replacing the whole file would delete {old_n - new_n} of its {old_n} lines. Use EDIT "
                "blocks for the parts you change (or DELETE the file if that is really meant).")
    return ""


def check_syntax(path: str, content: str) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) from language-specific checks."""
    ext = PurePosixPath(path).suffix.lower()
    errors: list[str] = []
    warnings: list[str] = []
    if ext == ".py":
        try:
            compile(content, path, "exec")
        except SyntaxError as exc:
            errors.append(f"{path}: Python syntax error on line {exc.lineno}: {exc.msg}")
    elif ext == ".json":
        try:
            json.loads(content)
        except ValueError as exc:
            errors.append(f"{path}: invalid JSON: {exc}")
    elif ext in (".js", ".mjs", ".cjs"):
        problem = _node_check(content, ext)
        if problem:
            errors.append(f"{path}: JavaScript syntax error: {problem}")
    elif ext in (".lua", ".luau"):
        problem = luau_bracket_check(content)
        if problem:
            warnings.append(f"{path}: {problem} (basic check only; no Luau parser is installed)")
    return errors, warnings


def _node_check(content: str, ext: str) -> str | None:
    node = shutil.which("node")
    if not node:
        return None
    fd, tmp = tempfile.mkstemp(suffix=ext)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        proc = subprocess.run(
            [node, "--check", tmp], capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0:
            lines = [l for l in proc.stderr.splitlines() if l.strip()]
            return next((l for l in lines if "Error" in l), lines[-1] if lines else "unknown error")
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return None


def luau_bracket_check(src: str) -> str | None:
    """Check (), [] and {} balance, skipping strings and comments."""
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[tuple[str, int]] = []
    i, line, n = 0, 1, len(src)
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if src.startswith("--", i):
            m = re.match(r"--\[(=*)\[", src[i:])
            if m:
                close = "]" + m.group(1) + "]"
                end = src.find(close, i + len(m.group(0)))
                if end == -1:
                    return f"unclosed block comment starting on line {line}"
                line += src.count("\n", i, end)
                i = end + len(close)
            else:
                end = src.find("\n", i)
                i = n if end == -1 else end
            continue
        m = re.match(r"\[(=*)\[", src[i:])
        if m:
            close = "]" + m.group(1) + "]"
            end = src.find(close, i + len(m.group(0)))
            if end == -1:
                return f"unclosed long string starting on line {line}"
            line += src.count("\n", i, end)
            i = end + len(close)
            continue
        if c in "\"'`":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                elif src[j] == "\n" and c != "`":
                    return f"unterminated string on line {line}"
                j += 1
            if j >= n:
                return f"unterminated string on line {line}"
            line += src.count("\n", i, j)
            i = j + 1
            continue
        if c in "([{":
            stack.append((c, line))
        elif c in ")]}":
            if not stack or stack[-1][0] != pairs[c]:
                return f"unexpected '{c}' on line {line}"
            stack.pop()
        i += 1
    if stack:
        return f"unclosed '{stack[-1][0]}' opened on line {stack[-1][1]}"
    return None


def merge_same_file_edits(changes: list[ProposedChange]) -> list[ProposedChange]:
    """Several EDIT blocks for one file become one change whose edits apply in order.

    Models often send one block per spot (an import at the top, a function further
    down). Anything else that touches a file twice is left for validate() to reject."""
    merged: list[ProposedChange] = []
    by_path: dict[str, ProposedChange] = {}
    for change in changes:
        key = change.path.replace("\\", "/").removeprefix("./").lower()
        first = by_path.get(key)
        if first is not None and first.action == "edit" and change.action == "edit":
            first.edits = first.edits + change.edits
            continue
        if change.action == "edit" and key not in by_path:
            change = ProposedChange(change.action, change.path, change.content, list(change.edits))
            by_path[key] = change
        merged.append(change)
    return merged


def validate(ws: Workspace, changes: list[ProposedChange], seen_hashes: dict[str, str] | None = None) -> ValidationResult:
    """Check proposed changes against the current files on disk.

    seen_hashes maps paths to the hash of the version the model was shown; a
    mismatch means the file changed after the model read it.
    """
    seen_hashes = seen_hashes or {}
    changes = merge_same_file_edits(changes)
    files: list[ValidatedFile] = []
    errors: list[str] = []
    warnings: list[str] = []
    touched: set[str] = set()
    for change in changes:
        try:
            abs_path = ws.resolve(change.path)
        except PathError as exc:
            errors.append(str(exc))
            continue
        rel = ws.rel(abs_path)
        if rel in touched:
            errors.append(f"{rel}: changed more than once in one proposal.")
            continue
        touched.add(rel)
        if ws.is_sensitive(rel):
            errors.append(f"{rel}: refusing to change a credentials or secrets file.")
            continue
        if any(part in (".git",) for part in PurePosixPath(rel).parts):
            errors.append(f"{rel}: refusing to change files inside .git.")
            continue
        exists = abs_path.is_file()
        if abs_path.exists() and not exists:
            errors.append(f"{rel}: is a directory.")
            continue
        old_bytes = abs_path.read_bytes() if exists else None
        old_hash = sha256_bytes(old_bytes) if old_bytes is not None else None
        old_text = None
        if old_bytes is not None:
            try:
                old_text = old_bytes.decode("utf-8")
            except UnicodeDecodeError:
                errors.append(f"{rel}: is not UTF-8 text; edit it manually.")
                continue
        if rel in seen_hashes and old_hash is not None and seen_hashes[rel] != old_hash:
            errors.append(f"{rel}: the file changed after the model read it. Re-run the task so it sees the current version.")
            continue

        if change.action == "delete":
            if not exists:
                errors.append(f"{rel}: cannot delete a file that does not exist.")
                continue
            files.append(ValidatedFile(rel, "delete", old_hash, None, old_text, None, make_diff(rel, old_text, None)))
            continue

        uses_crlf = old_text is not None and "\r\n" in old_text
        base = old_text.replace("\r\n", "\n") if old_text is not None else None
        if change.action == "edit":
            if base is None:
                errors.append(f"{rel}: EDIT used on a file that does not exist (use a FILE block to create it).")
                continue
            new_text, edit_errors = _apply_edits(base, change.edits, rel)
            if edit_errors:
                errors.extend(edit_errors)
                continue
        else:
            new_text = change.content or ""
        if REDACTED in new_text and (base is None or REDACTED not in base):
            errors.append(f"{rel}: the proposal contains '{REDACTED}' placeholders that would overwrite real values.")
            continue
        lost = lost_content(rel, base, new_text, whole_file=change.action != "edit")
        if lost:
            errors.append(lost)
            continue
        if base is not None and new_text == base:
            warnings.append(f"{rel}: proposed content is identical to the current file; skipped.")
            continue
        syn_errors, syn_warnings = check_syntax(rel, new_text)
        errors.extend(syn_errors)
        warnings.extend(syn_warnings)
        if syn_errors:
            continue
        if uses_crlf:
            new_text = new_text.replace("\n", "\r\n")
        new_hash = sha256_bytes(new_text.encode("utf-8"))
        files.append(ValidatedFile(
            rel, "modify" if exists else "create", old_hash, new_hash, old_text, new_text,
            make_diff(rel, base, new_text.replace("\r\n", "\n")),
        ))
    if not changes:
        errors.append("The response contained no file changes.")
    return ValidationResult(files, errors, warnings)


# Applying and reverting -------------------------------------------------------------

class ConflictError(RuntimeError):
    pass


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".fusion-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply_files(ws: Workspace, files: list[dict], backup_dir: Path) -> list[str]:
    """Apply validated file changes. All-or-nothing.

    Every file is checked first: it must still match the version the change
    was computed from. If anything changed in the meantime nothing is written.
    Returns the list of backup files written.
    """
    for f in files:
        current = ws.current_hash(f["path"])
        if current != f["old_hash"]:
            raise ConflictError(
                f"{f['path']} changed on disk after this change was proposed. "
                "Nothing was applied. Re-run the task to get a fresh proposal."
            )
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backups: list[str] = []
    done: list[dict] = []
    try:
        for f in files:
            target = ws.resolve(f["path"])
            if f["old_content"] is not None:
                backup = backup_dir / stamp / f["path"]
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                backups.append(str(backup))
            if f["action"] == "delete":
                target.unlink()
            else:
                _write_atomic(target, f["new_content"])
            done.append(f)
    except BaseException:
        for f in reversed(done):
            _restore(ws, f)
        raise
    ws.invalidate()
    return backups


def _restore(ws: Workspace, f: dict) -> None:
    target = ws.resolve(f["path"])
    if f["old_content"] is None:
        target.unlink(missing_ok=True)
    else:
        _write_atomic(target, f["old_content"])


def revert_files(ws: Workspace, files: list[dict]) -> None:
    """Undo applied changes, but only where the file still holds what was applied."""
    for f in files:
        current = ws.current_hash(f["path"])
        expected = None if f["action"] == "delete" else f["new_hash"]
        if current != expected:
            raise ConflictError(
                f"{f['path']} was edited after the change was applied, so it was not reverted automatically. "
                "Restore it from the backup folder if needed."
            )
    for f in files:
        _restore(ws, f)
    ws.invalidate()
