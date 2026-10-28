"""Ollama as an optional local Build provider (T10.1): native API, installed models only, memory limits.

A local fake server stands in for Ollama and the memory readings are injected; Ollama itself is not needed.
"""

import threading
import time

import pytest

from enoy import config as config_mod
from enoy.backends.base import BackendError, ChatMessage, ChatRequest
from enoy.backends.ollama import OllamaBackend
from enoy.localguard import ABSOLUTE_MAX_MIB, MODES, LocalGuard
from enoy.providers import Providers
from enoy.storage import Storage
from fakeapi import FakeAPI

TAGS = {"models": [{"name": "qwen3.5:9b", "size": 6_600_000_000, "details": {"parameter_size": "9B", "quantization_level": "Q4_K_M"}},
                   {"name": "qwen3:1.7b", "size": 1_400_000_000, "details": {"parameter_size": "1.7B", "quantization_level": "Q4_K_M"}}]}
TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read",
                                           "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]


def reply(content="", calls=(), prompt=120, out=30):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"function": {"name": n, "arguments": a}} for n, a in calls]
    return (200, {"model": "qwen3:1.7b", "message": msg, "done": True, "prompt_eval_count": prompt, "eval_count": out})


@pytest.fixture
def api():
    a = FakeAPI()
    yield a
    a.close()


def backend(api, mode="medium"):
    guard = LocalGuard(lambda: mode, workload=lambda: {"total_mib": 0.0, "runner_pids": []})
    return OllamaBackend("ollama", {"base_url": api.url, "timeout_s": 10}, "qwen3:1.7b", options=guard.request_options)


def test_memory_limits_stay_under_two_gigabytes():
    assert MODES == {"chill": 1200, "medium": 1600, "full": 1900} and ABSOLUTE_MAX_MIB == 2000
    assert all(v < ABSOLUTE_MAX_MIB for v in MODES.values())
    cfg, warnings = config_mod.validate({"build": {"local_memory": "huge"}})
    assert warnings and cfg["build"]["local_memory"] == "medium"
    p = cfg["build"]["providers"]["ollama"]
    assert p["kind"] == "ollama" and p["local"] and not p["enabled"] and p["base_url"] == "http://127.0.0.1:11434"


def test_installed_models_are_listed_and_nothing_is_downloaded(api):
    api.add("/api/tags", (200, TAGS))
    b = backend(api)
    assert b.list_models() == ["qwen3.5:9b", "qwen3:1.7b"]
    api.add("/api/tags", (200, TAGS))
    info = b.models_info()
    assert info[1] == {"name": "qwen3:1.7b", "size_mib": 1335, "parameters": "1.7B", "quantization": "Q4_K_M"}
    assert all("pull" not in r["path"] for r in api.requests) and b.status().available


@pytest.mark.parametrize("mode,ctx", [("chill", 4096), ("medium", 8192), ("full", 16384)])
def test_chat_uses_the_native_api_with_memory_mode_limits(api, mode, ctx):
    api.add("/api/chat", reply("Hello"))
    r = backend(api, mode).chat(ChatRequest([ChatMessage("system", "Be brief."), ChatMessage("user", "Hi")], num_predict=64))
    body = api.requests[0]["body"]
    assert body["options"]["num_ctx"] == ctx and body["options"]["num_predict"] == 64 and body["stream"] is False
    assert body["messages"] == [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hi"}]
    assert r.text == "Hello" and r.usage == {"input_tokens": 120, "cached_tokens": 0, "output_tokens": 30}


def test_tool_calls_round_trip(api):
    api.add("/api/chat", reply(calls=[("read_file", {"path": "a.py"})]), reply("Done"))
    b = backend(api)
    first = b.chat(ChatRequest([ChatMessage("user", "Read a.py")], tools=TOOLS))
    assert first.tool_calls == [{"id": "call_0", "name": "read_file", "arguments": {"path": "a.py"}}]
    assert api.requests[0]["body"]["tools"] == TOOLS
    b.chat(ChatRequest([ChatMessage("user", "Read a.py"),
                        ChatMessage("assistant", "", tool_calls=first.tool_calls, provider_content=first.provider_content),
                        ChatMessage("tool", "print(1)", tool_name="read_file", tool_call_id="call_0")], tools=TOOLS))
    sent = api.requests[1]["body"]["messages"]
    assert sent[1]["tool_calls"] == [{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]
    assert sent[2] == {"role": "tool", "content": "print(1)", "tool_name": "read_file"}


def test_a_model_that_is_not_installed_is_not_downloaded(api):
    api.add("/api/chat", (404, {"error": "model 'qwen3:1.7b' not found"}))
    with pytest.raises(BackendError) as exc:
        backend(api).chat(ChatRequest([ChatMessage("user", "Hi")]))
    assert exc.value.kind == "model_missing" and "does not download" in str(exc.value)
    assert [r["path"] for r in api.requests] == ["/api/chat"]


def test_no_service_is_unavailable():
    b = OllamaBackend("ollama", {"base_url": "http://127.0.0.1:9", "timeout_s": 5}, "m",
                      options=LocalGuard(lambda: "medium", workload=lambda: {"total_mib": 0, "runner_pids": []}).request_options)
    with pytest.raises(BackendError) as exc:
        b.list_models()
    assert exc.value.kind == "unavailable" and "Ollama" in str(exc.value)


# The guard ----------------------------------------------------------------------------------------

def test_guard_caps_runner_processes_to_the_mode_limit():
    capped = []
    guard = LocalGuard(lambda: "chill", workload=lambda: {"total_mib": 500.0, "runner_pids": [41, 42]},
                       cap=lambda pid, n: capped.append((pid, n)), interval=0.01)
    with guard.watch(threading.Event()):
        time.sleep(0.08)
    assert (41, 1200 * 1024 * 1024) in capped and (42, 1200 * 1024 * 1024) in capped and not guard.stopped


def test_guard_stops_and_unloads_when_memory_passes_the_limit():
    readings = iter([800.0, 1300.0, 1700.0])
    unloaded, cancel = [], threading.Event()
    guard = LocalGuard(lambda: "medium", workload=lambda: {"total_mib": next(readings, 1700.0), "runner_pids": [7]},
                       cap=lambda pid, n: None, unload=lambda: unloaded.append(True), interval=0.01)
    with guard.watch(cancel):
        assert cancel.wait(2)
    assert unloaded and "1,600 MiB" in guard.stopped and "Middle" in guard.stopped


def test_guard_survives_when_memory_cannot_be_read():
    def broken():
        raise OSError("access denied")
    guard = LocalGuard(lambda: "full", workload=broken, interval=0.01)
    with guard.watch(threading.Event()):
        time.sleep(0.05)
    assert not guard.stopped


# Through the provider service ----------------------------------------------------------------------

@pytest.fixture
def prov(api, tmp_path):
    cfg = config_mod.validate({})[0]
    cfg["build"]["providers"]["ollama"].update(enabled=True, base_url=api.url)
    cfg["build"]["models"].append({"id": "local-qwen", "provider": "ollama", "model": "qwen3:1.7b", "roles": ["implementer"]})
    return cfg, Providers(lambda: cfg, Storage(tmp_path / "enoy.db"))


def test_local_provider_needs_no_key_and_costs_nothing(prov, api):
    cfg, p = prov
    api.add("/api/tags", (200, TAGS), (200, TAGS))
    assert p.test("ollama")["ok"] and p.status("ollama")["state"] == "ready" and p.status("ollama")["local"]
    assert p.status("ollama")["installed"][0]["name"] == "qwen3.5:9b"
    api.add("/api/chat", reply("OK"))
    p.complete("local-qwen", [ChatMessage("user", "Say OK")], max_output=16)
    call = p.db._one("SELECT * FROM model_calls")
    assert call["cost_usd"] == 0.0 and call["cost_source"] == "local"


def test_memory_stop_becomes_a_clear_limit_error(prov, api, monkeypatch):
    cfg, p = prov
    p.local_guard.workload = lambda: {"total_mib": 1900.0, "runner_pids": [9]}
    p.local_guard.cap = lambda pid, n: None
    p.local_guard.interval = 0.01
    api.add("/api/chat", lambda req: (time.sleep(2), reply("too late"))[1])
    api.add("/api/generate", (200, {"done": True}))
    with pytest.raises(BackendError) as exc:
        p.complete("local-qwen", [ChatMessage("user", "Hi")], max_output=16)
    assert exc.value.kind == "limit" and "MiB" in str(exc.value)
    unload = [r for r in api.requests if r["path"] == "/api/generate"]
    assert unload and unload[0]["body"] == {"model": "qwen3:1.7b", "keep_alive": 0}
