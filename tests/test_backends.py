"""Backend adapters against fake services: no real Ollama or Claude calls."""

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fusion_ai import config as config_mod
from fusion_ai.backends.base import BackendError, ChatMessage, ChatRequest
from fusion_ai.backends.claude_code import ClaudeCodeBackend
from fusion_ai.backends.ollama import OllamaBackend


# ---------------------------------------------------------------- fake Ollama

class FakeOllama:
    def __init__(self, mode="ok", models=("qwen3.5:9b",)):
        self.mode = mode
        self.bodies = []
        self.models = list(models)
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/version":
                    self._json(200, {"version": "0.0-test"})
                elif self.path == "/api/tags":
                    self._json(200, {"models": [{"name": m} for m in outer.models]})
                elif self.path == "/api/ps":
                    self._json(200, {"models": [{"name": outer.models[0], "size": 1000, "size_vram": 800, "context_length": 8192}]})
                else:
                    self._json(404, {"error": "nope"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.bodies.append(body)
                if self.path == "/api/show":
                    self._json(200, {"capabilities": ["completion", "tools", "thinking"]})
                    return
                if body["model"] not in outer.models:
                    self._json(404, {"error": f"model '{body['model']}' not found"})
                    return
                if outer.mode == "slow":
                    time.sleep(5)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Connection", "close")  # Body ends when the connection closes.
                self.end_headers()
                self.close_connection = True
                if outer.mode == "malformed":
                    self.wfile.write(b"this is not json\n")
                    return
                if outer.mode == "oom":
                    self.wfile.write(json.dumps({"error": "CUDA out of memory"}).encode() + b"\n")
                    return
                if outer.mode == "truncated":
                    self.wfile.write(json.dumps({"message": {"content": "partial"}, "done": False}).encode() + b"\n")
                    return
                for word in ["Hello", " there"]:
                    self.wfile.write(json.dumps({"message": {"role": "assistant", "content": word}, "done": False}).encode() + b"\n")
                    self.wfile.flush()
                    if outer.mode == "stream_slow":
                        time.sleep(3)
                self.wfile.write(json.dumps({"message": {"content": ""}, "done": True, "done_reason": "stop",
                                             "eval_count": 2, "prompt_eval_count": 10}).encode() + b"\n")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def ollama_settings(url, **kw):
    s = dict(config_mod.DEFAULTS["ollama"])
    s["endpoint"] = url
    s.update(kw)
    return s


def req(**kw):
    return ChatRequest(messages=[ChatMessage("user", "hi")], **kw)


@pytest.fixture
def fake_ollama():
    servers = []

    def make(**kw):
        s = FakeOllama(**kw)
        servers.append(s)
        return s
    yield make
    for s in servers:
        s.close()


def test_ollama_streaming_success(fake_ollama):
    srv = fake_ollama()
    tokens = []
    backend = OllamaBackend(ollama_settings(srv.url))
    result = backend.chat(req(on_token=tokens.append))
    assert result.text == "Hello there"
    assert tokens == ["Hello", " there"]
    assert result.usage["eval_count"] == 2
    assert backend.verified
    status = backend.status()
    assert status.available and status.extra["loaded"]["gpu_percent"] == 80
    assert "tools" in status.extra["capabilities"]


def test_ollama_receives_the_memory_mode_settings(fake_ollama):
    srv = fake_ollama()
    backend = OllamaBackend(ollama_settings(srv.url, num_ctx=16384, num_predict=4096))
    backend.chat(req(num_ctx=4096, num_predict=2048, num_batch=256, keep_alive=60))
    chat = [b for b in srv.bodies if "messages" in b][-1]
    assert chat["options"]["num_ctx"] == 4096 and chat["options"]["num_predict"] == 2048
    assert chat["options"]["num_batch"] == 256 and chat["keep_alive"] == 60
    backend.chat(req())  # Without a memory mode: the user's own settings, Ollama's own batch size.
    chat = [b for b in srv.bodies if "messages" in b][-1]
    assert chat["options"]["num_ctx"] == 16384 and chat["options"]["num_predict"] == 4096
    assert "num_batch" not in chat["options"]


def test_ollama_service_missing():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    backend = OllamaBackend(ollama_settings(f"http://127.0.0.1:{port}"))
    status = backend.status()
    assert not status.available and "Is Ollama running" in status.detail
    with pytest.raises(BackendError) as e:
        backend.chat(req())
    assert e.value.kind == "unavailable"


def test_ollama_model_missing(fake_ollama):
    srv = fake_ollama(models=("llama3:8b",))
    backend = OllamaBackend(ollama_settings(srv.url))
    status = backend.status()
    assert not status.available and "not installed" in status.detail and "llama3:8b" in status.detail
    with pytest.raises(BackendError) as e:
        backend.chat(req())
    assert e.value.kind == "model_missing"


def test_ollama_timeout(fake_ollama):
    srv = fake_ollama(mode="slow")
    backend = OllamaBackend(ollama_settings(srv.url, request_timeout_s=10))
    backend.settings["request_timeout_s"] = 1  # Below the schema minimum on purpose.
    start = time.monotonic()
    with pytest.raises(BackendError) as e:
        backend.chat(req())
    assert e.value.kind == "timeout"
    assert time.monotonic() - start < 4


def test_ollama_cancel_mid_stream(fake_ollama):
    srv = fake_ollama(mode="stream_slow")
    backend = OllamaBackend(ollama_settings(srv.url))
    r = req()
    threading.Timer(0.5, r.cancel.set).start()
    start = time.monotonic()
    with pytest.raises(BackendError) as e:
        backend.chat(r)
    assert e.value.kind == "cancelled"
    assert time.monotonic() - start < 2.5  # Interrupted mid-stream, not after the stream ended.


@pytest.mark.parametrize("mode,kind", [("malformed", "malformed"), ("truncated", "malformed"), ("oom", "failed")])
def test_ollama_bad_responses(fake_ollama, mode, kind):
    srv = fake_ollama(mode=mode)
    with pytest.raises(BackendError) as e:
        OllamaBackend(ollama_settings(srv.url)).chat(req())
    assert e.value.kind == kind
    if mode == "oom":
        assert "context size" in str(e.value)


# ---------------------------------------------------------------- fake Claude Code CLI

FAKE_CLI = r'''
import json, os, sys, time
args = sys.argv[1:]
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": mode != "logged_out", "authMethod": "claude.ai", "subscriptionType": "pro",
                      "email": "someone@example.com", "accessToken": "should-not-leak"}))
    sys.exit(0)
if args == ["--version"]:
    print("9.9.9 (Claude Code)")
    sys.exit(0)
prompt = sys.stdin.read()
with open(os.environ["FAKE_CLAUDE_LOG"], "w", encoding="utf-8") as fh:
    json.dump({"args": args, "prompt": prompt}, fh)
if mode == "slow":
    time.sleep(30)
if mode == "limit":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "Claude AI usage limit reached|1700000000"}))
    sys.exit(1)
if mode == "garbage":
    print("<html>not json</html>")
    sys.exit(1)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "READY",
                  "session_id": "abc-123", "usage": {"input_tokens": 12, "output_tokens": 3}}))
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    log = tmp_path / "claude_call.json"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))

    def make(mode="ok", **settings):
        monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
        s = dict(config_mod.DEFAULTS["claude"])
        s.update(settings)
        return ClaudeCodeBackend(s, command_prefix=[sys.executable, str(script)]), log
    return make


def test_claude_success_uses_no_tools_and_stdin(fake_claude):
    backend, log = fake_claude()
    result = backend.chat(ChatRequest(messages=[ChatMessage("system", "SYS"), ChatMessage("user", "Do the thing")]))
    assert result.text == "READY" and result.session_id == "abc-123"
    assert result.usage == {"input_tokens": 12, "output_tokens": 3}
    call = json.loads(log.read_text(encoding="utf-8"))
    args = call["args"]
    assert args[:3] == ["-p", "--output-format", "json"]
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--system-prompt") + 1] == "SYS"
    assert "--safe-mode" in args  # On by default.
    assert call["prompt"] == "Do the thing"
    assert backend.verified


def test_claude_defaults_to_opus_high_effort(fake_claude):
    from fusion_ai import config as config_mod

    cfg, _ = config_mod.validate({})
    assert cfg["claude"]["model"] == "claude-opus-5-5" and cfg["claude"]["effort"] == "high"
    backend, log = fake_claude(model=cfg["claude"]["model"], effort=cfg["claude"]["effort"])
    backend.chat(req())
    args = json.loads(log.read_text(encoding="utf-8"))["args"]
    assert args[args.index("--model") + 1] == "claude-opus-5-5"
    assert args[args.index("--effort") + 1] == "high"
    backend, log = fake_claude(model="", effort="")
    backend.chat(req())
    args = json.loads(log.read_text(encoding="utf-8"))["args"]
    assert "--model" not in args and "--effort" not in args  # Empty = Claude Code's own defaults.
    with pytest.raises(config_mod.ConfigError):
        config_mod.validate_strict({"claude": {"effort": "extreme"}})


def test_claude_safe_mode_can_be_turned_off(fake_claude):
    backend, log = fake_claude(safe_mode=False)
    backend.chat(req())
    assert "--safe-mode" not in json.loads(log.read_text(encoding="utf-8"))["args"]


def test_claude_resume_sends_only_latest_message(fake_claude):
    backend, log = fake_claude()
    backend.chat(ChatRequest(messages=[ChatMessage("user", "old"), ChatMessage("assistant", "a"), ChatMessage("user", "new")],
                             resume_session="abc-123"))
    call = json.loads(log.read_text(encoding="utf-8"))
    assert call["prompt"] == "new"
    assert call["args"][call["args"].index("--resume") + 1] == "abc-123"


def test_claude_status_is_truthful_and_hides_tokens(fake_claude):
    backend, _ = fake_claude()
    st = backend.status()
    assert st.available and not st.verified and "Not yet tested" in st.detail
    assert "accessToken" not in json.dumps(st.as_dict()) and "someone@example.com" not in json.dumps(st.as_dict())


def test_claude_logged_out(fake_claude):
    backend, _ = fake_claude("logged_out")
    st = backend.status()
    assert not st.available and "not signed in" in st.detail


def test_claude_usage_limit(fake_claude):
    backend, _ = fake_claude("limit")
    with pytest.raises(BackendError) as e:
        backend.chat(req())
    assert e.value.kind == "limit"


def test_claude_malformed_output(fake_claude):
    backend, _ = fake_claude("garbage")
    with pytest.raises(BackendError) as e:
        backend.chat(req())
    assert e.value.kind == "failed"


def test_claude_timeout_kills_process(fake_claude):
    backend, _ = fake_claude("slow", timeout_s=1)
    start = time.monotonic()
    with pytest.raises(BackendError) as e:
        backend.chat(req())
    assert e.value.kind == "timeout" and time.monotonic() - start < 10


def test_claude_disabled(fake_claude):
    backend, _ = fake_claude(enabled=False)
    assert not backend.status().available
    with pytest.raises(BackendError) as e:
        backend.chat(req())
    assert e.value.kind == "unavailable"


def test_claude_not_installed(tmp_path):
    backend = ClaudeCodeBackend({**config_mod.DEFAULTS["claude"], "executable": str(tmp_path / "missing.exe")})
    st = backend.status()
    assert not st.available and "not installed" in st.detail
