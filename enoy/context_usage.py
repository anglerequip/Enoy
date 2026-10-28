"""Context-window readings based only on provider-reported request usage."""

from __future__ import annotations

import re
from typing import Any


# Current Enoy stack models. Custom or unknown models deliberately have no
# guessed capacity; the UI omits a percentage when a reliable denominator is
# unavailable.
CODEX_CONTEXT_WINDOWS = {
    "gpt-6-astra": 1_050_000,
    "gpt-6.1-sol": 1_050_000,
    "gpt-6-sol": 1_050_000,
    "gpt-6.1-terra": 1_050_000,
    "gpt-6-luna": 1_050_000,
}
CLAUDE_CONTEXT_WINDOWS = {
    "claude-opus-5-5": 1_000_000,
    "claude-sonnet-5-5": 1_000_000,
    "claude-haiku-5-5": 1_000_000,
    # Claude Code resolves these aliases to the current Sonnet/Opus models.
    "sonnet": 1_000_000,
    "opus": 1_000_000,
}


def _number(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def context_reading(provider: str, model: str, usage: dict[str, Any]) -> dict[str, Any] | None:
    """Return request input usage and its percent of the known model window.

    If the provider supplies an explicit context usage structure, prefer it.
    Otherwise use the adapter's reported input counters. This is an estimate
    of the latest Enoy request, not a claim about hidden CLI/tool overhead or
    subscription quota.
    """
    if not isinstance(usage, dict):
        return None

    explicit = usage.get("context_window") or usage.get("contextWindow")
    if not isinstance(explicit, dict):
        explicit = {}
    used = next((_number(explicit.get(k)) for k in
                 ("used_tokens", "total_input_tokens", "input_tokens", "usedTokens", "totalInputTokens")
                 if _number(explicit.get(k)) is not None), None)
    size = next((_number(explicit.get(k)) for k in
                 ("context_window_size", "contextWindowSize", "model_context_window", "modelContextWindow")
                 if _number(explicit.get(k)) is not None), None)
    percent = next((float(explicit[k]) for k in ("used_percentage", "usedPercentage")
                    if isinstance(explicit.get(k), (int, float)) and not isinstance(explicit.get(k), bool)
                    and 0 <= explicit[k] <= 100), None)

    if used is None:
        if provider == "claude":
            fields = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            values = [_number(usage.get(k)) for k in fields]
            if any(v is not None for v in values):
                used = sum(v or 0 for v in values)
        else:
            used = next((_number(usage.get(k)) for k in ("input_tokens", "total_input_tokens")
                         if _number(usage.get(k)) is not None), None)

    if size is None:
        model_id = re.split(r"[/ :]", str(model or "").strip().lower())[-1]
        capacities = CLAUDE_CONTEXT_WINDOWS if provider == "claude" else CODEX_CONTEXT_WINDOWS
        size = capacities.get(model_id)

    if percent is None and used is not None and size:
        percent = min(100.0, used * 100.0 / size)
    if used is None or not size or percent is None:
        return None
    return {"used_tokens": used, "window_tokens": size, "used_percent": round(percent, 1),
            "basis": "provider input usage for the latest Enoy request"}
