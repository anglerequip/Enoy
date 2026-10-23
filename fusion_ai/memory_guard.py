"""Memory modes for the local model: Chill 1,200 MiB, Middle 1,600 MiB, Full 1,900 MiB.

Absolute rule: no mode may allow 2,000 MiB or more. ABSOLUTE_MAX_MIB is checked when
this module loads, so a wrong edit to MODES stops the app instead of loosening the rule.

The metric the limits apply to: the **private working set** (Task Manager's "Memory"
column) of Ollama's server process plus its model runner processes (llama-server.exe on
current Ollama). Private memory belongs to one process only, so summing it counts nothing
twice, and a process is counted once even if it shows up twice in the process list.
Reported next to it, never added to it: private commit, total working set (it also
counts shared, memory-mapped pages of the model file), GPU memory (VRAM, from Ollama)
and Enoy's own memory.

Three thresholds per mode:
- warning (ceiling minus `margin_percent`, 10 % by default): a request is only started
  if its estimate fits under it. Above it the status reads "Approaching limit".
- stop (ceiling minus STOP_MARGIN_PERCENT): the running request is stopped and the
  model is unloaded through Ollama's API. The gap to the ceiling covers the reaction
  time (memory is checked four times a second while Qwen works).
- ceiling: the number shown for the mode.

Before each request (prevention): the mode's runtime settings cap the context size and
answer length; other models Ollama has loaded are unloaded first so only one runner
holds memory; the request's memory is estimated from earlier measurements of that model
and context size (or the model file's size if never measured) and a smaller context is
used if needed. If nothing fits, or Windows itself is short of free RAM, the request is
refused with an explanation. A refused request is never sent to Claude instead.

Windows hard cap (the `os_cap` setting, on by default): every Ollama model runner gets a
Windows hard working-set maximum of (stop threshold minus the server's own memory), set
with SetProcessWorkingSetSizeEx within a quarter second of the runner appearing. Windows
then keeps the runner's RAM at or under the cap and moves anything above it to the page
file; Qwen keeps working, possibly slower. Tested on this PC: a 16k context that uses
1,521 MiB uncapped stayed at 1,103 MiB with an 1,100 MiB cap and answered normally.
Limits of the cap: it covers RAM, not commit or page file use; it starts up to a quarter
second after the runner does; it is per runner (two runners are handled by unloading one);
Ollama's server process (a few MiB) is only monitored.

While Qwen works (monitoring): above the stop threshold the request is stopped and the
model unloaded. If memory stays high for 8 seconds, Ollama's model runner process is
ended as a last resort (Ollama starts a new one on the next request). No other process
is ever touched. A second runner (two models resident at once) is unloaded.

Not used: a job object memory limit. It caps commit, and the runner commits about
8,300 MiB (measured) while holding about 1,000 MiB in RAM, so it would fail at load.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable

from . import winproc

log = logging.getLogger(__name__)

MIB = winproc.MIB
MODES = {"chill": 1200, "medium": 1600, "full": 1900}
ABSOLUTE_MAX_MIB = 2000
if max(MODES.values()) >= ABSOLUTE_MAX_MIB:  # Not an assert: asserts vanish under python -O.
    raise RuntimeError(f"Memory modes must stay below {ABSOLUTE_MAX_MIB} MiB")
LABEL = {"chill": "Chill", "medium": "Middle", "full": "Full"}
STOP_MARGIN_PERCENT = 4
CTX_STEPS = (32768, 16384, 8192, 4096, 2048)
# Per-mode caps on the request settings that drive memory. They only ever lower the
# user's own settings. Measured on this PC with qwen3.5:9b (private working set, prompt
# filling most of the context): 4k 1,082 MiB, 8k 1,292, 16k 1,521; with num_batch 256,
# 4k 1,055 and 8k 1,199. Context size drives the KV cache and compute buffers; from 8k
# on, part of the model no longer fits in the GPU and moves to RAM. Answer length only
# matters through the context, so it is capped mainly to keep the answer inside it.
# num_batch None = Ollama's default (512).
RUNTIME = {
    "chill": {"max_ctx": 4096, "max_predict": 2048, "num_batch": 256},
    "medium": {"max_ctx": 8192, "max_predict": 4096, "num_batch": 256},
    "full": {"max_ctx": 16384, "max_predict": None, "num_batch": None},
}
# How long Ollama keeps the model in RAM after an answer. Shorter in the low modes so the
# memory is given back soon; Full keeps the configured value.
KEEP_ALIVE_S = {"chill": 60, "medium": 300, "full": None}
FREE_RAM_RESERVE_MIB = 512       # Left free for Windows and other programs when Qwen loads.
RUNNER_NAMES = {"llama-server.exe", "ollama_llama_server.exe", "ollama.exe"}
# Estimate for a model never measured on this PC: a share of the model file plus a cost per
# context token. Calibrated on 2026-10-23 with qwen3.5:9b (6,247 MiB file): 1,055 MiB at 4k
# (batch 256) and 1,521 MiB at 16k. It is replaced by a real measurement after the first answer.
CTX_COST_MIB_PER_16K = 450
WEIGHT_SHARE = 0.151


def duration_s(value) -> float | None:
    """Ollama keep_alive ("15m", "30s", "1h", 300, "-1") in seconds; None means forever or unknown."""
    text = str(value).strip().lower()
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*([smh]?)", text)
    if not m:
        return None
    n = float(m.group(1))
    if n < 0:
        return None
    return n * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def mib(n: float | None) -> float | None:
    return None if n is None else round(n / MIB, 1)


def free_ram_mib() -> float | None:
    """Physical RAM Windows reports as available right now."""
    from .diagnostics import memory_gb
    free = memory_gb().get("free_gb")
    return None if free is None else free * 1024


def own_memory() -> dict | None:
    """Enoy's own private working set: this Python process and the WebView2 processes it started."""
    import os
    procs = winproc.list_processes()
    me = [p for p in procs if p.pid == os.getpid()]
    if not me:
        return None
    mine = [winproc.measure(p) for p in me + winproc.descendants(me[0].pid, procs)]
    known = [p.private_ws for p in mine if p.private_ws is not None]
    return {"total_mib": mib(sum(known)) or 0.0, "processes": len(mine), "complete": len(known) == len(mine)}


class MemorySupervisor:
    def __init__(self, get_cfg: Callable[[], dict], db, backend, notify: Callable[..., None]):
        self.get_cfg = get_cfg
        self.db = db
        self.backend = backend          # The Ollama backend (unload, loaded_models, list_models).
        self.notify = notify            # notify(kind, text, reason="", outcome="", task_id=None)
        self.state = "idle"
        self.last_action: dict | None = None
        self.blocked: dict | None = None  # Last request refused before loading, until one is allowed again.
        self.peak_mib = 0.0
        self.current: dict = {"total_mib": 0.0, "processes": [], "measured": False, "runner_pids": []}
        self.gpu: dict = {"vram_mib": None, "models": [], "at": 0.0}
        self.active: dict | None = None
        self.loaded: tuple[str, int] | None = None  # Model and context last used; it stays loaded after a request.
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._over_since: float | None = None
        self._sizes: dict[str, dict] = {}
        self._caps: dict[int, float | None] = {}  # Runner pid -> Windows cap in MiB (None: Windows refused).
        self._cap_logged: set[int] = set()

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

    def warn_for(self, mode: str) -> float:
        return round(MODES[mode] * (1 - self.cfg["margin_percent"] / 100), 1)

    @property
    def warn_mib(self) -> float:
        return self.warn_for(self.mode)

    @property
    def stop_mib(self) -> float:
        return round(self.ceiling_mib * (1 - STOP_MARGIN_PERCENT / 100), 1)

    @property
    def os_cap(self) -> bool:
        return bool(self.cfg.get("os_cap", True))

    # Measurement -------------------------------------------------------------------

    def workload(self) -> dict:
        """Ollama's server and runner processes, measured. Other programs are ignored."""
        procs = winproc.list_processes()
        by_pid = {p.pid: p for p in procs}
        ollama = [p for p in procs if p.name.lower() == "ollama.exe"
                  and (by_pid.get(p.ppid) is None or by_pid[p.ppid].name.lower() != "ollama.exe")]
        servers, runners, seen = [], [], set()
        for s in ollama:
            mine = [c for c in winproc.descendants(s.pid, procs) if c.name.lower() in RUNNER_NAMES]
            # The server is the one Ollama's tray app started, or one that owns a model runner.
            # A CLI client such as `ollama list` is neither and is not counted.
            parent = by_pid.get(s.ppid)
            if mine or (parent is not None and parent.name.lower() == "ollama app.exe"):
                for p, bucket in [(s, servers)] + [(c, runners) for c in mine]:
                    if p.pid not in seen:  # Each process once, even if reached twice.
                        seen.add(p.pid)
                        bucket.append(p)
        measured = [winproc.measure(p) for p in servers + runners]
        ok = all(p.private_ws is not None for p in measured)
        total = sum(p.private_ws or 0 for p in measured)
        rows = []
        for p in measured:
            d = p.as_dict()
            d["role"] = "runner" if p in runners else "server"
            rows.append(d)
        add = lambda key: round(sum(r.get(key) or 0 for r in rows), 1)
        return {"total_mib": mib(total) or 0.0, "commit_mib": add("private_commit_mib"),
                "working_set_mib": add("working_set_mib"), "processes": rows, "measured": ok or not measured,
                "servers": len(servers), "runner_pids": [p.pid for p in runners]}

    def _refresh_gpu(self, force: bool = False) -> None:
        """Ollama's own report of what is loaded and how much of it sits on the GPU (at most every 5 s)."""
        if not force and time.time() - self.gpu["at"] < 5:
            return
        try:
            models = self.backend.loaded_models()
        except Exception:
            self.gpu = {"vram_mib": None, "models": [], "at": time.time(), "error": "Ollama did not answer."}
            return
        rows = [{"name": m.get("name") or m.get("model"), "size_mib": mib(m.get("size")),
                 "vram_mib": mib(m.get("size_vram")), "context_length": m.get("context_length")} for m in models]
        self.gpu = {"vram_mib": round(sum(r["vram_mib"] or 0 for r in rows), 1) if rows else 0.0,
                    "models": rows, "at": time.time()}

    # Estimates -----------------------------------------------------------------------

    def _profiles(self) -> dict:
        return self.db.kv_get("memory_profiles", {}) or {}

    def record(self, model: str, ctx: int, total_mib: float, fresh: bool = False) -> None:
        """Peak memory of the current load of model at ctx. The first reading of a new request replaces
        the stored value, so an old reading (other GPU load, other settings) does not block forever."""
        profiles = self._profiles()
        key = f"{model}|{ctx}"
        if fresh or total_mib > profiles.get(key, 0):
            profiles[key] = round(total_mib, 1)
            self.db.kv_set("memory_profiles", profiles)

    def forget(self) -> int:
        """Drop all stored measurements; estimates start again from the model file size."""
        n = len(self._profiles())
        self.db.kv_set("memory_profiles", {})
        return n

    def _model_info(self, model: str) -> dict:
        if model not in self._sizes:
            try:
                for m in self.backend.list_models():
                    if m.get("name") in (model, f"{model}:latest"):
                        d = m.get("details") or {}
                        self._sizes[model] = {"size_mib": (m.get("size") or 0) / MIB,
                                              "quant": d.get("quantization_level"), "params": d.get("parameter_size")}
            except Exception:  # Ollama offline: no estimate from size.
                return {}
        return self._sizes.get(model, {})

    def estimate(self, model: str, ctx: int) -> tuple[float | None, str]:
        """(MiB, "measured" | "estimated" | "unknown")."""
        known = {int(k.split("|")[1]): v for k, v in self._profiles().items() if k.split("|")[0] == model}
        if ctx in known:
            return known[ctx], "measured"
        size = self._model_info(model).get("size_mib")
        from_size = round(size * WEIGHT_SHARE + CTX_COST_MIB_PER_16K * ctx / 16384, 1) if size else None
        if known:
            pts = sorted(known.items())
            lower = [p for p in pts if p[0] < ctx]
            upper = [p for p in pts if p[0] > ctx]
            if lower and upper:  # Between two measurements: on the line between them.
                (c0, m0), (c1, m1) = lower[-1], upper[0]
                return round(m0 + (ctx - c0) * (m1 - m0) / (c1 - c0), 1), "estimated"
            if upper:  # Smaller than anything measured: never more than the smallest measurement.
                c1, m1 = upper[0]
                return round(min(m1, from_size) if from_size else m1, 1), "estimated"
            (c0, m0) = lower[-1]  # Larger than anything measured: grow at the known (or default) rate.
            per = (m0 - lower[-2][1]) / (c0 - lower[-2][0]) if len(lower) >= 2 else 0
            per = max(per, CTX_COST_MIB_PER_16K / 16384)
            return round(m0 + (ctx - c0) * per, 1), "estimated"
        if not from_size:
            return None, "unknown"
        return from_size, "estimated"

    def fit(self, model: str, ctx: int, mode: str) -> dict:
        """Largest context (from the mode's cap down) whose estimate fits under the mode's warning threshold.

        No side effects: used both before a request and for the mode cards in Settings.
        """
        start = min(ctx, RUNTIME[mode]["max_ctx"])
        steps = [start] + [c for c in CTX_STEPS if c < start]
        warn = self.warn_for(mode)
        for c in steps:
            est, source = self.estimate(model, c)
            if est is None:
                return {"fits": None, "ctx": start, "estimate": None, "source": source}
            if est <= warn:
                return {"fits": True, "ctx": c, "estimate": est, "source": source}
        est, source = self.estimate(model, steps[-1])
        return {"fits": False, "ctx": steps[-1], "estimate": est, "source": source}

    # Before a request ---------------------------------------------------------------

    def plan(self, model: str, ctx: int, predict: int | None = None) -> dict:
        """May this request run, and with which settings? Never raises a limit or a setting."""
        mode, label, ceiling = self.mode, LABEL[self.mode], self.ceiling_mib
        if self.state in ("recovering", "unloading"):
            return {"ok": False, "reason": f"Enoy is freeing memory after the {label} limit was reached. Try again in a moment."}
        now = self.workload()
        self.current = now
        if not now["measured"]:
            return self._refuse(model, "Windows did not allow Enoy to measure Ollama's memory, so the "
                                f"{label} limit ({ceiling:,} MiB) could not be checked. Loading was blocked. Restarting "
                                "Ollama normally (not as administrator) usually fixes this.")
        notes = []
        self._one_resident_model(model, notes)
        f = self.fit(model, ctx, mode)
        cap = RUNTIME[mode]["max_predict"]
        num_predict = predict if cap is None or predict is None else min(predict, cap)
        if predict is not None and num_predict != predict:
            notes.append(f"Answer length capped at {num_predict:,} tokens in {label} mode.")
        if f["fits"] is False:
            return self._refuse(model, self._shortfall(model, f))
        if f["fits"] is None:
            notes.append(f"No memory estimate for {model} yet; it is measured while it runs and stopped at "
                         f"{self.stop_mib:,.0f} MiB ({label} limit {ceiling:,} MiB).")
        elif f["ctx"] != ctx:
            notes.insert(0, f"Context {'capped' if f['ctx'] == RUNTIME[mode]['max_ctx'] else 'reduced'} from {ctx:,} "
                            f"to {f['ctx']:,} tokens so {model} stays under the {label} limit ({ceiling:,} MiB): about "
                            f"{f['estimate']:,.0f} MiB ({f['source']}).")
        need = (f["estimate"] or 0) - now["total_mib"] if self.loaded and self.loaded[0] == model else (f["estimate"] or 0)
        free = free_ram_mib()
        if free is not None and need > 0 and free - need < FREE_RAM_RESERVE_MIB:
            return self._refuse(model, f"Windows has only {free:,.0f} MiB of RAM free and {model} needs about "
                                f"{need:,.0f} MiB more. Loading it now would leave less than {FREE_RAM_RESERVE_MIB} MiB "
                                "for everything else, so it was blocked. Close some programs and try again.")
        self.blocked = None
        return {"ok": True, "ctx": f["ctx"], "num_predict": num_predict, "num_batch": RUNTIME[mode]["num_batch"],
                "estimate": f["estimate"],
                "source": f["source"], "note": " ".join(notes)}

    def _refuse(self, model: str, reason: str) -> dict:
        self.blocked = {"at": time.time(), "model": model, "reason": reason}
        return {"ok": False, "reason": reason}

    def _shortfall(self, model: str, f: dict) -> str:
        label, ceiling, warn = LABEL[self.mode], self.ceiling_mib, self.warn_mib
        info = self._model_info(model)
        what = model + (f" ({info['params']} parameters, {info['quant']} quantization, "
                        f"{info['size_mib']:,.0f} MiB file)" if info.get("quant") else "")
        others = [LABEL[m] for m in MODES if m != self.mode and self.fit(model, 1 << 30, m)["fits"]]
        tips = [f"switch to {' or '.join(others)} mode in Settings → Memory & PC" if others else "",
                "pick a smaller model or a smaller quantization of it in Settings (measured on first use)",
                "lower the context size in Settings → Local model"]
        return (f"{what} needs about {f['estimate']:,.0f} MiB of RAM ({f['source']}) even with a {f['ctx']:,}-token "
                f"context, but {label} mode starts requests only below {warn:,.0f} MiB (limit {ceiling:,} MiB). "
                f"Loading it was blocked to keep Windows responsive. To continue: "
                + "; ".join(t for t in tips if t) + ".")

    def _one_resident_model(self, model: str, notes: list[str]) -> None:
        """Unload every model except the one about to be used, so only one runner holds memory."""
        try:
            loaded = self.backend.loaded_models()
        except Exception:
            return  # Ollama offline: the request itself will report it.
        for m in loaded:
            name = m.get("name") or m.get("model")
            if name and name not in (model, f"{model}:latest"):
                try:
                    self.backend.unload(name)
                except Exception as exc:
                    self.notify("memory.unload_failed", f"Could not unload {name} before loading {model}: {exc}",
                                outcome="failed")
                    continue
                text = f"Unloaded {name} so it does not hold memory next to {model}."
                notes.append(text)
                self.notify("memory.extra_model_unloaded", text, reason="One local model at a time.", outcome="unloaded")

    def keep_alive(self, configured) -> str | int:
        """The configured keep_alive, shortened to the mode's cap. Never lengthened."""
        cap = KEEP_ALIVE_S[self.mode]
        if cap is None or configured is None:
            return configured if cap is None else int(cap)
        own = duration_s(configured)
        return int(cap) if own is None or own > cap else configured

    def begin(self, task_id: int | None, cancel, model: str, ctx: int) -> None:
        with self._lock:
            self.active = {"task_id": task_id, "cancel": cancel, "model": model, "ctx": ctx, "since": time.time(),
                           "fresh": True}

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
            wait = 0.25 if self.active is not None else 1.0 if self.current.get("runner_pids") else 5.0
            self._stop.wait(wait)

    def tick(self) -> dict:
        w = self.workload()
        if self._apply_caps(w):
            w = self.workload()  # Windows trims to a new cap at once: act on the capped reading.
        self._log_caps(w)
        self.current = w
        total = w["total_mib"]
        self.peak_mib = max(self.peak_mib, total)
        active = self.active
        w["at_cap"] = self._at_cap(w)
        # A runner held at the Windows cap is paging, so its reading understates what it needs:
        # record it as not fitting this mode, so the next request uses a smaller context.
        seen = max(total, self.stop_mib + 1) if w["at_cap"] else total
        if active and w["runner_pids"]:
            # Fresh only once the model is loaded (a runner below 200 MiB is still starting).
            fresh = active.get("fresh") and total > 200
            self.record(active["model"], active["ctx"], seen, fresh=bool(fresh))
            if fresh:
                active["fresh"] = False
        elif self.loaded and w["runner_pids"] and total > 50:
            self.record(*self.loaded, seen)  # Memory can still grow a little after the answer.
        elif not w["runner_pids"] or total <= 50:
            self.loaded = None
        if w["runner_pids"]:
            self._refresh_gpu()
        elif self.gpu["models"]:
            self.gpu = {"vram_mib": 0.0, "models": [], "at": time.time()}
        if not w["measured"]:
            self.state = "measurement_failed"
            return w
        if len(w["runner_pids"]) > 1:
            self._duplicate_runners(w, active)
        if total > self.stop_mib:
            self._exceeded(total, active)
        elif w["at_cap"]:
            self._over_since = None
            if self.state != "at_cap":
                self._action("at_cap", f"Ollama's model runner reached the Windows RAM cap ({self.runner_cap_mib(w):,.0f} "
                             f"MiB). Windows is moving the rest to the page file, so Qwen may be slower. The next "
                             f"request uses a smaller context.")
                self.notify("memory.at_cap", self.last_action["text"], outcome="paging",
                            task_id=active.get("task_id") if active else None)
            self.state = "at_cap"
        elif total > self.warn_mib:
            self._over_since = None
            if self.state not in ("unloading", "recovering"):
                self.state = "approaching"
        else:
            self._over_since = None
            if self.state != "idle" or w["runner_pids"]:
                self.state = "within" if w["runner_pids"] else "idle"
        return w

    def runner_cap_mib(self, w: dict | None = None) -> float:
        """Windows working-set cap per runner: the stop threshold minus what the server holds."""
        w = w or self.current
        server = sum(p.get("private_ws_mib") or 0 for p in w.get("processes", []) if p.get("role") == "server")
        return max(256.0, round(self.stop_mib - server, 1))

    def _apply_caps(self, w: dict) -> bool:
        """Give every runner the Windows hard working-set cap for the current mode (again after a mode change).
        True if a cap was set or changed."""
        pids = set(w["runner_pids"])
        for gone in set(self._caps) - pids:
            self._caps.pop(gone, None)
            self._cap_logged.discard(gone)
        if not self.os_cap:
            for pid in [p for p, c in self._caps.items() if c is not None]:
                if winproc.release_working_set_cap(pid):
                    self.notify("memory.os_cap_removed", f"Removed the Windows RAM cap from Ollama's model runner "
                                f"(pid {pid}) because you switched it off.", outcome="removed")
                self._caps.pop(pid, None)
            return False
        cap = self.runner_cap_mib(w)
        changed = False
        for pid in pids:
            if self._caps.get(pid) is not None and abs(self._caps[pid] - cap) < 1:
                continue  # Already capped at this value. A refused cap is tried again.
            got = winproc.cap_working_set(pid, int(cap * MIB))
            self._caps[pid] = None if got is None else mib(got)
            self._cap_logged.discard(pid)  # A new cap value is logged again.
            changed = changed or got is not None
        return changed

    def _log_caps(self, w: dict) -> None:
        """Log each runner's cap once it holds a model. Ollama also starts short-lived runners to check
        the GPU; those stay small and are not logged."""
        sizes = {p["pid"]: p.get("private_ws_mib") or 0 for p in w["processes"]}
        for pid, cap in self._caps.items():
            if pid in self._cap_logged or sizes.get(pid, 0) < 200:
                continue
            self._cap_logged.add(pid)
            if cap is None:
                self.notify("memory.os_cap_failed", f"Windows refused the {self.runner_cap_mib(w):,.0f} MiB RAM cap for "
                            f"Ollama's model runner (pid {pid}). The supervisor still stops and unloads it at "
                            f"{self.stop_mib:,.0f} MiB.", outcome="failed")
            else:
                self.notify("memory.os_cap_applied", f"Windows holds Ollama's model runner (pid {pid}) to at most "
                            f"{cap:,.0f} MiB of RAM ({LABEL[self.mode]} mode); it uses {sizes.get(pid, 0):,.0f} MiB now.",
                            outcome="capped")

    def _at_cap(self, w: dict) -> bool:
        """Is a capped runner within 8 MiB of its Windows cap?"""
        for proc in w["processes"]:
            cap = self._caps.get(proc["pid"])
            if proc.get("role") == "runner" and cap and (proc.get("private_ws_mib") or 0) >= cap - 8:
                return True
        return False

    def _cap_state(self) -> dict:
        sizes = {p["pid"]: p.get("private_ws_mib") or 0 for p in self.current.get("processes", [])}
        runners = [p for p in self._caps if sizes.get(p, 0) >= 200]  # Runners holding a model.
        capped = [p for p in runners if self._caps[p] is not None]
        return {"enabled": self.os_cap, "cap_mib": self.runner_cap_mib(), "runners": len(runners),
                "capped": len(capped), "failed": [p for p in runners if self._caps[p] is None]}

    def _duplicate_runners(self, w: dict, active: dict | None) -> None:
        """Two runners means two models (or two copies) in memory: keep only the one in use."""
        keep = active["model"] if active else (self.loaded[0] if self.loaded else None)
        self._refresh_gpu(force=True)
        extra = [m["name"] for m in self.gpu["models"] if m["name"] and m["name"] not in (keep, f"{keep}:latest")]
        if not extra:
            return  # Ollama is between two loads of the same model; the old runner exits by itself.
        for name in extra:
            try:
                self.backend.unload(name)
            except Exception:
                pass
        self._action("duplicate_unloaded", f"{len(w['runner_pids'])} model runners were in memory at "
                     f"{w['total_mib']:,.0f} MiB; unloaded {', '.join(extra)} and kept {keep or 'none'}.")
        self.notify("memory.duplicate_unloaded", self.last_action["text"], reason="One local model at a time.",
                    outcome="unloaded")

    def _exceeded(self, total: float, active: dict | None) -> None:
        label = LABEL[self.mode]
        now = time.time()
        if self._over_since is None:
            self._over_since = now
            self.state = "unloading"
            if active and active.get("cancel") is not None:
                # Record why first, then stop: the request thread reports the reason when it ends.
                self.notify("memory.request_stopped", f"Stopped the running Qwen request: the local model reached "
                            f"{total:,.0f} MiB, past the stop point of {self.stop_mib:,.0f} MiB ({label} limit "
                            f"{self.ceiling_mib:,} MiB).",
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
                     f"(stop point {self.stop_mib:,.0f} MiB, {LABEL[self.mode]} limit {self.ceiling_mib:,} MiB).")
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

    def status(self, model: str | None = None, ctx: int | None = None) -> dict:
        w = self.current
        total = w.get("total_mib", 0.0)
        cap = self._cap_state()
        modes = {}
        for k, v in MODES.items():
            modes[k] = {"label": LABEL[k], "ceiling_mib": v, "warn_mib": self.warn_for(k),
                        "max_ctx": RUNTIME[k]["max_ctx"], "max_predict": RUNTIME[k]["max_predict"]}
            if model:
                modes[k]["fit"] = self.fit(model, ctx or RUNTIME[k]["max_ctx"], k)
        return {
            "mode": self.mode, "label": LABEL[self.mode], "model": model, "ceiling_mib": self.ceiling_mib,
            "warn_mib": self.warn_mib,
            "stop_mib": self.stop_mib, "absolute_max_mib": ABSOLUTE_MAX_MIB, "modes": modes,
            "state": self.state, "total_mib": total, "peak_mib": self.peak_mib,
            "headroom_mib": round(self.ceiling_mib - total, 1),
            "commit_mib": w.get("commit_mib"), "working_set_mib": w.get("working_set_mib"),
            "gpu": {k: v for k, v in self.gpu.items() if k != "at"},
            "own": own_memory(),
            "measured": w.get("measured", False),
            "processes": w.get("processes", []), "servers": w.get("servers", 0),
            "runners": len(w.get("runner_pids", [])),
            "within_limit": total <= self.ceiling_mib,
            "active": {k: v for k, v in (self.active or {}).items() if k != "cancel"} or None,
            "last_action": self.last_action, "profiles": self._profiles(),
            "runtime": RUNTIME[self.mode],
            "metric": "Private working set (Task Manager's Memory column) of Ollama's server and model runner",
            "enforcement": self._enforcement_text(cap),
            "enforce": True,
            "os_cap": cap,
            "hard_cap": cap["enabled"] and not cap["failed"],
            "blocked": self.blocked,
            "keep_alive_s": KEEP_ALIVE_S[self.mode],
            "shared_note": ("Ollama is shared: if another program uses the same Ollama, its model counts toward this "
                            "limit and can be unloaded too."),
        }

    def _enforcement_text(self, cap: dict) -> str:
        before = ("Before a request: the context is capped for the mode, other models are unloaded, and the request is "
                  "refused if its estimate does not fit.")
        watch = (f"While Qwen works, memory is checked four times a second; at {self.stop_mib:,.0f} MiB the request is "
                 f"stopped and the model unloaded.")
        if not cap["enabled"]:
            return (f"{before} {watch} The Windows RAM cap is off, so this is a supervisor only: a fast spike can pass "
                    f"the stop point for a moment.")
        if cap["failed"]:
            return (f"{before} Windows refused the RAM cap for runner pid {', '.join(map(str, cap['failed']))}, so for "
                    f"that runner this is a supervisor only. {watch}")
        return (f"{before} Windows hard cap: each Ollama model runner may hold at most {cap['cap_mib']:,.0f} MiB of RAM; "
                f"Windows moves anything above that to the page file, which can slow Qwen down. The cap starts within "
                f"a quarter second of the runner and does not cover commit or the page file. {watch}")
