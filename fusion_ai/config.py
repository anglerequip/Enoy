"""Configuration: defaults, validation, loading and saving with backups.

The config is a plain nested dict stored as JSON. Every setting has a type and,
where it matters, an allowed range or set of choices. Invalid values are
replaced by defaults and reported as warnings instead of crashing the app.
"""

from __future__ import annotations

import copy
import re
import json
import shutil
import time
from pathlib import Path
from typing import Any

from . import paths

ROUTING_MODES = ("auto", "local_first", "claude_first", "manual")
AGGRESSIVENESS = ("prefer_qwen", "balanced", "prefer_claude")
CLOUD_POLICIES = ("auto", "never")
MEMORY_MODES = ("chill", "medium", "full")   # Chill / Middle / Full: memory_guard.MODES (1,200 / 1,600 / 1,900 MiB).
PC_PROFILES = ("observe", "assist", "automation")
CLAUDE_EFFORT = ("", "low", "medium", "high", "xhigh", "max")  # "" = Claude Code's own default.
WORKFLOWS = (
    "local_only",          # Qwen only, never escalate.
    "escalate",            # Qwen first, Claude when objective signals say so.
    "local_then_review",   # Qwen implements, Claude reviews the diff.
    "claude_plans",        # Claude writes the plan, Qwen implements it.
    "compare",             # Both propose independently; the user picks one.
)

DEFAULT_ALLOWLIST = [
    "python -m pytest",
    "pytest",
    "python -m unittest",
    "python -m py_compile",
    "npm test",
    "npm run test",
    "npm run lint",
    "npx tsc --noEmit",
    "git status",
    "git diff",
    "git log --oneline",
    "selene .",
    "stylua --check .",
]

DEFAULTS: dict[str, Any] = {
    "ollama": {
        "endpoint": "http://127.0.0.1:11434",
        "model": "qwen3.5:9b",
        "num_ctx": 16384,
        "num_predict": 4096,
        "temperature": 0.6,
        "think": False,
        "keep_alive": "15m",
        "connect_timeout_s": 5,
        "request_timeout_s": 900,
    },
    "claude": {
        "enabled": True,
        "executable": "",
        "model": "claude-opus-5-5",     # Passed to Claude Code as --model. Empty = Claude Code's default.
        "effort": "high",               # Passed as --effort.
        "timeout_s": 600,
        "confirm_before_send": True,
        "safe_mode": True,
    },
    "routing": {
        "mode": "auto",                 # Smart router: Qwen or Claude per task, from the request itself.
        "workflow": "escalate",
        "max_local_attempts": 2,
        "max_escalations": 1,
        "max_review_rounds": 1,
        "max_test_fix_attempts": 2,
        "escalate_high_complexity": True,
        "aggressiveness": "balanced",
        "target_local_share": 70,       # Observed target only; never forces a model.
        "stats_window": 50,
        "failure_escalation": True,
        "manual_override": True,
        "log_decisions": True,
        "claude_fallback": "local",     # When Claude is wanted but unavailable: "local" or "stop".
        "max_handoffs": 2,
        "zero_paid_mode": False,        # Never use Claude automatically; only when you pick it yourself.
    },
    "lead": {
        "enabled": True,                # Claude reads each new message first: it answers, asks, or plans and hands
                                        # the code to Qwen and the team. Picking a model in the chat skips the lead.
        "final_review": True,           # After a delegated run, Claude checks the results and writes the answer.
        "max_followups": 1,             # Fix rounds Claude may order after its final check.
    },
    "team": {
        "default_new_chats": False,     # TEAM ROLES for new chats; each chat keeps its own setting.
        "max_tasks": 6,                 # Developer tasks per plan.
        "max_parallel": 2,              # Team tasks running at the same time (different models only).
        "max_repair_rounds": 2,
        "max_model_calls": 20,          # Per run, all models.
        "max_claude_calls": 6,          # Per run.
        "review_min_lines": 60,         # Added lines before a model review is added to the automatic checks.
    },
    "memory": {
        "mode": "chill",                # Default on a new install (1,200 MiB; Qwen 3.5 9B runs at a 4k context).
        "margin_percent": 10,           # Warning threshold below the ceiling (monitoring delay, spikes).
        "enforce": True,                # No longer used: enforcement is always on. Kept so old config files load.
        "os_cap": True,                 # Windows hard RAM cap (working-set maximum) on Ollama's model runner.
        "allow_terminate_runner": True, # Last resort: end Ollama's model runner if unloading fails.
        "unload_on_exit": True,         # Free the model's memory when Enoy closes.
    },
    "pc": {
        "profile": "observe",           # observe | assist | automation. Least privilege by default.
        "hardware_read": True,          # Read-only hardware inventory (CPU, RAM, GPU, disks, Windows).
        "approved_apps": [],            # [{"name": "Notepad", "path": "notepad.exe"}]; only these can be opened.
    },
    "tools": {
        "enabled": True,                # Models may ask Enoy to run tools (weather, web search, read a page).
        "online": True,                 # Tools that use the internet. Off: everything stays offline.
        "permissions": {"web": True},   # Granted permission groups. Tools with outside effects always ask first.
        "enabled_tools": {},            # {"web_search": False} turns one tool off.
        "timeouts": {},                 # {"fetch_page": 25} seconds, per tool.
        "max_rounds": 5,                # Tool rounds per answer before the model must answer.
        "search_provider": "auto",      # auto | brave | searxng | wikipedia
        "brave_monthly_limit": 900,     # Brave searches per calendar month; Brave's monthly free credit covers ~1,000.
        "searxng_url": "",              # Your SearXNG instance, e.g. http://localhost:8888 (JSON format enabled).
        "wikipedia_lang": "en",
        "contact": "",                  # Optional contact (e-mail or site) added to the User-Agent; MET Norway asks for one.
        "page_chars": 8000,             # Page text given to a model per fetch.
        "claude_web": True,             # Claude may use Claude Code's own WebSearch and WebFetch for live questions.
        "agent": True,                  # Qwen works as an agent: reads, searches, edits and runs tools in a loop.
        "agent_apply": True,            # Agent edits are written at once (backups, one Undo per turn). Off: one proposal at the end.
        "agent_max_steps": 14,          # Tool rounds per agent turn.
        "claude_enoy_mcp": False,       # Claude calls Enoy's tools itself (MCP). Needs Claude Code without --safe-mode;
                                        # off: Enoy reads the PC facts first and puts them in Claude's request.
    },
    "privacy": {
        "cloud_policy": "auto",         # "auto": permitted context may go to Claude; "never": everything stays local.
        "cloud_excluded_globs": [],     # Files Qwen may read but Claude never receives.
    },
    "context": {
        "max_files": 8,
        "max_chars": 40000,
        "max_file_chars": 14000,
        "history_tasks": 5,
    },
    "permissions": {
        "auto_apply_patches": False,
        "test_after_apply": True,       # After you apply a change, run the project's tests if that command is allowlisted.
        "command_allowlist": DEFAULT_ALLOWLIST,
        "command_timeout_s": 300,
        "extra_excluded_globs": [],
    },
}

# (type, check) per leaf. check returns an error string or None.
def _range(lo: float, hi: float):
    return lambda v: None if lo <= v <= hi else f"must be between {lo} and {hi}"


def _choice(options):
    return lambda v: None if v in options else f"must be one of {', '.join(options)}"


def _url(v: str):
    return None if v.startswith(("http://", "https://")) else "must start with http:// or https://"


SCHEMA: dict[str, dict[str, tuple[type | tuple, Any]]] = {
    "ollama": {
        "endpoint": (str, _url),
        "model": (str, lambda v: None if v.strip() else "must not be empty"),
        "num_ctx": (int, _range(2048, 262144)),
        "num_predict": (int, _range(128, 32768)),
        "temperature": ((int, float), _range(0, 2)),
        "think": (bool, None),
        "keep_alive": (str, None),
        "connect_timeout_s": ((int, float), _range(1, 120)),
        "request_timeout_s": ((int, float), _range(10, 7200)),
    },
    "claude": {
        "enabled": (bool, None),
        "executable": (str, None),
        "model": (str, None),
        "effort": (str, _choice(CLAUDE_EFFORT)),
        "timeout_s": ((int, float), _range(10, 7200)),
        "confirm_before_send": (bool, None),
        "safe_mode": (bool, None),
    },
    "routing": {
        "mode": (str, _choice(ROUTING_MODES)),
        "workflow": (str, _choice(WORKFLOWS)),
        "max_local_attempts": (int, _range(1, 5)),
        "max_escalations": (int, _range(0, 3)),
        "max_review_rounds": (int, _range(0, 3)),
        "max_test_fix_attempts": (int, _range(0, 5)),
        "escalate_high_complexity": (bool, None),
        "aggressiveness": (str, _choice(AGGRESSIVENESS)),
        "target_local_share": (int, _range(0, 100)),
        "stats_window": (int, _range(10, 500)),
        "failure_escalation": (bool, None),
        "manual_override": (bool, None),
        "log_decisions": (bool, None),
        "claude_fallback": (str, _choice(("local", "stop"))),
        "max_handoffs": (int, _range(1, 5)),
        "zero_paid_mode": (bool, None),
    },
    "lead": {
        "enabled": (bool, None),
        "final_review": (bool, None),
        "max_followups": (int, _range(0, 3)),
    },
    "team": {
        "default_new_chats": (bool, None),
        "max_tasks": (int, _range(1, 12)),
        "max_parallel": (int, _range(1, 4)),
        "max_repair_rounds": (int, _range(0, 5)),
        "max_model_calls": (int, _range(1, 100)),
        "max_claude_calls": (int, _range(0, 50)),
        "review_min_lines": (int, _range(0, 5000)),
    },
    "memory": {
        "mode": (str, _choice(MEMORY_MODES)),
        "margin_percent": (int, _range(5, 25)),
        "enforce": (bool, None),
        "os_cap": (bool, None),
        "allow_terminate_runner": (bool, None),
        "unload_on_exit": (bool, None),
    },
    "pc": {
        "profile": (str, _choice(PC_PROFILES)),
        "hardware_read": (bool, None),
        "approved_apps": (list, lambda v: None if all(isinstance(x, dict) and isinstance(x.get("name"), str)
                                                      and isinstance(x.get("path"), str) for x in v)
                          else "must be a list of {name, path}"),
    },
    "tools": {
        "enabled": (bool, None),
        "online": (bool, None),
        "permissions": (dict, lambda v: None if all(isinstance(k, str) for k in v) else "must be an object"),
        "enabled_tools": (dict, lambda v: None if all(isinstance(k, str) for k in v) else "must be an object"),
        "timeouts": (dict, lambda v: None if all(isinstance(k, str) for k in v) else "must be an object"),
        "max_rounds": (int, _range(1, 10)),
        "search_provider": (str, _choice(("auto", "brave", "searxng", "wikipedia"))),
        "brave_monthly_limit": (int, _range(0, 100000)),
        "searxng_url": (str, lambda v: None if not v else _url(v)),
        "wikipedia_lang": (str, lambda v: None if re.fullmatch(r"[a-z]{2,3}", v) else "must be a language code like en"),
        "contact": (str, lambda v: None if len(v) <= 120 else "is too long"),
        "page_chars": (int, _range(1000, 20000)),
        "claude_web": (bool, None),
        "agent": (bool, None),
        "agent_apply": (bool, None),
        "agent_max_steps": (int, _range(2, 40)),
        "claude_enoy_mcp": (bool, None),
    },
    "privacy": {
        "cloud_policy": (str, _choice(CLOUD_POLICIES)),
        "cloud_excluded_globs": (list, lambda v: None if all(isinstance(x, str) for x in v) else "must be a list of strings"),
    },
    "context": {
        "max_files": (int, _range(1, 40)),
        "max_chars": (int, _range(2000, 400000)),
        "max_file_chars": (int, _range(500, 200000)),
        "history_tasks": (int, _range(0, 30)),
    },
    "permissions": {
        "auto_apply_patches": (bool, None),
        "test_after_apply": (bool, None),
        "command_allowlist": (list, lambda v: None if all(isinstance(x, str) for x in v) else "must be a list of strings"),
        "command_timeout_s": ((int, float), _range(1, 7200)),
        "extra_excluded_globs": (list, lambda v: None if all(isinstance(x, str) for x in v) else "must be a list of strings"),
    },
}


class ConfigError(ValueError):
    pass


def validate(raw: Any) -> tuple[dict, list[str]]:
    """Merge raw over the defaults. Returns (config, warnings).

    Unknown keys are dropped with a warning; wrong types or out-of-range values
    fall back to the default value with a warning.
    """
    cfg = copy.deepcopy(DEFAULTS)
    warnings: list[str] = []
    if not isinstance(raw, dict):
        return cfg, ["Config file is not a JSON object; using defaults."]
    for section, value in raw.items():
        if section not in SCHEMA:
            warnings.append(f"Unknown section '{section}' ignored.")
            continue
        if not isinstance(value, dict):
            warnings.append(f"Section '{section}' must be an object; using defaults.")
            continue
        for key, item in value.items():
            if key not in SCHEMA[section]:
                warnings.append(f"Unknown setting '{section}.{key}' ignored.")
                continue
            typ, check = SCHEMA[section][key]
            # bool is a subclass of int; do not accept True for numeric settings.
            if isinstance(item, bool) and typ is not bool:
                ok = False
            else:
                ok = isinstance(item, typ)
            if not ok:
                warnings.append(f"'{section}.{key}' has the wrong type; using default.")
                continue
            if check is not None:
                problem = check(item)
                if problem:
                    warnings.append(f"'{section}.{key}' {problem}; using default.")
                    continue
            cfg[section][key] = item
    return cfg, warnings


def validate_strict(raw: Any) -> dict:
    """Like validate() but raises on any problem. Used for settings edits."""
    cfg, warnings = validate(raw)
    if warnings:
        raise ConfigError("; ".join(warnings))
    return cfg


def load(path: Path | None = None) -> tuple[dict, list[str]]:
    path = path or paths.config_path()
    if not path.exists():
        return copy.deepcopy(DEFAULTS), []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return copy.deepcopy(DEFAULTS), [f"Could not read config ({exc}); using defaults. The file was left untouched."]
    return validate(raw)


def backup(path: Path | None = None, keep: int = 15) -> Path | None:
    """Copy the current config file into the backups folder."""
    path = path or paths.config_path()
    if not path.exists():
        return None
    dest_dir = paths.config_backups_dir()
    dest = dest_dir / f"config-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1000000}.json"
    shutil.copy2(path, dest)
    old = sorted(dest_dir.glob("config-*.json"))
    for stale in old[:-keep]:
        stale.unlink(missing_ok=True)
    return dest


def save(cfg: dict, path: Path | None = None) -> None:
    """Validate, back up the previous file, then write atomically."""
    validate_strict(cfg)
    path = path or paths.config_path()
    backup(path)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(path)


def merge_update(cfg: dict, update: dict) -> dict:
    """Return a copy of cfg with the sections/keys in update applied."""
    merged = copy.deepcopy(cfg)
    for section, values in update.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section].update(values)
        else:
            merged[section] = values
    return merged
