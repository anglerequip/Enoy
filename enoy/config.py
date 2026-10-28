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

ROUTING_MODES = ("auto", "manual")
AGGRESSIVENESS = ("balanced",)
CLOUD_POLICIES = ("auto", "never")
PC_PROFILES = ("observe", "assist", "automation")
CLAUDE_EFFORT = ("", "low", "medium", "high", "xhigh", "max")
WORKFLOWS = ("chatgpt_leads",)

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

DEFAULTS: dict[str, Any] = {'coordinator': {'enabled': True, 'claude_reviews': True, 'claude_complex': True},
 'chat': {'experience': 'chatgpt'},
 'blender': {'enabled': False, 'server_executable': '', 'host': '127.0.0.1', 'port': 9876},
 'agent': {'native_tools': True, 'access_mode': 'workspace', 'self_check': True},
 'codex': {'enabled': True, 'executable': '', 'model': 'gpt-6.1-sol', 'effort': 'medium', 'timeout_s': 1800,
           'adaptive_routing': True, 'execution_model': 'gpt-6-luna', 'execution_effort': 'low',
           'chat_model': 'gpt-6-luna', 'chat_effort': 'low', 'model_stack_revision': 2},
 'claude': {'enabled': True,
            'executable': '',
            'model': 'claude-opus-5-5',
            'effort': 'medium',
            'timeout_s': 600,
            'confirm_before_send': False,
            'safe_mode': True},
 'routing': {'max_attempts': 2, 'failure_escalation': True},
 'lead': {'final_review': True, 'max_followups': 1},
 'team': {'default_new_chats': False,
          'max_tasks': 6,
          'max_parallel': 2,
          'max_repair_rounds': 2,
          'max_model_calls': 20,
          'max_claude_calls': 6,
          'review_min_lines': 60},
 'pc': {'profile': 'observe', 'hardware_read': True, 'approved_apps': []},
 'tools': {'enabled': True,
           'online': True,
           'permissions': {'web': True},
           'enabled_tools': {},
           'timeouts': {},
           'max_rounds': 5,
           'search_provider': 'auto',
           'brave_monthly_limit': 900,
           'searxng_url': '',
           'wikipedia_lang': 'en',
           'contact': '',
           'page_chars': 8000,
           'claude_web': True,
           'claude_enoy_mcp': False,
           'claude_files': True},
 'privacy': {'cloud_policy': 'auto', 'cloud_excluded_globs': []},
 'context': {'max_files': 8, 'max_chars': 40000, 'max_file_chars': 14000, 'history_tasks': 5},
 'permissions': {'auto_apply_patches': False,
                 'test_after_apply': True,
                 'command_allowlist': ['python -m pytest',
                                       'pytest',
                                       'python -m unittest',
                                       'python -m py_compile',
                                       'npm test',
                                       'npm run test',
                                       'npm run lint',
                                       'npx tsc --noEmit',
                                       'git status',
                                       'git diff',
                                       'git log --oneline',
                                       'selene .',
                                       'stylua --check .'],
                 'command_timeout_s': 300,
                 'extra_excluded_globs': []}}

LEAD_STYLES = ("claude_first", "balanced")


# (type, check) per leaf. check returns an error string or None.
def _range(lo: float, hi: float):
    return lambda v: None if lo <= v <= hi else f"must be between {lo} and {hi}"


def _choice(options):
    return lambda v: None if v in options else f"must be one of {', '.join(options)}"


def _url(v: str):
    return None if v.startswith(("http://", "https://")) else "must start with http:// or https://"


SCHEMA: dict[str, dict[str, tuple[type | tuple, Any]]] = {
    "blender": {"enabled": (bool, None), "server_executable": (str, None),
                "host": (str, _choice(("127.0.0.1", "localhost", "::1"))),
                "port": (int, _range(1, 65535))},
    "agent": {"native_tools": (bool, None), "access_mode": (str, _choice(("workspace", "full"))),
              "self_check": (bool, None)},
    "coordinator": {"enabled": (bool, None), "claude_reviews": (bool, None), "claude_complex": (bool, None)},
    "chat": {"experience": (str, _choice(("chatgpt", "integrated")))},
    "codex": {
        "enabled": (bool, None),
        "executable": (str, None),
        "model": (str, None),
        "effort": (str, _choice(("", "low", "medium", "high", "xhigh", "max", "ultra"))),
        "adaptive_routing": (bool, None),
        "execution_model": (str, None),
        "execution_effort": (str, _choice(("", "low", "medium", "high", "xhigh", "max", "ultra"))),
        "chat_model": (str, None),
        "chat_effort": (str, _choice(("", "low", "medium", "high", "xhigh", "max"))),
        "model_stack_revision": (int, _choice((2,))),
        "timeout_s": ((int, float), _range(10, 7200)),
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
    "routing": {"max_attempts": (int, _range(1, 5)), "failure_escalation": (bool, None)},
    "lead": {"final_review": (bool, None), "max_followups": (int, _range(0, 3))},
    "team": {
        "default_new_chats": (bool, None),
        "max_tasks": (int, _range(1, 12)),
        "max_parallel": (int, _range(1, 4)),
        "max_repair_rounds": (int, _range(0, 5)),
        "max_model_calls": (int, _range(1, 100)),
        "max_claude_calls": (int, _range(0, 50)),
        "review_min_lines": (int, _range(0, 5000)),
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
        "claude_enoy_mcp": (bool, None),
        "claude_files": (bool, None),
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
    # One-time migration of the previous shipped defaults. A missing stack
    # revision distinguishes older configs; only replace the known defaults.
    codex_raw = raw.get('codex', {}) if isinstance(raw.get('codex'), dict) else {}
    if 'model_stack_revision' not in codex_raw:
        if codex_raw.get('execution_model', 'gpt-6-luna') == 'gpt-6-luna' and codex_raw.get('execution_effort', 'high') == 'high':
            cfg['codex']['execution_effort'] = 'low'
        claude_raw = raw.get('claude', {}) if isinstance(raw.get('claude'), dict) else {}
        if claude_raw.get('model', 'claude-opus-5-5') == 'claude-opus-5-5' and claude_raw.get('effort', 'high') == 'high':
            cfg['claude']['effort'] = 'medium'
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
