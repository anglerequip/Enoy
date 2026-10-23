"""Secret detection and redaction for logs, model prompts and stored records."""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# Each pattern matches a secret value as a whole.
_TOKEN_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    # Roblox session cookie.
    re.compile(r"_\|WARNING:-DO-NOT-SHARE-THIS\.[^\s\"']+"),
]

# key = value / key: value assignments where the key name suggests a secret.
_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<key>\b[\w.\-]*(?:api[_\-]?key|secret|token|passwd|password|pwd|auth[_\-]?token|authorization|credential|private[_\-]?key|access[_\-]?key)[\w.\-]*)
    (?P<sep>\s*["']?\s*[:=]\s*)
    (?P<quote>["']?)
    (?P<value>[^\s"',;]{6,})
    """
)

_ENV_NAME = re.compile(r"(?i)(key|secret|token|passw|pwd|credential|auth|cookie|session)")


def redact(text: str) -> str:
    """Return text with recognisable secrets replaced by [REDACTED]."""
    if not text:
        return text
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)

    def _assignment(match: re.Match) -> str:
        value = match.group("value")
        if value == REDACTED or value.lower() in {"none", "null", "true", "false", "undefined"}:
            return match.group(0)
        if not match.group("quote") and not _looks_like_secret(value):
            # Unquoted right-hand sides are usually code (`tokens = text.split()`).
            return match.group(0)
        return f"{match.group('key')}{match.group('sep')}{match.group('quote')}{REDACTED}"

    return _ASSIGNMENT.sub(_assignment, text)


def _looks_like_secret(value: str) -> bool:
    return (
        len(value) >= 8
        and any(c.isdigit() for c in value)
        and any(c.isalpha() for c in value)
        and not any(c in value for c in "()[]{}")
    )


def contains_secret(text: str) -> bool:
    return redact(text) != text


def is_secret_env_name(name: str) -> bool:
    """True for environment variable names that probably hold credentials."""
    return bool(_ENV_NAME.search(name))
