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
        self.settings = {"model": "qwen3.5:9b", "num_ctx": 16384, "num_predict": 4096, "enabled": True}
        self.size_mib = size_mib
        self.loaded = []
        self.unloaded = []

    def list_models(self):
        return [{"name": "qwen3.5:9b", "size": self.size_mib * MIB,
                 "details": {"parameter_size": "9.7B", "quantization_level": "Q4_K_M"}}]

    def loaded_models(self):
        return [{"name": n, "size": 6000 * MIB, "size_vram": 5300 * MIB, "context_length": 4096} for n in self.loaded]

    def unload(self, model):
        self.unloaded.append(model)
        self.loaded = [n for n in self.loaded if n != model]


class FakeProcs:
    """Replaces winproc's process calls: an Ollama server, its runner, and an unrelated program."""

    def __init__(self, monkeypatch, runner_mib=0.0, second_runner_mib=0.0, cap_ok=True):
        self.runner_mib = runner_mib
        self.second_runner_mib = second_runner_mib
        self.terminated = []
        self.caps = []          # (pid, MiB) asked of Windows.
        self.cap_ok = cap_ok
        monkeypatch.setattr(winproc, "IS_WINDOWS", True)
        monkeypatch.setattr(winproc, "list_processes", self.list)
        monkeypatch.setattr(winproc, "measure", self.measure)
        monkeypatch.setattr(winproc, "terminate", self.terminate)
        monkeypatch.setattr(winproc, "cap_working_set", self.cap)
        monkeypatch.setattr(winproc, "release_working_set_cap", lambda pid: self.released.append(pid) or True)
        self.released = []

    def list(self):
        procs = [winproc.ProcInfo(10, 1, "ollama app.exe"), winproc.ProcInfo(20, 10, "ollama.exe"),
                 winproc.ProcInfo(99, 1, "chrome.exe"), winproc.ProcInfo(98, 99, "ollama.exe")]  # A "fake" ollama under chrome.
        if self.runner_mib:
            procs += [winproc.ProcInfo(30, 20, "llama-server.exe"), winproc.ProcInfo(31, 30, "conhost.exe")]
        if self.second_runner_mib:
            procs += [winproc.ProcInfo(32, 20, "llama-server.exe")]
        return procs

    def measure(self, p):
        p.private_ws = {20: 14 * MIB, 30: int(self.runner_mib * MIB), 32: int(self.second_runner_mib * MIB),
                        99: 5000 * MIB, 98: 3 * MIB}.get(p.pid, 1 * MIB)
        p.private_commit = p.private_ws * 8
        p.working_set = p.private_ws + 100 * MIB
        return p

    def cap(self, pid, max_bytes):
        self.caps.append((pid, round(max_bytes / MIB)))
        if not self.cap_ok:
            return None
        if pid == 30:  # Windows trims the working set to the new cap at once.
            self.runner_mib = min(self.runner_mib, max_bytes / MIB)
        return max_bytes

    def terminate(self, pid):
        self.terminated.append(pid)
        self.runner_mib = 0
        return True


@pytest.fixture(autouse=True)
def plenty_of_free_ram(monkeypatch):
    """Tests decide free RAM and Enoy's own memory; the real PC's numbers would make them flaky."""
    monkeypatch.setattr(memory_guard, "free_ram_mib", lambda: 16000.0)
    monkeypatch.setattr(memory_guard, "own_memory", lambda: {"total_mib": 180.0, "processes": 3, "complete": True})


def supervisor(cfg, mode="chill", backend=None, db=None):
    cfg["memory"]["mode"] = mode
    events = []
    sup = MemorySupervisor(lambda: cfg, db or Storage(":memory:"), backend or FakeOllama(),
                           lambda kind, text, reason="", outcome="", task_id=None: events.append((kind, text, task_id)))
    return sup, events


# ---------------------------------------------------------------- modes and measurement

def test_modes_and_units():
    assert MODES == {"chill": 1200, "medium": 1600, "full": 1900}
    assert memory_guard.LABEL == {"chill": "Chill", "medium": "Middle", "full": "Full"}
    assert MIB == 1_048_576  # MiB, not decimal MB.
    assert memory_guard.mib(1500 * MIB) == 1500.0


def test_full_mode_stays_below_2000_mib(cfg):
    assert memory_guard.ABSOLUTE_MAX_MIB == 2000
    assert max(MODES.values()) < 2000 and MODES["full"] == 1900
    for mode in MODES:
        sup, _ = supervisor(cfg, mode)
        assert sup.warn_mib < sup.stop_mib < sup.ceiling_mib < 2000
        assert all(m["ceiling_mib"] < 2000 for m in sup.status()["modes"].values())
    with pytest.raises(config_mod.ConfigError):  # The limits are not a setting anyone can raise.
        config_mod.validate_strict({"memory": {"ceiling_mib": 4000}})


def test_each_mode_selects_its_budget_and_runtime(cfg):
    for mode, ceiling, ctx, predict, batch in [("chill", 1200, 4096, 2048, 256), ("medium", 1600, 8192, 4096, 256),
                                               ("full", 1900, 16384, None, None)]:
        sup, _ = supervisor(cfg, mode)
        st = sup.status()
        assert (st["ceiling_mib"], st["runtime"]["max_ctx"], st["runtime"]["max_predict"], st["runtime"]["num_batch"]) == \
            (ceiling, ctx, predict, batch)
        assert st["warn_mib"] == round(ceiling * 0.9, 1) and st["stop_mib"] == round(ceiling * 0.96, 1)


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

def test_incompatible_model_is_blocked_with_an_explanation(cfg, monkeypatch):
    FakeProcs(monkeypatch)
    sup, _ = supervisor(cfg, "chill", FakeOllama(size_mib=9000))  # About 1,500 MiB even at 2k context.
    plan = sup.plan("qwen3.5:9b", 16384)
    assert not plan["ok"]
    r = plan["reason"]
    assert "Chill mode starts requests only below 1,080 MiB (limit 1,200 MiB)" in r
    assert "Q4_K_M quantization" in r and "2,048-token context" in r and "estimated" in r
    assert "switch to Middle or Full mode" in r and "smaller quantization" in r and "lower the context size" in r
    assert "Claude" not in r


def test_modes_cap_the_context_and_answer_length(cfg, monkeypatch):
    FakeProcs(monkeypatch)
    sup, _ = supervisor(cfg, "chill")
    plan = sup.plan("qwen3.5:9b", 16384, 4096)
    # 6,290 MiB file: about 950 + 112 MiB at 4k fits under the 1,080 MiB start point.
    assert plan["ok"] and plan["ctx"] == 4096 and plan["num_predict"] == 2048 and plan["num_batch"] == 256
    assert "Context capped from 16,384 to 4,096" in plan["note"] and "Answer length capped" in plan["note"]
    sup, _ = supervisor(cfg, "medium")
    plan = sup.plan("qwen3.5:9b", 16384, 4096)
    assert plan["ok"] and plan["ctx"] == 8192 and plan["num_predict"] == 4096
    sup, _ = supervisor(cfg, "full")
    plan = sup.plan("qwen3.5:9b", 16384, 4096)
    assert plan["ok"] and plan["ctx"] == 16384 and plan["num_predict"] == 4096 and plan["num_batch"] is None
    assert sup.plan("qwen3.5:9b", 2048, 1024)["ctx"] == 2048  # A smaller own setting is never raised.


def test_measured_profile_beats_the_estimate(cfg, monkeypatch):
    FakeProcs(monkeypatch)
    sup, _ = supervisor(cfg, "medium")
    sup.record("qwen3.5:9b", 16384, 1521)
    sup.record("qwen3.5:9b", 4096, 1082)
    assert sup.estimate("qwen3.5:9b", 16384) == (1521, "measured")
    est, src = sup.estimate("qwen3.5:9b", 8192)
    assert src == "estimated" and 1082 < est < 1521
    plan = sup.plan("qwen3.5:9b", 16384)
    assert plan["ctx"] == 8192 and "capped" in plan["note"]  # Middle's context cap; the estimate fits under 1,440.
    cfg["memory"]["mode"] = "chill"
    assert sup.plan("qwen3.5:9b", 16384)["ctx"] == 2048  # 4k measured at 1,082 > 1,080 warning.


def test_new_load_replaces_old_readings_and_small_contexts_stay_below_them(cfg, monkeypatch):
    fp = FakeProcs(monkeypatch, runner_mib=1700)
    sup, _ = supervisor(cfg, "full")
    sup.record("qwen3.5:9b", 8192, 1622)  # Old readings from a heavier day.
    sup.record("qwen3.5:9b", 16384, 1747)
    est, src = sup.estimate("qwen3.5:9b", 4096)
    assert src == "estimated" and est == pytest.approx(1062, abs=1)  # From the file size, below 1,622.
    assert sup.estimate("qwen3.5:9b", 12288)[0] == pytest.approx(1684.5)  # Between the two readings.
    cfg["memory"]["os_cap"] = False
    fp.runner_mib = 1300
    sup.begin(1, threading.Event(), "qwen3.5:9b", 16384)
    sup.tick()
    assert sup.estimate("qwen3.5:9b", 16384) == (1314, "measured")  # Today's load replaced the old 1,747.
    fp.runner_mib = 1400
    sup.tick()
    assert sup.estimate("qwen3.5:9b", 16384) == (1414, "measured")  # Within one load: the peak.
    assert sup.forget() == 2 and sup._profiles() == {}


def test_other_models_are_unloaded_before_loading(cfg, monkeypatch):
    FakeProcs(monkeypatch)
    backend = FakeOllama()
    backend.loaded = ["llama3:8b", "qwen3.5:9b"]
    sup, events = supervisor(cfg, "full", backend)
    plan = sup.plan("qwen3.5:9b", 16384)
    assert plan["ok"] and backend.unloaded == ["llama3:8b"] and "Unloaded llama3:8b" in plan["note"]
    assert [e[0] for e in events] == ["memory.extra_model_unloaded"]


def test_loading_is_blocked_when_memory_cannot_be_measured(cfg, monkeypatch):
    FakeProcs(monkeypatch)
    monkeypatch.setattr(winproc, "measure", lambda p: p)  # Access denied.
    sup, _ = supervisor(cfg, "full")
    plan = sup.plan("qwen3.5:9b", 16384)
    assert not plan["ok"] and "could not be checked" in plan["reason"] and sup.blocked


def test_loading_is_blocked_when_windows_is_short_of_ram(cfg, monkeypatch):
    FakeProcs(monkeypatch)
    monkeypatch.setattr(memory_guard, "free_ram_mib", lambda: 1200.0)
    sup, _ = supervisor(cfg, "full")
    plan = sup.plan("qwen3.5:9b", 16384)
    assert not plan["ok"] and "only 1,200 MiB of RAM free" in plan["reason"]


# ---------------------------------------------------------------- monitoring and mitigation

def test_warning_threshold_then_stop_point(cfg, monkeypatch):
    fp = FakeProcs(monkeypatch, runner_mib=1450)
    backend = FakeOllama()
    backend.loaded = ["qwen3.5:9b"]
    cfg["memory"]["os_cap"] = False
    sup, events = supervisor(cfg, "medium", backend)
    sup.tick()
    assert sup.state == "approaching"  # 1,464 MiB > 1,440 warning, < 1,536 stop point.
    assert backend.unloaded == []
    fp.runner_mib = 1530  # 1,544 MiB: past the stop point, still under the 1,600 MiB ceiling.
    cancel = threading.Event()
    sup.begin(7, cancel, "qwen3.5:9b", 8192)
    sup.tick()
    assert cancel.is_set() and backend.unloaded == ["qwen3.5:9b"]
    kinds = [e[0] for e in events]
    assert kinds.index("memory.request_stopped") < kinds.index("memory.model_unloaded")
    assert events[0][2] == 7  # Logged against the task that was stopped.


def test_last_resort_terminates_only_the_runner(cfg, monkeypatch):
    fp = FakeProcs(monkeypatch, runner_mib=1300)
    cfg["memory"]["os_cap"] = False  # The supervisor alone, as when Windows refuses the cap.
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


def test_enforcement_cannot_be_switched_off(cfg, monkeypatch):
    FakeProcs(monkeypatch, runner_mib=1300)
    backend = FakeOllama()
    backend.loaded = ["qwen3.5:9b"]
    cfg["memory"]["enforce"] = False  # Old setting: ignored, so Full can never pass 2,000 MiB on purpose.
    cfg["memory"]["os_cap"] = False
    sup, events = supervisor(cfg, "chill", backend)
    sup.tick()
    assert sup.state == "unloading" and backend.unloaded == ["qwen3.5:9b"]


def test_windows_cap_is_applied_to_runners_only_and_follows_the_mode(cfg, monkeypatch):
    fp = FakeProcs(monkeypatch, runner_mib=1000)
    sup, events = supervisor(cfg, "chill")
    sup.tick()
    assert fp.caps == [(30, 1138)]  # Stop point 1,152 minus the server's 14 MiB. Never the server or chrome.
    sup.tick()
    assert fp.caps == [(30, 1138)]  # Not asked again while nothing changed.
    assert [e[0] for e in events] == ["memory.os_cap_applied"]
    cfg["memory"]["mode"] = "full"
    sup.tick()
    assert fp.caps[-1] == (30, 1810)  # 1,824 - 14: a new mode re-caps the running runner.
    st = sup.status()
    assert st["hard_cap"] is True and "Windows hard cap" in st["enforcement"] and st["os_cap"]["cap_mib"] == 1810
    cfg["memory"]["os_cap"] = False
    sup.tick()
    assert fp.released == [30] and sup.status()["hard_cap"] is False and events[-1][0] == "memory.os_cap_removed"


def test_a_model_already_over_the_limit_is_capped_not_unloaded(cfg, monkeypatch):
    FakeProcs(monkeypatch, runner_mib=1700)  # Loaded by another program before Enoy started.
    backend = FakeOllama()
    backend.loaded = ["qwen3.5:9b"]
    sup, events = supervisor(cfg, "medium", backend)
    sup.tick()
    assert backend.unloaded == [] and sup.state == "at_cap" and sup.current["total_mib"] <= sup.stop_mib
    kinds = [e[0] for e in events]
    assert "memory.os_cap_applied" in kinds and "memory.model_unloaded" not in kinds


def test_a_vanished_gpu_check_runner_is_not_reported_as_refused(cfg, monkeypatch):
    fp = FakeProcs(monkeypatch, runner_mib=5, cap_ok=False)  # Ollama's short GPU check: gone before the cap lands.
    sup, events = supervisor(cfg, "chill")
    sup.tick()
    assert events == [] and sup.status()["hard_cap"] is True
    fp.runner_mib = 0
    sup.tick()
    assert events == [] and sup._caps == {}


def test_refused_windows_cap_is_reported(cfg, monkeypatch):
    FakeProcs(monkeypatch, runner_mib=1000, cap_ok=False)
    sup, events = supervisor(cfg, "chill")
    sup.tick()
    st = sup.status()
    assert st["hard_cap"] is False and "refused" in st["enforcement"] and "supervisor only" in st["enforcement"]
    assert events[0][0] == "memory.os_cap_failed"
    cfg["memory"]["os_cap"] = False
    sup2, _ = supervisor(cfg, "chill")
    sup2.tick()
    assert "cap is off" in sup2.status()["enforcement"]


def test_a_runner_held_at_the_cap_is_not_recorded_as_fitting(cfg, monkeypatch):
    fp = FakeProcs(monkeypatch, runner_mib=1000)
    sup, events = supervisor(cfg, "chill")
    sup.tick()  # Caps the runner at 1,138 MiB.
    fp.runner_mib = 1135  # Windows holds it there: paging.
    sup.begin(4, threading.Event(), "qwen3.5:9b", 4096)
    sup.tick()
    assert sup.state == "at_cap" and any(e[0] == "memory.at_cap" and e[2] == 4 for e in events)
    assert sup.estimate("qwen3.5:9b", 4096)[0] > sup.warn_mib  # Next time Chill picks a smaller context.


def test_duplicate_runners_keep_only_the_model_in_use(cfg, monkeypatch):
    FakeProcs(monkeypatch, runner_mib=600, second_runner_mib=500)
    backend = FakeOllama()
    backend.loaded = ["llama3:8b", "qwen3.5:9b"]
    sup, events = supervisor(cfg, "full", backend)
    sup.begin(3, threading.Event(), "qwen3.5:9b", 8192)
    sup.tick()
    assert backend.unloaded == ["llama3:8b"]
    assert any(e[0] == "memory.duplicate_unloaded" for e in events)


def test_processes_are_counted_once(cfg, monkeypatch):
    fp = FakeProcs(monkeypatch, runner_mib=1000)
    orig = fp.list
    monkeypatch.setattr(winproc, "list_processes", lambda: orig() + [winproc.ProcInfo(30, 20, "llama-server.exe")])
    sup, _ = supervisor(cfg)
    w = sup.workload()
    assert w["runner_pids"] == [30] and w["total_mib"] == pytest.approx(1014, abs=1)


def test_status_separates_the_metrics(cfg, monkeypatch):
    FakeProcs(monkeypatch, runner_mib=300)
    backend = FakeOllama()
    backend.loaded = ["qwen3.5:9b"]
    sup, _ = supervisor(cfg, "chill", backend)
    sup.tick()
    st = sup.status("qwen3.5:9b", 16384)
    assert st["within_limit"] and st["state"] == "within" and "Private working set" in st["metric"]
    assert st["total_mib"] == 314 and st["headroom_mib"] == 886
    assert st["commit_mib"] == 314 * 8 and st["working_set_mib"] == 514  # Reported, not counted.
    assert st["gpu"]["vram_mib"] == 5300 and st["own"]["total_mib"] == 180.0
    assert st["modes"]["chill"]["fit"]["fits"] is True and st["modes"]["full"]["fit"]["ctx"] == 16384


def test_orchestrator_blocks_without_escalating_to_claude(cfg, tmp_path, monkeypatch):
    FakeProcs(monkeypatch)
    cfg["claude"]["confirm_before_send"] = False
    local = FakeOllama(replies=["should not run"], size_mib=9000)
    orc = Orchestrator(Storage(":memory:"), cfg, {LOCAL: local, CLAUDE: FakeBackend(CLAUDE, ["no"])}, EventBus(),
                       backups_dir=tmp_path / "bk", synchronous=True)
    orc.memory = MemorySupervisor(lambda: orc.cfg, orc.db, local, orc._memory_notify)
    pid = orc.open_project(str(paths.general_chat_dir()))["id"]
    t = orc.db.get_task(orc.start_task(pid, "Explain recursion")["id"])
    assert t["status"] == "failed" and "Chill mode starts requests only below 1,080 MiB" in t["summary"]
    assert local.requests == [] and orc.backends[CLAUDE].requests == []
    blocked = [a for a in orc.db.activity(t["id"]) if a["kind"] == "memory.load_blocked"]
    assert blocked and blocked[0]["outcome"] == "blocked" and "1,200 MiB" in blocked[0]["reason"]
    orc.cfg["memory"]["mode"] = "full"
    local.replies = ["Recursion is a function calling itself."]
    t2 = orc.db.get_task(orc.start_task(pid, "Explain recursion")["id"])
    req = local.requests[0]
    assert t2["status"] == "completed" and req.num_ctx == 8192 and req.num_predict == 4096
    assert any("Context reduced" in e["text"] for e in orc.db.events(t2["id"]))
    assert any(a["kind"] == "memory.context_reduced" for a in orc.db.activity(t2["id"]))


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
    st, d = call(server, "GET", "/api/memory")
    assert d["available"] is False and d["ceiling_mib"] == 1600 and d["label"] == "Middle"  # Not started in tests.
    assert call(server, "POST", "/api/memory/forget", {})[0] == 409
    assert any("Middle (1,600 MiB)" in r["text"] for r in server.app.orc.db._all("SELECT text FROM activity"))
    assert call(server, "POST", "/api/pc/apps", {"apps": [{"name": "Evil", "path": "C:\\x\\run.bat"}]})[0] == 400
    st, d = call(server, "POST", "/api/pc/apps", {"apps": [{"name": "Notepad", "path": "C:\\Windows\\notepad.exe"}]})
    assert st == 200 and d["approved_apps"][0]["name"] == "Notepad"


def test_api_hardware_permission(server):
    assert call(server, "GET", "/api/hardware")[0] == 200
    cfg = call(server, "GET", "/api/config")[1]["config"]
    cfg["pc"]["hardware_read"] = False
    assert call(server, "POST", "/api/config", {"config": cfg})[0] == 200
    assert call(server, "GET", "/api/hardware")[0] == 403


# ---------------------------------------------------------------- keep-alive, blocked status, briefs

def test_keep_alive_is_shortened_per_mode_never_lengthened(cfg):
    assert memory_guard.duration_s("15m") == 900 and memory_guard.duration_s("30s") == 30
    assert memory_guard.duration_s(-1) is None and memory_guard.duration_s("soon") is None
    sup, _ = supervisor(cfg, "chill")
    assert sup.keep_alive("15m") == 60 and sup.keep_alive("10s") == "10s" and sup.keep_alive(-1) == 60
    sup, _ = supervisor(cfg, "medium")
    assert sup.keep_alive("15m") == 300
    sup, _ = supervisor(cfg, "full")
    assert sup.keep_alive("15m") == "15m"


def test_keep_alive_reaches_ollama(cfg, tmp_path):
    orc = make_orc(cfg, tmp_path, qwen=["Hi."])
    orc.backends[LOCAL].settings.update(num_ctx=16384, keep_alive="15m")
    orc.memory, _ = supervisor(cfg, "medium")
    orc.memory.plan = lambda model, ctx, predict=None: {"ok": True, "ctx": ctx, "note": ""}
    orc.start_task(orc.open_project(str(paths.general_chat_dir()))["id"], "Say hi")
    assert orc.backends[LOCAL].requests[-1].keep_alive == 300


def test_blocked_status_is_reported_until_a_request_fits(cfg, monkeypatch):
    FakeProcs(monkeypatch)
    sup, _ = supervisor(cfg, "chill", FakeOllama(size_mib=9000))
    assert not sup.plan("qwen3.5:9b", 16384)["ok"]
    st = sup.status()
    assert st["blocked"]["model"] == "qwen3.5:9b" and "Chill" in st["blocked"]["reason"] and "shared" in st["shared_note"]
    cfg["memory"]["mode"] = "full"
    assert sup.plan("qwen3.5:9b", 16384)["ok"]
    assert sup.status()["blocked"] is None


def test_pc_results_stay_out_of_the_claude_code_brief(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "list_windows", lambda: [pc.Window(1, "Secret Discord channel", 5, "discord.exe", "normal")])
    orc = make_orc(cfg, tmp_path, qwen=["(normal answer)"])
    pid = orc.open_project(str(paths.general_chat_dir()))["id"]
    t = orc.start_task(pid, "which windows are open?")
    assert "Secret Discord channel" in orc.db.messages(t["id"])[-1]["content"]
    assert "Secret Discord channel" not in orc.continuation_brief(t["id"])
