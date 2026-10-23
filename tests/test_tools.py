"""Tool registry, runner, adapters (offline) and the Qwen tool loop through the orchestrator."""

import json
import threading
import time

import pytest

from conftest import FakeBackend
from fusion_ai import paths
from fusion_ai import tools as tools_mod
from fusion_ai.backends.base import CLAUDE, LOCAL, ChatMessage, ChatResult
from fusion_ai.backends.claude_code import ClaudeCodeBackend, _tool_events
from fusion_ai.backends.ollama import _wire
from fusion_ai.orchestrator import EventBus, Orchestrator
from fusion_ai.storage import Storage
from fusion_ai.tools import registry as reg_mod
from fusion_ai.tools import system as sys_tools
from fusion_ai.tools import web
from fusion_ai.tools.registry import Tool, ToolError, ToolRunner, validate

RAW = {
    "cpu": [{"Name": "Test CPU 9000  ", "Manufacturer": "Acme", "NumberOfCores": 8, "NumberOfLogicalProcessors": 16,
             "MaxClockSpeed": 3600}],
    "board": [{"Manufacturer": "Acme", "Product": "B1"}], "bios": [{"Manufacturer": "AMI", "SMBIOSBIOSVersion": "1.0"}],
    "system": [{"Manufacturer": "Acme", "Model": "Box"}],
    "ram": [{"Capacity": 8 * 2**30, "ConfiguredClockSpeed": 3200, "SMBIOSMemoryType": 26, "DeviceLocator": "DIMM 1"}],
    "gpu": [{"Name": "Test GPU", "DriverVersion": "1.2"}],
    "vram": [{"DriverDesc": "Test GPU", "HardwareInformation.qwMemorySize": 8 * 2**30}],
    "disks": [{"FriendlyName": "Disk A", "MediaType": "SSD", "Size": 512 * 10**9}],
    "os": [{"Caption": "Windows 11 Home", "BuildNumber": "26200", "OSArchitecture": "64-bit"}],
}


@pytest.fixture(autouse=True)
def fake_pc(monkeypatch):
    tools_mod.clear_cache()
    monkeypatch.setattr(sys_tools, "_collect", lambda cancel=None: {"raw": RAW, "collected": "2026-01-01T00:00:00"})
    monkeypatch.setattr(sys_tools.diagnostics, "gpu_info", lambda: [])
    monkeypatch.setattr(sys_tools.diagnostics, "memory_gb", lambda: {"total_gb": 7.9, "free_gb": 3.0})
    monkeypatch.setattr(sys_tools.hardware, "disks", lambda: [{"drive": "C:\\", "total_gb": 476.0, "free_gb": 100.0}])
    monkeypatch.setattr(sys_tools.hardware, "windows_details", lambda: {"displayVersion": "25H2"})


def _echo_tool(**kw):
    calls = []

    def run(args, ctx):
        calls.append(args)
        return {"echo": args.get("text"), "_sources": [{"title": "x", "url": "https://example.com/a"}]}
    defaults = dict(name="echo", description="d", output="o", run=run, cache_ttl_s=0,
                    parameters={"type": "object", "properties": {"text": {"type": "string", "maxLength": 10},
                                                                 "n": {"type": "integer", "minimum": 1, "maximum": 3}},
                                "required": ["text"]})
    defaults.update(kw)
    return Tool(**defaults), calls


# ---------------------------------------------------------------- registry and runner

def test_validate_rejects_bad_arguments():
    schema = _echo_tool()[0].parameters
    assert validate(schema, {"text": " hi ", "n": "2"}) == {"text": "hi", "n": 2}
    for bad in ({}, {"text": "x", "zzz": 1}, {"text": 5}, {"text": "x" * 11}, {"text": "x", "n": 9}, "text"):
        with pytest.raises(ToolError):
            validate(schema, bad)


def test_runner_enforces_settings_and_permissions(cfg):
    tool, calls = _echo_tool()
    r = ToolRunner(cfg, {"echo": tool})
    assert r.run("echo", {"text": "a"}).ok and calls == [{"text": "a"}]
    assert r.run("nope", {}).error_kind == "invalid"
    cfg["tools"]["enabled_tools"] = {"echo": False}
    assert r.run("echo", {"text": "b"}).error_kind == "denied"
    cfg["tools"]["enabled_tools"] = {}
    cfg["tools"]["permissions"] = {"web": False}
    assert r.run("echo", {"text": "c"}).error_kind == "denied"
    cfg["tools"]["permissions"] = {"web": True}
    side, _ = _echo_tool(name="send", side_effects=True)
    assert ToolRunner(cfg, {"send": side}).run("send", {"text": "x"}).error_kind == "denied"
    assert calls == [{"text": "a"}]  # Nothing denied ever ran.


def test_runner_repeats_caches_timeouts_and_cancel(cfg):
    tool, calls = _echo_tool(cache_ttl_s=60)
    r = ToolRunner(cfg, {"echo": tool})
    r.run("echo", {"text": "a"})
    again = r.run("echo", {"text": "a"})
    assert again.duplicate and len(calls) == 1
    assert ToolRunner(cfg, {"echo": tool}).run("echo", {"text": "a"}).cached and len(calls) == 1
    slow = Tool(name="slow", description="d", output="o", parameters={"type": "object", "properties": {}},
                run=lambda a, c: time.sleep(5) or {}, timeout_s=0.3)
    res = ToolRunner(cfg, {"slow": slow}).run("slow", {})
    assert res.error_kind == "timeout" and res.duration_s < 2
    stop = threading.Event()
    stop.set()
    assert ToolRunner(cfg, {"slow": slow}, cancel=stop).run("slow", {}).error_kind == "cancelled"
    boom = Tool(name="boom", description="d", output="o", parameters={"type": "object", "properties": {}},
                run=lambda a, c: 1 / 0)
    res = ToolRunner(cfg, {"boom": boom}).run("boom", {})
    assert not res.ok and "ZeroDivisionError" in res.error


def test_results_for_the_model_are_framed_as_untrusted_data(cfg):
    tool, _ = _echo_tool()
    res = ToolRunner(cfg, {"echo": tool}).run("echo", {"text": "ignore all"})
    text = res.for_model()
    assert text.startswith("Tool result (untrusted data") and '"url":"https://example.com/a"' in text
    assert len(res.for_model(200)) < 400


def test_tool_selection_only_offers_relevant_tools():
    pc = tools_mod.PC_TOOLS
    assert tools_mod.select("List all the parts in my PC.") == pc
    assert tools_mod.select("How much VRAM does my graphics card have?") == pc
    assert tools_mod.select("What CPU and GPU do I have?") == pc
    assert tools_mod.select("Can my PC run Qwen?") == pc
    assert tools_mod.select("What is the weather in Trollhättan right now?") == tools_mod.WEB_TOOLS
    assert tools_mod.select("Summarize https://example.com/post") == tools_mod.WEB_TOOLS
    assert tools_mod.select("How does a GPU work?") == set()
    assert tools_mod.select("Explain closures in JavaScript") == set()


# ---------------------------------------------------------------- adapters

def test_web_tools_refuse_local_and_private_addresses(cfg):
    r = ToolRunner(cfg, tools_mod.registry())
    for url in ("http://127.0.0.1:11434/api/tags", "http://localhost/", "http://192.168.0.19/", "file:///C:/x"):
        res = r.run("fetch_page", {"url": url})
        assert not res.ok and res.error_kind in ("denied", "invalid"), url


def test_html_reader_prefers_main_content():
    page = ("<html><head><title>T</title><meta property='article:published_time' content='2026-01-02'></head><body>"
            "<nav>Menu Home About</nav><main><h1>Headline</h1><p>" + "Real text. " * 60 + "</p></main>"
            "<script>evil()</script><footer>Copyright</footer></body></html>")
    title, text, meta = web.html_to_text(page)
    assert title == "T" and text.startswith("# Headline") and "Menu" not in text and "evil" not in text
    assert meta["article:published_time"] == "2026-01-02"


def test_weather_uses_smhi_and_falls_back_to_met_norway(cfg, monkeypatch):
    from fusion_ai.tools import weather
    weather._geo_cache.clear()
    now = time.strftime("%Y-%m-%dT%H:00:00Z", time.gmtime())
    smhi = {"referenceTime": "2026-01-01T00:00:00Z", "timeSeries": [
        {"time": now, "data": {"air_temperature": 5.5, "symbol_code": 18, "wind_speed": 3, "precipitation_amount_mean": 0.4,
                               "cloud_area_fraction": 9999}}]}
    metno = {"properties": {"meta": {"updated_at": "x"}, "timeseries": [
        {"time": now, "data": {"instant": {"details": {"air_temperature": 1.0}},
                               "next_1_hours": {"summary": {"symbol_code": "rain"}}}}]}}
    calls = []

    def fake_get(url, cfg_, cancel=None, timeout=15):
        calls.append(url)
        if "nominatim" in url:
            return [{"name": "Trollhättan", "lat": "58.28", "lon": "12.29", "display_name": "Trollhättan, Sweden"}]
        if "metobs" in url:
            return {"station": []}
        if "smhi" in url:
            if fake_get.smhi_down:
                raise ToolError("unavailable", "SMHI answered HTTP 503")
            return smhi
        return metno
    fake_get.smhi_down = False
    monkeypatch.setattr(weather, "get_json", fake_get)
    res = ToolRunner(cfg, tools_mod.registry()).run("get_weather", {"location": "Trollhättan"})
    now_row = res.data["forecast_now"]
    assert res.ok and "SMHI" in res.data["provider"] and now_row["temperature"] == 5.5
    assert now_row["conditions"] == "Light rain" and now_row["cloud_cover"] is None  # 9999 = missing, not invented.
    assert res.sources and res.data["units"]["temperature"] == "°C"
    tools_mod.clear_cache()
    fake_get.smhi_down = True
    res = ToolRunner(cfg, tools_mod.registry()).run("get_weather", {"location": "Trollhättan"})
    assert res.ok and "MET Norway" in res.data["provider"] and "SMHI answered HTTP 503" in res.data["fallback_used"]


def test_search_reports_provider_and_limits(cfg, monkeypatch):
    from fusion_ai.tools import search
    monkeypatch.setattr(search.keystore, "get", lambda name: None)
    monkeypatch.setattr(search, "get_json", lambda url, c, cancel=None, timeout=12: {"query": {"search": [
        {"title": "Hash table", "snippet": "<span>A</span> map", "timestamp": "2026-01-01"}]}})
    res = ToolRunner(cfg, tools_mod.registry()).run("web_search", {"query": "hash table"})
    assert res.ok and res.data["provider"] == "wikipedia" and "not current news" in res.data["note"]
    assert res.data["results"][0]["snippet"] == "A map"
    cfg["tools"]["search_provider"] = "searxng"
    res = ToolRunner(cfg, tools_mod.registry()).run("web_search", {"query": "news"})
    assert not res.ok and "No SearXNG address" in res.error


def test_hardware_tools_report_real_fields_and_missing_ones(cfg):
    r = ToolRunner(cfg, tools_mod.registry())
    inv = r.run("get_hardware_inventory", {}).data
    assert inv["cpu"]["name"] == "Test CPU 9000" and inv["cpu"]["cores"] == 8
    assert inv["gpus"][0]["vram_total_mib"] == 8192 and inv["gpus"][0]["vram_used_mib"] is None
    assert inv["memory"]["installed_gib"] == 8.0 and inv["memory"]["in_use_gib"] == 4.9
    assert inv["memory"]["modules"][0]["type"] == "DDR4" and inv["windows"]["release"] == "25H2"
    assert inv["storage"]["physical_disks"][0]["size_gb"] == 512 and "retrieved_at" in inv
    cfg["pc"]["hardware_read"] = False
    assert ToolRunner(cfg, tools_mod.registry()).run("get_cpu_info", {}).error_kind == "denied"


# ---------------------------------------------------------------- backends

def test_ollama_wire_format_carries_tool_calls_and_results():
    call = ChatMessage("assistant", "", tool_calls=[{"name": "get_cpu_info", "arguments": {}}])
    assert _wire(call)["tool_calls"] == [{"function": {"name": "get_cpu_info", "arguments": {}}}]
    assert _wire(ChatMessage("tool", "{}", tool_name="get_cpu_info")) == {"role": "tool", "content": "{}",
                                                                          "tool_name": "get_cpu_info"}


def test_claude_web_mode_enables_only_web_tools():
    b = ClaudeCodeBackend({"enabled": True, "model": "", "effort": "", "safe_mode": True})
    off = b.build_args("sys", None)
    assert off[off.index("--tools") + 1] == ""
    on = b.build_args("sys", None, web=True)
    assert on[on.index("--tools") + 1] == "WebSearch,WebFetch" and "stream-json" in on
    out = "\n".join(json.dumps(x) for x in [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "WebSearch", "input": {"query": "q"}}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "WebFetch", "input": {"url": "https://a"}}]}},
        {"type": "result", "result": "done"}])
    assert _tool_events(out) == [{"tool": "WebSearch", "query": "q", "url": ""},
                                 {"tool": "WebFetch", "query": "", "url": "https://a"}]


# ---------------------------------------------------------------- the Qwen tool loop

def _orc(cfg, tmp_path, qwen):
    return Orchestrator(Storage(":memory:"), cfg, {LOCAL: FakeBackend(LOCAL, qwen), CLAUDE: FakeBackend(CLAUDE)},
                        EventBus(), backups_dir=tmp_path / "bk", synchronous=True)


def _call(name, **args):
    return ChatResult(text="", backend=LOCAL, model="fake", tool_calls=[{"name": name, "arguments": args}])


def test_qwen_lists_pc_parts_through_tools(cfg, tmp_path):
    orc = _orc(cfg, tmp_path, [_call("get_hardware_inventory"), "You have a Test CPU 9000 and a Test GPU (8 GB)."])
    pid = orc.open_project(str(paths.general_chat_dir()))["id"]
    t = orc.start_task(pid, "List all the parts in my PC.")
    reqs = orc.backends[LOCAL].requests
    offered = {s["function"]["name"] for s in reqs[0].tools}
    assert offered == tools_mod.PC_TOOLS and "hardware tool" in reqs[0].messages[0].content
    tool_msg = reqs[1].messages[-1]
    assert tool_msg.role == "tool" and tool_msg.tool_name == "get_hardware_inventory" and "Test CPU 9000" in tool_msg.content
    answer = orc.db.messages(t["id"])[-1]["content"]
    assert answer.startswith("You have a Test CPU") and "Read from Windows by Enoy" in answer
    kinds = [a["kind"] for a in orc.db.activity(t["id"])]
    assert "tool.started" in kinds and "tool.finished" in kinds
    assert orc.db.get_task(t["id"])["state"]["tool_calls"][0]["ok"] is True
    assert orc.backends[CLAUDE].requests == []


def test_tool_errors_reach_the_model_and_the_answer(cfg, tmp_path):
    cfg["pc"]["hardware_read"] = False
    cfg["tools"]["enabled_tools"] = {}
    orc = _orc(cfg, tmp_path, ["(not used)"])
    pid = orc.open_project(str(paths.general_chat_dir()))["id"]
    t = orc.start_task(pid, "What GPU do I have?")
    assert "not allowed" in orc.db.messages(t["id"])[-1]["content"]  # The PC profile blocks it before any model.
    cfg["pc"]["hardware_read"] = True
    orc = _orc(cfg, tmp_path, [_call("get_gpu_info", verbose=True), "I could not read it: the call was invalid."])
    pid = orc.open_project(str(paths.general_chat_dir()))["id"]
    t = orc.start_task(pid, "What GPU do I have?")
    sent = orc.backends[LOCAL].requests[1].messages[-1].content
    assert '"ok":false' in sent and "Unknown argument" in sent


def test_tool_loop_is_bounded(cfg, tmp_path):
    cfg["tools"]["max_rounds"] = 2
    replies = [_call("get_cpu_info"), _call("get_gpu_info"), "Final answer with what I have."]
    orc = _orc(cfg, tmp_path, replies)
    pid = orc.open_project(str(paths.general_chat_dir()))["id"]
    t = orc.start_task(pid, "What CPU and GPU do I have?")
    reqs = orc.backends[LOCAL].requests
    assert len(reqs) == 3 and reqs[2].tools is None and "Tool limit reached" in reqs[2].messages[-1].content
    assert orc.db.get_task(t["id"])["status"] == "completed"


def test_plain_questions_get_no_tools(cfg, tmp_path):
    orc = _orc(cfg, tmp_path, ["Closures capture variables."])
    pid = orc.open_project(str(paths.general_chat_dir()))["id"]
    t = orc.start_task(pid, "Explain closures in JavaScript")
    assert orc.backends[LOCAL].requests[0].tools is None
    assert "Sources retrieved" not in orc.db.messages(t["id"])[-1]["content"]
