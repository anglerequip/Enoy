"""Project folder access: safe paths, file tree, search, indexing, retrieval.

All file access by the agent goes through Workspace, which keeps paths inside
the project root, refuses sensitive files, and skips generated folders.

Note: this is an application-level guard, not an operating-system sandbox.
Commands that Enoy runs (after approval) execute with the user's normal
permissions.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "dist", "build", "out",
    ".next", ".nuxt", ".turbo", ".cache", "target", "bin", "obj", ".idea", ".vs",
    ".vscode", "coverage", ".fusion", "Packages", "DevPackages", ".gradle",
}

# Files the agent may never read, index, or send to a model.
SENSITIVE_GLOBS = [
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.pfx", "*.p12", "*.keystore", "*.jks",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", "*.ppk", ".npmrc", ".pypirc", ".netrc",
    "_netrc", ".git-credentials", "credentials", "credentials.*", "*credentials*.json",
    "secrets.*", "*.secret", "*.secrets", "*.kdbx", "service-account*.json", "*.cookie",
    "cookies.sqlite", "Login Data", ".ROBLOSECURITY", "auth.json",
]

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".mp3", ".wav", ".ogg",
    ".mp4", ".mov", ".avi", ".zip", ".7z", ".rar", ".gz", ".tar", ".exe", ".dll", ".so",
    ".dylib", ".pdb", ".pyc", ".class", ".jar", ".pdf", ".rbxl", ".rbxm", ".rbxlx",
    ".rbxmx", ".fbx", ".obj", ".blend", ".ttf", ".otf", ".woff", ".woff2", ".db",
    ".sqlite", ".bin", ".dat", ".psd", ".glb", ".gltf",
}

INSTRUCTION_FILES = ["FUSION.md", ".fusion/instructions.md", "CLAUDE.md", "AGENTS.md"]

WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)),
}

MAX_READ_BYTES = 2_000_000

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "into", "file", "files", "code",
    "make", "add", "fix", "please", "can", "you", "should", "would", "when", "what", "how",
    "why", "use", "using", "new", "all", "are", "not", "but", "have", "has", "need", "want",
    "function", "explain", "change", "update", "write", "create", "does", "about", "there",
}


class PathError(PermissionError):
    """A path was rejected by the workspace guard."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class FileInfo:
    path: str          # Project-relative, forward slashes.
    size: int
    mtime: float
    symbols: list[str] = field(default_factory=list)


class Workspace:
    def __init__(self, root: str | Path, extra_excluded_globs: list[str] | None = None):
        root_path = Path(root)
        if not root_path.is_dir():
            raise FileNotFoundError(f"Project folder does not exist: {root}")
        self.root = Path(os.path.realpath(root_path))
        self.extra_excluded_globs = list(extra_excluded_globs or [])
        self._index: dict[str, FileInfo] | None = None

    @property
    def name(self) -> str:
        return self.root.name

    # Path safety -----------------------------------------------------------

    def resolve(self, rel: str) -> Path:
        """Turn a project-relative path into an absolute path inside the root.

        Rejects absolute paths, drive letters, UNC paths, '..' escapes, NTFS
        alternate data streams, reserved device names and symlinks or
        junctions that point outside the project.
        """
        if not isinstance(rel, str) or not rel.strip():
            raise PathError("Empty path")
        cleaned = rel.strip().replace("\\", "/")
        if cleaned.startswith("/") or cleaned.startswith("//") or re.match(r"^[A-Za-z]:", cleaned):
            raise PathError(f"Absolute paths are not allowed: {rel}")
        parts = PurePosixPath(cleaned).parts
        for part in parts:
            if part == "..":
                raise PathError(f"Path leaves the project folder: {rel}")
            if ":" in part:
                raise PathError(f"Invalid character ':' in path: {rel}")
            if part.split(".")[0].lower() in WINDOWS_RESERVED:
                raise PathError(f"Reserved Windows device name in path: {rel}")
        candidate = self.root.joinpath(*parts)
        real = Path(os.path.realpath(candidate))
        try:
            real.relative_to(self.root)
        except ValueError:
            raise PathError(f"Path resolves outside the project folder (symlink or junction): {rel}") from None
        return real

    def rel(self, absolute: Path) -> str:
        return absolute.relative_to(self.root).as_posix()

    def is_sensitive(self, rel: str) -> bool:
        name = PurePosixPath(rel.replace("\\", "/")).name
        lowered = name.lower()
        for pattern in SENSITIVE_GLOBS:
            if fnmatch.fnmatch(lowered, pattern.lower()):
                # Allow harmless templates such as .env.example.
                if lowered.endswith((".example", ".sample", ".template")):
                    return False
                return True
        return False

    def is_excluded(self, rel: str) -> bool:
        parts = PurePosixPath(rel).parts
        if any(p in EXCLUDED_DIRS for p in parts[:-1]):
            return True
        return any(fnmatch.fnmatch(rel, g) for g in self.extra_excluded_globs)

    @staticmethod
    def is_binary_name(rel: str) -> bool:
        return PurePosixPath(rel).suffix.lower() in BINARY_EXTENSIONS

    # Reading -----------------------------------------------------------------

    def read_text(self, rel: str) -> tuple[str, str]:
        """Return (content, sha256). Raises PathError for refused files."""
        path = self.resolve(rel)
        rel_norm = self.rel(path)
        if self.is_sensitive(rel_norm):
            raise PathError(f"'{rel_norm}' looks like a credentials or secrets file. Enoy does not read these.")
        if not path.is_file():
            raise FileNotFoundError(rel_norm)
        if path.stat().st_size > MAX_READ_BYTES:
            raise PathError(f"'{rel_norm}' is larger than {MAX_READ_BYTES // 1_000_000} MB.")
        data = path.read_bytes()
        if b"\x00" in data[:8192] or self.is_binary_name(rel_norm):
            raise PathError(f"'{rel_norm}' is a binary file.")
        text = data.decode("utf-8", errors="replace")
        return text, sha256_bytes(data)

    def current_hash(self, rel: str) -> str | None:
        path = self.resolve(rel)
        if not path.is_file():
            return None
        return sha256_bytes(path.read_bytes())

    # Tree and index ------------------------------------------------------------

    def walk(self):
        """Yield project-relative paths of indexable files."""
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".git"))
            base = Path(dirpath)
            # Do not follow directory symlinks/junctions out of the project.
            kept = []
            for d in dirnames:
                full = base / d
                real = Path(os.path.realpath(full))
                try:
                    real.relative_to(self.root)
                    kept.append(d)
                except ValueError:
                    continue
            dirnames[:] = kept
            for fname in sorted(filenames):
                rel = self.rel(base / fname)
                if self.is_excluded(rel):
                    continue
                yield rel

    def tree(self, max_entries: int = 3000) -> dict:
        """Nested tree for the file explorer. Sensitive files are flagged."""
        root: dict = {"name": self.name, "path": "", "type": "dir", "children": []}
        index: dict[str, dict] = {"": root}
        count = 0
        truncated = False
        for rel in self.walk():
            if count >= max_entries:
                truncated = True
                break
            count += 1
            parts = rel.split("/")
            parent = ""
            for i, part in enumerate(parts[:-1]):
                key = "/".join(parts[: i + 1])
                if key not in index:
                    node = {"name": part, "path": key, "type": "dir", "children": []}
                    index[parent]["children"].append(node)
                    index[key] = node
                parent = key
            index[parent]["children"].append({
                "name": parts[-1],
                "path": rel,
                "type": "file",
                "sensitive": self.is_sensitive(rel),
                "binary": self.is_binary_name(rel),
            })
        return {"tree": root, "count": count, "truncated": truncated}

    def build_index(self) -> dict[str, FileInfo]:
        index: dict[str, FileInfo] = {}
        for rel in self.walk():
            if self.is_sensitive(rel) or self.is_binary_name(rel):
                continue
            path = self.root / rel
            try:
                st = path.stat()
            except OSError:
                continue
            info = FileInfo(rel, st.st_size, st.st_mtime)
            if st.st_size <= 400_000:
                try:
                    info.symbols = extract_symbols(path.read_text(encoding="utf-8", errors="replace"), rel)
                except OSError:
                    pass
            index[rel] = info
        self._index = index
        return index

    @property
    def index(self) -> dict[str, FileInfo]:
        if self._index is None:
            self.build_index()
        return self._index

    def invalidate(self) -> None:
        self._index = None

    # Search ---------------------------------------------------------------------

    def search(self, query: str, max_results: int = 200) -> list[dict]:
        """Case-insensitive search of file names and contents."""
        q = query.lower().strip()
        if not q:
            return []
        results: list[dict] = []
        for rel, info in self.index.items():
            if q in rel.lower():
                results.append({"path": rel, "line": 0, "text": "(file name match)"})
            if info.size > 1_000_000:
                continue
            try:
                text = (self.root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if q in line.lower():
                    results.append({"path": rel, "line": n, "text": line.strip()[:200]})
                    if len(results) >= max_results:
                        return results
        return results

    # Instructions and project facts ---------------------------------------------

    def instructions(self) -> list[tuple[str, str]]:
        """Project instruction files, in priority order."""
        found = []
        for name in INSTRUCTION_FILES:
            try:
                text, _ = self.read_text(name)
            except (FileNotFoundError, PathError):
                continue
            found.append((name, text[:8000]))
        return found

    def detect(self) -> dict:
        """Languages, frameworks and test tools present in the project."""
        exts: dict[str, int] = {}
        for rel in self.index:
            ext = PurePosixPath(rel).suffix.lower()
            exts[ext] = exts.get(ext, 0) + 1
        facts = {
            "languages": sorted(exts.items(), key=lambda kv: -kv[1])[:8],
            "roblox": any(r.endswith((".luau", ".lua")) for r in self.index)
            or any(r.endswith(".project.json") for r in self.index),
            "git": (self.root / ".git").exists(),
            "test_commands": self.suggest_test_commands(),
        }
        return facts

    def suggest_test_commands(self) -> list[str]:
        commands = []
        idx = self.index
        has_py_tests = any(
            PurePosixPath(r).name.startswith("test_") and r.endswith(".py") or r.endswith("_test.py")
            for r in idx
        )
        if has_py_tests or "pytest.ini" in idx or "pyproject.toml" in idx:
            commands.append("python -m pytest -q")
        if "package.json" in idx:
            try:
                pkg = json.loads((self.root / "package.json").read_text(encoding="utf-8"))
                scripts = pkg.get("scripts", {})
                if "test" in scripts:
                    commands.append("npm test")
                if "lint" in scripts:
                    commands.append("npm run lint")
            except (OSError, ValueError):
                pass
            if "tsconfig.json" in idx:
                commands.append("npx tsc --noEmit")
        if "selene.toml" in idx:
            commands.append("selene .")
        if "stylua.toml" in idx or ".stylua.toml" in idx:
            commands.append("stylua --check .")
        return commands

    # Retrieval -------------------------------------------------------------------

    def relevant_files(self, text: str, max_files: int = 8) -> list[tuple[str, float]]:
        """Rank files by how well they match the words in a task description.

        Plain keyword scoring: explicit path mentions first, then file-name and
        symbol matches, then content hits. Deterministic and testable.
        """
        lowered = text.lower()
        words = {w.lower() for w in _WORD.findall(text)} - _STOP
        scores: dict[str, float] = {}
        for rel, info in self.index.items():
            score = 0.0
            rel_lower = rel.lower()
            name = PurePosixPath(rel_lower).name
            stem = PurePosixPath(rel_lower).stem
            if rel_lower in lowered or (len(name) > 4 and name in lowered):
                score += 100
            for w in words:
                if w == stem:
                    score += 12
                elif w in stem:
                    score += 6
                elif w in rel_lower:
                    score += 3
                if any(w == s.lower() for s in info.symbols):
                    score += 8
                elif any(w in s.lower() for s in info.symbols):
                    score += 2
            if score == 0 and words and info.size <= 300_000:
                try:
                    content = (self.root / rel).read_text(encoding="utf-8", errors="replace").lower()
                except OSError:
                    content = ""
                hits = sum(min(content.count(w), 5) for w in words)
                score += hits * 0.5
            if score > 0:
                scores[rel] = score
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[:max_files]


_SYMBOL_PATTERNS = {
    ".py": re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", re.M),
    ".js": re.compile(r"(?:function\s+([A-Za-z_$][\w$]*)|class\s+([A-Za-z_$][\w$]*)|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\()", re.M),
    ".lua": re.compile(r"(?:function\s+([\w.:]+)|local\s+function\s+(\w+)|local\s+(\w+)\s*=\s*\{)", re.M),
}
_SYMBOL_PATTERNS[".ts"] = _SYMBOL_PATTERNS[".tsx"] = _SYMBOL_PATTERNS[".jsx"] = _SYMBOL_PATTERNS[".mjs"] = _SYMBOL_PATTERNS[".js"]
_SYMBOL_PATTERNS[".luau"] = _SYMBOL_PATTERNS[".lua"]


def extract_symbols(text: str, rel: str) -> list[str]:
    pattern = _SYMBOL_PATTERNS.get(PurePosixPath(rel).suffix.lower())
    if not pattern:
        return []
    symbols = []
    for match in pattern.finditer(text):
        name = next((g for g in match.groups() if g), None)
        if name:
            symbols.append(name.split(".")[-1].split(":")[-1])
    return symbols[:200]
