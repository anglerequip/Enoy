"""Local backend: Ollama's HTTP API (default model qwen3.5:9b).

Uses only the documented endpoints: /api/version, /api/tags, /api/show,
/api/ps and /api/chat (streaming NDJSON). Never pulls or deletes models.
"""

from __future__ import annotations

import http.client
import json
import queue
import socket
import threading
import time
from urllib.parse import urlparse

from .base import LOCAL, BackendError, BackendStatus, ChatRequest, ChatResult


class OllamaBackend:
    name = LOCAL

    def __init__(self, settings: dict):
        self.settings = settings
        self.verified = False
        self._capabilities: dict[str, list[str]] = {}

    # HTTP helpers ---------------------------------------------------------------

    def _conn(self, timeout: float) -> http.client.HTTPConnection:
        url = urlparse(self.settings["endpoint"])
        host = url.hostname or "127.0.0.1"
        port = url.port or (443 if url.scheme == "https" else 80)
        cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        return cls(host, port, timeout=timeout)

    def _request(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        timeout = timeout or float(self.settings["connect_timeout_s"])
        conn = self._conn(timeout)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=payload, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = resp.read()
        except (ConnectionError, OSError) as exc:
            raise BackendError("unavailable", f"Cannot reach Ollama at {self.settings['endpoint']} ({exc}). Is Ollama running?") from exc
        finally:
            conn.close()
        try:
            parsed = json.loads(data or b"{}")
        except ValueError as exc:
            raise BackendError("malformed", f"Ollama returned a response that is not JSON (HTTP {resp.status}).") from exc
        if resp.status == 404:
            raise BackendError("model_missing", parsed.get("error", "Not found"))
        if resp.status >= 400:
            raise BackendError("failed", parsed.get("error", f"HTTP {resp.status}"))
        return parsed

    # Status ----------------------------------------------------------------------

    def version(self) -> str:
        return self._request("GET", "/api/version").get("version", "")

    def list_models(self) -> list[dict]:
        return self._request("GET", "/api/tags").get("models", [])

    def loaded_models(self) -> list[dict]:
        return self._request("GET", "/api/ps").get("models", [])

    def unload(self, model: str) -> None:
        """Ask Ollama to free a model's memory now (keep_alive 0 is Ollama's supported way)."""
        self._request("POST", "/api/generate", {"model": model, "keep_alive": 0}, timeout=30)

    def capabilities(self, model: str) -> list[str]:
        if model not in self._capabilities:
            info = self._request("POST", "/api/show", {"model": model}, timeout=15)
            self._capabilities[model] = list(info.get("capabilities", []))
        return self._capabilities[model]

    def status(self) -> BackendStatus:
        model = self.settings["model"]
        try:
            version = self.version()
        except BackendError as exc:
            return BackendStatus(self.name, False, str(exc), model=model)
        try:
            models = [m.get("name", "") for m in self.list_models()]
        except BackendError as exc:
            return BackendStatus(self.name, False, f"Ollama {version} is running but listing models failed: {exc}", model, version)
        if model not in models and f"{model}:latest" not in models:
            return BackendStatus(
                self.name, False,
                f"Model '{model}' is not installed. Installed: {', '.join(models) or 'none'}. "
                "Pick an installed model in Settings, or download it yourself with Ollama.",
                model, version, extra={"installed": models},
            )
        extra: dict = {"installed": models}
        try:
            extra["capabilities"] = self.capabilities(model)
        except BackendError:
            extra["capabilities"] = []
        try:
            for m in self.loaded_models():
                if m.get("name") == model or m.get("model") == model:
                    size = m.get("size") or 0
                    vram = m.get("size_vram") or 0
                    extra["loaded"] = {
                        "size_gb": round(size / 1e9, 2),
                        "vram_gb": round(vram / 1e9, 2),
                        "gpu_percent": round(100 * vram / size) if size else None,
                        "context_length": m.get("context_length"),
                    }
        except BackendError:
            pass
        detail = f"Ollama {version}, model '{model}' installed"
        detail += ", loaded in memory" if "loaded" in extra else ", not loaded yet (first request loads it)"
        return BackendStatus(self.name, True, detail, model, version, self.verified, extra)

    # Chat ---------------------------------------------------------------------------

    def chat(self, request: ChatRequest) -> ChatResult:
        s = self.settings
        body: dict = {
            "model": s["model"],
            "messages": [{"role": m.role, "content": m.content} | ({"images": [i.b64() for i in m.images]} if m.images else {})
                         for m in request.messages],
            "stream": True,
            "think": bool(s["think"]),
            "keep_alive": s["keep_alive"] if request.keep_alive is None else request.keep_alive,
            "options": {
                "num_ctx": int(request.num_ctx or s["num_ctx"]),
                "num_predict": int(request.num_predict or s["num_predict"]),
                "temperature": float(s["temperature"]),
            },
        }
        if request.num_batch:
            body["options"]["num_batch"] = int(request.num_batch)
        if request.json_schema is not None:
            body["format"] = request.json_schema
        total_timeout = float(s["request_timeout_s"])
        start = time.monotonic()
        conn = self._conn(total_timeout)
        items: queue.Queue = queue.Queue()

        def reader():
            # All blocking network I/O happens here. On Windows a blocked recv
            # cannot be interrupted from another thread, so the caller waits on
            # the queue instead and simply abandons this thread on cancel.
            try:
                conn.request("POST", "/api/chat", body=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                if resp.status != 200:
                    items.put(("status", resp.status, resp.read()))
                    return
                while True:
                    line = resp.readline()
                    if not line:
                        items.put(("eof",))
                        return
                    items.put(("line", line))
            except Exception as exc:  # Reported to the caller through the queue.
                items.put(("error", exc))

        threading.Thread(target=reader, daemon=True).start()
        text_parts: list[str] = []
        think_parts: list[str] = []
        final: dict = {}
        got_response = False
        try:
            while True:
                if request.cancel.is_set():
                    raise self._stopped("cancelled", total_timeout)
                if time.monotonic() - start > total_timeout:
                    raise self._stopped("timeout", total_timeout)
                try:
                    item = items.get(timeout=0.1)
                except queue.Empty:
                    continue
                kind = item[0]
                if kind == "error":
                    exc = item[1]
                    if isinstance(exc, TimeoutError):
                        raise self._stopped("timeout", total_timeout) from exc
                    if not got_response and isinstance(exc, (ConnectionError, OSError)):
                        raise BackendError("unavailable", f"Cannot reach Ollama at {s['endpoint']} ({exc}). Is Ollama running?") from exc
                    raise BackendError("failed", f"Connection to Ollama was interrupted ({exc}).") from exc
                if kind == "status":
                    status, raw = item[1], item[2]
                    try:
                        message = json.loads(raw).get("error", "")
                    except ValueError:
                        message = raw[:300].decode(errors="replace")
                    if status == 404 or "not found" in message.lower():
                        raise BackendError("model_missing", f"Ollama says: {message or 'model not found'}")
                    raise BackendError("failed", _explain_error(message or f"HTTP {status}"))
                if kind == "eof":
                    break
                got_response = True
                line = item[1].strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError as exc:
                    raise BackendError("malformed", f"Ollama sent a line that is not JSON: {line[:120]!r}") from exc
                if not isinstance(chunk, dict):
                    raise BackendError("malformed", "Ollama sent an unexpected stream item.")
                if chunk.get("error"):
                    raise BackendError("failed", _explain_error(chunk["error"]))
                msg = chunk.get("message") or {}
                if msg.get("thinking"):
                    think_parts.append(msg["thinking"])
                    if request.on_thinking:
                        request.on_thinking(msg["thinking"])
                if msg.get("content"):
                    text_parts.append(msg["content"])
                    if request.on_token:
                        request.on_token(msg["content"])
                if chunk.get("done"):
                    final = chunk
                    break
        finally:
            # Closing tells Ollama to stop generating if we gave up early.
            try:
                if conn.sock is not None:
                    conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        if not final:
            raise BackendError("malformed", "Ollama's response ended before it reported completion.")
        text = "".join(text_parts)
        if not text.strip():
            raise BackendError("malformed", "The local model returned an empty answer.")
        self.verified = True
        usage = {}
        for key in ("prompt_eval_count", "eval_count", "total_duration", "load_duration"):
            if key in final:
                usage[key] = final[key]
        if final.get("done_reason") == "length":
            usage["truncated"] = True
        return ChatResult(
            text=text, backend=self.name, model=s["model"], thinking="".join(think_parts),
            usage=usage, duration_s=time.monotonic() - start,
        )

    @staticmethod
    def _stopped(reason: str, timeout: float) -> BackendError:
        if reason == "cancelled":
            return BackendError("cancelled", "Cancelled.")
        return BackendError("timeout", f"The local model did not finish within {timeout:.0f} seconds.")


def _explain_error(message: str) -> str:
    lowered = message.lower()
    if "memory" in lowered or "out of memory" in lowered or "cuda" in lowered:
        return (f"Ollama ran out of memory ({message}). Lower the context size in Settings, "
                "close other GPU-heavy programs, or pick a smaller model.")
    return f"Ollama error: {message}"
