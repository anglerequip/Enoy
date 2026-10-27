"""Artifacts: saved, reusable work products (documents, code, HTML, SVG, data).

Content lives in the database with a version history. Saving never calls a model.
Saving from a chat message copies the message text (or one of its code blocks)
exactly as it was written.
"""

from __future__ import annotations

import json
import re

TYPES = {
    # type: (default extension, MIME type, label)
    "markdown": (".md", "text/markdown", "Document"),
    "text": (".txt", "text/plain", "Text"),
    "code": (".txt", "text/plain", "Code"),
    "html": (".html", "text/html", "HTML"),
    "svg": (".svg", "image/svg+xml", "SVG"),
    "json": (".json", "application/json", "JSON"),
    "csv": (".csv", "text/csv", "CSV"),
}
LANG_EXT = {
    "python": ".py", "py": ".py", "javascript": ".js", "js": ".js", "typescript": ".ts", "ts": ".ts",
    "tsx": ".tsx", "jsx": ".jsx", "lua": ".lua", "luau": ".luau", "css": ".css", "bash": ".sh", "sh": ".sh",
    "shell": ".sh", "powershell": ".ps1", "ps1": ".ps1", "bat": ".bat", "sql": ".sql", "java": ".java",
    "kotlin": ".kt", "c": ".c", "cpp": ".cpp", "c++": ".cpp", "csharp": ".cs", "cs": ".cs", "c#": ".cs",
    "go": ".go", "rust": ".rs", "rs": ".rs", "ruby": ".rb", "php": ".php", "yaml": ".yml", "yml": ".yml",
    "toml": ".toml", "xml": ".xml", "ini": ".ini", "dockerfile": ".dockerfile",
}
# Code-block languages that are really another artifact type.
LANG_TYPE = {"html": "html", "svg": "svg", "json": "json", "csv": "csv", "markdown": "markdown", "md": "markdown",
             "text": "text", "txt": "text", "plaintext": "text"}
MAX_TITLE = 200
MAX_CONTENT = 2_000_000
MAX_NOTE = 300


class ArtifactError(ValueError):
    pass


class Conflict(Exception):
    """The artifact changed since the editor loaded it."""

    def __init__(self, current: dict):
        super().__init__("This artifact was changed elsewhere since you opened it.")
        self.current = current


def clean_title(title: str) -> str:
    t = re.sub(r"[\x00-\x1f]", " ", str(title or "")).strip()
    if not t:
        raise ArtifactError("Give the artifact a title.")
    return t[:MAX_TITLE]


def check(type_: str, content: str, language: str = "") -> tuple[str, str, str]:
    if type_ not in TYPES:
        raise ArtifactError(f"Unknown artifact type: {type_}.")
    if not isinstance(content, str):
        raise ArtifactError("Content must be text.")
    if len(content) > MAX_CONTENT:
        raise ArtifactError(f"The content is too long ({len(content):,} characters; limit {MAX_CONTENT:,}).")
    language = re.sub(r"[^\w+#.-]", "", str(language or "")).lower()[:30] if type_ == "code" else ""
    return type_, content, language


def code_blocks(text: str) -> list[tuple[str, str]]:
    """Fenced code blocks in a message, in the order the chat shows them.

    Mirrors the chat renderer: Enoy's <<<FILE / <<<EDIT change blocks are not
    code blocks, so fences inside them are skipped.
    """
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^<<<(FILE|EDIT)\s+.+$", line.strip()):
            i += 1
            while i < len(lines) and lines[i].strip() != ">>>END":
                i += 1
            i += 1
            continue
        fence = re.match(r"^\s*```\s*([\w+#.-]*)", line)
        if fence:
            buf = []
            i += 1
            while i < len(lines) and not re.match(r"^\s*```", lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append((fence.group(1).lower(), "\n".join(buf)))
            continue
        i += 1
    return out


def from_message(text: str, block: int | None = None) -> dict:
    """Type, language, content and a suggested title for saving a chat message. No model call."""
    if block is not None:
        blocks = code_blocks(text)
        if not 0 <= block < len(blocks):
            raise ArtifactError("That code block does not exist in the message.")
        lang, code = blocks[block]
        type_ = LANG_TYPE.get(lang, "code")
        return {"type": type_, "language": lang if type_ == "code" else "", "content": code,
                "title": suggest_title(code, f"{lang or 'Code'} snippet")}
    return {"type": "markdown", "language": "", "content": text, "title": suggest_title(text, "Saved answer")}


def suggest_title(text: str, fallback: str) -> str:
    for line in str(text or "").splitlines():
        s = line.strip().lstrip("#").strip().strip("*").strip()
        if s and not s.startswith("```") and not s.startswith("<"):
            return s[:80]
    return fallback


def export(art: dict) -> tuple[str, str, bytes]:
    """File name, MIME type and bytes. Refuses to label content as a format it is not."""
    type_ = art["type"]
    ext, mime, _ = TYPES[type_]
    if type_ == "code":
        ext = LANG_EXT.get(art.get("language") or "", ".txt")
    content = art["content"]
    if type_ == "json":
        try:
            json.loads(content)
        except ValueError as exc:
            raise ArtifactError(f"This is not valid JSON ({exc}). Fix it, or change the type to Text to export it.") from exc
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", art["title"]).strip().strip(".")[:80] or "artifact"
    if base.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        base = "_" + base
    return base + ext, mime, content.encode("utf-8")
