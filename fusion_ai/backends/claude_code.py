"""Claude backend: the official Claude Code CLI in non-interactive (print) mode.

Why the CLI: it is Anthropic's supported way to use a Claude subscription from
other programs. `claude -p` runs one request and exits; `claude auth status`
reports sign-in without spending usage. Enoy never reads Claude's tokens
or cookies and never calls the API directly, so there are no separate API
charges.

Each call runs with all Claude Code tools disabled (`--tools ""`) from an empty
working directory: Claude only sees what Enoy puts in the prompt, and it
cannot edit files or run commands. Changes it proposes go through the same
validation and approval as Qwen's. The one exception is a request marked
`web_tools`: then only Claude Code's own WebSearch and WebFetch are enabled
(nothing that touches this PC), and the searches and pages it used are read
from the stream-json output so Enoy can show them.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .. import paths
from .base import CLAUDE, BackendError, BackendStatus, ChatRequest, ChatResult

LIMIT_MARKERS = ("usage limit", "limit reached", "rate limit", "out of extra usage", "limit will reset", "resets at")
AUTH_MARKERS = ("not logged in", "please run /login", "invalid api key", "authentication", "oauth token")


def find_executable(configured: str = "") -> str | None:
    if configured:
        return configured if Path(configured).is_file() else None
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "claude.exe",
        Path.home() / ".claude" / "local" / "claude.exe",
        Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


class ClaudeCodeBackend:
    name = CLAUDE

    def __init__(self, settings: dict, command_prefix: list[str] | None = None):
        """command_prefix replaces the executable (used by tests with a fake CLI)."""
        self.settings = settings
        self.command_prefix = command_prefix
        self.verified = False
        self.last_verified: float | None = None
        self._auth_cache: tuple[float, dict] | None = None

    def _base_cmd(self) -> list[str]:
        if self.command_prefix:
            return list(self.command_prefix)
        exe = find_executable(self.settings.get("executable", ""))
        if not exe:
            raise BackendError(
                "unavailable",
                "Claude Code is not installed or not found. Install it from https://claude.com/claude-code, "
                "or set its path in Settings.",
            )
        return [exe]

    def _run_quick(self, args: list[str], timeout: float = 20) -> subprocess.CompletedProcess:
        return subprocess.run(
            self._base_cmd() + args, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(paths.claude_workdir()), stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def auth_status(self, max_age_s: float = 60) -> dict:
        """`claude auth status` output. Free: it does not contact the model."""
        if self._auth_cache and time.time() - self._auth_cache[0] < max_age_s:
            return self._auth_cache[1]
        proc = self._run_quick(["auth", "status"])
        try:
            info = json.loads(proc.stdout)
        except ValueError:
            info = {"loggedIn": False, "raw": (proc.stdout + proc.stderr).strip()[:300]}
        # Keep only what the UI needs; never store tokens.
        info = {k: info.get(k) for k in ("loggedIn", "authMethod", "subscriptionType", "apiProvider", "raw") if k in info}
        self._auth_cache = (time.time(), info)
        return info

    def status(self) -> BackendStatus:
        if not self.settings.get("enabled", True):
            return BackendStatus(self.name, False, "Claude is turned off in Settings (local-only mode).")
        try:
            proc = self._run_quick(["--version"])
            version = proc.stdout.strip()
            auth = self.auth_status()
        except BackendError as exc:
            return BackendStatus(self.name, False, str(exc))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return BackendStatus(self.name, False, f"Claude Code did not respond ({exc}).")
        if not auth.get("loggedIn"):
            return BackendStatus(
                self.name, False,
                "Claude Code is installed but not signed in. Open a terminal and run: claude auth login",
                self.settings.get("model", ""), version, extra={"auth": auth},
            )
        plan = auth.get("subscriptionType") or auth.get("authMethod") or "unknown plan"
        if self.verified:
            detail = f"Claude Code {version}, signed in ({plan}). Last real request succeeded."
        else:
            detail = (f"Claude Code {version}, signed in ({plan}). Not yet tested with a real request; "
                      "use 'Test Claude' in Settings (uses a small amount of your Claude usage).")
        return BackendStatus(self.name, True, detail, self.settings.get("model", "") or "Claude Code default",
                             version, self.verified, extra={"auth": auth, "last_verified": self.last_verified})

    def build_args(self, system_prompt: str, resume: str | None, images: bool = False, web: bool = False,
                   mcp_config: str | None = None, enoy_tools: list[str] | None = None,
                   effort: str | None = None) -> list[str]:
        builtin = "WebSearch,WebFetch" if web else ""
        allowed = (["WebSearch", "WebFetch"] if web else []) + [f"mcp__enoy__{n}" for n in enoy_tools or []]
        tools = ["--tools", builtin] + (["--allowedTools", ",".join(allowed)] if allowed else [])
        if mcp_config:
            # Only Enoy's server, never the user's own MCP servers. --safe-mode would also drop Enoy's
            # server, so it is replaced by narrower switches: no user settings (hooks, plugins), no skills.
            tools += ["--mcp-config", mcp_config, "--strict-mcp-config", "--setting-sources", "project",
                      "--disable-slash-commands"]
        web = web or bool(mcp_config)  # stream-json either way, to see the tool calls.
        if images:
            # Plain-text input cannot carry pictures. Claude Code's stream-json input takes
            # one JSON user message with image blocks; it requires stream-json output too.
            args = ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"] + tools
        elif web:
            # stream-json shows each tool call, so Enoy can list the searches and pages used.
            args = ["-p", "--output-format", "stream-json", "--verbose"] + tools
        else:
            args = ["-p", "--output-format", "json"] + tools
        if self.settings.get("safe_mode", True) and not mcp_config:
            # Skip the user's own Claude Code customizations (CLAUDE.md, memory,
            # plugins, skills, MCP servers). They are not needed here because
            # Enoy sends its own context, and they add tokens to every request.
            args.append("--safe-mode")
        if system_prompt:
            args += ["--system-prompt", system_prompt]
        if self.settings.get("model"):
            args += ["--model", self.settings["model"]]
        level = self.settings.get("effort") if effort is None else effort  # "" = Claude Code's own default.
        if level:
            args += ["--effort", level]
        if resume:
            args += ["--resume", resume]
        return args

    def chat(self, request: ChatRequest) -> ChatResult:
        if not self.settings.get("enabled", True):
            raise BackendError("unavailable", "Claude is turned off in Settings.")
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        convo = [m for m in request.messages if m.role != "system"]
        if request.resume_session:
            # Claude Code keeps the earlier turns; send only the newest message.
            prompt = convo[-1].content if convo else ""
        elif len(convo) == 1:
            prompt = convo[0].content
        else:
            prompt = "\n\n".join(f"## {m.role.capitalize()}\n{m.content}" for m in convo)
        sent = convo[-1:] if request.resume_session else convo
        pictures = [img for m in sent for img in m.images]
        if pictures:
            blocks = [{"type": "image", "source": {"type": "base64", "media_type": img.media_type, "data": img.b64()}}
                      for img in pictures]
            prompt = json.dumps({"type": "user", "message": {"role": "user",
                                                             "content": blocks + [{"type": "text", "text": prompt}]}}) + "\n"
        mcp_config = _write_mcp_config(request.enoy_tools, request.enoy_env) if request.enoy_tools else None
        cmd = self._base_cmd() + self.build_args(system, request.resume_session, bool(pictures), request.web_tools,
                                                 mcp_config, request.enoy_tools, request.effort)
        timeout = float(self.settings.get("timeout_s", 600))
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(paths.claude_workdir()), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise BackendError("unavailable", f"Could not start Claude Code: {exc}") from exc

        result: dict = {}

        def communicate():
            try:
                result["out"], result["err"] = proc.communicate(prompt)
            except Exception as exc:  # pragma: no cover - defensive
                result["exc"] = exc

        worker = threading.Thread(target=communicate, daemon=True)
        worker.start()
        stopped = None
        while worker.is_alive():
            if request.cancel.is_set():
                stopped = "cancelled"
            elif time.monotonic() - start > timeout:
                stopped = "timeout"
            if stopped:
                _kill_tree(proc)
                worker.join(5)
                break
            worker.join(0.2)
        if stopped == "cancelled":
            raise BackendError("cancelled", "Cancelled.")
        if stopped == "timeout":
            raise BackendError("timeout", f"Claude did not finish within {timeout:.0f} seconds.")
        if "exc" in result:
            raise BackendError("failed", f"Claude Code failed: {result['exc']}")
        out, err = result.get("out", ""), result.get("err", "")
        data = _parse_json_output(out)
        if data is None:
            combined = (out + "\n" + err).strip()
            raise _classify_failure(combined or f"Claude Code exited with code {proc.returncode} and no output.")
        text = data.get("result") or ""
        if data.get("is_error") or data.get("subtype") not in (None, "success"):
            raise _classify_failure(text or json.dumps(data)[:500])
        if not text.strip():
            raise BackendError("malformed", "Claude returned an empty answer.")
        self.verified = True
        self.last_verified = time.time()
        usage = {}
        if isinstance(data.get("usage"), dict):
            for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                if key in data["usage"]:
                    usage[key] = data["usage"][key]
        if "num_turns" in data:
            usage["num_turns"] = data["num_turns"]
        return ChatResult(
            text=text, backend=self.name, model=self.settings.get("model") or "Claude Code default",
            usage=usage, session_id=data.get("session_id"), duration_s=time.monotonic() - start,
            tool_events=_tool_events(out),
        )


def _write_mcp_config(names: list[str], env: dict) -> str:
    """MCP config for one request: Enoy's own server, offering only `names`. Holds settings, never secrets."""
    import sys
    root = Path(__file__).resolve().parents[2]
    cfg = {"mcpServers": {"enoy": {
        "command": sys.executable, "args": ["-m", "fusion_ai.tools.mcp_server"],
        "env": {"PYTHONPATH": str(root), "PYTHONIOENCODING": "utf-8", "ENOY_MCP_TOOLS": ",".join(names),
                **{k: v if isinstance(v, str) else json.dumps(v) for k, v in env.items()}}}}}
    # One file per request: team runs can ask Claude for two tasks at once, each with its own project view.
    folder = paths.claude_workdir()
    for old in folder.glob("enoy-mcp*.json"):
        try:
            if time.time() - old.stat().st_mtime > 86400:
                old.unlink()
        except OSError:
            pass
    path = folder / f"enoy-mcp-{os.getpid()}-{secrets.token_hex(4)}.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return str(path)


def _tool_events(out: str) -> list[dict]:
    """The tools Claude Code used, from stream-json output: [{tool, query|url}]."""
    events = []
    for line in out.splitlines():
        if '"tool_use"' not in line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict) or item.get("type") != "assistant":
            continue
        for block in (item.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                inp = block.get("input") or {}
                events.append({"tool": block.get("name", ""), "query": str(inp.get("query", ""))[:300],
                               "url": str(inp.get("url", ""))[:500]})
    return events


def _parse_json_output(out: str) -> dict | None:
    out = out.strip()
    if not out:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        # stream-json prints one event per line; the final "result" event holds the answer.
        data = None
        for line in reversed(out.splitlines()):
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("type") == "result":
                data = item
                break
        if data is None:
            try:
                data = json.loads(out.splitlines()[-1])
            except ValueError:
                return None
    if isinstance(data, list):
        results = [d for d in data if isinstance(d, dict) and d.get("type") == "result"]
        data = results[-1] if results else None
    return data if isinstance(data, dict) else None


def _classify_failure(message: str) -> BackendError:
    lowered = message.lower()
    if any(m in lowered for m in LIMIT_MARKERS):
        return BackendError("limit", f"Claude usage limit reached: {message[:300]}")
    if any(m in lowered for m in AUTH_MARKERS):
        return BackendError("unavailable", f"Claude Code is not signed in or its sign-in expired: {message[:300]}")
    return BackendError("failed", f"Claude Code error: {message[:500]}")


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        proc.kill()
