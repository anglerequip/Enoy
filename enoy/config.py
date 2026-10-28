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
                 'extra_excluded_globs': []},
 # Build workspace (planning.md). Build providers must run on this PC, so they never cost money;
 # only the signed-in Claude and Codex apps reach the cloud, on the user's plans.
 'build': {'providers': {'ollama': {'kind': 'ollama', 'base_url': 'http://127.0.0.1:11434', 'enabled': False,
                                    'local': True, 'max_concurrency': 1, 'daily_limit_usd': 0.0,
                                    'monthly_limit_usd': 0.0, 'timeout_s': 1800}},
           # "model": "" means the CLI's own configured model.
           'models': [{'id': 'claude-cli', 'provider': 'claude_cli', 'model': '', 'roles': ['planner', 'reviewer']},
                      {'id': 'codex-cli', 'provider': 'codex_cli', 'model': '', 'roles': ['planner', 'reviewer']}],
           'roles': {'planner': 'claude-cli', 'implementer': '', 'reviewer': 'codex-cli'},
           'parallel': False,
           # Local models stay under 2,000 MiB: chill 1,200 / medium ("Middle") 1,600 / full 1,900 (localguard.py).
           'local_memory': 'medium'}}

BUILD_ROLES = ('planner', 'implementer', 'reviewer')
CLI_PROVIDERS = ('claude_cli', 'codex_cli')
_NUM = (int, float)
_PROVIDER_FIELDS = {'kind': (str,), 'base_url': (str,), 'enabled': (bool,), 'local': (bool,), 'max_concurrency': (int,),
                    'daily_limit_usd': _NUM, 'monthly_limit_usd': _NUM, 'timeout_s': _NUM,
                    'api_version': (str,), 'account_id': (str,)}
_MODEL_FIELDS = {'id': (str,), 'provider': (str,), 'model': (str,), 'roles': (list,),
                 'price_in': _NUM + (type(None),), 'price_cached': _NUM + (type(None),),
                 'price_out': _NUM + (type(None),), 'context': (int, type(None)), 'max_output': (int, type(None))}
_SECRETISH = re.compile(r'(?i)key|secret|token|passw|credential|authoriz|bearer')
_LOCAL_HOSTS = ('127.0.0.1', 'localhost', '[::1]')


def _is(v: Any, types: tuple) -> bool:
    if isinstance(v, bool) and bool not in types:
        return False
    return isinstance(v, types)


def url_host(url: str) -> str | None:
    """The lower-cased host of an http(s) address, or None if it is not one."""
    m = re.fullmatch(r'https?://(\[[^\]]+\]|[^/:\s@]+)(:\d+)?(/\S*)?', url or '')
    return m.group(1).lower() if m else None


def runs_on_this_pc(url: str) -> bool:
    return url_host(url) in _LOCAL_HOSTS


def _provider_problem(pid: str, p: Any) -> str | None:
    if not re.fullmatch(r'[a-z0-9_-]{1,40}', pid) or pid in CLI_PROVIDERS:
        return f"provider id '{pid}' is not allowed"
    if not isinstance(p, dict):
        return f"provider '{pid}' must be an object"
    for k, v in p.items():
        if k != 'api_version' and _SECRETISH.search(k):
            return f"provider '{pid}' must not contain '{k}'; keys are kept in Windows Credential Manager"
        if k not in _PROVIDER_FIELDS:
            return f"provider '{pid}' has an unknown setting '{k}'"
        if not _is(v, _PROVIDER_FIELDS[k]):
            return f"provider '{pid}' setting '{k}' has the wrong type"
    if p.get('kind') not in ('openai', 'ollama'):
        return f"provider '{pid}' kind must be openai or ollama"
    host = url_host(p.get('base_url', ''))
    if host is None:
        return f"provider '{pid}' base_url is not a web address"
    if host not in _LOCAL_HOSTS:
        return (f"provider '{pid}' points at {host}, which is not this PC; Build only uses free models "
                "that run on this PC")
    if not 1 <= p.get('max_concurrency', 1) <= 8:
        return f"provider '{pid}' max_concurrency must be 1-8"
    if p.get('daily_limit_usd', 0) < 0 or p.get('monthly_limit_usd', 0) < 0:
        return f"provider '{pid}' spending limits cannot be negative"
    if not 10 <= p.get('timeout_s', 600) <= 7200:
        return f"provider '{pid}' timeout_s must be 10-7200"
    return None


def _check_providers(v: dict) -> str | None:
    for pid, p in v.items():
        problem = _provider_problem(pid, p)
        if problem:
            return problem
    return None


def _check_models(v: list) -> str | None:
    seen = set()
    for m in v:
        if not isinstance(m, dict) or not {'id', 'provider'} <= set(m):
            return 'every model needs an id and a provider'
        for k, val in m.items():
            if k not in _MODEL_FIELDS or not _is(val, _MODEL_FIELDS[k]):
                return f"model '{m.get('id')}' setting '{k}' is not allowed or has the wrong type"
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,60}', m['id']) or m['id'] in seen:
            return f"model id '{m['id']}' is invalid or used twice"
        seen.add(m['id'])
        if not set(m.get('roles', [])) <= set(BUILD_ROLES):
            return f"model '{m['id']}' roles must be from {', '.join(BUILD_ROLES)}"
        if any((m.get(k) or 0) < 0 for k in ('price_in', 'price_cached', 'price_out')):
            return f"model '{m['id']}' prices cannot be negative"
    return None


def _check_roles(v: dict) -> str | None:
    if not set(v) <= set(BUILD_ROLES) or not all(isinstance(x, str) for x in v.values()):
        return 'roles must map planner, implementer and reviewer to model ids'
    return None


def _drop_cloud_providers(raw: dict, warnings: list[str]) -> dict:
    """Older versions shipped paid API providers (DeepSeek, Fireworks, Anthropic). Drop every provider
    that does not run on this PC one by one, so the user's local providers survive."""
    build = raw.get('build')
    providers = build.get('providers') if isinstance(build, dict) else None
    if not isinstance(providers, dict):
        return raw
    kept = {pid: p for pid, p in providers.items() if not isinstance(p, dict) or runs_on_this_pc(p.get('base_url', ''))}
    if len(kept) == len(providers):
        return raw
    for pid in providers.keys() - kept.keys():
        warnings.append(f"Build provider '{pid}' does not run on this PC and could cost money; it was removed.")
    build = {k: v for k, v in build.items() if k != 'providers'} | ({'providers': kept} if kept else {})
    return raw | {'build': build}


def _check_build_links(cfg: dict, warnings: list[str]) -> None:
    """Models must name a known provider; each role must name a model allowed to play it."""
    b = cfg['build']
    known = set(b['providers']) | set(CLI_PROVIDERS)
    kept = []
    for m in b['models']:
        if m['provider'] in known:
            kept.append(m)
        else:
            warnings.append(f"Model '{m['id']}' uses unknown provider '{m['provider']}'; it was removed.")
    b['models'] = kept
    models = {m['id']: m for m in kept}
    roles = {r: '' for r in BUILD_ROLES} | b['roles']
    for role, mid in roles.items():
        if mid and (mid not in models or role not in models[mid].get('roles', [])):
            warnings.append(f"Build role '{role}' points at '{mid}', which is not a model allowed for that role.")
            roles[role] = ''
    b['roles'] = roles

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
    "build": {"providers": (dict, _check_providers), "models": (list, _check_models), "roles": (dict, _check_roles),
              "parallel": (bool, None), "local_memory": (str, _choice(("chill", "medium", "full")))},
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
    raw = _drop_cloud_providers(raw, warnings)
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
    for p in cfg['build']['providers'].values():  # Fill settings a hand-written provider left out.
        for k, v in (('enabled', False), ('max_concurrency', 1), ('daily_limit_usd', 0.0),
                     ('monthly_limit_usd', 0.0), ('timeout_s', 600)):
            p.setdefault(k, v)
        p['local'] = True  # Every Build provider runs on this PC (checked above); the stored flag is not trusted.
    _check_build_links(cfg, warnings)
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
