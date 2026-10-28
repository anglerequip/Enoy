"""Build scheduler: which approved task runs next, and what happens after it.

- Manual: run one ready task.
- Sequential: run ready tasks one after another until the plan is done or needs the user.
- Parallel (off by default): run independent tasks that share no files at the same time;
  merges still happen one at a time.

A task is only ever run by whoever claims it (one conditional database update), so the same task
never runs twice at once. Every attempt starts a new worktree and a new model context. A failed
attempt is retried up to max_attempts; blocked tasks wait for the user; when the network is down
the run pauses and resumes later without counting a failure.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .devagent import TaskExecution
from .devgate import ReviewResult, finish_task, record_if_merged
from .devstore import DevStore, TransitionError
from .gitwork import GitWork


@dataclass
class RunReport:
    status: str            # completed | attention | idle | offline | stopped
    ran: list[dict] = field(default_factory=list)
    reason: str = ""


class BuildRunner:
    def __init__(self, store: DevStore, git: GitWork, complete_for: Callable[[dict], Callable], allowlist,
                 review: Callable | None = None, sync: Callable[[int], dict] | None = None, max_attempts: int = 2,
                 max_fix_rounds: int = 2, venv_root: Path | None = None, online: Callable[[], bool] | None = None,
                 parallel_allowed: bool = False, max_parallel: int = 2, provider: str = "", model: str = ""):
        self.store, self.git, self.complete_for = store, git, complete_for
        self.allowlist = list(allowlist)
        self.review, self.sync = review, sync
        self.max_attempts, self.max_fix_rounds = max_attempts, max_fix_rounds
        self.venv_root, self.online = venv_root, online
        self.parallel_allowed, self.max_parallel = parallel_allowed, max_parallel
        self.provider, self.model = provider, model
        self.cancel = threading.Event()
        self._merge_lock = threading.Lock()

    def stop(self) -> None:
        """Stop the running task(s): the model loop and any command stop, and the task goes back to ready."""
        self.cancel.set()

    # Helpers ----------------------------------------------------------------------------------

    def _prepare(self, plan_id: int) -> None:
        plan = self.store.get_plan(plan_id)
        if plan["approved_version"] is None:
            raise TransitionError("Only an approved plan can run.")
        if not plan["integration_branch"]:
            branch, base = self.git.ensure_integration(plan_id)
            self.store.set_plan_git(plan_id, branch, base)
        if plan["status"] != "completed":
            self.store.set_plan_status(plan_id, "running")

    def _deps_done(self, task: dict) -> list[dict]:
        done = {t["task_key"]: t for t in self.store.tasks(task["plan_id"]) if t["status"] == "completed"}
        return [{"key": k, "title": done[k]["title"], "summary": done[k]["summary"]} for k in task["depends_on"]
                if k in done]

    def ready(self, plan_id: int) -> list[dict]:
        self.store.refresh_ready(plan_id)
        return [t for t in self.store.tasks(plan_id) if t["status"] == "ready"]

    def _retry_if_allowed(self, task_id: int) -> None:
        t = self.store.get_task(task_id)
        if t["status"] == "failed" and t["attempts"] < self.max_attempts:
            self.store.transition(task_id, "ready", blocker=(
                f"Attempt {t['attempts']} failed: {t['blocker']} It runs again in a new worktree "
                f"(attempt {t['attempts'] + 1} of {self.max_attempts})."[:2000]))

    # One task -----------------------------------------------------------------------------------

    def run_task(self, task_id: int) -> dict:
        """Claim and run one ready task to its end: completed, failed (maybe requeued), blocked, waiting or ready."""
        task = self.store.get_task(task_id)
        self._prepare(task["plan_id"])
        claim = self.store.claim(task_id)
        if not claim:
            return {"task": task["task_key"], "status": "not_ready", "reason": f"The task is {task['status']}."}
        task = self.store.get_task(task_id)
        plan = self.store.get_plan(task["plan_id"])
        body = self.store.version(plan["id"], plan["approved_version"])["body"]
        reason = ""
        try:
            result = TaskExecution(store=self.store, task=task, plan_body=body, git=self.git,
                                   complete=self.complete_for(task), allowlist=self.allowlist,
                                   venv_root=self.venv_root, max_fix_rounds=self.max_fix_rounds, cancel=self.cancel,
                                   deps_done=self._deps_done(task), provider=self.provider, model=self.model).run(claim)
            reason = result.reason
            if result.ok:
                with self._merge_lock:  # One merge at a time, each on top of the previous one.
                    finished = finish_task(self.store, self.store.get_task(task_id), claim, self.git, self.allowlist,
                                           review=self.review, sync=self.sync, venv_root=self.venv_root)
                reason = finished.reason
        except Exception as exc:  # Git or disk trouble: record it instead of leaving the task claimed.
            reason = f"{type(exc).__name__}: {exc}"
            current = self.store.get_task(task_id)
            if current["claim"] == claim:
                for status in ("failed", "ready"):
                    try:
                        self.store.transition(task_id, status, claim=claim, blocker=reason[:2000])
                        break
                    except TransitionError:
                        continue
        self._retry_if_allowed(task_id)
        final = self.store.get_task(task_id)
        if self.store.tasks(task["plan_id"]) and all(t["status"] == "completed" for t in self.store.tasks(task["plan_id"])):
            self.store.set_plan_status(task["plan_id"], "completed")
        return {"task": task["task_key"], "status": final["status"], "reason": reason or final["blocker"]}

    # Many tasks -----------------------------------------------------------------------------------

    def _report(self, plan_id: int, ran: list[dict]) -> RunReport:
        tasks = self.store.tasks(plan_id)
        if tasks and all(t["status"] == "completed" for t in tasks):
            self.store.set_plan_status(plan_id, "completed")
            return RunReport("completed", ran)
        stuck = [t for t in tasks if t["status"] in ("failed", "blocked")]
        if stuck:
            return RunReport("attention", ran, "; ".join(f"{t['task_key']} is {t['status']}" for t in stuck))
        return RunReport("idle", ran)

    def _interrupted(self, plan_id: int, ran: list[dict]) -> RunReport | None:
        if self.cancel.is_set():
            return RunReport("stopped", ran, "Stopped by the user.")
        waiting = [t for t in self.store.tasks(plan_id) if t["status"] == "waiting_network"]
        if waiting:
            return RunReport("offline", ran, "No connection to the model provider; the run will continue when it "
                                             "is back. Nothing was counted as a failure.")
        return None

    def run_all(self, plan_id: int) -> RunReport:
        """Run ready tasks one after another until none is left or the run must pause."""
        self.cancel.clear()
        self._prepare(plan_id)
        ran: list[dict] = []
        while True:
            ready = self.ready(plan_id)
            if not ready:
                return self._report(plan_id, ran)
            ran.append(self.run_task(ready[0]["id"]))
            paused = self._interrupted(plan_id, ran)
            if paused:
                return paused

    def pick_batch(self, ready: list[dict]) -> list[dict]:
        """Ready tasks that touch no common file, at most max_parallel."""
        batch, used = [], set()
        for t in ready:
            files = set(t["allowed_files"]) | set(t["test_files"])
            if t["status"] == "ready" and not files & used:
                batch.append(t)
                used |= files
            if len(batch) >= self.max_parallel:
                break
        return batch

    def run_parallel(self, plan_id: int) -> RunReport:
        if not self.parallel_allowed:
            raise RuntimeError("Parallel runs are off. Turn on build.parallel in Settings once you trust the "
                               "isolation checks; until then tasks run one at a time.")
        self.cancel.clear()
        self._prepare(plan_id)
        ran: list[dict] = []
        while True:
            batch = self.pick_batch(self.ready(plan_id))
            if not batch:
                return self._report(plan_id, ran)
            results: list[dict] = []
            threads = [threading.Thread(target=lambda t=t: results.append(self.run_task(t["id"])), daemon=True)
                       for t in batch]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            ran += sorted(results, key=lambda r: r["task"])
            paused = self._interrupted(plan_id, ran)
            if paused:
                return paused

    # After a restart or a network outage ------------------------------------------------------------

    def resume(self, plan_id: int) -> dict:
        """Interrupted tasks: record them if their work was merged, else queue them again. Waiting tasks:
        queue them again when the provider is reachable."""
        counts = {"recorded": 0, "requeued": 0, "ready": 0, "waiting": 0}
        for t in self.store.tasks(plan_id):
            if t["status"] == "interrupted":
                if record_if_merged(self.store, t, self.git, sync=self.sync):
                    counts["recorded"] += 1
                else:
                    self.store.transition(t["id"], "ready", blocker="Enoy stopped during this task; it runs again.")
                    counts["requeued"] += 1
            elif t["status"] == "waiting_network":
                if self.online is None or self.online():
                    self.store.transition(t["id"], "ready", blocker="")
                    counts["ready"] += 1
                else:
                    counts["waiting"] += 1
        return counts
