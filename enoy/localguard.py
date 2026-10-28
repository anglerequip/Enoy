"""Memory limits for local models run by Ollama (Build workspace).

Enoy's memory modes keep a local model well under 2,000 MiB of RAM:
- the context window and prompt batch are sized for the mode (smaller context, less memory);
- every Ollama model runner gets a Windows hard working-set cap at the mode's limit
  (SetProcessWorkingSetSizeEx, no administrator rights needed);
- while a request runs, Enoy measures Ollama's processes (private working set, Task Manager's
  "Memory" column). At 96 % of the limit it stops the request and unloads the model; if memory
  stays high for 8 seconds, it ends the runner process (Ollama starts a new one next time).
Other programs' processes are never touched.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Callable

from . import winproc

MODES = {"chill": 1200, "medium": 1600, "full": 1900}
LABEL = {"chill": "Chill", "medium": "Middle", "full": "Full"}
ABSOLUTE_MAX_MIB = 2000
CTX = {"chill": 4096, "medium": 8192, "full": 16384}
BATCH = {"chill": 256, "medium": 512, "full": 512}
STOP_PERCENT = 96
LAST_RESORT_S = 8.0
RUNNER_NAMES = {"llama-server.exe", "ollama_llama_server.exe", "ollama.exe"}


def ollama_workload() -> dict:
    """Ollama's server and model runner processes, measured. Nothing else is counted."""
    procs = winproc.list_processes()
    by_pid = {p.pid: p for p in procs}
    servers = [p for p in procs if p.name.lower() == "ollama.exe"
               and (by_pid.get(p.ppid) is None or by_pid[p.ppid].name.lower() != "ollama.exe")]
    runners, seen = [], set()
    for s in servers:
        for c in winproc.descendants(s.pid, procs):
            if c.name.lower() in RUNNER_NAMES and c.pid not in seen:
                seen.add(c.pid)
                runners.append(c)
    measured = [winproc.measure(p) for p in servers + runners]
    total = sum(p.private_ws or 0 for p in measured)
    return {"total_mib": round(total / winproc.MIB, 1), "runner_pids": [p.pid for p in runners]}


class LocalGuard:
    def __init__(self, mode: Callable[[], str], workload: Callable[[], dict] = ollama_workload,
                 cap: Callable[[int, int], object] = winproc.cap_working_set,
                 terminate: Callable[[int], object] = winproc.terminate,
                 unload: Callable[[], object] | None = None, interval: float = 0.5):
        self.mode, self.workload, self.cap, self.terminate = mode, workload, cap, terminate
        self.unload = unload
        self.interval = interval
        self.stopped = ""
        self.peak_mib = 0.0

    def _mode(self) -> str:
        m = self.mode()
        return m if m in MODES else "medium"

    def limit_mib(self) -> int:
        return min(MODES[self._mode()], ABSOLUTE_MAX_MIB - 100)

    def request_options(self) -> dict:
        m = self._mode()
        return {"num_ctx": CTX[m], "num_batch": BATCH[m]}

    def _check(self, cancel: threading.Event, over_since: list) -> None:
        w = self.workload()
        limit = self.limit_mib()
        for pid in w.get("runner_pids", []):
            self.cap(pid, limit * winproc.MIB)
        total = float(w.get("total_mib") or 0)
        self.peak_mib = max(self.peak_mib, total)
        if total < limit * STOP_PERCENT / 100:
            over_since.clear()
            return
        if not self.stopped:
            mode = self._mode()
            self.stopped = (f"The local model used {total:,.0f} MiB, over the {MODES[mode]:,} MiB limit of memory mode "
                            f"{LABEL[mode]}. Enoy stopped the request and unloaded the model. Choose a smaller model "
                            "or another memory mode in Settings → Providers.")
            cancel.set()
            if self.unload:
                try:
                    self.unload()
                except Exception:
                    pass
        if not over_since:
            over_since.append(time.monotonic())
        elif time.monotonic() - over_since[0] > LAST_RESORT_S:
            for pid in w.get("runner_pids", []):
                self.terminate(pid)

    @contextlib.contextmanager
    def watch(self, cancel: threading.Event):
        """Measure and cap Ollama while a request runs. Sets `stopped` (and cancel) if the limit is passed."""
        self.stopped, self.peak_mib = "", 0.0
        done = threading.Event()
        over_since: list = []

        def loop():
            while not done.is_set():
                try:
                    self._check(cancel, over_since)
                except Exception:  # Memory cannot be read (permissions, not Windows): the request still runs.
                    pass
                done.wait(self.interval)

        t = threading.Thread(target=loop, daemon=True, name="enoy-local-guard")
        t.start()
        try:
            yield self
        finally:
            done.set()
            t.join(timeout=2)
