"""Memory modes for the local model: Chill 700 MiB, Medium 1,500 MiB, Full 2,800 MiB.

What is measured: the private working set (Task Manager's "Memory" column) of
Ollama's server process plus its model runner processes (llama-server.exe on
current Ollama). Private memory is not shared between processes, so summing them
counts nothing twice. GPU memory (VRAM) is reported separately and does not count.

What is enforced, and how:

1. Before each local request (prevention): the request's memory is estimated
   from earlier measurements of that model and context size, or from the model's
   file size if it has never been measured. If it does not fit under the warning
   threshold, a smaller context is used when that fits (and the chat says so);
   otherwise the request is blocked with an explanation. The limit is never raised.
2. While a model is loaded (monitoring, every second while working): above the
   warning threshold new requests wait; above the ceiling the running request is
   stopped and the model is unloaded through Ollama's API. If memory stays above
   the ceiling, Ollama's model runner process is ended as a last resort (Ollama
   starts a new one on the next request). No other process is ever touched.

What is not possible: Windows can only hard-cap a process through a job object
the process is started in. Ollama starts its runner itself, and a cap that makes
its allocations fail would crash it mid-request. So this is a supervisor with a
short reaction time (about one second), not a hard cap: a fast allocation spike can
briefly exceed the ceiling before it is stopped.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from . import winproc

log = logging.getLogger(__name__)

MIB = winproc.MIB
MODES = {"chill": 700, "medium": 1500, "full": 2800}
LABEL = {"chill": "Chill", "medium": "Medium", "full": "Full"}
CTX_STEPS = (32768, 16384, 8192, 4096, 2048)
RUNNER_NAMES = {"llama-server.exe", "ollama_llama_server.exe", "ollama.exe"}
CTX_COST_MIB_PER_16K = 450       # Measured on this PC: 16k vs 4k context differed by ~440 MiB.
WEIGHT_SHARE = 0.16              # Measured: ~1,075 MiB for a 6.3 GB model fully on the GPU.


def mib(n: float | None) -> float | None:
    return None if n is None else round(n / MIB, 1)


class MemorySupervisor:
    def __init__(self, get_cfg: Callable[[], dict], db, backend, notify: Callable[..., None]):
        self.get_cfg = get_cfg
        self.db = db
        self.backend = backend          # The Ollama backend (unload, loaded_models, list_models).
        self.notify = notify            # notify(kind, text, reason="", outcome="")
        self.state = "idle"
        self.last_action: dict | None = None
        self.peak_mib = 0.0
        self.current: dict = {"total_mib": 0.0, "processes": [], "measured": False}
        self.active: dict | None = None
        self.loaded: tuple[str, int] | None = None  # Model and context last used; it stays loaded after a request.
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._over_since: float | None = None
        self._sizes: dict[str, float] = {}

    # Settings ----------------------------------------------------------------------

    @property
    def cfg(self) -> dict:
        return self.get_cfg()["memory"]

    @property
    def mode(self) -> str:
        return self.cfg["mode"]

    @property
    def ceiling_mib(self) -> int:
        return MODES[self.mode]

    @property
    def warn_mib(self) -> float:
        return round(self.ceiling_mib * (1 - self.cfg["margin_percent"] / 100), 1)

    # Measurement -------------------------------------------------------------------

    def workload(self) -> dict:
        """Ollama's server and runner processes, measured. Other programs are ignored."""
        procs = winproc.list_processes()
        by_pid = {p.pid: p for p in procs}
        ollama = [p for p in procs if p.name.lower() == "ollama.exe"
                  and (by_pid.get(p.ppid) is None or by_pid[p.ppid].name.lower() != "ollama.exe")]
        servers, runners = [], []
        for s in ollama:
            mine = [c for c in winproc.descendants(s.pid, procs) if c.name.lower() in RUNNER_NAMES]
            # The server is the one Ollama's tray app started, or one that owns a model runner.
            # A CLI client such as `ollama list` is neither and is not counted.
            parent = by_pid.get(s.ppid)
            if mine or (parent is not None and parent.name.lower() == "ollama app.exe"):
                servers.append(s)
                runners += mine
        measured = [winproc.measure(p) for p in servers + runners]
        ok = all(p.private_ws is not None for p in measured)
        total = sum(p.private_ws or 0 for p in measured)
        rows = []
        for p in measured:
            d = p.as_dict()
            d["role"] = "runner" if p in runners else "server"
            rows.append(d)
        return {"total_mib": mib(total) or 0.0, "processes": rows, "measured": ok or not measured,
                "servers": len(servers), "runner_pids": [p.pid for p in runners]}

    # Estimates -----------------------------------------------------------------------

    def _profiles(self) -> dict:
        return self.db.kv_get("memory_profiles", {}) or {}

    def record(self, model: str, ctx: int, total_mib: float) -> None:
        profiles = self._profiles()
        key = f"{model}|{ctx}"
        if total_mib > profiles.get(key, 0):
            profiles[key] = round(total_mib, 1)
            self.db.kv_set("memory_profiles", profiles)

    def _model_size_mib(self, model: str) -> float | None:
        if model not in self._sizes:
            try:
                for m in self.backend.list_models():
                    if m.get("name") in (model, f"{model}:latest"):
                        self._sizes[model] = (m.get("size") or 0) / MIB
            except Exception:  # Ollama offline: no estimate from size.
                return None
        return self._sizes.get(model)

    def estimate(self, model: str, ctx: int) -> tuple[float | None, str]:
        """(MiB, "measured" | "estimated" | "unknown")."""
        known = {int(k.split("|")[1]): v for k, v in self._profiles().items() if k.split("|")[0] == model}
        if ctx in known:
            return known[ctx], "measured"
        if known:
            pts = sorted(known.items())
            if len(pts) >= 2:
                (c0, m0), (c1, m1) = pts[0], pts[-1]
                per = (m1 - m0) / (c1 - c0) if c1 != c0 else CTX_COST_MIB_PER_16K / 16384
            else:
                (c0, m0), per = pts[0], CTX_COST_MIB_PER_16K / 16384
            return round(max(m0 + (ctx - c0) * per, 50), 1), "estimated"
        size = self._model_size_mib(model)
        if not size:
            return None, "unknown"
        return round(size * WEIGHT_SHARE + CTX_COST_MIB_PER_16K * ctx / 16384, 1), "estimated"

    # Before a request ---------------------------------------------------------------

    def plan(self, model: str, ctx: int) -> dict:
        """May this request run, and with which context size? Never raises the limit."""
        label, ceiling, warn = LABEL[self.mode], self.ceiling_mib, self.warn_mib
        if self.state in ("recovering", "unloading"):
            return {"ok": False, "reason": f"Enoy is freeing memory after the {label} limit was crossed. Try again in a moment."}
        steps = [ctx] + [c for c in CTX_STEPS if c < ctx]
        first_est = None
        for c in steps:
            est, source = self.estimate(model, c)
            if est is None:
                return {"ok": True, "ctx": ctx, "estimate": None, "source": source,
                        "note": f"No memory estimate for {model} yet; the {label} limit ({ceiling:,} MiB) is watched while it runs."}
            if first_est is None:
                first_est = (est, source)
            if est <= warn:
                note = ""
                if c != ctx:
                    note = (f"Context reduced from {ctx:,} to {c:,} tokens so {model} stays under the {label} limit "
                            f"({ceiling:,} MiB): about {est:,.0f} MiB {source}.")
                return {"ok": True, "ctx": c, "estimate": est, "source": source, "note": note}
        est, source = first_est
        smallest, s_src = self.estimate(model, steps[-1])
        fits = [LABEL[m] for m, v in MODES.items() if smallest is not None and smallest <= v * (1 - self.cfg["margin_percent"] / 100)]
        return {"ok": False, "estimate": est, "source": source,
                "reason": (f"{model} needs about {smallest:,.0f} MiB of RAM even with a {steps[-1]:,}-token context "
                           f"({s_src}), but {label} mode allows {ceiling:,} MiB. Loading it was blocked to keep Windows responsive. "
                           + (f"Switch to {' or '.join(fits)} mode in Settings → Memory & PC, or pick a smaller model."
                              if fits else "Pick a smaller model in Settings."))}

    def begin(self, task_id: int | None, cancel, model: str, ctx: int) -> None:
        with self._lock:
            self.active = {"task_id": task_id, "cancel": cancel, "model": model, "ctx": ctx, "since": time.time()}

    def end(self) -> None:
        with self._lock:
            if self.active:
                self.loaded = (self.active["model"], self.active["ctx"])
            self.active = None

    # Monitoring ----------------------------------------------------------------------

    def start(self) -> None:
        if self._thread or not winproc.IS_WINDOWS:
            return
        self._thread = threading.Thread(target=self._loop, name="memory-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # The supervisor must never take the app down.
                log.exception("Memory supervisor tick failed")
            busy = self.active is not None or self.current.get("runner_pids")
            self._stop.wait(1.0 if busy else 5.0)

    def tick(self) -> dict:
        w = self.workload()
        self.current = w
        total = w["total_mib"]
        self.peak_mib = max(self.peak_mib, total)
        active = self.active
        if active and w["runner_pids"]:
            self.record(active["model"], active["ctx"], total)
        elif self.loaded and w["runner_pids"] and total > 50:
            self.record(*self.loaded, total)  # Memory can still grow a little after the answer.
        elif not w["runner_pids"] or total <= 50:
            self.loaded = None
        if not w["measured"]:
            self.state = "measurement_failed"
            return w
        if total > self.ceiling_mib:
            self._exceeded(total, active)
        elif total > self.warn_mib:
            self._over_since = None
            if self.state not in ("unloading", "recovering"):
                self.state = "approaching"
        else:
            self._over_since = None
            if self.state != "idle" or w["runner_pids"]:
                self.state = "within" if w["runner_pids"] else "idle"
        return w

    def _exceeded(self, total: float, active: dict | None) -> None:
        label = LABEL[self.mode]
        if not self.cfg["enforce"]:
            if self.state != "exceeded":
                self.notify("memory.limit_exceeded", f"Local model uses {total:,.0f} MiB, over the {label} limit of "
                            f"{self.ceiling_mib:,} MiB. Enforcement is off, so nothing was stopped.", outcome="exceeded")
            self.state = "exceeded"
            return
        now = time.time()
        if self._over_since is None:
            self._over_since = now
            self.state = "unloading"
            if active and active.get("cancel") is not None:
                # Record why first, then stop: the request thread reports the reason when it ends.
                self.notify("memory.request_stopped", f"Stopped the running Qwen request: the local model reached "
                            f"{total:,.0f} MiB, over the {label} limit of {self.ceiling_mib:,} MiB.",
                            reason="Memory supervisor; you chose this limit in Settings.", outcome="stopped",
                            task_id=active.get("task_id"))
                active["cancel"].set()
            self._unload_all(total)
            return
        if now - self._over_since > 8 and self.cfg["allow_terminate_runner"] and self.current.get("runner_pids"):
            self.state = "recovering"
            ended = [pid for pid in self.current["runner_pids"] if winproc.terminate(pid)]
            self._action("terminated", f"Ended Ollama's model runner (pid {', '.join(map(str, ended)) or 'none'}): memory "
                         f"stayed at {total:,.0f} MiB after unloading. Ollama starts a new runner on the next request.")
            self.notify("memory.runner_terminated", self.last_action["text"], outcome="recovered" if ended else "failed")
            self._over_since = now

    def _unload_all(self, total: float) -> None:
        names = []
        try:
            for m in self.backend.loaded_models():
                name = m.get("name") or m.get("model")
                if name:
                    self.backend.unload(name)
                    names.append(name)
        except Exception as exc:
            self._action("unload_failed", f"Could not unload the model through Ollama: {exc}")
            self.notify("memory.unload_failed", self.last_action["text"], outcome="failed")
            return
        self._action("unloaded", f"Unloaded {', '.join(names) or 'the local model'} at {total:,.0f} MiB "
                     f"(limit {self.ceiling_mib:,} MiB, {LABEL[self.mode]} mode).")
        self.notify("memory.model_unloaded", self.last_action["text"], outcome="unloaded")

    def _action(self, kind: str, text: str) -> None:
        self.last_action = {"kind": kind, "text": text, "at": time.time()}

    def unload_on_exit(self) -> None:
        if self.cfg["unload_on_exit"]:
            try:
                for m in self.backend.loaded_models():
                    if m.get("name"):
                        self.backend.unload(m["name"])
            except Exception:
                pass

    # Reporting -----------------------------------------------------------------------

    def status(self) -> dict:
        w = self.current
        return {
            "mode": self.mode, "label": LABEL[self.mode], "ceiling_mib": self.ceiling_mib, "warn_mib": self.warn_mib,
            "modes": {k: {"label": LABEL[k], "ceiling_mib": v} for k, v in MODES.items()},
            "state": self.state, "total_mib": w.get("total_mib", 0.0), "peak_mib": self.peak_mib,
            "processes": w.get("processes", []), "servers": w.get("servers", 0),
            "within_limit": w.get("total_mib", 0.0) <= self.ceiling_mib,
            "active": {k: v for k, v in (self.active or {}).items() if k != "cancel"} or None,
            "last_action": self.last_action, "profiles": self._profiles(),
            "metric": "Private working set (Task Manager's Memory column) of Ollama's server and model runner",
            "enforcement": ("Supervisor: estimates block or shrink requests before loading; while running, memory is "
                            "checked every second and the request is stopped and the model unloaded above the limit. "
                            "Hard cap unavailable: Ollama starts its runner itself, so Windows cannot cap it without "
                            "crashing it. A fast spike can exceed the limit for about a second."),
            "enforce": self.cfg["enforce"],
        }
