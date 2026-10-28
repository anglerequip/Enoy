"""Adapter for the Anthropic Messages API (raw HTTP, standard library only).

Billed separately from a Claude Pro plan, so Enoy ships it switched off. Assistant turns are
sent back exactly as returned (thinking blocks included), and every tool result of one turn
goes back in a single user message.
"""

from __future__ import annotations

import time
import urllib.parse
from typing import Callable

from .base import BackendError, BackendStatus, ChatMessage, ChatRequest, ChatResult
from .http import request_json

DEFAULT_MAX_OUTPUT = 16000
DEFAULT_VERSION = "2026-10-21"


class AnthropicBackend:
    def __init__(self, name: str, settings: dict, model: str, key: Callable[[], str | None]):
        self.name = name
        self.settings = settings
        self.model = model
        self._key = key

    def _url(self, path: str) -> str:
        return self.settings["base_url"].rstrip("/") + path

    def _headers(self) -> tuple[dict, str]:
        key = self._key() or ""
        if not key:
            raise BackendError("auth", f"No API key is saved for {self.name}. Add it in Settings → Providers.")
        return {"x-api-key": key, "anthropic-version": self.settings.get("api_version") or DEFAULT_VERSION}, key

    def status(self) -> BackendStatus:
        if self._key():
            return BackendStatus(self.name, True, "Ready (not yet checked with a request).", self.model)
        return BackendStatus(self.name, False, "No API key saved.", self.model)

    def list_models(self, cancel=None) -> list[str]:
        headers, key = self._headers()
        ids, after = [], None
        for _ in range(20):
            query = {"limit": 1000} | ({"after_id": after} if after else {})
            data = request_json("GET", self._url("/v1/models?" + urllib.parse.urlencode(query)), headers=headers,
                                timeout=30, cancel=cancel, retries=1, secret=key, label=self.name)
            ids += [m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m]
            if not data.get("has_more"):
                break
            after = data.get("last_id")
        return ids

    @staticmethod
    def _messages(messages: list[ChatMessage]) -> tuple[str, list[dict]]:
        system, out = [], []
        for m in messages:
            if m.role == "system":
                system.append(m.content)
            elif m.role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                if out and out[-1]["role"] == "user" and all(b.get("type") == "tool_result" for b in out[-1]["content"]):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            elif m.role == "assistant":
                if isinstance(m.provider_content, list):
                    out.append({"role": "assistant", "content": m.provider_content})
                else:
                    blocks = ([{"type": "text", "text": m.content}] if m.content else []) + [
                        {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c.get("arguments", {})}
                        for c in m.tool_calls]
                    out.append({"role": "assistant", "content": blocks})
            else:
                blocks = [{"type": "image", "source": {"type": "base64", "media_type": i.media_type, "data": i.b64()}}
                          for i in m.images]
                out.append({"role": "user", "content": blocks + [{"type": "text", "text": m.content}]})
        return "\n\n".join(system), out

    @staticmethod
    def _tools(tools: list[dict]) -> list[dict]:
        """Enoy describes tools in the OpenAI function format; the Messages API wants input_schema."""
        out = []
        for t in tools:
            fn = t.get("function", t)
            out.append({"name": fn["name"], "description": fn.get("description", ""),
                        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
        return out

    def chat(self, request: ChatRequest) -> ChatResult:
        headers, key = self._headers()
        system, messages = self._messages(request.messages)
        body = {"model": request.model or self.model, "messages": messages,
                "max_tokens": request.num_predict or self.settings.get("max_output") or DEFAULT_MAX_OUTPUT}
        if system:
            body["system"] = system
        if request.tools:
            body["tools"] = self._tools(request.tools)
        if request.effort:
            body["output_config"] = {"effort": request.effort}
        start = time.time()
        data = request_json("POST", self._url("/v1/messages"), body=body, headers=headers,
                            timeout=self.settings.get("timeout_s", 600), cancel=request.cancel, secret=key,
                            label=self.name)
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            raise BackendError("malformed", f"{self.name} returned no answer.")
        stop = data.get("stop_reason") or ""
        if stop == "refusal":
            raise BackendError("failed", f"{self.name} declined this request (stop reason: refusal).")
        text, thinking, calls = [], [], []
        for b in data["content"]:
            if b.get("type") == "text":
                text.append(b.get("text", ""))
            elif b.get("type") == "thinking":
                thinking.append(b.get("thinking", ""))
            elif b.get("type") == "tool_use":
                calls.append({"id": b.get("id", ""), "name": b.get("name", ""),
                              "arguments": b.get("input") if isinstance(b.get("input"), dict) else {}})
        usage = data.get("usage") or {}
        result = ChatResult(text="".join(text), backend=self.name, model=data.get("model") or body["model"],
                            thinking="".join(thinking), tool_calls=calls, duration_s=time.time() - start,
                            provider_content=data["content"], stop_reason=stop)
        if "input_tokens" in usage:
            result.usage = {"input_tokens": usage["input_tokens"] + (usage.get("cache_creation_input_tokens") or 0),
                            "cached_tokens": usage.get("cache_read_input_tokens") or 0,
                            "output_tokens": usage.get("output_tokens", 0)}
        if request.on_token and result.text:
            request.on_token(result.text)
        return result
