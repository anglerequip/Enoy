"""Adapter for OpenAI-compatible chat APIs: DeepSeek, Fireworks, Ollama (/v1) and custom endpoints.

Usage is normalised to {input_tokens (uncached), cached_tokens, output_tokens} so that the
spend ledger prices every provider the same way.
"""

from __future__ import annotations

import json
import time
from typing import Callable

from .base import BackendError, BackendStatus, ChatMessage, ChatRequest, ChatResult
from .http import request_json

DEFAULT_MAX_OUTPUT = 8192


class OpenAICompatBackend:
    def __init__(self, name: str, settings: dict, model: str, key: Callable[[], str | None]):
        self.name = name
        self.settings = settings
        self.model = model
        self._key = key

    def _url(self, path: str) -> str:
        return self.settings["base_url"].rstrip("/") + path

    def _headers(self) -> tuple[dict, str]:
        key = self._key() or ""
        if not key and not self.settings.get("local"):
            raise BackendError("auth", f"No API key is saved for {self.name}. Add it in Settings → Providers.")
        return ({"Authorization": "Bearer " + key} if key else {}), key

    def status(self) -> BackendStatus:
        if self.settings.get("local") or self._key():
            return BackendStatus(self.name, True, "Ready (not yet checked with a request).", self.model)
        return BackendStatus(self.name, False, "No API key saved.", self.model)

    def list_models(self, cancel=None) -> list[str]:
        headers, key = self._headers()
        data = request_json("GET", self._url("/models"), headers=headers, timeout=30, cancel=cancel, retries=1,
                            secret=key, label=self.name)
        return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m]

    @staticmethod
    def _message(m: ChatMessage) -> dict:
        if m.role == "assistant" and isinstance(m.provider_content, dict):
            return m.provider_content  # Unchanged: DeepSeek needs its reasoning_content back during tool use.
        if m.role == "tool":
            return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
        if m.role == "assistant" and m.tool_calls:
            return {"role": "assistant", "content": m.content or None, "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments", {}))}}
                for c in m.tool_calls]}
        if m.images:
            return {"role": m.role, "content": [{"type": "text", "text": m.content}] + [
                {"type": "image_url", "image_url": {"url": f"data:{i.media_type};base64,{i.b64()}"}} for i in m.images]}
        return {"role": m.role, "content": m.content}

    def chat(self, request: ChatRequest) -> ChatResult:
        headers, key = self._headers()
        body = {"model": request.model or self.model, "messages": [self._message(m) for m in request.messages],
                "max_tokens": request.num_predict or self.settings.get("max_output") or DEFAULT_MAX_OUTPUT,
                "stream": False}
        if request.tools:
            body["tools"] = request.tools
        if request.json_schema and not request.tools:
            body["response_format"] = {"type": "json_object"}
        start = time.time()
        data = request_json("POST", self._url("/chat/completions"), body=body, headers=headers,
                            timeout=self.settings.get("timeout_s", 600), cancel=request.cancel, secret=key,
                            label=self.name)
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices or not isinstance(choices[0].get("message"), dict):
            raise BackendError("malformed", f"{self.name} returned no answer.")
        msg = choices[0]["message"]
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function") or {}
            call = {"id": c.get("id", ""), "name": fn.get("name", "")}
            try:
                args = json.loads(fn.get("arguments") or "{}")
                call["arguments"] = args if isinstance(args, dict) else {}
            except ValueError:
                call["arguments"] = {}
                call["error"] = "The tool arguments were not valid JSON."
            calls.append(call)
        usage = data.get("usage") or {}
        cached = usage.get("prompt_cache_hit_tokens")
        if cached is None:
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        result = ChatResult(text=msg.get("content") or "", backend=self.name, model=data.get("model") or body["model"],
                            thinking=msg.get("reasoning_content") or "", tool_calls=calls,
                            duration_s=time.time() - start, provider_content=msg,
                            stop_reason=choices[0].get("finish_reason") or "")
        if "prompt_tokens" in usage:
            result.usage = {"input_tokens": usage["prompt_tokens"] - cached, "cached_tokens": cached,
                            "output_tokens": usage.get("completion_tokens", 0)}
        if request.on_token and result.text:
            request.on_token(result.text)
        return result
