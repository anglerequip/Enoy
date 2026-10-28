"""Build workspace records: plans, versions, tasks, runs and frozen tests.

Uses the same SQLite database as the rest of Enoy (see the dev_* tables in storage.py).
Status changes go through `transition`, which allows only the moves in TRANSITIONS.
A task is worked on by whoever holds its claim token; a stale token cannot change it.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any

from .storage import Storage

# Task states. "pending" waits for dependencies; "ready" can be claimed.
ACTIVE = ("claimed", "testing", "implementing", "verifying", "reviewing", "merging")
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("ready", "cancelled"),
    "ready": ("claimed", "cancelled"),
    "claimed": ("testing", "ready", "waiting_network", "interrupted", "cancelled"),
    "testing": ("implementing", "ready", "blocked", "failed", "waiting_network", "interrupted", "cancelled"),
    "implementing": ("verifying", "ready", "blocked", "failed", "waiting_network", "interrupted", "cancelled"),
    "verifying": ("reviewing", "merging", "ready", "failed", "interrupted", "cancelled"),
    "reviewing": ("merging", "ready", "failed", "waiting_network", "interrupted", "cancelled"),
    "merging": ("completed", "ready", "failed", "interrupted"),
    "waiting_network": ("ready", "cancelled"),
    "interrupted": ("ready", "cancelled", "completed"),  # Completed: its work was merged before Enoy stopped.
    "blocked": ("ready", "cancelled"),
    "failed": ("ready", "cancelled"),
    "completed": (),
    "cancelled": (),
}
RUN_STATUSES = ("running", "completed", "failed", "blocked", "interrupted", "cancelled", "waiting_network")
_TASK_FIELDS = ("summary", "commit_id", "evidence", "blocker")
_RUN_FIELDS = ("phase", "worktree", "branch", "test_commit", "impl_commit", "summary", "error", "provider", "model")
_JSON_TASK = ("spec", "depends_on", "allowed_files", "test_files", "evidence")


class TransitionError(ValueError):
    """A status change the Build workflow does not allow."""


def canonical(body: Any) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(body: Any) -> str:
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def _task(row: dict | None) -> dict | None:
    if row is not None:
        for k in _JSON_TASK:
            row[k] = json.loads(row[k])
    return row


class DevStore:
    def __init__(self, storage: Storage):
        self.st = storage

    # Plans and versions ---------------------------------------------------------------

    def create_plan(self, project_id: int, title: str, goal: str) -> dict:
        now = time.time()
        pid = self.st._exec("INSERT INTO dev_plans (project_id, title, goal, status, created, updated) "
                            "VALUES (?, ?, ?, 'draft', ?, ?)", (project_id, title, goal, now, now)).lastrowid
        return self.get_plan(pid)

    def get_plan(self, plan_id: int) -> dict | None:
        return self.st._one("SELECT * FROM dev_plans WHERE id = ?", (plan_id,))

    def plans(self, project_id: int) -> list[dict]:
        return self.st._all("SELECT * FROM dev_plans WHERE project_id = ? ORDER BY id DESC", (project_id,))

    PLAN_STATUSES = ("draft", "approved", "running", "completed")

    def set_plan_status(self, plan_id: int, status: str) -> None:
        if status not in self.PLAN_STATUSES:
            raise ValueError(f"Unknown plan status {status}")
        self.st._exec("UPDATE dev_plans SET status = ?, updated = ? WHERE id = ?", (status, time.time(), plan_id))

    def set_plan_git(self, plan_id: int, branch: str, base_commit: str) -> None:
        self.st._exec("UPDATE dev_plans SET integration_branch = ?, base_commit = ?, updated = ? WHERE id = ?",
                      (branch, base_commit, time.time(), plan_id))

    def add_version(self, plan_id: int, body: dict, author: str = "", comment: str = "") -> dict:
        with self.st._tx():
            n = self.st._one("SELECT COALESCE(MAX(version), 0) + 1 AS n FROM dev_plan_versions WHERE plan_id = ?",
                             (plan_id,))["n"]
            self.st._exec("INSERT INTO dev_plan_versions (plan_id, version, body, sha, author, comment, created) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (plan_id, n, canonical(body), fingerprint(body), author, comment, time.time()))
            self.st._exec("UPDATE dev_plans SET updated = ? WHERE id = ?", (time.time(), plan_id))
        return self.version(plan_id, n)

    def version(self, plan_id: int, version: int) -> dict | None:
        row = self.st._one("SELECT * FROM dev_plan_versions WHERE plan_id = ? AND version = ?", (plan_id, version))
        if row:
            row["body"] = json.loads(row["body"])
        return row

    def versions(self, plan_id: int) -> list[dict]:
        rows = self.st._all("SELECT * FROM dev_plan_versions WHERE plan_id = ? ORDER BY version", (plan_id,))
        for row in rows:
            row["body"] = json.loads(row["body"])
        return rows

    def approve_version(self, plan_id: int, version: int) -> dict:
        """Lock a version (the database refuses later edits) and create its tasks."""
        with self.st._tx():
            row = self.version(plan_id, version)
            if row is None:
                raise TransitionError(f"Plan {plan_id} has no version {version}.")
            if row["approved"] is not None or self.get_plan(plan_id)["approved_version"] is not None:
                raise TransitionError("This plan is already approved.")
            if fingerprint(row["body"]) != row["sha"]:
                raise TransitionError("The stored plan does not match its fingerprint.")
            now = time.time()
            self.st._exec("UPDATE dev_plan_versions SET approved = ? WHERE id = ?", (now, row["id"]))
            self.st._exec("UPDATE dev_plans SET approved_version = ?, status = 'approved', updated = ? WHERE id = ?",
                          (version, now, plan_id))
            self._insert_tasks(plan_id, version, row["body"].get("tasks", []))
        return self.get_plan(plan_id)

    # Tasks ---------------------------------------------------------------------------

    def add_tasks(self, plan_id: int, version: int, tasks: list[dict]) -> list[int]:
        row = self.st._one("SELECT approved FROM dev_plan_versions WHERE plan_id = ? AND version = ?", (plan_id, version))
        if row is None or row["approved"] is None:
            raise TransitionError("Tasks come only from an approved plan version; this one is not approved.")
        with self.st._tx():
            return self._insert_tasks(plan_id, version, tasks)

    def _insert_tasks(self, plan_id: int, version: int, tasks: list[dict]) -> list[int]:
        ids, now = [], time.time()
        for t in tasks:
            deps = list(t.get("depends_on") or [])
            ids.append(self.st._exec(
                "INSERT INTO dev_tasks (plan_id, version, task_key, title, spec, depends_on, allowed_files, "
                "test_files, status, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (plan_id, version, t["id"], t.get("title", t["id"]), json.dumps(t), json.dumps(deps),
                 json.dumps(list(t.get("allowed_files") or [])), json.dumps(list(t.get("test_files") or [])),
                 "pending" if deps else "ready", now, now)).lastrowid)
        return ids

    def get_task(self, task_id: int) -> dict | None:
        return _task(self.st._one("SELECT * FROM dev_tasks WHERE id = ?", (task_id,)))

    def tasks(self, plan_id: int, version: int | None = None) -> list[dict]:
        if version is None:
            version = (self.get_plan(plan_id) or {}).get("approved_version")
        return [_task(r) for r in self.st._all("SELECT * FROM dev_tasks WHERE plan_id = ? AND version = ? ORDER BY id",
                                               (plan_id, version))]

    def refresh_ready(self, plan_id: int) -> list[int]:
        """Pending tasks whose dependencies are all completed become ready. Returns their IDs."""
        released = []
        with self.st._tx():
            tasks = self.tasks(plan_id)
            done = {t["task_key"] for t in tasks if t["status"] == "completed"}
            for t in tasks:
                if t["status"] == "pending" and set(t["depends_on"]) <= done:
                    self.st._exec("UPDATE dev_tasks SET status = 'ready', updated = ? WHERE id = ?", (time.time(), t["id"]))
                    released.append(t["id"])
        return released

    def claim(self, task_id: int) -> str | None:
        """Take a ready task. One conditional UPDATE, so only one caller can win. Returns the claim token."""
        token = secrets.token_hex(8)
        cur = self.st._exec("UPDATE dev_tasks SET status = 'claimed', claim = ?, attempts = attempts + 1, updated = ? "
                            "WHERE id = ? AND status = 'ready'", (token, time.time(), task_id))
        return token if cur.rowcount == 1 else None

    def transition(self, task_id: int, status: str, claim: str | None = None, **fields: Any) -> dict:
        unknown = set(fields) - set(_TASK_FIELDS)
        if unknown:
            raise ValueError(f"Unknown task fields: {sorted(unknown)}")
        with self.st._tx():
            task = self.get_task(task_id)
            if task is None:
                raise TransitionError(f"There is no Build task {task_id}.")
            if status == "claimed":
                raise TransitionError("Use claim() to take a task.")
            if status not in TRANSITIONS.get(task["status"], ()):
                raise TransitionError(f"A task cannot go from {task['status']} to {status}.")
            if task["claim"] and task["status"] in ACTIVE and claim != task["claim"]:
                raise TransitionError("This task is held by another claim; the change was refused.")
            if status == "completed" and not (fields.get("commit_id") or task["commit_id"]):
                raise TransitionError("A task is completed only with the commit that holds its work.")
            sets = {"status": status, "updated": time.time()}
            if status not in ACTIVE:
                sets["claim"] = ""
            if status in ("completed", "cancelled", "failed"):
                sets["finished"] = time.time()
            for k, v in fields.items():
                sets[k] = json.dumps(v) if k == "evidence" else v
            self.st._exec(f"UPDATE dev_tasks SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?",
                          (*sets.values(), task_id))
        return self.get_task(task_id)

    # Runs and frozen tests --------------------------------------------------------------

    def start_run(self, task_id: int, role: str, provider: str = "", model: str = "", phase: str = "") -> int:
        attempt = self.get_task(task_id)["attempts"]
        return self.st._exec("INSERT INTO dev_runs (task_id, attempt, role, provider, model, phase, status, started) "
                             "VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
                             (task_id, attempt, role, provider, model, phase, time.time())).lastrowid

    def get_run(self, run_id: int) -> dict | None:
        return self.st._one("SELECT * FROM dev_runs WHERE id = ?", (run_id,))

    def runs(self, task_id: int) -> list[dict]:
        return self.st._all("SELECT * FROM dev_runs WHERE task_id = ? ORDER BY id", (task_id,))

    def update_run(self, run_id: int, **fields: Any) -> None:
        unknown = set(fields) - set(_RUN_FIELDS)
        if unknown:
            raise ValueError(f"Unknown run fields: {sorted(unknown)}")
        if fields:
            self.st._exec(f"UPDATE dev_runs SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?",
                          (*fields.values(), run_id))

    def finish_run(self, run_id: int, status: str, **fields: Any) -> None:
        if status not in RUN_STATUSES or status == "running":
            raise ValueError(f"Not a finished run status: {status}")
        self.update_run(run_id, **fields)
        self.st._exec("UPDATE dev_runs SET status = ?, finished = ? WHERE id = ?", (status, time.time(), run_id))

    def freeze(self, task_id: int, run_id: int | None, hashes: dict[str, str]) -> None:
        """Record the approved tests' fingerprints. The database refuses to change them afterwards."""
        now = time.time()
        with self.st._tx():
            for path, sha in sorted(hashes.items()):
                self.st._exec("INSERT INTO dev_frozen_files (task_id, run_id, path, sha256, frozen) VALUES (?, ?, ?, ?, ?)",
                              (task_id, run_id, path, sha, now))

    def frozen(self, task_id: int) -> dict[str, str]:
        return {r["path"]: r["sha256"] for r in
                self.st._all("SELECT path, sha256 FROM dev_frozen_files WHERE task_id = ? ORDER BY path", (task_id,))}

    # Startup -------------------------------------------------------------------------

    def recover(self) -> list[int]:
        """Called at startup. Work that was running when Enoy stopped becomes interrupted; claims are released.

        Worktrees and commits are left alone: resuming checks git before redoing anything.
        """
        now = time.time()
        with self.st._tx():
            self.st._exec("UPDATE dev_runs SET status = 'interrupted', finished = ?, "
                          "error = 'Enoy stopped while this was running.' WHERE status = 'running'", (now,))
            marks = ",".join("?" * len(ACTIVE))
            ids = [r["id"] for r in self.st._all(f"SELECT id FROM dev_tasks WHERE status IN ({marks}) ORDER BY id", ACTIVE)]
            self.st._exec(f"UPDATE dev_tasks SET status = 'interrupted', claim = '', updated = ? WHERE status IN ({marks})",
                          (now, *ACTIVE))
        return ids
