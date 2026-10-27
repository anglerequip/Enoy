"""ChatGPT via the signed-in Codex CLI; Enoy owns context, edits and checks.

Requests run in an empty directory, with the shell tool disabled. No personal
Codex configuration or project instructions are loaded. Authentication remains
owned by the official CLI. No credentials are read by Enoy.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .base import CODEX, BackendError, BackendStatus, ChatRequest, ChatResult


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
        exe = configured if configured and Path(configured).is_file() else None
        if not configured:
            exe = shutil.which("codex")
        if not exe:
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

    def build_args(self, image_paths: list[str] | None = None) -> list[str]:
        args = ["exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--color", "never",
                "-c", "features.shell_tool=false"]
        if self.settings.get("model"):
            args += ["--model", self.settings["model"]]
        for path in image_paths or []:
            args += ["--image", path]
        return args + ["-"]

    def chat(self, request: ChatRequest) -> ChatResult:
        if not self.settings.get("enabled", True):
            raise BackendError("unavailable", "ChatGPT / Codex is disabled.")
        if request.cancel.is_set():
            raise BackendError("cancelled", "Codex request cancelled.")
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
                proc = subprocess.Popen(self._base_cmd() + self.build_args(image_paths), cwd=workdir,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", errors="replace",
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                try:
                    first = True
                    while True:
                        if request.cancel.is_set():
                            raise BackendError("cancelled", "Codex request cancelled.")
                        if time.monotonic() - started > self.settings.get("timeout_s", 600):
                            raise BackendError("timeout", "Codex request timed out.")
                        try:
                            stdout, stderr = proc.communicate(prompt if first else None, timeout=0.2)
                            break
                        except subprocess.TimeoutExpired:
                            first = False
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()
        except OSError as exc:
            raise BackendError("unavailable", f"Could not start Codex: {exc}") from exc
        answers, usage, error = [], {}, ""
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                answers.append(item.get("text", ""))
            if event.get("type") == "turn.completed":
                usage = event.get("usage") or {}
            if event.get("type") in ("error", "turn.failed"):
                error = str(event.get("message") or event.get("error") or "Codex failed")
        if proc.returncode or error:
            raise BackendError("failed", (error or stderr.strip() or "Codex failed")[:800])
        if not answers:
            raise BackendError("malformed", "Codex returned no assistant message.")
        text = "\n\n".join(answers)
        self.verified = True
        if request.on_token:
            request.on_token(text)
        return ChatResult(text, CODEX, self.settings.get("model") or "Codex default", usage=usage,
                          duration_s=time.monotonic() - started)
