"""Memory modes (supervisor), hardware inventory and PC access permissions."""

import http.client
import json
import threading

import pytest

from conftest import FakeBackend
from fusion_ai import config as config_mod
from fusion_ai import memory_guard, paths, pccontrol as pc, winproc
from fusion_ai.backends.base import CLAUDE, LOCAL, BackendError
from fusion_ai.memory_guard import MODES, MemorySupervisor
from fusion_ai.orchestrator import EventBus, Orchestrator
from fusion_ai.server import App, Server
from fusion_ai.storage import Storage

MIB = winproc.MIB


# ---------------------------------------------------------------- fakes

class FakeOllama(FakeBackend):
    """Local backend with the memory-related calls of the real Ollama backend."""

    def __init__(self, replies=None, size_mib=6290):
        super().__init__(LOCAL, replies)
        self.settings = {"model": "qwen3.5:9b", "num_ctx": 16384, "enabled": True}
        self.size_mib = size_mib
        self.loaded = []
        self.unloaded = []

    def list_models(self):
        return [{"name": "qwen3.5:9b", "size": self.size_mib * MIB}]

    def loaded_models(self):
        return [{"name": n} for n in self.loaded]

    def unload(self, model):
        self.unloaded.append(model)
        self.loaded = [n for n in self.loaded if n != model]


class FakeProcs:
    """Replaces winproc's process calls: an Ollama server, its runner, and an unrelated program."""

    def __init__(self, monkeypatch, runner_mib=0.0):
        self.runner_mib = runner_mib
        self.terminated = []
        monkeypatch.setattr(winproc, "IS_WINDOWS", True)
        monkeypatch.setattr(winproc, "list_processes", self.list)
        monkeypatch.setattr(winproc, "measure", self.measure)
        monkeypatch.setattr(winproc, "terminate", self.terminate)

    def list(self):
        procs = [winproc.ProcInfo(10, 1, "ollama app.exe"), winproc.ProcInfo(20, 10, "ollama.exe"),
                 winproc.ProcInfo(99, 1, "chrome.exe"), winproc.ProcInfo(98, 99, "ollama.exe")]  # A "fake" ollama under chrome.
        if self.runner_mib:
            procs += [winproc.ProcInfo(30, 20, "llama-server.exe"), winproc.ProcInfo(31, 30, "conhost.exe")]
        return procs

    def measure(self, p):
        p.private_ws = {20: 14 * MIB, 30: int(self.runner_mib * MIB), 99: 5000 * MIB, 98: 3 * MIB}.get(p.pid, 1 * MIB)
        p.private_commit = p.private_ws * 8
        return p

    def terminate(self, pid):
        self.terminated.append(pid)
        self.runner_mib = 0
        return True


def supervisor(cfg, mode="chill", backend=None, db=None):
    cfg["memory"]["mode"] = mode
    events = []
    sup = MemorySupervisor(lambda: cfg, db or Storage(":memory:"), backend or FakeOllama(),
                           lambda kind, text, reason="", outcome="", task_id=None: events.append((kind, text, task_id)))
    return sup, events


# ---------------------------------------------------------------- modes and measurement

def test_modes_and_units():
    assert MODES == {"chill": 700, "medium": 1500, "full": 2800}
    assert MIB == 1_048_576
    assert memory_guard.mib(1500 * MIB) == 1500.0


def test_default_mode_is_chill_and_persists(app_home):
    cfg, _ = config_mod.validate({})
    assert cfg["memory"]["mode"] == "chill" and cfg["pc"]["profile"] == "observe"
    cfg["memory"]["mode"] = "full"
    config_mod.save(cfg)
    loaded, _ = config_mod.load()
    assert loaded["memory"]["mode"] == "full"
    with pytest.raises(config_mod.ConfigError):
        config_mod.validate_strict({"memory": {"mode": "turbo"}})


def test_workload_counts_only_ollama_server_and_runner(cfg, monkeypatch):
    FakeProcs(monkeypatch, runner_mib=1000)
    sup, _ = supervisor(cfg)
    w = sup.workload()
    pids = {p["pid"] for p in w["processes"]}
    assert pids == {20, 30}  # Not chrome, not conhost, not the tray app, not a stray ollama.exe client.
    assert w["runner_pids"] == [30]
    assert w["total_mib"] == pytest.approx(1014, abs=1)  # Private memory is summed once per process.


def test_measurement_failure_is_reported(cfg, monkeypatch):
    fp = FakeProcs(monkeypatch, runner_mib=500)
    monkeypatch.setattr(winproc, "measure", lambda p: p)  # Access denied: no numbers.
    sup, _ = supervisor(cfg)
    sup.tick()
    assert sup.state == "measurement_failed"
    assert fp.terminated == []


# ---------------------------------------------------------------- prevention before loading

def test_incompatible_model_is_blocked_in_chill(cfg):
    sup, _ = supervisor(cfg, "chill")
    plan = sup.plan("qwen3.5:9b", 16384)
    assert not plan["ok"] and "Chill mode allows 700 MiB" in plan["reason"] and "Medium or Full" in plan["reason"]


def test_medium_shrinks_context_and_full_keeps_it(cfg):
    sup, _ = supervisor(cfg, "medium")
    plan = sup.plan("qwen3.5:9b", 16384)
    assert plan["ok"] and plan["ctx"] < 16384 and "Context reduced" in plan["note"]
    sup2, _ = supervisor(cfg, "full")
    assert sup2.plan("qwen3.5:9b", 16384) == {**sup2.plan("qwen3.5:9b", 16384), "ok": True, "ctx": 16384}


def test_measured_profile_beats_the_estimate(cfg):
    sup, _ = supervisor(cfg, "medium")
    sup.record("qwen3.5:9b", 16384, 1515)
    sup.record("qwen3.5:9b", 4096, 1075)
    assert sup.estimate("qwen3.5:9b", 16384) == (1515, "measured")
    est, src = sup.estimate("qwen3.5:9b", 8192)
    assert src == "estimated" and 1075 < est < 1515
    assert sup.plan("qwen3.5:9b", 16384)["ctx"] in (8192, 4096)  # 1515 > 1350 warning threshold.


# ---------------------------------------------------------------- monitoring and mitigation

def test_warning_threshold_then_ceiling(cfg, monkeypatch):
    fp = FakeProcs(monkeypatch, runner_mib=1400)
    backend = FakeOllama()
    backend.loaded = ["qwen3.5:9b"]
    sup, events = supervisor(cfg, "medium", backend)
    sup.tick()
    assert sup.state == "approaching"  # 1,414 MiB > 1,350 warning, < 1,500 ceiling.
    assert backend.unloaded == []
    fp.runner_mib = 1600
    cancel = threading.Event()
    sup.begin(7, cancel, "qwen3.5:9b", 8192)
    sup.tick()
    assert cancel.is_set() and backend.unloaded == ["qwen3.5:9b"]
    kinds = [e[0] for e in events]
    assert kinds.index("memory.request_stopped") < kinds.index("memory.model_unloaded")
    assert events[0][2] == 7  # Logged against the task that was stopped.


def test_last_resort_terminates_only_the_runner(cfg, monkeypatch):
    fp = FakeProcs(monkeypatch, runner_mib=900)
    backend = FakeOllama()
    backend.unload = lambda m: None  # Unloading "fails silently": memory stays high.
    backend.loaded = ["qwen3.5:9b"]
    sup, events = supervisor(cfg, "chill", backend)
    sup.tick()
    assert fp.terminated == []
    sup._over_since -= 10  # Still over the ceiling 10 s later.
    sup.tick()
    assert fp.terminated == [30]  # The runner only: never the server, chrome or anything else.
    assert any(e[0] == "memory.runner_terminated" for e in events)


def test_enforcement_off_reports_but_does_not_act(cfg, monkeypatch):
    FakeProcs(monkeypatch, runner_mib=900)
    backend = FakeOllama()
    backend.loaded = ["qwen3.5:9b"]
    cfg["memory"]["enforce"] = False
    sup, events = supervisor(cfg, "chill", backend)
    sup.tick()
    assert sup.state == "exceeded" and backend.unloaded == [] and events[0][0] == "memory.limit_exceeded"


def test_status_reports_hard_cap_unavailable(cfg, monkeypatch):
    FakeProcs(monkeypatch, runner_mib=300)
    sup, _ = supervisor(cfg, "chill")
    sup.tick()
    st = sup.status()
    assert st["within_limit"] and st["state"] == "within" and "Hard cap unavailable" in st["enforcement"]
    assert "Private working set" in st["metric"]


def test_orchestrator_blocks_without_escalating_to_claude(cfg, tmp_path, monkeypatch):
    FakeProcs(monkeypatch)
    cfg["claude"]["confirm_before_send"] = False
    local = FakeOllama(replies=["should not run"])
    orc = Orchestrator(Storage(":memory:"), cfg, {LOCAL: local, CLAUDE: FakeBackend(CLAUDE, ["no"])}, EventBus(),
                       backups_dir=tmp_path / "bk", synchronous=True)
    orc.memory = MemorySupervisor(lambda: orc.cfg, orc.db, local, orc._memory_notify)
    pid = orc.open_project(str(paths.general_chat_dir()))["id"]
    t = orc.db.get_task(orc.start_task(pid, "Explain recursion")["id"])
    assert t["status"] == "failed" and "Chill mode allows 700 MiB" in t["summary"]
    assert local.requests == [] and orc.backends[CLAUDE].requests == []
    orc.cfg["memory"]["mode"] = "medium"
    local.replies = ["Recursion is a function calling itself."]
    t2 = orc.db.get_task(orc.start_task(pid, "Explain recursion")["id"])
    assert t2["status"] == "completed" and local.requests[0].num_ctx < 16384
    assert any("Context reduced" in e["text"] for e in orc.db.events(t2["id"]))


def test_stopped_by_memory_is_not_reported_as_user_cancel(cfg, tmp_path, monkeypatch):
    FakeProcs(monkeypatch)
    local = FakeOllama()
    orc = Orchestrator(Storage(":memory:"), cfg, {LOCAL: local, CLAUDE: FakeBackend(CLAUDE)}, EventBus(),
                       backups_dir=tmp_path / "bk", synchronous=True)
    cfg["memory"]["mode"] = "full"
    orc.memory = MemorySupervisor(lambda: orc.cfg, orc.db, local, orc._memory_notify)

    def stopped_mid_answer(request):
        orc._memory_notify("memory.request_stopped", "Stopped the running Qwen request: over the limit.", task_id=tid[0])
        request.cancel.set()
        raise BackendError("cancelled", "Cancelled.")
    local.replies = [stopped_mid_answer]
    pid = orc.open_project(str(paths.general_chat_dir()))["id"]
    tid = [None]
    orig = orc.db.create_task

    def capture(*a, **k):
        t = orig(*a, **k)
        tid[0] = t["id"]
        return t
    monkeypatch.setattr(orc.db, "create_task", capture)
    t = orc.db.get_task(orc.start_task(pid, "Write a story")["id"])
    assert t["status"] == "failed" and "over the limit" in t["summary"] and "Cancelled by you" not in t["summary"]


# ---------------------------------------------------------------- PC access

def test_profiles_and_least_privilege(cfg):
    assert cfg["pc"]["profile"] == "observe"
    pc.check(cfg, "hardware.read")
    pc.check(cfg, "windows.observe")
    for cap in ("browser.navigate", "apps.open", "windows.control"):
        with pytest.raises(pc.PermissionDenied, match="Assist"):
            pc.check(cfg, cap)
    cfg["pc"]["profile"] = "assist"
    pc.check(cfg, "browser.navigate")
    for cap in ("desktop.interact", "browser.fill_forms", "system.settings.modify", "screen.capture"):
        with pytest.raises(pc.PermissionDenied, match="not available"):
            pc.check(cfg, cap)  # Never faked, in any profile.
    cfg["pc"]["hardware_read"] = False
    with pytest.raises(pc.PermissionDenied):
        pc.check(cfg, "hardware.read")


@pytest.mark.parametrize("text,expected", [
    ("what GPU do I have?", "hardware"), ("How much RAM does my PC have", "hardware"), ("what are my specs", "hardware"),
    ("my code uses too much memory", None), ("Explain CPU caches", None), ("Fix the ram leak in my game", None),
    ("which windows are open?", "list_windows"), ("what is using my RAM?", "processes"),
    ("open youtube.com", "open_url"), ("minimize spotify", "minimize"),
    ("Refactor the login module\nand add tests", None),
])
def test_detect_only_direct_requests(text, expected):
    got = pc.detect(text)
    assert (got[0] if got else None) == expected


def test_urls_are_checked():
    assert pc.check_url("youtube.com") == "https://youtube.com"
    for bad in ("javascript:alert(1)", "file:///C:/Windows/win.ini", "https://user:pass@example.com", "ftp://x.com"):
        with pytest.raises(ValueError):
            pc.check_url(bad)
    assert pc.looks_like_site("https://youtube.com") and not pc.looks_like_site("https://main.py")


def make_orc(cfg, tmp_path, qwen=None):
    return Orchestrator(Storage(":memory:"), cfg, {LOCAL: FakeBackend(LOCAL, qwen), CLAUDE: FakeBackend(CLAUDE)}, EventBus(),
                        backups_dir=tmp_path / "bk", synchronous=True)


def test_chat_pc_requests_respect_the_profile(cfg, tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(pc.webbrowser, "open", lambda url, new=0: opened.append(url))
    orc = make_orc(cfg, tmp_path, qwen=["(normal answer)"])
    pid = orc.open_project(str(paths.general_chat_dir()))["id"]
    t = orc.db.get_task(orc.start_task(pid, "open youtube.com")["id"])
    assert opened == [] and "not allowed in the Observe profile" in orc.db.messages(t["id"])[-1]["content"]
    assert any(a["kind"] == "pc.permission_denied" for a in orc.db.activity(t["id"]))
    orc.cfg["pc"]["profile"] = "assist"
    t = orc.db.get_task(orc.start_task(pid, "open youtube.com")["id"])
    assert opened == ["https://youtube.com"] and t["status"] == "completed"
    assert orc.db.messages(t["id"])[-1]["backend"] == "enoy" and orc.backends[LOCAL].requests == []  # No model used.
    t = orc.db.get_task(orc.start_task(pid, "open main.py")["id"])  # A file name, not a site: normal chat.
    assert opened == ["https://youtube.com"] and orc.backends[LOCAL].requests


def test_only_approved_programs_start(cfg, tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(pc, "open_app", lambda app: started.append(app["path"]))
    cfg["pc"]["profile"] = "assist"
    orc = make_orc(cfg, tmp_path)
    pid = orc.open_project(str(paths.general_chat_dir()))["id"]
    t = orc.start_task(pid, "open notepad")
    assert started == [] and "not on your approved programs list" in orc.db.messages(t["id"])[-1]["content"]
    orc.cfg["pc"]["approved_apps"] = [{"name": "Notepad", "path": r"C:\Windows\notepad.exe"}]
    orc.start_task(pid, "open notepad")
    assert started == [r"C:\Windows\notepad.exe"]


def test_window_actions_need_assist_and_a_real_window(cfg, tmp_path, monkeypatch):
    done = []
    win = pc.Window(123, "Spotify Premium", 5, "Spotify.exe", "normal")
    monkeypatch.setattr(pc, "list_windows", lambda: [win])
    monkeypatch.setattr(pc, "window_action", lambda action, hwnd: done.append((action, hwnd)))
    orc = make_orc(cfg, tmp_path, qwen=["normal answer"])
    pid = orc.open_project(str(paths.general_chat_dir()))["id"]
    t = orc.start_task(pid, "minimize spotify")
    assert done == [] and "Observe" in orc.db.messages(t["id"])[-1]["content"]
    orc.cfg["pc"]["profile"] = "assist"
    orc.start_task(pid, "minimize spotify")
    assert done == [("minimize", 123)]
    orc.start_task(pid, "restore the old version of foo()")  # No such window: an ordinary chat message.
    assert done == [("minimize", 123)] and orc.backends[LOCAL].requests


def test_hardware_question_uses_measured_facts_and_stays_local(cfg, tmp_path, monkeypatch):
    from fusion_ai import hardware
    monkeypatch.setattr(hardware, "inventory", lambda orc=None, max_age_s=30: {
        "cpu": {"name": "Test CPU 9000", "logical_processors": 16}, "memory": {"total_gb": 15.9, "free_gb": 6.0},
        "gpus": [{"name": "Test GPU", "vram_total_mb": 8192, "vram_used_mb": 1000, "driver": "1"}], "disks": [],
        "windows": {"name": "Windows 11"}})
    orc = make_orc(cfg, tmp_path, qwen=["You have a Test GPU with 8 GB."])
    pid = orc.open_project(str(paths.general_chat_dir()))["id"]
    t = orc.start_task(pid, "What GPU do I have?")
    sent = orc.db.transmissions(t["id"], LOCAL)[0]["content"]
    assert "Test GPU, 8,192 MB VRAM" in sent and "measured by Enoy" in sent
    assert orc.backends[CLAUDE].requests == []
    orc.cfg["pc"]["hardware_read"] = False
    t = orc.start_task(pid, "What GPU do I have?")
    assert "not allowed" in orc.db.messages(t["id"])[-1]["content"]


def test_hardware_inventory_reads_this_pc():
    from fusion_ai import hardware
    inv = hardware.inventory(max_age_s=0)
    assert inv["cpu"]["logical_processors"] >= 1
    if winproc.IS_WINDOWS:
        assert inv["memory"]["total_gb"] > 0 and inv["disks"] and inv["windows"]["name"].startswith("Windows")
    assert "CPU:" in hardware.as_text(inv)


# ---------------------------------------------------------------- API

@pytest.fixture
def server(cfg, tmp_path):
    orc = make_orc(cfg, tmp_path)
    srv = Server(App(orc, [], save_config=False)).start()
    yield srv
    srv.stop()


def call(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=20)
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{srv.port}", "X-Fusion-Token": srv.app.token})
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def test_api_profile_changes_need_confirmation_and_are_logged(server):
    assert call(server, "POST", "/api/pc/profile", {"profile": "assist"})[0] == 400
    st, d = call(server, "POST", "/api/pc/profile", {"profile": "assist", "confirm": True})
    assert st == 200 and d["profile"] == "assist"
    assert call(server, "POST", "/api/pc/profile", {"profile": "observe"})[0] == 200  # Revoking needs no confirmation.
    kinds = [a["kind"] for a in server.app.orc.db.activity(None, limit=20)] if False else \
        [r["kind"] for r in server.app.orc.db._all("SELECT kind FROM activity")]
    assert kinds.count("pc.profile_changed") == 2
    cfg = call(server, "GET", "/api/config")[1]["config"]
    cfg["pc"]["profile"] = "automation"
    assert call(server, "POST", "/api/config", {"config": cfg})[0] == 400  # No back door around the confirmation.


def test_api_memory_mode_and_apps(server):
    st, d = call(server, "POST", "/api/memory/mode", {"mode": "medium"})
    assert st == 200 and d["config"]["memory"]["mode"] == "medium"
    assert call(server, "POST", "/api/memory/mode", {"mode": "max"})[0] == 400
    assert call(server, "GET", "/api/memory")[1]["available"] is False  # Not started in tests.
    assert call(server, "POST", "/api/pc/apps", {"apps": [{"name": "Evil", "path": "C:\\x\\run.bat"}]})[0] == 400
    st, d = call(server, "POST", "/api/pc/apps", {"apps": [{"name": "Notepad", "path": "C:\\Windows\\notepad.exe"}]})
    assert st == 200 and d["approved_apps"][0]["name"] == "Notepad"


def test_api_hardware_permission(server):
    assert call(server, "GET", "/api/hardware")[0] == 200
    cfg = call(server, "GET", "/api/config")[1]["config"]
    cfg["pc"]["hardware_read"] = False
    assert call(server, "POST", "/api/config", {"config": cfg})[0] == 200
    assert call(server, "GET", "/api/hardware")[0] == 403
