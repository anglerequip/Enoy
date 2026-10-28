"""Build workspace model providers: status, connection tests, discovery and metered requests.

`complete()` is the only way Build code sends a request to an API provider. It reserves the
spending limit first, holds a concurrency slot, and records what the request cost.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .backends.anthropic_api import AnthropicBackend
from .backends.base import BackendError, ChatMessage, ChatRequest, ChatResult
from .backends.openai_compat import OpenAICompatBackend
from .config import CLI_PROVIDERS
from .spend import Spend, estimate_tokens
from .storage import Storage
from .tools import keystore


class Providers:
    def __init__(self, cfg: Callable[[], dict], db: Storage, spend: Spend | None = None):
        self._cfg = cfg
        self.db = db
        self.spend = spend or Spend(cfg, db)
        self._last: dict[str, str] = {}   # Result kind of the last connection test this session.
        self._slots: dict[tuple[str, int], threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()

    @property
    def build(self) -> dict:
        return self._cfg()["build"]

    def settings(self, pid: str) -> dict:
        p = self.build["providers"].get(pid)
        if p is None:
            raise KeyError(pid)
        return p

    def model(self, model_id: str) -> dict:
        m = next((m for m in self.build["models"] if m["id"] == model_id), None)
        if m is None:
            raise KeyError(model_id)
        return m

    def backend(self, pid: str, model: str = ""):
        p = self.settings(pid)
        cls = AnthropicBackend if p["kind"] == "anthropic" else OpenAICompatBackend
        return cls(pid, p, model, key=lambda: keystore.get("provider:" + pid))

    def _verified(self, pid: str) -> dict | None:
        v = self.db.kv_get("build.verified." + pid)
        return v if v and v.get("base_url") == self.settings(pid)["base_url"] else None

    def status(self, pid: str) -> dict:
        p = self.settings(pid)
        key_set = keystore.get("provider:" + pid) is not None
        verified = self._verified(pid)
        last = self._last.get(pid)
        if not p["enabled"]:
            state = "disabled"
        elif not p["local"] and not key_set:
            state = "no_key"
        elif last == "unavailable":
            state = "no_connection"
        elif last == "auth":
            state = "key_refused"
        elif last and last != "ok":
            state = "error"
        else:
            state = "verified" if verified else "ready"
        return {"id": pid, "kind": p["kind"], "base_url": p["base_url"], "enabled": p["enabled"], "local": p["local"],
                "key_set": key_set, "state": state, "verified": bool(verified),
                "verified_at": verified["at"] if verified else None,
                "models": self.db.kv_get("build.models." + pid, []), "spend": self.spend.totals(pid)}

    def statuses(self) -> list[dict]:
        return [self.status(pid) for pid in self.build["providers"]]

    def test(self, pid: str) -> dict:
        """Free connection check: list the provider's models. Sends no prompt and uses no tokens."""
        try:
            models = self.backend(pid).list_models()
        except BackendError as exc:
            self._last[pid] = exc.kind
            return {"ok": False, "kind": exc.kind, "error": str(exc)}
        self._last[pid] = "ok"
        self.db.kv_set("build.models." + pid, models[:500])
        return {"ok": True, "models": models}

    def slot(self, pid: str) -> threading.BoundedSemaphore:
        n = self.settings(pid).get("max_concurrency", 1)
        with self._lock:
            return self._slots.setdefault((pid, n), threading.BoundedSemaphore(n))

    def complete(self, model_id: str, messages: list[ChatMessage], max_output: int, role: str = "", purpose: str = "",
                 tools: list[dict] | None = None, cancel: threading.Event | None = None, **ids) -> ChatResult:
        m = self.model(model_id)
        if m["provider"] in CLI_PROVIDERS:
            raise ValueError(f"{model_id} runs through a signed-in app, not an API key.")
        prompt = sum(estimate_tokens(x.content) + 20 for x in messages) + sum(estimate_tokens(str(t)) for t in tools or [])
        call = self.spend.reserve(m, prompt, max_output, role=role, purpose=purpose, **ids)
        request = ChatRequest(messages, purpose=purpose or "chat", num_predict=max_output, tools=tools,
                              cancel=cancel or threading.Event())
        try:
            with self.slot(m["provider"]):
                result = self.backend(m["provider"], m.get("model", "")).chat(request)
        except BackendError as exc:
            self.spend.fail(call, str(exc))
            raise
        except BaseException as exc:
            self.spend.fail(call, f"{type(exc).__name__}: {exc}")
            raise
        self.spend.finish(call, result.usage)
        return result

    def paid_test(self, model_id: str, confirm: bool = False) -> dict:
        """One tiny real request. The provider charges for it, so the caller must confirm."""
        if not confirm:
            raise PermissionError("This sends a real request that the provider charges for. Confirm it first.")
        m = self.model(model_id)
        try:
            r = self.complete(model_id, [ChatMessage("user", "Reply with the single word OK.")], max_output=200,
                              role="test", purpose="connection test")
        except BackendError as exc:
            if m["provider"] in self.build["providers"]:
                self._last[m["provider"]] = exc.kind
            return {"ok": False, "kind": exc.kind, "error": str(exc)}
        self._last[m["provider"]] = "ok"
        self.db.kv_set("build.verified." + m["provider"], {"at": time.time(), "model": m.get("model", ""),
                                                           "base_url": self.settings(m["provider"])["base_url"]})
        return {"ok": True, "reply": r.text.strip()[:200], "usage": r.usage, "model": r.model}
