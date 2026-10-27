"""Read-only provider quotas. No credentials or model requests are needed."""
from __future__ import annotations

import json
import math
import queue
import subprocess
import threading
import time
from datetime import datetime

from .backends.base import BackendError


def window(label, used, resets):
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or not 0 <= used <= 100:
        raise ValueError("Usage must be a percentage between 0 and 100.")
    if isinstance(resets, bool) or not isinstance(resets, (int, float)) or not math.isfinite(resets) or resets <= 0:
        raise ValueError("Choose a valid reset date and time.")
    return {"label": label, "remaining_percent": round(100 - used, 1), "resets_at": resets}


def codex_limits(backend):
    """Use the official app-server handshake, then close this private stdio client."""
    proc = subprocess.Popen(backend._base_cmd() + ["app-server"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    lines = queue.Queue()
    def read():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + 18
    def send(data):
        proc.stdin.write(json.dumps(data) + "\n")
        proc.stdin.flush()
    def response(request_id):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError() from exc
            if line is None:
                raise ValueError("Codex usage service closed before responding.")
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("id") == request_id:
                if "error" in event:
                    raise ValueError("Codex could not return subscription limits. Check sign-in and try again.")
                return event.get("result") or {}
    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "enoy_usage", "version": "0.4.2"}}})
        response(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read"})
        result = response(2)
        limits = result.get("rateLimitsByLimitId", {}).get("codex") or result.get("rateLimits") or {}
        windows = []
        for key in ("primary", "secondary"):
            item = limits.get(key)
            if not isinstance(item, dict):
                continue
            minutes = item.get("windowDurationMins")
            label = "5 hours" if minutes == 300 else "Weekly" if minutes == 10080 else f"{minutes} minutes" if minutes else key.capitalize()
            windows.append(window(label, item.get("usedPercent"), item.get("resetsAt")))
        return {"windows": windows, "source": "Codex subscription", "checked_at": time.time(),
                "detail": "" if windows else "No subscription windows returned. API-key accounts may not expose them."}
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        reader.join(timeout=2)
        proc.stdin.close()
        proc.stdout.close()


def claude_limits(backend):
    """Read the CLI's experimental get_usage control response; send no model prompt.

    Older CLIs or accounts may not expose this data. Callers retain the manual
    snapshot as a fallback rather than inferring a quota from token counts.
    """
    proc = subprocess.Popen(
        backend._base_cmd() + ["-p", "--input-format", "stream-json", "--output-format",
                               "stream-json", "--verbose", "--safe-mode", "--tools", ""],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    lines = queue.Queue()
    def read():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        proc.stdin.write(json.dumps({"type": "control_request", "request_id": "enoy-usage",
                                    "request": {"subtype": "get_usage", "skip_behaviors": True}}) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 18
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Claude usage check timed out.")
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("Claude usage check timed out.") from exc
            if line is None:
                raise ValueError("Claude CLI closed before returning usage.")
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "control_response":
                continue
            response = event.get("response") or {}
            if response.get("request_id") != "enoy-usage":
                continue
            if response.get("subtype") != "success":
                raise ValueError("This Claude CLI could not return subscription usage.")
            return _claude_windows(response.get("response") or {})
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        reader.join(timeout=2)
        proc.stdin.close()
        proc.stdout.close()


def _claude_windows(data):
    limits = data.get("rate_limits")
    if not isinstance(limits, dict):
        raise ValueError("Claude returned no subscription limits. Check sign-in and network access.")
    windows = []
    for key, label in (("five_hour", "5 hours"), ("seven_day", "Weekly"),
                       ("seven_day_opus", "Weekly · Opus"), ("seven_day_sonnet", "Weekly · Sonnet")):
        item = limits.get(key)
        if not isinstance(item, dict):
            continue
        reset = item.get("resets_at")
        if isinstance(reset, str):
            reset = datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp()
        used = item.get("utilization")
        # An unused session window has no reset until the first request.
        validated = window(label, used, reset if reset is not None else 1)
        validated["resets_at"] = reset
        windows.append(validated)
    if not windows:
        raise ValueError("Claude returned no recognized subscription windows.")
    exhausted = any(w["remaining_percent"] == 0 and
                    (w["resets_at"] is None or w["resets_at"] > time.time()) for w in windows)
    return {"windows": windows, "source": "Claude subscription", "checked_at": time.time(),
            "detail": "Plan usage limit reached. Wait for the reported reset before sending more requests."
                      if exhausted else "Read from your signed-in Claude Code CLI. Refresh to check again."}


class UsageMonitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.cached = None
        self.claude_cached = None

    def read(self, orc, refresh=False):
        with self.lock:
            if refresh or not self.cached or time.time() - self.cached["checked_at"] > 60:
                backend = orc.backends.get("codex")
                try:
                    if not orc.cfg["codex"].get("enabled", True) or not hasattr(backend, "_base_cmd"):
                        raise ValueError("Enable and sign in to Codex to read usage.")
                    self.cached = codex_limits(backend)
                except (BackendError, OSError, ValueError, TimeoutError, subprocess.SubprocessError):
                    self.cached = {"windows": [], "checked_at": time.time(), "source": "Codex subscription",
                                   "detail": "Usage unavailable. Check Codex sign-in, then refresh usage."}
            if refresh or not self.claude_cached or time.time() - self.claude_cached["attempted_at"] > 60:
                backend = orc.backends.get("claude")
                try:
                    if not orc.cfg["claude"].get("enabled", True) or not hasattr(backend, "_base_cmd"):
                        raise ValueError("Enable and sign in to Claude to read usage.")
                    claude = claude_limits(backend)
                except (BackendError, OSError, ValueError, TypeError, TimeoutError, subprocess.SubprocessError):
                    claude = {"windows": [], "source": "Claude subscription", "checked_at": None,
                              "detail": "Live usage unavailable. Check Claude sign-in, network access and CLI version, then refresh usage."}
                self.claude_cached = {**claude, "attempted_at": time.time()}
            claude = dict(self.claude_cached)
            if not claude["windows"]:
                raw = orc.db.kv_get("claude_usage_snapshot")
                if raw:
                    snapshot = json.loads(raw)
                    claude = {**snapshot, "detail": claude["detail"] + " Showing your manual snapshot; it does not refresh automatically."}
            return {"codex": self.cached, "claude": claude}


def save_claude(db, data):
    windows = []
    for key, label in (("five_hour", "5 hours"), ("seven_day", "Weekly")):
        item = data.get(key)
        if item is not None:
            value = window(label, item.get("used_percent"), item.get("resets_at"))
            if value["resets_at"] <= time.time():
                raise ValueError("Reset time must be in the future.")
            windows.append(value)
    value = {"windows": windows, "source": "Manual snapshot", "checked_at": time.time()}
    db.kv_set("claude_usage_snapshot", json.dumps(value))
    return value
