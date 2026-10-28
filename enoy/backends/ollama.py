"""Adapter for a local Ollama service through its native API (/api/chat, /api/tags).

The native API is used because it accepts the context and batch sizes that keep a model inside
Enoy's memory modes; Ollama's OpenAI-compatible endpoint ignores them. Enoy only lists and uses
models that are already installed: it never calls /api/pull.
"""

from __future__ import annotations

import time
from typing import Callable

from .base import BackendError, BackendStatus, ChatMessage, ChatRequest, ChatResult
from .http import request_json

DEFAULT_MAX_OUTPUT = 4096


class OllamaBackend:
    def __init__(self, name: str, settings: dict, model: str, options: Callable[[], dict] | None = None):
        self.name = name
        self.settings = settings
        self.model = model
        self._options = options or (lambda: {})

    def _url(self, path: str) -> str:
        return self.settings["base_url"].rstrip("/") + path

    def status(self) -> BackendStatus:
        return BackendStatus(self.name, True, "Local model service (checked with Test connection).", self.model)

    def _get_tags(self, cancel=None) -> dict:
        try:
            return request_json("GET", self._url("/api/tags"), timeout=10, cancel=cancel, retries=0, label="Ollama")
        except BackendError as exc:
            if exc.kind == "unavailable":
                raise BackendError("unavailable", "Ollama is not running on this PC (or not at "
                                                  f"{self.settings['base_url']}). Start Ollama and test again.") from None
            raise

    def list_models(self, cancel=None) -> list[str]:
        return [m["name"] for m in self._get_tags(cancel).get("models", []) if isinstance(m, dict) and "name" in m]

    def models_info(self, cancel=None) -> list[dict]:
        out = []
        for m in self._get_tags(cancel).get("models", []):
            d = m.get("details") or {}
            out.append({"name": m.get("name", ""), "size_mib": round((m.get("size") or 0) / 1024 / 1024),
                        "parameters": d.get("parameter_size", ""), "quantization": d.get("quantization_level", "")})
        return out

    def unload(self, model: str | None = None) -> None:
        request_json("POST", self._url("/api/generate"), body={"model": model or self.model, "keep_alive": 0},
                     timeout=10, retries=0, label="Ollama")

    @staticmethod
    def _message(m: ChatMessage) -> dict:
        if m.role == "tool":
            return {"role": "tool", "content": m.content, "tool_name": m.tool_name}
        if m.role == "assistant" and isinstance(m.provider_content, dict):
            return m.provider_content
        out = {"role": m.role, "content": m.content}
        if m.role == "assistant" and m.tool_calls:
            out["tool_calls"] = [{"function": {"name": c["name"], "arguments": c.get("arguments", {})}} for c in m.tool_calls]
        if m.images:
            out["images"] = [i.b64() for i in m.images]
        return out

    def chat(self, request: ChatRequest) -> ChatResult:
        model = request.model or self.model
        options = dict(self._options())
        options["num_predict"] = request.num_predict or self.settings.get("max_output") or DEFAULT_MAX_OUTPUT
        body = {"model": model, "messages": [self._message(m) for m in request.messages], "stream": False,
                "options": options, "keep_alive": self.settings.get("keep_alive", "5m")}
        if request.tools:
            body["tools"] = request.tools
        if request.json_schema and not request.tools:
            body["format"] = "json"
        start = time.time()
        try:
            data = request_json("POST", self._url("/api/chat"), body=body, timeout=self.settings.get("timeout_s", 1800),
                                cancel=request.cancel, retries=1, label="Ollama")
        except BackendError as exc:
            if exc.kind == "failed" and "not found" in str(exc).lower():
                raise BackendError("model_missing", f"The model {model} is not installed in Ollama. Enoy does not "
                                                    f"download models; install it yourself (ollama pull {model}) "
                                                    "if you want to use it.") from None
            raise
        msg = data.get("message") if isinstance(data, dict) else None
        if not isinstance(msg, dict):
            raise BackendError("malformed", "Ollama returned no answer.")
        calls = []
        for i, c in enumerate(msg.get("tool_calls") or []):
            fn = c.get("function") or {}
            args = fn.get("arguments")
            calls.append({"id": f"call_{i}", "name": fn.get("name", ""), "arguments": args if isinstance(args, dict) else {}})
        result = ChatResult(text=msg.get("content") or "", backend=self.name, model=data.get("model") or model,
                            thinking=msg.get("thinking") or "", tool_calls=calls, duration_s=time.time() - start,
                            provider_content={k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")},
                            stop_reason=data.get("done_reason") or "")
        if "prompt_eval_count" in data or "eval_count" in data:
            result.usage = {"input_tokens": data.get("prompt_eval_count", 0), "cached_tokens": 0,
                            "output_tokens": data.get("eval_count", 0)}
        if request.on_token and result.text:
            request.on_token(result.text)
        return result
