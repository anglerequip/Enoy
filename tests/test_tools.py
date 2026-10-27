"""Tool registry, runner, adapters (offline) and the Qwen tool loop through the orchestrator."""

import json
import threading
import time

import pytest

from conftest import FakeBackend
from enoy import paths
from enoy import tools as tools_mod
from enoy.backends.base import CLAUDE, LOCAL, ChatMessage, ChatResult
from enoy.backends.claude_code import ClaudeCodeBackend, _tool_events
from enoy.orchestrator import EventBus, Orchestrator
from enoy.storage import Storage
from enoy.tools import registry as reg_mod
from enoy.tools import system as sys_tools
from enoy.tools import web
from enoy.tools.registry import Tool, ToolError, ToolRunner, validate

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
    from enoy.tools import weather
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
    from enoy.tools import search
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










# ---------------------------------------------------------------- cancellation and one Qwen at a time





# ---------------------------------------------------------------- Enoy tools for Claude



def test_mcp_mode_builds_a_strict_config_and_leaves_safe_mode():
    b = ClaudeCodeBackend({"enabled": True, "model": "", "effort": "", "safe_mode": True})
    args = b.build_args("sys", None, web=True, mcp_config="C:/x/enoy-mcp.json", enoy_tools=["get_gpu_info"])
    assert "--safe-mode" not in args and "--strict-mcp-config" in args and "--disable-slash-commands" in args
    assert args[args.index("--setting-sources") + 1] == "project"
    assert "mcp__enoy__get_gpu_info" in args[args.index("--allowedTools") + 1]
    assert "--safe-mode" in b.build_args("sys", None, web=True)


def test_mcp_server_only_runs_offered_tools(monkeypatch, capsys):
    import io
    import sys as _sys
    from enoy.tools import mcp_server
    monkeypatch.setenv("ENOY_MCP_TOOLS", "get_cpu_info")
    monkeypatch.setenv("ENOY_MCP_CFG", json.dumps({"tools": {"permissions": {"web": True}},
                                                   "pc": {"profile": "observe", "hardware_read": True}}))
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_cpu_info", "arguments": {}}},
             {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "web_search", "arguments": {"query": "x"}}}]
    monkeypatch.setattr(_sys, "stdin", io.StringIO("\n".join(json.dumps(x) for x in lines) + "\n"))
    mcp_server.main()
    out = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [r["id"] for r in out] == [1, 2, 3, 4]
    assert [t["name"] for t in out[1]["result"]["tools"]] == ["get_cpu_info"]
    assert not out[2]["result"]["isError"] and "Test CPU 9000" in out[2]["result"]["content"][0]["text"]
    assert out[3]["result"]["isError"] and "not offered" in out[3]["result"]["content"][0]["text"]




def test_brave_stops_at_the_monthly_limit_and_falls_back(cfg, monkeypatch, tmp_path):
    from enoy.tools import search
    monkeypatch.setattr(search, "_usage_file", lambda: tmp_path / "brave-usage.json")
    monkeypatch.setattr(search.keystore, "get", lambda name: "key")
    sent = []
    monkeypatch.setattr(search, "http_get", lambda url, c, cancel=None, timeout=12, accept="", headers=None: (
        sent.append(url) or (json.dumps({"web": {"results": [{"title": "T", "url": "https://x.example", "description": "d"}]}}).encode(), {})))
    monkeypatch.setattr(search, "get_json", lambda url, c, cancel=None, timeout=12: {"query": {"search": []}})
    cfg["tools"]["brave_monthly_limit"] = 2
    providers = [ToolRunner(cfg, tools_mod.registry()).run("web_search", {"query": f"q{i}"}).data["provider"] for i in range(3)]
    assert providers == ["brave", "brave", "wikipedia"] and len(sent) == 2
    assert search.brave_usage()["count"] == 2
