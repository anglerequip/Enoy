import json, os, sys, threading, time
from pathlib import Path
import pytest
from enoy import config as config_mod
from enoy.backends.base import BackendError, ChatMessage, ChatRequest
from enoy.backends.claude_code import ClaudeCodeBackend

def req(**kw):
    return ChatRequest([ChatMessage("user", "hi")], **kw)

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
    from enoy import config as config_mod

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
