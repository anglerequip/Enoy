"""Signed-in Codex with native project tools and CLI approval review."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .base import CODEX, BackendError, BackendStatus, ChatRequest, ChatResult


def find_executable(configured: str = "") -> str | None:
    """Find Codex even when a desktop launch lacks Codex's injected PATH."""
    if configured:
        return configured if Path(configured).is_file() else None
    found = shutil.which("codex")
    if found:
        return found
    candidates = [Path.home() / ".local" / "bin" / "codex.exe"]
    if os.environ.get("APPDATA"):
        candidates.append(Path(os.environ["APPDATA"]) / "npm" / "codex.cmd")
    if os.environ.get("LOCALAPPDATA"):
        bundled = Path(os.environ["LOCALAPPDATA"]) / "OpenAI" / "Codex" / "bin"
        candidates.append(bundled / "codex.exe")
        try:
            versions = [p for p in bundled.glob("*/codex.exe") if p.is_file()]
            candidates.extend(sorted(versions, key=lambda p: p.stat().st_mtime, reverse=True))
        except OSError:
            pass
    return next((str(p) for p in candidates if p.is_file()), None)


class CodexBackend:
    name = CODEX

    def __init__(self, settings: dict, command_prefix: list[str] | None = None):
        self.settings = settings
        self.command_prefix = command_prefix
        self.verified = False

    def _base_cmd(self) -> list[str]:
        if self.command_prefix:
            return list(self.command_prefix)
        configured = self.settings.get("executable", "")
        exe = find_executable(configured)
        if not exe:
            if configured:
                raise BackendError("unavailable", f"Configured Codex executable was not found: {configured}. Update its path in Settings.")
            raise BackendError("unavailable", "Codex CLI was not found. Install Codex and run codex login.")
        return [exe]

    def status(self) -> BackendStatus:
        if not self.settings.get("enabled", True):
            return BackendStatus(CODEX, False, "ChatGPT / Codex is disabled in Settings.")
        try:
            proc = subprocess.run(self._base_cmd() + ["login", "status"], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=15,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ready = proc.returncode == 0
            return BackendStatus(CODEX, ready, "Codex is signed in." if ready else "Run codex login to sign in.",
                                 self.settings.get("model") or "Codex default", verified=self.verified)
        except (BackendError, OSError, subprocess.TimeoutExpired) as exc:
            return BackendStatus(CODEX, False, str(exc))

    def build_args(self, image_paths: list[str] | None = None, native_tools: bool = False,
                   request: ChatRequest | None = None) -> list[str]:
        args = ["exec", "--skip-git-repo-check", "--json", "--color", "never"]
        if not native_tools:
            args += ["--ephemeral"]
        if native_tools:
            if request and request.access_mode == "full":
                # Supported Full Access policy; preserve approval checks, never bypass them.
                args += ["--sandbox", "danger-full-access", "-c", 'approval_policy="on-request"']
            else:
                args += ["--approve-for-me"]
            args += ["-c", "features.shell_tool=true"]
        else:
            args += ["--ignore-user-config", "--ignore-rules", "--sandbox", "read-only",
                     "-c", "features.shell_tool=false"]
        if self.settings.get("effort"):
            args += ["-c", "model_reasoning_effort=" + json.dumps(self.settings["effort"])]
        if self.settings.get("model"):
            args += ["--model", self.settings["model"]]
        if request and request.enoy_tools:
            values = {"command": sys.executable, "args": ["-m", "enoy.tools.mcp_server"],
                      "startup_timeout_sec": 30}
            values.update({"env." + k: v if isinstance(v, str) else json.dumps(v)
                           for k, v in request.enoy_env.items()})
            values["env.PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
            values["env.PYTHONIOENCODING"] = "utf-8"
            values["env.ENOY_MCP_TOOLS"] = ",".join(request.enoy_tools)
            for key, value in values.items():
                args += ["-c", "mcp_servers.enoy." + key + "=" + json.dumps(value)]
        for path in image_paths or []:
            args += ["--image", path]
        if native_tools and request and request.resume_session:
            args += ["resume", request.resume_session]
        return args + ["-"]

    def chat(self, request: ChatRequest) -> ChatResult:
        if not self.settings.get("enabled", True):
            raise BackendError("unavailable", "ChatGPT / Codex is disabled.")
        if request.cancel.is_set():
            raise BackendError("cancelled", "Codex request cancelled.")
        if request.native_tools and (not request.workspace or not Path(request.workspace).is_dir()):
            raise BackendError("failed", "Select an existing project folder before using native tools.")
        pictures = [picture for m in request.messages for picture in m.images]
        prompt = "\n\n".join(f"[{m.role}]\n{m.content}" for m in request.messages)
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="enoy-codex-") as workdir:
                image_paths = []
                for index, picture in enumerate(pictures):
                    extension = {"image/png": "png", "image/jpeg": "jpg"}.get(picture.media_type)
                    if not extension:
                        raise BackendError("failed", "Only PNG and JPEG image attachments are supported.")
                    path = Path(workdir) / f"attachment-{index + 1}.{extension}"
                    path.write_bytes(picture.data)
                    image_paths.append(str(path))
                from .process import run
                def on_line(line):
                    try:
                        event = json.loads(line)
                    except ValueError:
                        return
                    item = event.get("item") or {}
                    if request.on_event and event.get("type") in ("item.started", "item.completed"):
                        kind = item.get("type", "")
                        if kind in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
                            request.on_event({"tool": kind, "status": event["type"].split(".")[-1],
                                "summary": item.get("command") or item.get("query") or item.get("tool") or
                                    ", ".join(c.get("path", "") for c in item.get("changes", [])),
                                "exit_code": item.get("exit_code"), "result_status": item.get("status", ""),
                                "output": item.get("aggregated_output", "")})
                stdout, stderr, returncode = run(
                    self._base_cmd() + self.build_args(image_paths, request.native_tools, request), prompt,
                    request.workspace if request.native_tools and request.workspace else workdir,
                    request, self.settings.get("timeout_s", 600), on_line)
        except OSError as exc:
            raise BackendError("unavailable", f"Could not start Codex: {exc}") from exc
        answers, usage, error, session_id = [], {}, "", None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get("item") or {}
            if event.get("type") == "thread.started":
                session_id = event.get("thread_id")
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                answers.append(item.get("text", ""))
            if event.get("type") == "turn.completed":
                usage = event.get("usage") or {}
            if event.get("type") in ("error", "turn.failed"):
                error = str(event.get("message") or event.get("error") or "Codex failed")
        if returncode or error:
            raise BackendError("failed", (error or stderr.strip() or "Codex failed")[:800])
        if not answers:
            raise BackendError("malformed", "Codex returned no assistant message.")
        text = "\n\n".join(answers)
        self.verified = True
        if request.on_token:
            request.on_token(text)
        return ChatResult(text, CODEX, self.settings.get("model") or "Codex default", usage=usage,
                          duration_s=time.monotonic() - started, session_id=session_id)
