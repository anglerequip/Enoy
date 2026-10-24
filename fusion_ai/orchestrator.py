"""Task orchestrator: runs tasks, routes them, escalates, validates, records.

This is ordinary code, not a prompt. It owns every decision about which model
runs, what each model is sent, when to retry, when to escalate, and what needs
the user's approval. Models only produce text; the orchestrator turns that text
into validated proposals.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import queue
import re
import secrets
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from . import commands as cmdpolicy
from . import context as ctx
from .config import DEFAULTS
from . import images as imgs
from . import hardware
from . import knowledge as kb
from . import paths
from . import pccontrol as pc
from .memory_guard import RUNTIME, MemorySupervisor
from . import patches
from . import privacy
from . import team as team_mod
from . import tools as tools_mod
from .backends.base import CLAUDE, LOCAL, BackendError, ChatMessage, ChatRequest, ChatResult
from .project import PathError, Workspace, sha256_text
from .redact import redact
from .routing import AttemptSignals, classify, decide, should_escalate, sounds_uncertain
from .storage import STEP_OPEN, TERMINAL_STATUSES, Storage

log = logging.getLogger("fusion.orchestrator")

BACKEND_LABEL = {LOCAL: "Qwen (local)", CLAUDE: "Claude"}
MAX_READ_ROUNDS = 3  # How many times Qwen may ask for more project files in one answer.
CLAUDE_STEP_TITLE = {"plan": "Write an implementation plan", "review": "Review Qwen's change",
                     "edit": "Write the change", "ask": "Answer", "lead": "Read the request and decide (lead)",
                     "lead_review": "Check the team's results (lead)"}
CANCEL_NOTE = {
    LOCAL: "Cancel requested. Enoy closes its connection to Ollama; Ollama stops generating when the connection closes.",
    CLAUDE: "Cancel requested. Enoy stops the local Claude Code process. If Anthropic already received the "
            "request, it may still count toward your usage.",
}
EXPLAIN_SYSTEM = (
    "You explain a software task's workflow log to a beginner. Use only facts in the log. "
    "In 3 to 6 short sentences say what happened, which model did what and why, and what happens next. "
    "No headings, no speculation."
)


class EventBus:
    """Fan-out of live events to UI listeners (server-sent events)."""

    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=5000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


class TaskError(RuntimeError):
    pass


class Orchestrator:
    def __init__(self, storage: Storage, cfg: dict, backends: dict, bus: EventBus | None = None,
                 backups_dir: Path | None = None, synchronous: bool = False):
        """synchronous=True runs tasks in the calling thread (used by tests)."""
        self.db = storage
        self.cfg = cfg
        self.backends = backends
        self.bus = bus or EventBus()
        self.backups_dir = backups_dir
        self.synchronous = synchronous
        self._workspaces: dict[int, Workspace] = {}
        self.memory: MemorySupervisor | None = None  # Started by the app; tests run without it.
        self._stop_reason: dict[int, str] = {}
        self._partial: dict[int, str] = {}  # Text streamed so far, kept if the user stops the answer.
        self._work_lock = threading.Lock()  # Qwen and Claude can finish at once in a team run.
        self._cancel: dict[int, threading.Event] = {}
        self._run_cancel: dict[int, threading.Event] = {}
        self._local_lock = threading.Lock()
        self._claude_status: tuple[float, bool, str] | None = None
        self._threads: list[threading.Thread] = []
        # Requests in flight right now, per backend. Only set while a real request runs.
        self._busy: dict[str, dict] = {}
        self._backend_seen: dict[str, bool] = {}
        self._local_status: tuple[float, dict] | None = None
        self._dispatch_lock = threading.Lock()
        self._claude_version = ""

    # Infrastructure -------------------------------------------------------------

    def _spawn(self, fn: Callable, *args: Any) -> None:
        if self.synchronous:
            fn(*args)
            return
        t = threading.Thread(target=fn, args=args, daemon=True)
        self._threads.append(t)
        t.start()

    def wait_idle(self, timeout: float = 30) -> None:
        end = time.time() + timeout
        for t in list(self._threads):
            t.join(max(0, end - time.time()))

    def _event(self, task_id: int, type_: str, text: str, backend: str = "") -> None:
        ev = self.db.add_event(task_id, type_, text, backend)
        self.bus.publish({"type": "event", **ev})

    def _task_changed(self, task_id: int) -> None:
        self.bus.publish({"type": "task", "task": self.db.get_task(task_id)})

    def _set(self, task_id: int, **fields: Any) -> None:
        status = fields.get("status")
        before = self.db.get_task(task_id)["status"] if status else None
        self.db.update_task(task_id, **fields)
        if status in TERMINAL_STATUSES and before != status:
            self._close_open_steps(task_id)
            self._close_team(task_id, status)
            summary = self.db.get_task(task_id)["summary"]
            text = {"completed": "Task completed.", "failed": "Task failed.", "cancelled": "Task cancelled."}[status]
            if len(summary) > 140:
                summary = summary[:139].rstrip() + "…"
            self._act(task_id, f"task.{status}", text + (f" {summary}" if summary else ""), "system", "done",
                      outcome=status)
        self._task_changed(task_id)

    def _set_owner(self, task_id: int, backend: str, why: str) -> None:
        task = self.db.get_task(task_id)
        if task["owner"] != backend:
            self._event(task_id, "owner", f"Now handled by {BACKEND_LABEL[backend]}: {why}", backend)
        if task["owner"] and task["owner"] != backend:
            self.db.update_task_state(task_id, handoffs=task["state"].get("handoffs", 0) + 1)
        if task["owner"] != backend or task["state"].get("assign_reason") != why:
            self._act(task_id, "task.assigned", f"Assigned to {BACKEND_LABEL[backend]}.", backend, reason=why)
            self.db.update_task_state(task_id, assign_reason=why)
        self._set(task_id, owner=backend)

    # Structured activity -------------------------------------------------------------
    # Every entry comes from what the code is doing at that moment. No model is
    # asked to describe its own work.

    def _memory_notify(self, kind: str, text: str, reason: str = "", outcome: str = "", task_id: int | None = None) -> None:
        self._act(task_id, kind, text, LOCAL, "local", reason=reason, outcome=outcome)
        if task_id and kind == "memory.request_stopped":
            self._stop_reason[task_id] = (text + " Try again in a higher memory mode, with a smaller context or "
                                          "with a smaller model (Settings → Memory & PC).")
        elif task_id:
            self._event(task_id, "note", text, LOCAL)

    def start_memory_supervisor(self) -> None:
        """Called by the app (not by tests): watches the local model's memory in the background."""
        local = self.backends.get(LOCAL)
        if self.memory is None and local is not None and hasattr(local, "loaded_models") and hasattr(local, "unload"):
            self.memory = MemorySupervisor(lambda: self.cfg, self.db, local, self._memory_notify)
        if self.memory is not None:
            self.memory.start()

    def _act(self, task_id: int | None, kind: str, text: str, agent: str = "system", stage: str = "",
             reason: str = "", outcome: str = "", step_id: int | None = None) -> dict:
        ev = self.db.add_activity(task_id, kind, text, agent, stage, reason, outcome, step_id)
        self.bus.publish({"type": "activity", "event": ev})
        return ev

    def _step(self, task_id: int, title: str, agent: str, stage: str, reason: str = "", status: str = "working",
              attempt: int = 0, files: list[str] | None = None, needs_approval: bool = False,
              depends_on: list[int] | None = None, text: str = "", kind: str = "") -> int:
        if depends_on is None:
            prev = self.db.steps(task_id)
            depends_on = [prev[-1]["id"]] if prev else []
        step = self.db.add_step(task_id, title, agent, stage, reason, status, attempt, depends_on, files, needs_approval)
        kind = kind or ("approval.requested" if needs_approval else "task.waiting" if status == "waiting" else "step.started")
        self._act(task_id, kind, text or title, agent, stage, reason, step_id=step["id"])
        return step["id"]

    def _step_end(self, step_id: int | None, status: str, result: str = "", error: str = "",
                  kind: str = "", outcome: str = "", agent: str = "") -> None:
        step = self.db.get_step(step_id) if step_id else None
        if not step or step["status"] not in STEP_OPEN:
            return
        self.db.update_step(step_id, status=status, result=result[:600], error=error[:600])
        if not kind:
            kind = "approval.resolved" if step["needs_approval"] else f"step.{status}"
        note = result or error or status
        self._act(step["task_id"], kind, f"{step['title']}: {note}", agent or step["agent"], step["stage"],
                  outcome=outcome or status, step_id=step_id)

    def _close_open_steps(self, task_id: int, note: str = "Stopped when the task ended.") -> None:
        """Safety net: nothing stays marked as working once its task has ended.

        Check runs are left alone; they finish on their own and report their result.
        """
        for step in self.db.open_steps(task_id):
            if step["stage"] != "testing":
                self._step_end(step["id"], "cancelled", error=note)

    def _end_stage(self, task_id: int, stage: str, status: str, result: str, outcome: str = "") -> None:
        for step in self.db.open_steps(task_id):
            if step["stage"] == stage:
                self._step_end(step["id"], status, result, outcome=outcome)

    def workspace(self, project_id: int) -> Workspace:
        ws = self._workspaces.get(project_id)
        if ws is None:
            project = self.db.get_project(project_id)
            if not project:
                raise TaskError(f"Unknown project {project_id}")
            ws = Workspace(project["path"], self.cfg["permissions"]["extra_excluded_globs"])
            self._workspaces[project_id] = ws
        return ws

    def open_project(self, path: str) -> dict:
        ws = Workspace(path, self.cfg["permissions"]["extra_excluded_globs"])
        project = self.db.upsert_project(str(ws.root), ws.name)
        self._workspaces[project["id"]] = ws
        return project

    def update_config(self, cfg: dict) -> None:
        self.cfg = cfg
        for backend in self.backends.values():
            section = "ollama" if backend.name == LOCAL else "claude"
            backend.settings = cfg[section]
        self._claude_status = None
        self._workspaces.clear()

    # Backend availability -------------------------------------------------------------

    def claude_available(self, refresh: bool = False, max_age: float = 60) -> tuple[bool, str]:
        """Runs `claude --version` and `claude auth status`. Never sends a model request."""
        backend = self.backends.get(CLAUDE)
        if backend is None or not self.cfg["claude"]["enabled"]:
            return False, "Claude is turned off."
        if refresh or self._claude_status is None or time.time() - self._claude_status[0] > max_age:
            status = backend.status()
            self._claude_status = (time.time(), status.available, status.detail)
            self._claude_version = status.version
        return self._claude_status[1], self._claude_status[2]

    def _mark_claude_unavailable(self, reason: str) -> None:
        self._claude_status = (time.time(), False, reason)

    def backend_status(self, refresh: bool = False) -> dict:
        """Connection state for the activity panel. No model requests.

        Ollama is asked over local HTTP (cached 15 s). Claude's state comes from
        the Claude Code CLI's version and sign-in checks (cached 5 minutes).
        """
        now = time.time()
        if refresh or self._local_status is None or now - self._local_status[0] > 15:
            self._local_status = (now, self.backends[LOCAL].status().as_dict())
        local = dict(self._local_status[1])
        available, detail = self.claude_available(refresh, max_age=300)
        backend = self.backends.get(CLAUDE)
        claude = {"backend": CLAUDE, "available": available, "detail": detail,
                  "verified": bool(backend and backend.verified) or bool(self.db.kv_get("claude_last_verified")),
                  "enabled": bool(self.cfg["claude"]["enabled"]), "version": self._claude_version,
                  "model": self.cfg["claude"].get("model") or "Claude Code default",
                  "checked": self._claude_status[0] if self._claude_status else None}
        local["checked"] = self._local_status[0]
        for name, st in ((LOCAL, local), (CLAUDE, claude)):
            seen = self._backend_seen.get(name)
            if seen is None or seen != st["available"]:
                self._backend_seen[name] = st["available"]
                label = "Qwen (Ollama)" if name == LOCAL else "Claude (Claude Code)"
                self._act(None, "backend.connected" if st["available"] else "backend.disconnected",
                          f"{label} {'is available' if st['available'] else 'is not available'}: {st['detail']}", name)
        return {LOCAL: local, CLAUDE: claude}

    # Dispatch pause --------------------------------------------------------------------
    # Pausing stops NEW work from starting. Work already running continues; use
    # Cancel on a task to stop it.

    def dispatch_paused(self) -> bool:
        return bool(self.db.kv_get("dispatch_paused", False))

    def set_dispatch_paused(self, paused: bool) -> dict:
        self.db.kv_set("dispatch_paused", bool(paused))
        waiting = self.db.tasks_with_status("queued")
        if paused:
            self._act(None, "dispatch.paused", "New tasks are paused. Tasks already running continue.", "user")
        else:
            self._act(None, "dispatch.resumed", f"New tasks resumed; starting {len(waiting)} queued task(s).", "user")
            self._dispatch_queued()
        self.bus.publish({"type": "dispatch", "paused": bool(paused)})
        return {"paused": bool(paused), "queued": len(waiting)}

    def _dispatch_queued(self) -> None:
        with self._dispatch_lock:  # Two quick Resume clicks must not start a task twice.
            self._dispatch_queued_locked()

    def _dispatch_queued_locked(self) -> None:
        for task in self.db.tasks_with_status("queued"):
            action = task["state"].get("queued_action")
            if not action:
                continue
            self.db.update_task_state(task["id"], queued_action=None)
            self._cancel[task["id"]] = threading.Event()
            self._act(task["id"], "task.dispatched", "Started after dispatch was resumed.", "system", "request")
            if action.get("type") == "followup":
                text, backend = action.get("text", ""), action.get("backend")
                self._spawn(self._guard, task["id"], lambda tid, t=text, b=backend: self._run_followup(tid, t, b))
            else:
                self._spawn(self._guard, task["id"], self._run_new)

    def _queue_if_paused(self, task_id: int, action: dict) -> bool:
        if not self.dispatch_paused():
            return False
        self.db.update_task_state(task_id, queued_action=action)
        self._set(task_id, status="queued")
        self._act(task_id, "task.queued", "Queued: new tasks are paused.", "system", "request",
                  reason="You paused new tasks in the Activity panel.")
        return True

    # Task entry points ---------------------------------------------------------------

    def start_task(self, project_id: int, objective: str, kind: str | None = None,
                   backend: str | None = None, files: list[str] | None = None, local_only: bool = False,
                   team: bool | None = None, images: list[str] | None = None) -> dict:
        objective = objective.strip()
        if not objective:
            raise TaskError("The task is empty.")
        image_ids = imgs.valid_ids(images)
        title = objective.splitlines()[0][:80]
        task = self.db.create_task(project_id, title, objective, kind or "auto",
                                   state={"override": backend, "kind_override": kind, "extra_paths": files or [],
                                          "local_only": bool(local_only),
                                          "team": self.cfg["team"]["default_new_chats"] if team is None else bool(team)})
        self.db.add_message(task["id"], "user", objective, images=image_ids)
        self._act(task["id"], "task.created", f"Task received: {title}", "user", "request")
        self._cancel[task["id"]] = threading.Event()
        self._task_changed(task["id"])
        if self._queue_if_paused(task["id"], {"type": "new"}):
            return self.db.get_task(task["id"])
        self._spawn(self._guard, task["id"], self._run_new)
        return self.db.get_task(task["id"])

    def send_message(self, task_id: int, text: str, backend: str | None = None,
                     files: list[str] | None = None, images: list[str] | None = None) -> dict:
        """Follow-up message in an existing task (conversation continues)."""
        task = self._require(task_id)
        if task["status"] in ("running", "queued"):
            raise TaskError("This task is still running.")
        if not text.strip():
            raise TaskError("The message is empty.")
        self.db.add_message(task_id, "user", text, images=imgs.valid_ids(images))
        new_files = [f for f in (files or []) if f not in task["state"].get("extra_paths", [])]
        self.db.update_task_state(task_id, followup=text, extra_paths=new_files)
        self._act(task_id, "task.message", "Follow-up message received.", "user", "request")
        self._cancel[task_id] = threading.Event()
        if self._queue_if_paused(task_id, {"type": "followup", "text": text, "backend": backend}):
            return self.db.get_task(task_id)
        self._spawn(self._guard, task_id, lambda tid: self._run_followup(tid, text, backend))
        return self.db.get_task(task_id)

    def cancel(self, task_id: int) -> None:
        task = self._require(task_id)
        if task["status"] == "running":
            busy = [name for name, b in self._busy.items() if b.get("task_id") == task_id]
            note = CANCEL_NOTE[busy[0]] if busy else "Cancel requested. The task stops at its next checkpoint."
            self._act(task_id, "task.cancel_requested", note, "user", outcome="requested")
        ev = self._cancel.get(task_id)
        if ev:
            ev.set()
        if task["status"] in ("awaiting_claude", "queued", "interrupted", "paused", "awaiting_approval") and (
                task["status"] != "awaiting_approval" or self._team_active(task)):
            self.db.update_task_state(task_id, pending_claude=None, queued_action=None)
            self._set(task_id, status="cancelled")
            self._event(task_id, "status", "Cancelled by you.")

    def resume(self, task_id: int) -> dict:
        task = self._require(task_id)
        if task["status"] in ("running", "queued"):
            raise TaskError("This task is already running.")
        self._cancel[task_id] = threading.Event()
        self._event(task_id, "status", "Resuming from the saved task state.")
        self._spawn(self._guard, task_id, self._run_resume)
        return self.db.get_task(task_id)

    def ask_claude(self, task_id: int) -> dict:
        """The user asks Claude to take over or double-check a task."""
        task = self._require(task_id)
        block = self._cloud_block(task_id)
        if block:
            raise TaskError(f"Nothing was sent: {block}. Change that setting first if you want Claude's help.")
        available, why = self.claude_available(refresh=True)
        if not available:
            raise TaskError(f"Claude is not available: {why}")
        self._cancel[task_id] = threading.Event()
        self._spawn(self._guard, task_id, lambda tid: self._escalate(tid, "You asked for Claude.", user_requested=True))
        return self.db.get_task(task_id)

    def approve_claude(self, task_id: int) -> None:
        task = self._require(task_id)
        if (task["state"].get("team_run") or {}).get("status") == "awaiting_claude":
            team_mod.TeamRunner(self, task_id).answer_claude(True)
            self._cancel[task_id] = threading.Event()
            self._spawn(self._guard, task_id, lambda tid: team_mod.TeamRunner(self, tid).continue_run())
            return
        pending = task["state"].get("pending_claude")
        if task["status"] != "awaiting_claude" or not pending:
            raise TaskError("Nothing is waiting to be sent to Claude.")
        self._cancel[task_id] = threading.Event()
        self._spawn(self._guard, task_id, self._send_claude)

    def decline_claude(self, task_id: int) -> None:
        task = self._require(task_id)
        if (task["state"].get("team_run") or {}).get("status") == "awaiting_claude":
            team_mod.TeamRunner(self, task_id).answer_claude(False)
            self._cancel[task_id] = threading.Event()
            self._spawn(self._guard, task_id, lambda tid: team_mod.TeamRunner(self, tid).continue_run())
            return
        pending = task["state"].get("pending_claude")
        if not pending:
            raise TaskError("Nothing is waiting to be sent to Claude.")
        self.db.update_task_state(task_id, pending_claude=None)
        self._step_end(pending.get("step_id"), "skipped", "You kept this task with Qwen; nothing was sent to Claude.",
                       outcome="declined", agent="user")
        self._event(task_id, "claude", "You chose not to send this to Claude. Nothing was transmitted.")
        self._cancel[task_id] = threading.Event()
        self._spawn(self._guard, task_id, lambda tid: self._after_claude_skipped(tid, pending, "You kept this task local.",
                                                                                 declined=True))

    # Guard ----------------------------------------------------------------------

    def _require(self, task_id: int) -> dict:
        task = self.db.get_task(task_id)
        if not task:
            raise TaskError(f"Unknown task {task_id}")
        return task

    def _guard(self, task_id: int, fn: Callable[[int], None]) -> None:
        try:
            self._set(task_id, status="running")
            self._check_cancel(task_id)
            fn(task_id)
        except _Cancelled:
            why = self._stop_reason.pop(task_id, None)
            if why:  # Stopped by the memory supervisor, not by the user.
                self._set(task_id, status="failed", summary=why)
                self._event(task_id, "error", why, LOCAL)
            else:
                self._set(task_id, status="cancelled", summary="Cancelled by you.")
                self._event(task_id, "status", "Cancelled.")
        except Exception as exc:  # Never leave a task stuck in "running".
            log.error("Task %s crashed: %s", task_id, traceback.format_exc())
            self.db.update_task_state(task_id, failures=[f"Internal error: {exc}"])
            self._set(task_id, status="failed", summary=f"Internal error: {exc}")
            self._event(task_id, "error", f"Internal error: {exc}")

    def _check_cancel(self, task_id: int) -> None:
        ev = self._cancel.get(task_id)
        if ev and ev.is_set():
            raise _Cancelled()

    # Core flows ---------------------------------------------------------------------

    def _gather(self, task_id: int, objective: str) -> ctx.ContextPack:
        task = self.db.get_task(task_id)
        project = self.db.get_project(task["project_id"])
        ws = self.workspace(task["project_id"])
        ws.invalidate()
        others = [t for t in self.db.list_tasks(task["project_id"], 20) if t["id"] != task_id]
        history = ctx.history_summary(others, self.cfg["context"]["history_tasks"])
        pack = ctx.gather(ws, objective, self.cfg["context"], task["state"].get("extra_paths"),
                          history=history, project_summary=project.get("summary", ""))
        if task["state"].get("hardware_facts"):
            pack.system_facts = task["state"]["hardware_facts"]
        # Only this task's own project contributes instructions and documents.
        pack.user_instructions = project.get("instructions") or ""
        pack.knowledge = kb.retrieve(self.db.project_files(project["id"], with_content=True), objective)
        if pack.knowledge:
            self._event(task_id, "context", "Project knowledge used: "
                        + ", ".join(sorted({k.name for k in pack.knowledge})))
        self.db.update_task_state(
            task_id,
            relevant_files=[{"path": f.path, "sha": f.sha[:12]} for f in pack.files],
            file_versions={f.path: f.sha for f in pack.files},
        )
        names = ", ".join(f.path for f in pack.files) or "none"
        self._event(task_id, "context", f"Selected {len(pack.files)} relevant file(s): {names}")
        if not ctx.is_general(ws) and not ws.index:
            self._event(task_id, "note", f"This project's folder ({ws.root}) is empty, so there is no code to read or "
                        "change. To work on existing code, use Open folder… and pick the folder that holds it.")
        return pack

    def _analyze(self, task_id: int, objective: str, title: str) -> tuple[ctx.ContextPack, int]:
        step = self._step(task_id, title, "system", "analysis",
                          "Every task starts with a quick local analysis by Enoy itself (no model).")
        pack = self._gather(task_id, objective)
        self.db.update_step(step, files=[f.path for f in pack.files])
        return pack, step

    def _run_new(self, task_id: int) -> None:
        task = self.db.get_task(task_id)
        if self._pc_request(task_id, task["objective"]):
            return
        task = self.db.get_task(task_id)
        state = task["state"]
        pack, analysis = self._analyze(task_id, task["objective"], "Inspect the project and choose a model")
        self.db.update_task_state(task_id, analysis_step=analysis)
        kind_override = state.get("kind_override")
        if ctx.is_general(self.workspace(task["project_id"])):
            kind_override = "ask"  # No project folder: nothing to edit.
        cls = classify(task["objective"], len(pack.files), kind_override, self._routing_history())
        available, why = self.claude_available()
        if self.cfg["routing"].get("zero_paid_mode") and state.get("override") != CLAUDE:
            available = False  # Only a model you pick yourself may use Claude.
        block = self._cloud_block(task_id)
        decision = decide(cls, self.cfg["routing"], available, state.get("override"), local_only=block)
        decision = self._research_route(decision, task["objective"], available, state.get("override"), block)
        # The one authoritative routing decision for this task. Retries, resumes and
        # follow-ups reuse it instead of routing again.
        route = decision.as_dict() | {"task_id": task_id, "kind": cls.kind, "score": cls.score,
                                      "context": [f.path for f in pack.files], "decided": time.time()}
        self.db.update_task_state(task_id, route=route, initial_backend=decision.backend)
        if self.cfg["routing"]["log_decisions"]:
            log.info("Routing decision for task %s: %s -> %s (%s, %s complexity, confidence %s). %s", task_id,
                     cls.category, decision.backend, cls.scope, cls.complexity, decision.confidence or "n/a", decision.reason)
        self._act(task_id, "task.classified",
                  f"Classified: {cls.category.replace('_', ' ')}, {cls.complexity} complexity, {cls.scope}, {cls.risk} risk"
                  + (f", confidence {decision.confidence}" if decision.confidence else "") + ".",
                  "system", "analysis", reason="; ".join(cls.signals) or "No special signals.")
        if block:
            self._act(task_id, "privacy.checked", f"Local only: {block}. Nothing from this task is sent to Claude.",
                      "system", "analysis", outcome="local-only")
        general = ctx.is_general(self.workspace(task["project_id"]))
        lead, lead_why = self._lead_check(task_id, state, text=task["objective"], cls=cls)
        if lead:
            self._step_end(analysis, "completed", f"{len(pack.files)} relevant file(s); Claude leads and decides.")
            self._lead(task_id, pack, cls)
            return
        if lead_why and self._lead_unavailable(task_id, lead_why):
            return
        if (state.get("team") and team_mod.eligible(cls.kind, general) and decision.backend is not None
                and not self._team_skips_images(task_id)):
            self._step_end(analysis, "completed", f"{len(pack.files)} relevant file(s); {cls.kind} task; handed to the team.")
            self._set(task_id, kind=cls.kind, complexity=cls.complexity, workflow="team",
                      routing_reason="TEAM ROLES is on: the orchestrator plans the work and routes each part to Qwen or Claude.")
            team_mod.TeamRunner(self, task_id).start(task["objective"])
            return
        if not state.get("team") and cls.kind == "edit" and not general and (cls.needs.get("multi_step") or cls.complexity == "high"):
            self._act(task_id, "team.suggested", "This looks like multi-step work; TEAM ROLES could plan, test and review it.",
                      "system", "analysis", reason="; ".join(cls.signals[:3]) or "multi-step request")
            self._event(task_id, "note", "This looks like multi-step work. Turning TEAM ROLES on for a chat lets Enoy "
                                         "plan it, run the checks and review it as a team.")
        self._set(task_id, kind=cls.kind, complexity=cls.complexity, workflow=decision.workflow,
                  routing_reason=decision.reason)
        self._event(task_id, "route",
                    f"{cls.kind} task, {cls.complexity} complexity"
                    + (f" ({'; '.join(cls.signals)})" if cls.signals else "")
                    + f". {decision.reason}")
        who = BACKEND_LABEL.get(decision.backend, "no model")
        self._step_end(analysis, "completed", f"{len(pack.files)} relevant file(s); {cls.kind} task, "
                                              f"{cls.complexity} complexity; chose {who}.")
        if decision.backend is None:
            self._act(task_id, "task.assigned", "No model selected.", "system", "analysis", reason=decision.reason,
                      outcome="blocked")
            self._set(task_id, status="failed", summary=decision.reason)
            self._event(task_id, "error", decision.reason)
            return
        if decision.backend == CLAUDE:
            self._set_owner(task_id, CLAUDE, decision.reason)
            purpose = "plan" if decision.workflow == "claude_plans" else cls.kind
            self._request_claude(task_id, purpose, pack, reason=decision.reason)
            return
        self._set_owner(task_id, LOCAL, decision.reason)
        self._local_loop(task_id, pack, reason=decision.reason)

    def _run_followup(self, task_id: int, text: str, backend: str | None) -> None:
        if self._pc_request(task_id, text):
            return
        task = self.db.get_task(task_id)
        general = ctx.is_general(self.workspace(task["project_id"]))
        lead, lead_why = self._lead_check(task_id, task["state"], backend, text=text)
        if lead:
            for tt in self.db.team_tasks(task_id):
                if tt["status"] in team_mod.open_statuses():
                    self.db.update_team_task(tt["id"], status="cancelled", error="Replaced by your new message.")
            self.db.update_task_state(task_id, team_run={})
            pack, analysis = self._analyze(task_id, task["objective"] + "\n" + text, "Inspect the project for the follow-up")
            self._step_end(analysis, "completed", f"{len(pack.files)} relevant file(s); Claude leads and decides.")
            self._lead(task_id, pack, classify(text, len(pack.files)), followup=text)
            return
        if lead_why and task["workflow"] == "claude_leads" and self._lead_unavailable(task_id, lead_why):
            return
        if (task["state"].get("team") and team_mod.eligible(classify(text).kind, general)
                and not self._team_skips_images(task_id, latest_only=True)):
            # A new piece of development work in a team chat: a fresh team run for it.
            for tt in self.db.team_tasks(task_id):
                if tt["status"] in team_mod.open_statuses():
                    self.db.update_team_task(tt["id"], status="cancelled", error="Replaced by your new request.")
            self.db.update_task_state(task_id, team_run={})
            team_mod.TeamRunner(self, task_id).start(f"{task['objective']}\n\nFollow-up: {text}")
            return
        pack, analysis = self._analyze(task_id, task["objective"] + "\n" + text, "Inspect the project for the follow-up")
        chosen = backend or task["owner"] or LOCAL
        if backend:
            why = f"You picked {BACKEND_LABEL[backend]} for this message."
        else:
            why = f"Follow-ups stay with the model that handled the chat ({BACKEND_LABEL[chosen]})."
        self._step_end(analysis, "completed", f"{len(pack.files)} relevant file(s); chose {BACKEND_LABEL[chosen]}.")
        block = self._cloud_block(task_id)
        if chosen == CLAUDE and block:
            if backend == CLAUDE:
                self._fail(task_id, f"Not sent to Claude: {block}. Pick Qwen, or change that setting.")
                return
            chosen, why = LOCAL, f"Claude handled this chat before, but now {block}; continuing with Qwen."
        if chosen == CLAUDE:
            available, detail = self.claude_available()
            if not available:
                self._set(task_id, status="failed", summary=f"Claude is not available: {detail}")
                self._event(task_id, "error", f"Claude is not available: {detail}")
                return
            self._set_owner(task_id, CLAUDE, why)
            self._request_claude(task_id, task["kind"], pack, extra=f"Follow-up from the user:\n{text}", reason=why)
            return
        self._set_owner(task_id, LOCAL, why)
        self._local_loop(task_id, pack, followup=text, reason=why)

    def _run_resume(self, task_id: int) -> None:
        task = self.db.get_task(task_id)
        run = task["state"].get("team_run") or {}
        if run and run.get("status") not in ("completed", "failed", "cancelled", None):
            self._act(task_id, "task.retry", "Resuming the team run from its saved task graph.", "user",
                      reason=f"You pressed Resume; the task was {task['status']}.")
            team_mod.TeamRunner(self, task_id).continue_run()
            return
        pack, analysis = self._analyze(task_id, task["objective"], "Reload the saved task state")
        brief = self.continuation_brief(task_id)
        backend = task["owner"] or LOCAL
        self._event(task_id, "status", f"Resuming with {BACKEND_LABEL[backend]} from the continuation brief.")
        self._act(task_id, "task.retry", f"Retrying with {BACKEND_LABEL[backend]} from the saved state.", "user",
                  reason=f"You pressed Retry/Resume; the task was {task['status']}.")
        if (backend == CLAUDE and task["workflow"] == "claude_leads" and self.cfg["lead"]["enabled"]
                and self.claude_available()[0] and not self._cloud_block(task_id)):
            # The lead decides again on the user's latest message. The continuation brief is built around
            # the chat's first message, which made Claude "write the change" for an old or answered request.
            users = [m["content"] for m in self.db.messages(task_id) if m["role"] == "user"]
            latest = users[-1] if users else task["objective"]
            followup = latest if len(users) > 1 else ""
            self._step_end(analysis, "completed", "Claude (lead) decides again on your latest message.")
            self._lead(task_id, pack, classify(latest, len(pack.files)), followup=followup)
            return
        if backend == CLAUDE and self.claude_available()[0] and not self._cloud_block(task_id):
            self._step_end(analysis, "completed", "Loaded the continuation brief; Claude owned this task.")
            self._request_claude(task_id, task["kind"], pack, extra=brief,
                                 reason="Retry: Claude was handling this task when it stopped.")
        else:
            self._step_end(analysis, "completed", "Loaded the continuation brief; continuing with Qwen.")
            self._set_owner(task_id, LOCAL, "Retry: continuing locally from the saved state.")
            self._local_loop(task_id, pack, feedback=f"You are resuming an interrupted task. Saved state:\n{brief}")

    # Claude as the lead -------------------------------------------------------------------

    def _lead_check(self, task_id: int, state: dict, backend: str | None = None,
                    text: str = "", cls: Any = None) -> tuple[bool, str]:
        """(True, "") when Claude leads this message; (False, why) when it should but cannot; (False, "") when
        the lead is off, you picked a model yourself, or the message is casual enough for Qwen alone."""
        if not self.cfg["lead"]["enabled"] or backend or state.get("override"):
            return False, ""
        if text and not self._claude_first() and self._casual(text, cls):
            self._act(task_id, "lead.skipped", "Casual message: Qwen answers directly, Claude is not asked.",
                      "system", "analysis", reason="Short chat or a simple question; no code changes, research or "
                      "hard reasoning.", outcome="local")
            return False, ""
        if self._cloud_block(task_id):
            return False, ""  # A local-only chat or project: your privacy choice, not a failure.
        if self.cfg["routing"].get("zero_paid_mode"):
            return False, "zero-paid mode is on, so Claude is never used automatically"
        ok, why = self.claude_available()
        return (True, "") if ok else (False, f"Claude is not available ({why})")

    def _claude_first(self) -> bool:
        """Claude-first lead: every message starts with Claude, and Claude hands most of the writing to Qwen."""
        return self.cfg["lead"].get("style", "claude_first") == "claude_first"

    def _lead_effort(self, purpose: str) -> str | None:
        """--effort for the lead's own requests (lead.effort); None = the Claude setting, as for all other work."""
        level = self.cfg["lead"].get("effort", "medium")
        return None if purpose not in ("lead", "lead_review") or level == "inherit" else level

    def _casual(self, text: str, cls: Any = None) -> bool:
        """Small talk or a simple question that Qwen handles well on its own."""
        cls = cls or classify(text, 0)
        if cls.kind != "ask" or cls.score > 1 or len(text) > 500 or "```" in text:
            return False
        if cls.category not in ("question", "explain", "prose", "general"):
            return False
        names = tools_mod.select(text)
        if names & {"web_search", "fetch_page"} and not tools_mod.weather_only(text):
            return False  # Research: Claude can search the web.
        return True

    def _lead_unavailable(self, task_id: int, why: str) -> bool:
        """Claude should lead but cannot. Say so, then follow Settings: continue with Qwen, or stop (True)."""
        stop = self.cfg["routing"].get("claude_fallback", "local") == "stop"
        self._act(task_id, "lead.unavailable", f"Claude cannot lead this message: {why}.", "system", "analysis",
                  outcome="stopped" if stop else "qwen")
        if stop:
            self._fail(task_id, f"Claude cannot lead this message: {why}. Settings say to stop instead of continuing "
                                "with Qwen. Pick Qwen in the chat to run it locally.")
            return True
        self._event(task_id, "note", f"Claude cannot lead this message: {why}. Qwen handles it instead, and its "
                                     "replies are labeled Qwen.")
        return False

    def _lead(self, task_id: int, pack: ctx.ContextPack, cls: Any, followup: str = "") -> None:
        self._set(task_id, kind=cls.kind, complexity=cls.complexity, workflow="claude_leads",
                  routing_reason="Claude leads: it reads every new message first and decides whether to answer, ask, "
                                 "or plan the work for Qwen and the team.")
        self._act(task_id, "lead.started", "Claude (lead) is reading the request.", CLAUDE, "analysis",
                  reason="Settings: Claude leads new messages.")
        self._set_owner(task_id, CLAUDE, "Claude leads this chat.")
        self._request_claude(task_id, "lead", pack, extra=f"Follow-up from the user:\n{followup}" if followup else "",
                             reason="Claude leads: it decides how to handle each message.")

    def _handle_lead(self, task_id: int, pending: dict, text: str) -> None:
        task = self.db.get_task(task_id)
        ws = self.workspace(task["project_id"])
        step_id = pending.get("step_id")
        mode, message, plan_text = ctx.parse_lead(text)
        note = ""
        plan = None
        if mode == "handoff":
            if plan_text:
                self._lead_handoff(task_id, pending, message, plan_text)
                return
            mode = "answer"  # An empty handoff: Claude's words are the answer.
        if mode == "delegate":
            if ctx.is_general(ws) or not ws.index and not plan_text:
                note = "Claude chose to delegate, but there is no project code to change here."
            elif not plan_text:
                note = "Claude chose to delegate but sent no plan, so nothing was changed."
            else:
                try:
                    plan = team_mod.parse_plan(plan_text, self.cfg["team"]["max_tasks"])
                except team_mod.PlanError as exc:
                    note = f"Enoy could not use Claude's plan ({exc}), so nothing was changed."
            if plan is None:
                self._act(task_id, "lead.plan_rejected", note, "system", "plan")
                mode = "answer"
        if note:
            message = (message + f"\n\n*{note}*").strip()
        self.db.update_task_state(task_id, decisions=[f"Claude (lead) chose: {mode}."])
        self.db.add_memory(task["project_id"], "decisions", f"Claude (lead) chose to {mode}: {_first_line(message)}",
                           False, "Claude (lead)", task_id)
        if message:
            self.db.add_message(task_id, "assistant", message, CLAUDE)
            self.bus.publish({"type": "message_done", "task_id": task_id, "backend": CLAUDE})
        if plan is None:
            self._step_end(step_id, "completed", "Asked you a question." if mode == "clarify" else "Answered directly.")
            self._act(task_id, f"lead.{mode}", "Claude asked you a question." if mode == "clarify"
                      else "Claude answered directly; no team work was needed.", CLAUDE, "claude")
            self._complete(task_id, _first_line(message))
            return
        self._step_end(step_id, "completed", f"Planned {len(plan[0])} task(s) for the team.")
        self._set(task_id, kind="edit", workflow="claude_leads")
        objective = task["objective"] + (f"\n\nFollow-up: {pending['followup']}" if pending.get("followup") else "")
        team_mod.TeamRunner(self, task_id).start_from_lead(objective, plan)

    def _lead_handoff(self, task_id: int, pending: dict, message: str, instructions: str) -> None:
        """Claude planned the reply; Qwen writes it (agent mode, with its tools) and the user sees Qwen's text."""
        task = self.db.get_task(task_id)
        self._step_end(pending.get("step_id"), "completed", "Handed the reply to Qwen with instructions.")
        self.db.update_task_state(task_id, decisions=["Claude (lead) chose: handoff."])
        self._act(task_id, "lead.handoff", "Claude planned the reply; Qwen writes it.", CLAUDE, "plan",
                  reason="Claude first: Claude decides, Qwen does the writing it can do from clear instructions.",
                  outcome="qwen")
        if message:
            self.db.add_message(task_id, "assistant", message, CLAUDE)
            self.bus.publish({"type": "message_done", "task_id": task_id, "backend": CLAUDE})
        users = [m["content"] for m in self.db.messages(task_id) if m["role"] == "user"]
        latest = pending.get("followup") or (users[-1] if users else task["objective"])
        brief = (f"{latest}\n\n---\nClaude, who leads this chat, read the message above and wrote these instructions "
                 f"for your reply. Follow them; your reply is what the user sees:\n{instructions}")
        ws = self.workspace(task["project_id"])
        pack = ctx.gather(ws, latest + "\n" + instructions, self.cfg["context"]) if ws.index else ctx.ContextPack()
        try:
            self._local_loop(task_id, pack, followup=brief, reason="Claude handed this reply to Qwen.")
        finally:
            # The chat stays Claude-led: the next message starts with Claude again, and Retry asks Claude.
            self._set(task_id, workflow="claude_leads")

    def _handle_lead_review(self, task_id: int, pending: dict, text: str) -> None:
        message, verdict, instruction = ctx.parse_lead_review(text)
        self._step_end(pending.get("step_id"), "completed", "Checked the results"
                       + (f" and asked for a fix: {instruction[:120]}" if verdict == "fix" else "."))
        if message:
            self.db.add_message(task_id, "assistant", message, CLAUDE)
            self.bus.publish({"type": "message_done", "task_id": task_id, "backend": CLAUDE})
        self._act(task_id, "lead.reviewed", "Claude checked the team's results" + (" and ordered a fix." if verdict == "fix"
                  else "."), CLAUDE, "review")
        self._set(task_id, status="running")
        team_mod.TeamRunner(self, task_id).finish_after_lead(verdict, instruction, bool(message))

    def team_task_action(self, team_task_id: int, action: str, provider: str | None = None) -> dict:
        """Your controls on one team task: cancel it, retry it, or pick its model."""
        tt = self.db.get_team_task(team_task_id)
        if not tt:
            raise TaskError("No such team task.")
        task_id = tt["task_id"]
        runner = team_mod.TeamRunner(self, task_id)
        label = f"{tt['plan_key']} {tt['title'][:60]}"
        if action == "cancel":
            if tt["status"] not in ("planned", "ready", "paused", "blocked"):
                raise TaskError("Only a task that has not started can be cancelled. Use Cancel on the chat to stop running work.")
            self.db.update_team_task(team_task_id, status="cancelled", error="You cancelled it.")
            runner.msg("user", tt["role"], "cancellation", f"You cancelled {label}.", team_task_id)
        elif action == "model":
            if provider not in (LOCAL, CLAUDE):
                raise TaskError("Pick Qwen or Claude.")
            if tt["status"] not in ("planned", "ready", "paused", "blocked", "failed") or tt["role"] not in ("developer", "debugger"):
                raise TaskError("The model can only be changed for a developer task that is not running or finished.")
            if provider == CLAUDE and self._cloud_block(task_id):
                raise TaskError(f"Not sent to Claude: {self._cloud_block(task_id)}.")
            self.db.update_team_task(team_task_id, provider=provider, route_reason=f"You picked {BACKEND_LABEL[provider]}.")
            runner.msg("user", tt["role"], "assignment", f"You moved {label} to {BACKEND_LABEL[provider]}.", team_task_id)
        elif action == "retry":
            if tt["status"] not in ("failed", "blocked", "cancelled") or tt["role"] not in ("developer", "debugger"):
                raise TaskError("Only a developer task that failed, was blocked or was cancelled can be retried.")
            for other in self.db.team_tasks(task_id):  # Its dependents get another chance too.
                if other["status"] == "blocked":
                    self.db.update_team_task(other["id"], status="planned", error="")
            self.db.update_team_task(team_task_id, status="planned", attempts=0, error="")
            runner.msg("user", tt["role"], "assignment", f"You asked to retry {label}.", team_task_id)
            task = self.db.get_task(task_id)
            if task["status"] not in ("running", "queued", "awaiting_claude"):
                runner._save_run(status="running", finished=None)
                self._set(task_id, status="running")
                self._cancel[task_id] = threading.Event()
                self._spawn(self._guard, task_id, lambda tid: team_mod.TeamRunner(self, tid).continue_run())
        else:
            raise TaskError(f"Unknown action {action}.")
        self._act(task_id, f"team.task_{action}", f"You changed {label}: {action}.", "user", "team")
        self._task_changed(task_id)
        return self.db.get_team_task(team_task_id)

    def hold_delegation(self, task_id: int, hold: bool) -> dict:
        """Pause or resume handing out team tasks in one chat. Finished work is kept."""
        task = self._require(task_id)
        run = task["state"].get("team_run") or {}
        if not run:
            raise TaskError("This chat has no team run.")
        runner = team_mod.TeamRunner(self, task_id)
        runner._save_run(hold=bool(hold))
        if hold:
            self._act(task_id, "team.hold", "You paused delegation. The current step finishes; nothing new starts.", "user")
            if task["status"] != "running" and run.get("status") not in ("completed", "failed", "cancelled"):
                runner._pause("You paused delegation for this chat.")
        elif run.get("status") == "paused":
            for tt in self.db.team_tasks(task_id):
                if tt["status"] == "paused":
                    self.db.update_team_task(tt["id"], status="planned")
            runner._save_run(status="running")
            self._act(task_id, "team.resumed", "Delegation resumed where it stopped.", "user")
            self._set(task_id, status="running")
            self._cancel[task_id] = threading.Event()
            self._spawn(self._guard, task_id, lambda tid: team_mod.TeamRunner(self, tid).continue_run())
        self._task_changed(task_id)
        return self.db.get_task(task_id)

    # Local attempts ---------------------------------------------------------------------

    def _signals(self, task_id: int) -> AttemptSignals:
        task = self.db.get_task(task_id)
        s = task["state"]
        return AttemptSignals(
            local_attempts=task["local_attempts"],
            invalid_outputs=s.get("invalid_outputs", 0),
            backend_errors=s.get("backend_errors", 0),
            test_failures=task["test_fix_attempts"],
            uncertain=s.get("uncertain", False),
            escalations=s.get("escalations", 0),
            repeated_error=s.get("repeated_error", False),
            scope_grew=s.get("scope_grew", False),
            handoffs=s.get("handoffs", 0),
        )

    def _chat_local(self, task_id: int, messages: list[ChatMessage], purpose: str, step_id: int | None = None,
                    tools: list[dict] | None = None) -> Any:
        backend = self.backends[LOCAL]
        cancel = self._cancel.setdefault(task_id, threading.Event())
        if not self._local_lock.acquire(blocking=False):
            self._event(task_id, "status", "Waiting for the local model to finish another task.", LOCAL)
            other = self._busy.get(LOCAL, {}).get("task_id")
            if step_id:
                self.db.update_step(step_id, status="waiting")
            self._act(task_id, "task.waiting", "Waiting for Qwen to finish another request.", LOCAL, "local",
                      reason="Qwen runs one request at a time" + (f"; it is busy with task #{other}." if other else "."),
                      step_id=step_id)
            while not self._local_lock.acquire(timeout=0.5):
                self._check_cancel(task_id)
            if step_id:
                self.db.update_step(step_id, status="working")
        busy = {"task_id": task_id, "step_id": step_id, "purpose": purpose, "since": time.time(), "chunks": 0}
        try:
            limits = self._memory_plan(task_id, step_id)
        except BackendError:
            self._local_lock.release()
            raise
        self._busy[LOCAL] = busy
        self._partial[task_id] = ""

        def on_token(t: str) -> None:
            busy["chunks"] += 1
            self._partial[task_id] += t
            self.bus.publish({"type": "token", "task_id": task_id, "backend": LOCAL, "text": t})

        try:
            self._event(task_id, "status", f"{BACKEND_LABEL[LOCAL]} is working ({backend.settings['model']}).", LOCAL)
            req = ChatRequest(
                messages=messages, purpose=purpose, cancel=cancel, on_token=on_token, tools=tools, **limits,
                on_thinking=lambda t: self.bus.publish({"type": "thinking", "task_id": task_id, "backend": LOCAL, "text": t}),
            )
            if self.memory is not None:
                self.memory.begin(task_id, cancel, backend.settings["model"], limits["num_ctx"] or backend.settings["num_ctx"])
            sent = "\n\n".join(f"[{m.role}]\n{m.content}" for m in messages)
            chars = len(sent)
            n_img = sum(len(m.images) for m in messages)
            pics = f" and {n_img} image(s)" if n_img else ""
            self.db.add_transmission(task_id, LOCAL, purpose, [], sent + (f"\n\n[{n_img} image(s) attached]" if n_img else ""))
            self._event(task_id, "send", f"Sent {chars:,} characters{pics} to the local model (stays on this PC).", LOCAL)
            self._act(task_id, "backend.request", f"Sent {chars:,} characters{pics} to Qwen (stays on this PC).", LOCAL,
                      "local", step_id=step_id)
            result = backend.chat(req)
            self._partial.pop(task_id, None)
            self._count_work(task_id, LOCAL, result)
            return result
        finally:
            if self.memory is not None:
                self.memory.end()
            self._busy.pop(LOCAL, None)
            self._local_lock.release()

    # ------------------------------------------------------------ tools

    def _agent_loop(self, task_id: int, pack: ctx.ContextPack, followup: str = "", reason: str = "") -> None:
        """Qwen as an agent: it looks, edits, runs and checks with tools until it can answer.

        Edits are written at once with backups (tools.agent_apply) and become one applied change set with Undo,
        or are staged and proposed at the end. Commands outside the allowlist are handed back for approval."""
        task = self.db.get_task(task_id)
        ws = self.workspace(task["project_id"])
        general = ctx.is_general(ws)
        text = followup or task["objective"]
        image_ids = self._image_ids(task_id)
        if image_ids and not self._local_sees_images():
            why = f"Qwen's model ({self.backends[LOCAL].settings['model']}) cannot read images."
            if self._maybe_escalate(task_id, pack, why, force_reason=True):
                return
            self._fail(task_id, why + " Pick a model with vision in Settings, or ask Claude.")
            return
        s = self.cfg.get("tools", {})
        files = None
        if not general:
            backup_dir = (self.backups_dir or Path(ws.root) / ".fusion" / "backups") / f"task-{task_id}"

            def run(command: str) -> dict:
                started = self.run_command(task["project_id"], command, task_id=task_id, wait=True)
                r = self.db.get_command_run(started["run_id"]) or {}
                return {"status": r.get("status"), "exit_code": r.get("exit_code"),
                        "output": ((r.get("stdout") or "") + "\n" + (r.get("stderr") or "")).strip()}
            files = tools_mod.AgentFiles(ws, bool(s.get("agent_apply", True)), backup_dir, run)
        num_ctx, _ = self._local_window()
        offer = None
        if num_ctx < 8192:  # A small window cannot hold every tool definition plus real work.
            offer = (tools_mod.CORE_AGENT_TOOLS if files else set()) | tools_mod.select(text)
        runner = self._tool_runner(task_id, LOCAL, offer, services={"files": files} if files else None)
        names = [t.name for t in runner.available()]
        if not names:
            runner = None
        budget = ctx.prompt_budget(*self._local_window())
        if general:
            where = "There is no project folder in this chat (general chat)."
        else:
            listing = ctx.file_list(ws, min(2500, budget // 8)) if hasattr(ctx, "file_list") else ""
            rules = "\n".join(f"{p}:\n{t[:1500]}" for p, t in ws.instructions()[:2])
            where = (f"Project folder: {ws.name}. Paths are relative to it.\n" + (f"Files:\n{listing}\n" if listing else "")
                     + (f"Project instructions:\n{rules}\n" if rules else "")
                     + ("Edits are written to disk at once (with backups; the user can undo the whole turn)."
                        if files and files.apply else "Edits are staged and shown to the user as one proposal at the end."))
        system = tools_mod.AGENT_SYSTEM.format(names=", ".join(names) or "none", context=where) if runner else \
            ctx.local_system_prompt(ws, pack, "ask")
        convo = self._conversation(task_id, limit=8)
        messages = [ChatMessage("system", system)] + convo + [ChatMessage("user", text, self._load_images(task_id, image_ids))]
        step = self._step(task_id, "Qwen works on it (agent)", LOCAL, "local", reason or "Agent mode: Qwen uses tools.",
                          files=[f.path for f in pack.files])
        self._set(task_id, local_attempts=task["local_attempts"] + 1, workflow="agent")
        try:
            if runner:
                result = self._chat_local_tools(task_id, messages, "agent", step, runner, budget,
                                                rounds=int(s.get("agent_max_steps", 14)))
            else:
                result = self._chat_local(task_id, messages, "ask", step)
        except BackendError as exc:
            self._agent_finish_files(task_id, files, partial=True)
            if exc.kind == "cancelled":
                raise _Cancelled() from exc
            self._step_end(step, "failed", error=str(exc))
            self._event(task_id, "error", f"Local model failed: {exc}", LOCAL)
            if exc.kind == "memory":
                self._fail(task_id, str(exc))
                return
            if self._maybe_escalate(task_id, pack, f"Qwen (agent) failed: {exc}", force_reason=True):
                return
            self._fail(task_id, f"Qwen could not finish: {exc}")
            return
        except _Cancelled:
            self._agent_finish_files(task_id, files, partial=True)
            raise
        answer = self._with_sources(result.text, runner) if runner and runner.calls else result.text
        self.db.add_message(task_id, "assistant", answer, LOCAL)
        self.bus.publish({"type": "message_done", "task_id": task_id, "backend": LOCAL})
        meta = self._usage_note(result)
        if meta:
            self._event(task_id, "usage", meta, LOCAL)
        if files and files.suggested:
            self.db.update_task_state(task_id, suggested_commands=files.suggested[:5])
        changed = self._agent_finish_files(task_id, files)
        used = len(runner.calls) if runner else 0
        self._step_end(step, "completed", f"Done after {used} tool call(s)" + (f"; changed {changed}." if changed else "."))
        if files and not files.apply and changed:
            self._await_approval(task_id, ask=True)
            return
        self._complete(task_id, _first_line(result.text))

    def _agent_finish_files(self, task_id: int, files: Any, partial: bool = False) -> str:
        """Record the agent's file changes as one change set (applied with Undo, or pending for review)."""
        if files is None:
            return ""
        changes = files.changeset_files()
        if not changes:
            return ""
        names = ", ".join(f["path"] for f in changes)
        note = "Agent changes" + (" (stopped before it finished)" if partial else "")
        cs_id = self.db.create_changeset(task_id, LOCAL, changes, {"ok": True, "errors": [], "warnings": []}, note)
        if files.apply:
            self.db.set_changeset_status(cs_id, "applied")
            self.db.update_task_state(task_id, decisions=[f"Qwen (agent) changed {names}"])
            self._event(task_id, "applied", f"Qwen changed: {names}. Backups kept; Undo restores them.", LOCAL)
        else:
            self._event(task_id, "proposal", f"Qwen proposes changes to {names}.", LOCAL)
        self.bus.publish({"type": "changeset", "task_id": task_id, "changeset_id": cs_id})
        return names

    def _tools_wanted(self, text: str) -> bool:
        s = self.cfg.get("tools", {})
        return bool(s.get("enabled", True) and tools_mod.wants_tools(text))

    def _tool_runtime(self) -> dict:
        """Facts about the local model for check_local_model_fit (all read from Ollama and Enoy, not guessed)."""
        rt = hardware.inventory(self).get("runtime", {})
        local = self.backends.get(LOCAL)
        model = local.settings.get("model") if local else None
        try:
            for m in local.list_models() if local else []:
                if m.get("name") == model or m.get("model") == model:
                    rt["configured_model_size_on_disk_mib"] = round((m.get("size") or 0) / 2**20)
                    details = m.get("details") or {}
                    rt["parameter_size"] = details.get("parameter_size")
                    rt["quantization"] = details.get("quantization_level")
        except Exception as exc:
            rt["model_list_error"] = str(exc)
        num_ctx, num_predict = self._local_window()
        rt["context_window_tokens"] = num_ctx
        rt["memory_mode_limit_note"] = ("Enoy caps the model runner's RAM to the memory mode's limit; a larger "
                                        "context needs more memory.")
        return rt

    def _tool_runner(self, task_id: int, agent: str, offer: set[str] | None = None,
                     services: dict | None = None) -> tools_mod.ToolRunner:
        cancel = self._cancel.setdefault(task_id, threading.Event())

        def on_event(kind: str, info: dict) -> None:
            if kind == "tool.started":
                args = ", ".join(f"{k}={v}" for k, v in info["args"].items())
                self._act(task_id, "tool.started",
                          redact(f"{BACKEND_LABEL.get(agent, agent)} → {info['tool']}({args})"[:300]),
                          agent, "tools", reason="The answer needs information from outside this chat.")
                return
            r: tools_mod.ToolResult = info["result"]
            outcome = "ok" if r.ok else ("cancelled" if r.error_kind == "cancelled" else "failed")
            what = ""
            if r.ok and r.tool == "web_search":
                what = f" via {r.data.get('provider')}: {len(r.data.get('results', []))} result(s)"
            elif r.ok and r.tool == "fetch_page":
                what = f": {r.data.get('url', '')}"
            elif r.ok and r.tool == "get_weather":
                what = f": {r.data.get('location', {}).get('name', '')}, {r.data.get('provider', '').split(',')[0]}"
            note = ("repeat of an earlier call; reused" if r.duplicate else "from cache" if r.cached
                    else f"{r.duration_s:.1f} s")
            text = f"{r.tool} succeeded{what} ({note})" if r.ok else f"{r.tool} {outcome}: {r.error}"
            self._act(task_id, "tool.finished", redact(text[:400]), agent, "tools", outcome=outcome)
            state = self.db.get_task(task_id)["state"]
            calls = (state.get("tool_calls") or [])[-19:] + [{
                "tool": r.tool, "agent": agent, "ok": r.ok, "error": r.error[:200], "cached": r.cached,
                "sources": [s.get("url") for s in r.sources][:5], "at": time.time()}]
            self.db.update_task_state(task_id, tool_calls=calls,
                                      live_info=bool(state.get("live_info") or (r.ok and r.sources)))

        return tools_mod.ToolRunner(self.cfg, tools_mod.registry(), cancel, on_event, runtime=self._tool_runtime,
                                    offer=offer, services=services)

    def _chat_local_tools(self, task_id: int, messages: list[ChatMessage], purpose: str, step_id: int | None,
                          runner: tools_mod.ToolRunner, budget: int, rounds: int | None = None) -> Any:
        """Qwen with native tool calling: call, run the tools it asks for, return the real results, repeat.

        Bounded by tools.max_rounds; after that Qwen must answer with what it has."""
        msgs = list(messages)
        specs = [t.spec() for t in runner.available()]
        rounds = rounds or int(self.cfg.get("tools", {}).get("max_rounds", 5))
        per_result = max(2500, min(8000, budget // 3))
        spec_chars = len(json.dumps(specs))
        for n in range(rounds + 1):
            self._check_cancel(task_id)
            last = n == rounds
            if last:
                msgs.append(ChatMessage("user", "Tool limit reached. Answer now with the information you have, "
                                                "and say what you could not find out."))
            _fit_window(msgs, budget - spec_chars)
            result = self._chat_local(task_id, msgs, purpose, step_id, tools=None if last else specs)
            if not result.tool_calls or last:
                if not result.text.strip():
                    raise BackendError("malformed", "Qwen called tools but gave no answer.")
                return result
            self.bus.publish({"type": "message_done", "task_id": task_id, "backend": LOCAL})
            calls = result.tool_calls[:4]  # At most four calls per round.
            msgs.append(ChatMessage("assistant", result.text, tool_calls=calls))
            for call in calls:
                res = runner.run(call["name"], call["arguments"])
                msgs.append(ChatMessage("tool", res.for_model(per_result), tool_name=call["name"]))
                if res.error_kind == "cancelled":
                    raise _Cancelled()
        raise BackendError("failed", "The tool loop ended without an answer.")

    def _claude_web(self, task_id: int, purpose: str) -> bool:
        """Give Claude Claude Code's own WebSearch/WebFetch when the latest message needs live information."""
        s = self.cfg.get("tools", {})
        if not (s.get("enabled", True) and s.get("online", True) and s.get("claude_web", True)
                and s.get("permissions", {}).get("web")):
            return False
        if purpose not in ("lead", "ask"):
            return False
        users = [m for m in self.db.messages(task_id) if m["role"] == "user"]
        text = users[-1]["content"] if users else self.db.get_task(task_id)["objective"]
        return tools_mod.wants_tools(text)

    def _claude_enoy_tools(self, task_id: int, purpose: str) -> list[str]:
        """Enoy tools Claude may call through Enoy's MCP server: hardware and weather (Claude has its own web tools)."""
        s = self.cfg.get("tools", {})
        if not s.get("enabled", True) or purpose not in ("lead", "ask"):
            return []
        users = [m for m in self.db.messages(task_id) if m["role"] == "user"]
        text = users[-1]["content"] if users else self.db.get_task(task_id)["objective"]
        wanted = tools_mod.select(text) & (tools_mod.PC_TOOLS | {"get_weather"})
        runner = tools_mod.ToolRunner(self.cfg, tools_mod.registry(), offer=wanted)
        return sorted(t.name for t in runner.available())

    def _research_route(self, decision: Any, text: str, claude_ok: bool, override: str | None, block: str) -> Any:
        """Questions that need web research go to Claude when Qwen's search can only reach Wikipedia.

        Weather stays with Qwen (official weather data needs no search), and so does everything when the user
        picked a model, the chat is local-only, zero-paid mode is on, or Claude may not search the web."""
        s = self.cfg.get("tools", {})
        if (decision.backend != LOCAL or override or block or not claude_ok or decision.source != "router"
                or self.cfg["routing"].get("zero_paid_mode") or not s.get("enabled", True)
                or not s.get("online", True) or not s.get("claude_web", True)):
            return decision
        if "wikipedia" not in tools_mod.search.configured(self.cfg)[:1]:
            return decision  # Qwen has a real web search (Brave or SearXNG).
        names = tools_mod.select(text)
        if not names & {"web_search", "fetch_page"} or tools_mod.about_this_pc(text) or tools_mod.weather_only(text):
            return decision
        return dataclasses.replace(
            decision, backend=CLAUDE, workflow="escalate", source="policy",
            reason="This needs current information from the web. Claude can search it through Claude Code; Qwen's "
                   "search only reaches Wikipedia until a Brave key or SearXNG is set in Settings → Tools.")

    def _prefetch_for_claude(self, task_id: int, wanted: list[str]) -> str:
        """Run the read-only PC tools Claude would need and return the results as prompt text."""
        pc_wanted = [n for n in wanted if n in tools_mod.PC_TOOLS]
        if not pc_wanted:
            return ""
        runner = self._tool_runner(task_id, "system")
        name = "check_local_model_fit" if "check_local_model_fit" in pc_wanted and len(pc_wanted) == 1 \
            else "get_hardware_inventory"
        res = runner.run(name, {})
        if not res.ok:
            return f"Enoy tried to read this PC's hardware for you ({name}) and it failed: {res.error}"
        return ("Facts about the user's PC, read from Windows by Enoy just now (read-only; answer from these, "
                "do not ask the user to look them up):\n" + res.for_model(9000))

    def _mcp_env(self) -> dict:
        try:
            runtime = self._tool_runtime()
        except Exception as exc:
            runtime = {"error": str(exc)}
        return {"ENOY_MCP_CFG": {"tools": self.cfg.get("tools", {}), "pc": self.cfg.get("pc", {})},
                "ENOY_MCP_RUNTIME": runtime}

    def _record_claude_tools(self, task_id: int, events: list[dict], step_id: int | None) -> None:
        for e in events[:20]:
            what = f'"{e["query"]}"' if e.get("query") else e.get("url", "")
            self._act(task_id, "tool.finished", redact(f"Claude used {e['tool']}: {what}"[:400]), CLAUDE, "tools",
                      outcome="ok", step_id=step_id)
        state = self.db.get_task(task_id)["state"]
        calls = (state.get("tool_calls") or [])[-19:] + [
            {"tool": e["tool"], "agent": CLAUDE, "ok": True, "error": "", "cached": False,
             "sources": [e["url"]] if e.get("url") else [], "query": e.get("query", ""), "at": time.time()}
            for e in events[:20]]
        self.db.update_task_state(task_id, tool_calls=calls[-20:], live_info=bool(state.get("live_info") or events))
        if not events:
            self._act(task_id, "tool.finished", "Claude had web tools available but did not use them.", CLAUDE, "tools",
                      outcome="unused", step_id=step_id)

    @staticmethod
    def _with_sources(text: str, runner: tools_mod.ToolRunner) -> str:
        """Add the sources Enoy actually retrieved, so provenance never depends on the model citing them."""
        used = runner.sources()
        # Failed data sources matter to the reader; an agent's misses while exploring files do not.
        failed = [r for r in runner.calls if not r.ok and r.error_kind != "invalid"
                  and r.tool not in tools_mod.WORKSPACE_TOOLS]
        lines = []
        if used:
            lines.append("**Sources retrieved by Enoy:** " + " · ".join(
                f"[{s.get('title') or s['url']}]({s['url']})" for s in used[:8]))
        if failed:
            lines.append("**Tools that failed:** " + "; ".join(f"{r.tool}: {r.error}" for r in failed[:4]))
        local = sorted({r.tool for r in runner.calls if r.ok and r.tool in tools_mod.PC_TOOLS})
        if local:
            lines.append(f"*Read from Windows by Enoy (read-only) at {time.strftime('%H:%M')}: {', '.join(local)}.*")
        if not runner.calls and not runner.services:
            lines.append("*No tools were used: this answer comes from the model's own knowledge, not live data.*")
        return text.rstrip() + ("\n\n---\n" + "\n\n".join(lines) if lines else "")

    def _memory_plan(self, task_id: int | None, step_id: int | None = None) -> dict:
        """Request settings the memory mode allows (ChatRequest fields), or BackendError("memory") if the model cannot fit."""
        if self.memory is None:
            return {"num_ctx": None}
        local = self.backends[LOCAL]
        predict = local.settings.get("num_predict")
        plan = self.memory.plan(local.settings["model"], int(local.settings["num_ctx"]), int(predict) if predict else None)
        if not plan["ok"]:
            self._act(task_id, "memory.load_blocked", "Local model not loaded: it would exceed the memory limit.", LOCAL,
                      "local", reason=plan["reason"], outcome="blocked", step_id=step_id)
            raise BackendError("memory", plan["reason"])
        if plan.get("note") and task_id:
            self._act(task_id, "memory.context_reduced" if plan.get("ctx") != local.settings["num_ctx"] else "memory.note",
                      plan["note"], LOCAL, "local", step_id=step_id)
        return {"num_ctx": plan.get("ctx"), "num_predict": plan.get("num_predict"), "num_batch": plan.get("num_batch"),
                "keep_alive": self.memory.keep_alive(local.settings.get("keep_alive"))}

    def _team_skips_images(self, task_id: int, latest_only: bool = False) -> bool:
        """The team works from text only, so a message with pictures is answered directly."""
        if not self._image_ids(task_id, latest_only):
            return False
        self._event(task_id, "note", "This message has images, so it is answered directly; TEAM ROLES works from text only.")
        return True

    def _image_ids(self, task_id: int, latest_only: bool = False) -> list[str]:
        """Images the user attached in this chat, newest last, at most one message's worth."""
        users = [m for m in self.db.messages(task_id) if m["role"] == "user"]
        if latest_only:
            users = users[-1:]
        ids: list[str] = []
        for m in reversed(users):
            for i in reversed(m.get("images") or []):
                if i not in ids:
                    ids.append(i)
        return list(reversed(ids[:imgs.MAX_PER_MESSAGE]))

    def _load_images(self, task_id: int, ids: list[str]) -> list:
        out = []
        for i in ids:
            try:
                out.append(imgs.load(i))
            except imgs.ImageError:
                self._event(task_id, "note", "An attached image is missing from disk and was left out.")
        return out

    def _local_sees_images(self) -> bool:
        backend = self.backends[LOCAL]
        if not hasattr(backend, "capabilities"):
            return True
        try:
            return "vision" in backend.capabilities(backend.settings["model"])
        except BackendError:
            return True  # Unknown: let the request show the real error.

    def _conversation(self, task_id: int, limit: int = 6) -> list[ChatMessage]:
        msgs = self.db.messages(task_id)[:-1]  # Latest user message is rebuilt with context.
        return [ChatMessage(m["role"], m["content"]) for m in msgs[-limit:] if m["role"] in ("user", "assistant")]

    def _local_loop(self, task_id: int, pack: ctx.ContextPack, feedback: str = "", followup: str = "",
                    plan: str = "", reason: str = "") -> None:
        task = self.db.get_task(task_id)
        ws = self.workspace(task["project_id"])
        if self.cfg.get("tools", {}).get("agent", True) and self.cfg.get("tools", {}).get("enabled", True) \
                and not plan and not feedback:
            return self._agent_loop(task_id, pack, followup, reason)
        kind = task["kind"]
        max_attempts = self.cfg["routing"]["max_local_attempts"]
        attempts_here = 0
        step_reason = reason or task["state"].get("assign_reason", "")
        title = "Write the change" if kind == "edit" else "Answer"
        image_ids = self._image_ids(task_id)
        if image_ids and not self._local_sees_images():
            why = f"Qwen's model ({self.backends[LOCAL].settings['model']}) cannot read images."
            self._event(task_id, "note", why, LOCAL)
            if self._maybe_escalate(task_id, pack, why, force_reason=True):
                return
            self._fail(task_id, why + " Pick a model with vision in Settings, or ask Claude.")
            return
        pictures = self._load_images(task_id, image_ids)
        budget = ctx.prompt_budget(*self._local_window())
        read_rounds, read_note = 0, ""
        runner = None
        if kind != "edit" and self._tools_wanted(followup or task["objective"]):
            runner = self._tool_runner(task_id, LOCAL, tools_mod.select(followup or task["objective"]))
            if not runner.available():
                runner = None
            else:  # Tool definitions travel with every request and use part of Qwen's window.
                budget -= len(json.dumps([t.spec() for t in runner.available()]))
        while True:
            self._check_cancel(task_id)
            attempts_here += 1
            task = self.db.get_task(task_id)
            self._set(task_id, local_attempts=task["local_attempts"] + 1)
            step = self._step(task_id, title if attempts_here == 1 else f"{title} (attempt {attempts_here} of {max_attempts})",
                              LOCAL, "local", step_reason, attempt=attempts_here,
                              files=[f.path for f in pack.files])
            reads_left = MAX_READ_ROUNDS - read_rounds if ws.index else None
            system = ctx.local_system_prompt(ws, pack, kind, list_chars=min(3000, budget // 8), reads_left=reads_left)
            if runner:
                system += "\n\n" + tools_mod.TOOL_SYSTEM.format(names=", ".join(t.name for t in runner.available()))
            extra = []
            if plan:
                extra.append(f"Follow this implementation plan:\n{plan}")
            if followup:
                extra.append(f"Follow-up from the user:\n{followup}")
            if read_note:
                extra.append(read_note)
            if feedback:
                extra.append(feedback)
            convo = self._conversation(task_id) if followup else []
            # Only as much file content as fits Qwen's context window; the rest would be cut off by Ollama unseen.
            fixed = (len(system) + sum(len(m.content) for m in convo)
                     + len(ctx.task_message(task["objective"], dataclasses.replace(pack, files=[]), "\n\n".join(extra))))
            shown = ctx.fit_files(pack, budget - fixed - 100)  # 100: headings around the files.
            user = ctx.task_message(task["objective"], shown, "\n\n".join(extra))
            messages = [ChatMessage("system", system)] + convo + [ChatMessage("user", user, pictures)]
            try:
                if runner:
                    result = self._chat_local_tools(task_id, messages, kind, step, runner, budget)
                else:
                    result = self._chat_local(task_id, messages, kind, step)
            except BackendError as exc:
                if exc.kind == "cancelled":
                    partial = self._partial.pop(task_id, "").strip()
                    if partial and task_id not in self._stop_reason:  # Stopped by the user: keep what was written.
                        self.db.add_message(task_id, "assistant", partial + "\n\n*Stopped before the answer was finished.*",
                                            LOCAL)
                    raise _Cancelled() from exc
                self._step_end(step, "failed", error=str(exc))
                step_reason = f"Retry after a local model error ({exc.kind})."
                state = self.db.update_task_state(task_id, backend_errors=self.db.get_task(task_id)["state"].get("backend_errors", 0) + 1,
                                                  failures=[f"Local model: {exc}"])
                self._event(task_id, "error", f"Local model failed: {exc}", LOCAL)
                if exc.kind == "memory":
                    self._fail(task_id, str(exc))
                    return
                if exc.kind in ("unavailable", "model_missing"):
                    # Retrying will not help; escalate if allowed, else stop with a clear message.
                    if self._maybe_escalate(task_id, pack, f"Local model unavailable: {exc}", force_reason=True):
                        return
                    self._fail(task_id, f"{exc} See Diagnostics for help.")
                    return
                if attempts_here < max_attempts:
                    self._event(task_id, "retry", f"Retrying locally (attempt {attempts_here + 1} of {max_attempts}).", LOCAL)
                    continue
                if self._maybe_escalate(task_id, pack, f"Local model failed: {exc}"):
                    return
                self._fail(task_id, f"The local model failed {attempts_here} time(s): {exc}")
                return

            asked = ctx.read_requests(result.text)
            if asked and reads_left:
                read_rounds += 1
                attempts_here -= 1  # Looking up files is not a failed attempt.
                self._set(task_id, local_attempts=self.db.get_task(task_id)["local_attempts"] - 1)
                added, refused = ctx.add_files(ws, pack, asked, self.cfg["context"]["max_file_chars"])
                self.db.update_task_state(task_id, relevant_files=[{"path": f.path, "sha": f.sha[:12]} for f in pack.files],
                                          file_versions={f.path: f.sha for f in pack.files})
                self.bus.publish({"type": "message_done", "task_id": task_id, "backend": LOCAL})
                got = ", ".join(added) or "none"
                self._step_end(step, "completed", f"Read files: {got}.")
                self._event(task_id, "context", f"Qwen asked to see {len(asked)} file(s). Added: {got}"
                            + (". Not given: " + "; ".join(refused) if refused else "."), LOCAL)
                self._act(task_id, "context.files_read", f"Qwen read {len(added)} more file(s): {got}.", LOCAL, "local",
                          reason="Qwen asked for files it needed.", outcome="read", step_id=step)
                read_note = ("Enoy added the files you asked for." if added else "")
                if refused:
                    read_note += " Files Enoy could not give you: " + "; ".join(refused) + "."
                step_reason = "Continue with the files Qwen asked for."
                continue
            if asked:
                result.text = ctx.strip_reads(result.text)
                if not result.text:
                    self._step_end(step, "failed", error="Asked for more files after the limit.")
                    if attempts_here < max_attempts:
                        feedback = ctx.NO_MORE_READS + " Give your answer now."
                        continue
                    self._fail(task_id, "Qwen kept asking for more files instead of answering. Name the files in your "
                                        "message, or ask Claude.")
                    return
            if runner:
                result.text = self._with_sources(result.text, runner)
            self.db.add_message(task_id, "assistant", result.text, LOCAL)
            self.bus.publish({"type": "message_done", "task_id": task_id, "backend": LOCAL})
            meta = self._usage_note(result)
            if meta:
                self._event(task_id, "usage", meta, LOCAL)
            uncertain = sounds_uncertain(result.text)
            if uncertain:
                self.db.update_task_state(task_id, uncertain=True)
                self._event(task_id, "note", "The local model said it is unsure. You can ask Claude to check.", LOCAL)
            if result.usage.get("truncated"):
                self._event(task_id, "note", "The answer hit the output length limit and may be cut off.", LOCAL)

            if kind != "edit":
                self._step_end(step, "completed", "Answered" + self._result_note(result) + ".")
                self._complete(task_id, _first_line(result.text))
                return

            parsed = patches.parse(result.text)
            if not ws.index and not parsed.changes and not parsed.errors:
                # Empty folder and no new files: Qwen explained that there is nothing to change. That is the answer.
                self._step_end(step, "completed", "Answered: the project folder is empty.")
                self._complete(task_id, _first_line(result.text))
                return
            validation = patches.validate(ws, parsed.changes, task["state"].get("file_versions"))
            problems = parsed.errors + validation.errors + self._unseen_rewrites(ws, parsed.changes, shown)
            if parsed.commands:
                self.db.update_task_state(task_id, suggested_commands=parsed.commands)
            if not validation.files and not problems:
                problems = ["No usable file changes were produced."]
            self._note_scope(task_id, {c.path for c in parsed.changes})
            if self._check_proposal(task_id, step, LOCAL, result, validation, problems, parsed.explanation):
                self._after_local_proposal(task_id, pack)
                return
            invalid = self.db.get_task(task_id)["state"].get("invalid_outputs", 0) + 1
            self.db.update_task_state(task_id, invalid_outputs=invalid, failures=["Invalid local proposal: " + "; ".join(problems[:4])])
            self._event(task_id, "validation", "The local proposal could not be used:\n- " + "\n- ".join(problems[:8]), LOCAL)
            if attempts_here < max_attempts:
                step_reason = "Retry: the previous proposal failed Enoy's checks."
                seen = {f.path for f in shown.files}
                unseen = sorted({c.path for c in parsed.changes if c.action == "edit" and c.path not in seen})
                feedback = ("Your previous answer could not be applied because:\n- " + "\n- ".join(problems[:8])
                            + "\nSend corrected blocks for ALL intended changes, following the block format exactly.")
                if unseen and MAX_READ_ROUNDS - read_rounds > 0:
                    feedback += ("\nYou edited " + ", ".join(unseen) + " without seeing it, so the SEARCH lines were "
                                 "guessed. First ask for the part you need, for example <<<READ " + unseen[0]
                                 + " | text to find>>>, then copy the SEARCH lines exactly.")
                self._event(task_id, "retry", f"Asking the local model to correct it (attempt {attempts_here + 1} of {max_attempts}).", LOCAL)
                continue
            if self._maybe_escalate(task_id, pack, "Local proposals failed validation:\n- " + "\n- ".join(problems[:8])):
                return
            self._fail(task_id, "The local model could not produce a valid change: " + "; ".join(problems[:3]))
            return

    @staticmethod
    def _unseen_rewrites(ws: Workspace, changes: list, shown: ctx.ContextPack) -> list[str]:
        """A whole-file rewrite of an existing file the model saw only in part would delete the rest."""
        full = {f.path for f in shown.files if not f.truncated}
        problems = []
        for c in changes:
            if c.action != "write":
                continue
            try:
                rel = ws.rel(ws.resolve(c.path))
            except (PathError, ValueError):
                continue  # validate() reports paths outside the project.
            if rel not in full and ws.current_hash(rel) is not None:
                problems.append(f"'{rel}' exists and you did not see all of it, so a FILE block would delete the parts "
                                "you did not see. Change it with an EDIT block instead.")
        return problems

    def _local_window(self) -> tuple[int, int | None]:
        """Context window and answer length Qwen gets in the current memory mode."""
        s = self.backends[LOCAL].settings
        num_ctx = int(s.get("num_ctx") or DEFAULTS["ollama"]["num_ctx"])
        predict = int(s["num_predict"]) if s.get("num_predict") else None
        if self.memory is not None:
            limits = RUNTIME[self.memory.mode]
            num_ctx = min(num_ctx, limits["max_ctx"])
            if predict and limits["max_predict"]:
                predict = min(predict, limits["max_predict"])
        return num_ctx, predict

    def _after_local_proposal(self, task_id: int, pack: ctx.ContextPack) -> None:
        task = self.db.get_task(task_id)
        workflow = task["workflow"]
        available, _ = self.claude_available()
        available = available and not self._cloud_block(task_id)
        if workflow == "local_then_review" and available:
            reviews = task["state"].get("review_rounds", 0)
            if reviews < self.cfg["routing"]["max_review_rounds"]:
                cs = self.db.list_changesets(task_id, "pending")[0]
                self._request_claude(task_id, "review", pack, changeset_id=cs["id"],
                                     reason="The 'Qwen writes, Claude reviews' workflow asks Claude to check Qwen's change.")
                return
        if workflow == "compare" and available and not task["state"].get("compared"):
            self.db.update_task_state(task_id, compared=True)
            analysis = task["state"].get("analysis_step")
            self._request_claude(task_id, "edit", pack, extra="Propose your own independent solution. Another model is also proposing one; the user will compare them.",
                                 reason="The 'compare' workflow asks Claude for an independent solution.",
                                 depends_on=[analysis] if analysis else None)
            return
        self._await_approval(task_id)

    def _maybe_escalate(self, task_id: int, pack: ctx.ContextPack, report: str, force_reason: bool = False) -> bool:
        task = self.db.get_task(task_id)
        available, why = self.claude_available()
        signals = self._signals(task_id)
        if force_reason:
            signals.invalid_outputs = max(signals.invalid_outputs, self.cfg["routing"]["max_local_attempts"])
        block = self._cloud_block(task_id)
        if block:
            ok, reason = False, f"Claude might help, but {block}"
        elif self.cfg["routing"].get("zero_paid_mode"):
            ok, reason = False, "zero-paid mode is on, so Claude is never used automatically"
        else:
            ok, reason = should_escalate(signals, self.cfg["routing"], task["workflow"], available)
        if not ok:
            self._event(task_id, "route", f"Not escalating: {reason}")
            self._act(task_id, "task.not_escalated", "Not escalating to Claude.", "system", reason=reason)
            return False
        self._escalate(task_id, reason, report=report, pack=pack)
        return True

    def _escalate(self, task_id: int, reason: str, report: str = "", pack: ctx.ContextPack | None = None,
                  user_requested: bool = False) -> None:
        task = self.db.get_task(task_id)
        pack = pack or self._gather(task_id, task["objective"])
        escalations = task["state"].get("escalations", 0) + 1
        self.db.update_task_state(task_id, escalations=escalations)
        self._set(task_id, escalation_reason=reason)
        self._act(task_id, "task.escalated", "Escalated from Qwen to Claude." if not user_requested
                  else "You asked Claude to take over.", CLAUDE, "claude", reason=reason)
        self._set_owner(task_id, CLAUDE, f"Escalated: {reason}")
        extra = self._handoff_package(task_id, reason, report, user_requested)
        self.db.update_task_state(task_id, handoff_log=[{"at": time.time(), "from": LOCAL, "to": CLAUDE,
                                                          "reason": reason, "chars": len(extra)}])
        self._act(task_id, "task.handoff", f"Handed the task from Qwen to Claude with a {len(extra):,}-character "
                  "summary of what happened so far (not the whole conversation).", CLAUDE, "claude", reason=reason)
        self._request_claude(task_id, task["kind"] if task["kind"] in ("ask", "edit") else "ask", pack, extra=extra,
                             reason=f"Escalated: {reason}")

    # Claude requests ---------------------------------------------------------------------

    def _conversation_for_claude(self, task_id: int, objective: str, session: str | None,
                                 limit: int = 8, chars: int = 1500) -> str:
        """The chat Claude has not seen yet, without the latest user message (sent separately).

        A resumed Claude session already holds Claude's own turns, so only what came after its last reply is
        added; a new session gets the recent chat. Privacy redaction runs on the whole prompt afterwards.
        """
        msgs = [m for m in self.db.messages(task_id) if m["role"] in ("user", "assistant")]
        if msgs and msgs[-1]["role"] == "user":
            msgs = msgs[:-1]
        if session:
            last = max((i for i, m in enumerate(msgs) if m.get("backend") == CLAUDE), default=-1)
            msgs = msgs[last + 1:]
        if msgs and msgs[0]["role"] == "user" and msgs[0]["content"].strip() == objective.strip():
            msgs = msgs[1:]  # Already sent as the first message.
        msgs = msgs[-limit:]
        if not msgs:
            return ""
        who = {LOCAL: "Qwen", CLAUDE: "Claude"}
        lines = []
        for m in msgs:
            name = "User" if m["role"] == "user" else who.get(m.get("backend") or "", "Enoy")
            text = m["content"].strip()
            if len(text) > chars:
                text = text[:chars].rstrip() + " […]"
            lines.append(f"[{name}]\n{text}")
        return ctx.CONVERSATION_NOTE + "\n\n" + "\n\n".join(lines)

    def _request_claude(self, task_id: int, purpose: str, pack: ctx.ContextPack, extra: str = "",
                        changeset_id: int | None = None, reason: str = "",
                        depends_on: list[int] | None = None) -> None:
        """Prepare a Claude request; send now or wait for the user's OK."""
        task = self.db.get_task(task_id)
        ws = self.workspace(task["project_id"])
        state = task["state"]
        block = self._cloud_block(task_id)
        if block:  # Every caller checks first; this is the last line of defence.
            raise TaskError(f"Not sent to Claude: {block}.")
        globs = self.cfg["privacy"]["cloud_excluded_globs"]
        cloud_knowledge, private_knowledge = self._cloud_knowledge(pack)
        pack = dataclasses.replace(pack, knowledge=cloud_knowledge)
        private_files = [f for f in pack.files if privacy.cloud_excluded(f.path, globs)]
        private = privacy.private_lines([f.content for f in private_files]) | private_knowledge
        check = privacy.CloudCheck(excluded_files=[f.path for f in private_files])
        session = state.get("claude_session")
        already = state.get("claude_sent", {}) if session else {}
        allowed = [f for f in pack.files if f not in private_files]
        new_files = {f.path for f in allowed if already.get(f.path) != f.sha}
        skipped_same = [f.path for f in allowed if f.path not in new_files]

        system = ctx.CLAUDE_LEAD_SYSTEM if purpose in ("lead", "lead_review") else ctx.CLAUDE_SYSTEM
        header = ctx.project_header(ws, pack)
        parts = []
        if not session:
            parts.append(header)
        # A follow-up: Claude also needs the replies it did not write itself (Qwen's, the team's), or it
        # answers the first message again as if nobody had.
        is_followup = extra.startswith("Follow-up from the user:")
        label = "First message of this chat" if is_followup else "Task"
        talk = self._conversation_for_claude(task_id, task["objective"], session) if is_followup else ""
        asked = "\n\n".join(x for x in (talk, extra) if x)
        if purpose in ("lead", "edit") and not ctx.is_general(ws) and not ws.index:
            parts.append(ctx.CLAUDE_EMPTY_FOLDER.format(path=ws.root))
        if purpose == "lead":
            if not ctx.is_general(ws):
                parts.append(ctx.memory_block(self.db.memory(task["project_id"], None, 30)))
            parts.append(ctx.task_message(task["objective"], pack, asked, only=new_files, label=label))
            first = self._claude_first()
            parts.append(ctx.LEAD_INSTRUCTIONS.format(
                max_tasks=self.cfg["team"]["max_tasks"],
                delegate_note=ctx.LEAD_NO_DELEGATE if ctx.is_general(ws) else "",
                handoff_mode=ctx.LEAD_HANDOFF_MODE if first else "",
                style_note=ctx.CLAUDE_FIRST_NOTE.format(share=self.cfg["routing"]["target_local_share"]) if first else ""))
        elif purpose == "lead_review":
            parts.append(f"The user's request:\n{task['objective'].strip()}")
            parts.append(extra)
            parts.append(ctx.LEAD_REVIEW_INSTRUCTIONS)
        elif purpose == "review":
            cs = self.db.get_changeset(changeset_id)
            diff = "\n".join(f["diff"] for f in cs["files"] if not privacy.cloud_excluded(f["path"], globs))
            parts.append(ctx.task_message(task["objective"], pack, only=new_files))
            parts.append(ctx.REVIEW_INSTRUCTIONS + "\n\nProposed change (unified diff):\n" + diff)
        elif purpose == "plan":
            parts.append(ctx.task_message(task["objective"], pack, only=new_files))
            parts.append(ctx.PLAN_INSTRUCTIONS)
        else:
            parts.append(ctx.task_message(task["objective"], pack, asked, only=new_files, label=label))
            if purpose == "edit":
                parts.append(patches.FORMAT_INSTRUCTIONS)
        if skipped_same:
            parts.append("Files you already have from earlier in this conversation (unchanged): " + ", ".join(skipped_same))
        if purpose == "review" and extra:
            parts.append(extra)
        if purpose == "lead" and extra.startswith("Follow-up from the user:"):
            followup = extra.split("\n", 1)[1] if "\n" in extra else ""
        else:
            followup = ""
        prompt = privacy.finalize("\n\n".join(p for p in parts if p), private, check)
        files_meta = [{"path": f.path, "sha": f.sha, "chars": len(f.content)} for f in allowed if f.path in new_files]
        check.sent_files = [f["path"] for f in files_meta]
        self._act(task_id, "privacy.checked", check.summary(), "system", "review" if purpose == "review" else "claude",
                  outcome="passed")
        pending = {
            "purpose": purpose, "system": system, "prompt": prompt, "files": files_meta,
            "chars": len(prompt) + len(system), "changeset_id": changeset_id, "session": session,
            "skipped_unchanged": skipped_same, "privacy": check.summary(),
            # A resumed Claude session already has earlier pictures; send only new ones.
            "images": self._image_ids(task_id, latest_only=bool(session)),
            "knowledge": sorted({k.name for k in cloud_knowledge}),
            "followup": followup,
        }
        if pending["images"]:
            pending["privacy"] += f" Includes {len(pending['images'])} image(s); images are not scanned for secrets."
        reason = reason or state.get("assign_reason", "")
        confirm = self.cfg["claude"]["confirm_before_send"]
        names = [f["path"] for f in files_meta]
        pending["reason"] = reason
        pending["step_id"] = self._step(
            task_id, CLAUDE_STEP_TITLE.get(purpose, "Answer"), CLAUDE, "review" if purpose == "review" else "claude",
            reason, status="waiting" if confirm else "queued", files=names, needs_approval=confirm,
            depends_on=depends_on,
            text=(f"Waiting for your OK to send {pending['chars']:,} characters, {len(names)} file(s)"
                  f"{' and ' + str(len(pending['images'])) + ' image(s)' if pending['images'] else ''} to Claude."
                  if confirm else f"Prepared a {purpose} request for Claude."),
        )
        self.db.update_task_state(task_id, pending_claude=pending)
        if confirm:
            self._set(task_id, status="awaiting_claude")
            self._event(task_id, "claude",
                        f"Ready to send a {purpose} request to Claude: {len(files_meta)} file(s), "
                        f"{pending['chars']:,} characters. Waiting for your OK.", CLAUDE)
            return
        self._send_claude(task_id)

    def _send_claude(self, task_id: int) -> None:
        task = self.db.get_task(task_id)
        pending = task["state"].get("pending_claude")
        if not pending:
            raise TaskError("No Claude request is prepared.")
        self._set(task_id, status="running")
        self._set_owner(task_id, CLAUDE, pending.get("reason") or task["state"].get("assign_reason") or "Claude request.")
        step_id = pending.get("step_id")
        step = self.db.get_step(step_id) if step_id else None
        if step and step["needs_approval"] and step["status"] == "waiting":
            self._act(task_id, "approval.resolved", "You approved sending this request to Claude.", "user", step["stage"],
                      outcome="approved", step_id=step_id)
        if step:
            self.db.update_step(step_id, status="working")
        image_ids = pending.get("images") or []
        pics = f" and {len(image_ids)} image(s)" if image_ids else ""
        web = self._claude_web(task_id, pending["purpose"])
        enoy = self._claude_enoy_tools(task_id, pending["purpose"])
        if enoy and not self.cfg["tools"].get("claude_enoy_mcp"):
            # Claude Code cannot call Enoy's tools in safe mode: Enoy reads the PC facts first instead.
            facts = self._prefetch_for_claude(task_id, enoy)
            if facts and facts not in pending["prompt"]:
                pending["prompt"] += "\n\n" + facts
                pending["chars"] = len(pending["system"]) + len(pending["prompt"])
            enoy = []
        if web and tools_mod.CLAUDE_WEB_NOTE not in pending["system"]:
            pending["system"] += "\n\n" + tools_mod.CLAUDE_WEB_NOTE
        if enoy and tools_mod.CLAUDE_ENOY_NOTE not in pending["system"]:
            pending["system"] += "\n\n" + tools_mod.CLAUDE_ENOY_NOTE
        trans_id = self.db.add_transmission(task_id, CLAUDE, pending["purpose"], pending["files"],
                                            f"[system]\n{pending['system']}\n\n[user]\n{pending['prompt']}"
                                            + (f"\n\n[{len(image_ids)} image(s) attached]" if image_ids else ""))
        self._event(task_id, "send", f"Sending {pending['chars']:,} characters, {len(pending['files'])} file(s){pics} to Claude.", CLAUDE)
        names = ", ".join(f["path"] for f in pending["files"])
        self._act(task_id, "backend.request", f"Sent {pending['chars']:,} characters and {len(pending['files'])} file(s) "
                  f"to Claude{': ' + names if names else ''}.", CLAUDE, step["stage"] if step else "claude", step_id=step_id)
        self._event(task_id, "status", "Claude is working (no live text; the answer appears when it is finished)."
                    + (" It may search the web through Claude Code." if web else "")
                    + (" It may use Enoy's tools: " + ", ".join(enoy) + "." if enoy else ""), CLAUDE)
        if web or enoy:
            offered = (["Claude Code's WebSearch and WebFetch"] if web else []) + (["Enoy's " + ", ".join(enoy)] if enoy else [])
            self._act(task_id, "tool.offered", "Claude may use " + " and ".join(offered) + " for this answer.",
                      CLAUDE, step["stage"] if step else "claude",
                      reason="The message asks for current or outside information.", step_id=step_id)
        req = ChatRequest(
            messages=[ChatMessage("system", pending["system"]),
                      ChatMessage("user", pending["prompt"], self._load_images(task_id, image_ids))],
            purpose=pending["purpose"], cancel=self._cancel.setdefault(task_id, threading.Event()),
            resume_session=pending.get("session"), web_tools=web, enoy_tools=enoy,
            enoy_env=self._mcp_env() if enoy else {}, effort=self._lead_effort(pending["purpose"]),
        )
        self._set(task_id, claude_calls=task["claude_calls"] + 1)
        self._busy[CLAUDE] = {"task_id": task_id, "step_id": step_id, "purpose": pending["purpose"], "since": time.time()}
        try:
            result = self.backends[CLAUDE].chat(req)
        except BackendError as exc:
            if exc.kind == "cancelled":
                raise _Cancelled() from exc
            self._step_end(step_id, "failed", error=str(exc))
            if pending.get("session") and exc.kind == "failed":
                # The saved Claude session may be gone; next request starts fresh.
                self.db.update_task_state(task_id, claude_session=None, claude_sent={})
            if exc.kind in ("limit", "unavailable"):
                self._mark_claude_unavailable(str(exc))
            self.db.update_task_state(task_id, pending_claude=None, failures=[f"Claude: {exc}"],
                                      limitations=[str(exc)] if exc.kind in ("limit", "unavailable") else [])
            self._event(task_id, "error", f"Claude request failed: {exc}", CLAUDE)
            self._after_claude_skipped(task_id, pending, f"Claude failed: {exc}")
            return
        finally:
            self._busy.pop(CLAUDE, None)
        sent = dict(task["state"].get("claude_sent", {}) if pending.get("session") else {})
        sent.update({f["path"]: f["sha"] for f in pending["files"]})
        self.db.update_task_state(task_id, pending_claude=None, claude_session=result.session_id, claude_sent=sent)
        self._count_work(task_id, CLAUDE, result)
        if result.usage:
            note = ", ".join(f"{k}={v}" for k, v in result.usage.items())
            self.db.set_transmission_usage(trans_id, note)
            self._event(task_id, "usage", f"Reported by Claude Code: {note}", CLAUDE)
        self.db.kv_set("claude_last_verified", time.time())
        if web or enoy:
            self._record_claude_tools(task_id, result.tool_events, step_id)
        if pending["purpose"] not in ("lead", "lead_review"):  # The lead's reply is split into message and plan first.
            self.db.add_message(task_id, "assistant", result.text, CLAUDE)
            self.bus.publish({"type": "message_done", "task_id": task_id, "backend": CLAUDE})
        self._handle_claude_result(task_id, pending, result.text)

    def _handle_claude_result(self, task_id: int, pending: dict, text: str) -> None:
        task = self.db.get_task(task_id)
        ws = self.workspace(task["project_id"])
        purpose = pending["purpose"]
        step_id = pending.get("step_id")
        if purpose == "lead":
            self._handle_lead(task_id, pending, text)
            return
        if purpose == "lead_review":
            self._handle_lead_review(task_id, pending, text)
            return
        if purpose == "plan":
            self._step_end(step_id, "completed", "Wrote the implementation plan.")
            self.db.update_task_state(task_id, plan=text, decisions=["Plan written by Claude."])
            self._event(task_id, "plan", "Claude wrote the plan. The local model will implement it.", CLAUDE)
            self._set_owner(task_id, LOCAL, "Implementing Claude's plan.")
            pack = self._gather(task_id, task["objective"] + "\n" + text)
            self._local_loop(task_id, pack, plan=text, reason="Implementing Claude's plan (workflow: Claude plans, Qwen writes).")
            return
        if purpose == "review":
            cs_id = pending["changeset_id"]
            self.db.set_changeset_review(cs_id, text)
            rounds = task["state"].get("review_rounds", 0) + 1
            self.db.update_task_state(task_id, review_rounds=rounds)
            verdict = "CHANGES_NEEDED" if re.search(r"VERDICT:\s*CHANGES_NEEDED", text, re.I) else (
                "APPROVE" if re.search(r"VERDICT:\s*APPROVE", text, re.I) else "UNCLEAR")
            self._event(task_id, "review", f"Claude's review verdict: {verdict}.", CLAUDE)
            self._step_end(step_id, "completed", f"Review verdict: {verdict}.")
            if verdict == "CHANGES_NEEDED" and rounds <= self.cfg["routing"]["max_review_rounds"]:
                self.db.set_changeset_status(cs_id, "superseded", "Replaced after Claude's review.")
                self._set_owner(task_id, LOCAL, "Applying Claude's review feedback.")
                pack = self._gather(task_id, task["objective"])
                # review_rounds is checked in _after_local_proposal, so this cannot loop forever.
                self._local_loop(task_id, pack, feedback=f"A reviewer found problems with your previous change:\n{text}\n"
                                                         "Produce a corrected change addressing every point.",
                                 reason="Claude's review asked for changes.")
                return
            self._await_approval(task_id)
            return
        if purpose == "edit":
            parsed = patches.parse(text)
            if not parsed.changes and not parsed.errors:
                # No change blocks at all: Claude explained instead (the code is not here, the request is a
                # question...). Its reply is the answer, not a broken proposal to retry.
                if parsed.commands:
                    self.db.update_task_state(task_id, suggested_commands=parsed.commands)
                self._event(task_id, "note", "Claude replied without changing any files, so nothing in the project "
                                             "changed.", CLAUDE)
                self._step_end(step_id, "completed", "Answered without changing any files.")
                self._complete(task_id, _first_line(text))
                return
            validation = patches.validate(ws, parsed.changes, task["state"].get("file_versions"))
            problems = parsed.errors + validation.errors
            if parsed.commands:
                self.db.update_task_state(task_id, suggested_commands=parsed.commands)
            if not validation.files and not problems:
                problems = ["No usable file changes were produced."]
            if self._check_proposal(task_id, step_id, CLAUDE, None, validation, problems, parsed.explanation):
                self._await_approval(task_id)
                return
            self.db.update_task_state(task_id, failures=["Invalid Claude proposal: " + "; ".join(problems[:4])])
            self._event(task_id, "validation", "Claude's proposal could not be applied:\n- " + "\n- ".join(problems[:8]), CLAUDE)
            if self.db.list_changesets(task_id, "pending"):
                self._await_approval(task_id)
                return
            self._fail(task_id, "Claude's proposal failed validation. Use 'Ask Claude' to retry or edit the request.")
            return
        self._step_end(step_id, "completed", "Answered.")
        self._complete(task_id, _first_line(text))

    def _after_claude_skipped(self, task_id: int, pending: dict, why: str, declined: bool = False) -> None:
        """Claude was declined or failed: keep whatever local work exists."""
        purpose = pending["purpose"]
        if purpose == "lead_review":  # The team's work is done; Enoy's own summary is the answer.
            self._set(task_id, status="running")
            team_mod.TeamRunner(self, task_id).end_lead_run(False, why)
            return
        if purpose == "lead" and not declined and self.cfg["routing"].get("claude_fallback", "local") == "stop":
            self._fail(task_id, f"{why} Settings say to stop instead of continuing with Qwen.")
            return
        self._set_owner(task_id, LOCAL, f"{why} Claude did not handle this request.")
        task = self.db.get_task(task_id)
        if self.db.list_changesets(task_id, "pending"):
            self._event(task_id, "status", f"{why} The local proposal is still available for review.")
            self._await_approval(task_id)
            return
        if purpose == "plan":
            self._set_owner(task_id, LOCAL, f"{why} Implementing without a plan.")
            self._set(task_id, status="running")
            self._local_loop(task_id, self._gather(task_id, task["objective"]))
            return
        if self.db.list_changesets(task_id, "applied"):
            self._complete(task_id, f"{why} The change already applied stays in place.")
            return
        has_local_answer = any(m["role"] == "assistant" and m["backend"] == LOCAL for m in self.db.messages(task_id))
        if has_local_answer and task["kind"] != "edit":
            self._complete(task_id, f"{why} The local answer stands.")
            return
        fallback = self.cfg["routing"].get("claude_fallback", "local") == "local"
        if task["local_attempts"] == 0 and (declined or fallback):
            # The task went to Claude first and nothing local exists yet. Qwen takes it
            # over, once: the workflow becomes local-only so it cannot bounce back.
            reason = ("You chose to keep this task with Qwen instead of sending it to Claude." if declined else
                      f"{why} Continuing with Qwen (Settings: if Claude is unavailable, continue with Qwen).")
            self._act(task_id, "task.fallback", "Continuing with Qwen.", LOCAL, reason=reason)
            self._set(task_id, status="running", workflow="local_only")
            self.db.update_task_state(task_id, assign_reason=reason)  # Owner is already Qwen; no second event.
            self._local_loop(task_id, self._gather(task_id, task["objective"]), reason=reason)
            return
        self._fail(task_id, f"{why} You can run the task with Qwen instead.")

    # Results ---------------------------------------------------------------------------------

    def _check_proposal(self, task_id: int, step_id: int | None, backend: str, result: Any,
                        validation: patches.ValidationResult, problems: list[str], explanation: str) -> bool:
        """Record the model's step outcome and Enoy's own check of the proposal."""
        note = self._result_note(result) if result is not None else ""
        if not problems and validation.files:
            self._step_end(step_id, "completed", f"Proposed changes to {len(validation.files)} file(s){note}.")
            check = self._step(task_id, "Check the proposed change", "system", "patch",
                               "Every proposal is checked before you see it (paths, exact matches, syntax).")
            cs_id = self._propose(task_id, backend, validation, explanation)
            names = ", ".join(f"{f.action} {f.path}" for f in validation.files)
            self._step_end(check, "completed", f"Change set #{cs_id} passed the checks: {names}", kind="patch.proposed")
            return True
        self._step_end(step_id, "failed", error=f"Its proposal could not be used{note}.")
        check = self._step(task_id, "Check the proposed change", "system", "patch",
                           "Every proposal is checked before you see it (paths, exact matches, syntax).")
        self._step_end(check, "failed", error="; ".join(problems[:4]), kind="validation.failed")
        return False

    def _propose(self, task_id: int, backend: str, validation: patches.ValidationResult, explanation: str) -> int:
        cs_id = self.db.create_changeset(task_id, backend, [f.as_dict() for f in validation.files],
                                         validation.summary(), explanation[:2000])
        names = ", ".join(f"{f.action} {f.path}" for f in validation.files)
        self._event(task_id, "proposal", f"{BACKEND_LABEL[backend]} proposed change set #{cs_id}: {names}", backend)
        for w in validation.warnings:
            self._event(task_id, "warning", w, backend)
        self.bus.publish({"type": "changeset", "task_id": task_id, "changeset_id": cs_id})
        return cs_id

    def _await_approval(self, task_id: int, ask: bool = False) -> None:
        """ask: always wait for you, even with auto-apply on (the team holds this change for your review)."""
        pending = self.db.list_changesets(task_id, "pending")
        if not ask and self.cfg["permissions"]["auto_apply_patches"] and len(pending) == 1 and all(
                f["action"] != "delete" for f in pending[0]["files"]):
            self._event(task_id, "status", "Auto-apply is on in Settings; applying the change.")
            self._act(task_id, "approval.resolved", "Applied without asking because auto-apply is on in Settings.",
                      "system", "approval", reason="Settings: apply changes automatically.", outcome="auto-approved")
            self.approve_changeset(pending[0]["id"])
            return
        if not any(st["stage"] == "approval" for st in self.db.open_steps(task_id)):
            self._step(task_id, "Review the proposed change", "user", "approval",
                       "Enoy only writes files after you approve them.", status="waiting", needs_approval=True,
                       files=sorted({f["path"] for cs in pending for f in cs["files"]}),
                       text=f"Waiting for you to apply or discard {len(pending)} proposed change set(s).")
        self._set(task_id, status="awaiting_approval",
                  summary=f"{len(pending)} change set(s) waiting for your review.")
        self._event(task_id, "status", "Waiting for you to review the proposed change on the Changes screen.")

    def _complete(self, task_id: int, summary: str) -> None:
        self._set(task_id, status="completed", summary=summary[:300])
        self._event(task_id, "status", "Done.")

    def _fail(self, task_id: int, message: str) -> None:
        self._set(task_id, status="failed", summary=message[:300])
        self._event(task_id, "error", message)

    @staticmethod
    def _result_note(result) -> str:
        u = getattr(result, "usage", None) or {}
        bits = []
        if "eval_count" in u:
            bits.append(f"{u['eval_count']} output tokens")
        if getattr(result, "duration_s", None):
            bits.append(f"{result.duration_s:.0f} s")
        return f" ({', '.join(bits)})" if bits else ""

    @staticmethod
    def _usage_note(result) -> str:
        u = result.usage
        if not u:
            return ""
        bits = []
        if "prompt_eval_count" in u:
            bits.append(f"{u['prompt_eval_count']} prompt tokens")
        if "eval_count" in u:
            bits.append(f"{u['eval_count']} output tokens")
        if "eval_count" in u and result.duration_s:
            bits.append(f"{result.duration_s:.1f} s")
        return "Reported by Ollama: " + ", ".join(bits) if bits else ""

    # Change sets ---------------------------------------------------------------------------

    def _team_active(self, task: dict) -> bool:
        return (task["state"].get("team_run") or {}).get("status") not in (None, "completed", "failed", "cancelled")

    def approve_changeset(self, cs_id: int) -> dict:
        cs = self.db.get_changeset(cs_id)
        task = self.db.get_task(cs["task_id"]) if cs else None
        if task and self._team_active(task):
            result = self._apply_changeset(cs_id, finish=False)
            team_mod.TeamRunner(self, task["id"]).task_applied(cs_id)
            self._resume_team(task["id"])
            return result
        return self._apply_changeset(cs_id)

    def _resume_team(self, task_id: int) -> None:
        """Continue a team run after you answered it, unless other changes still wait for you."""
        if self.db.list_changesets(task_id, "pending"):
            return
        self._cancel[task_id] = threading.Event()
        self._spawn(self._guard, task_id, lambda tid: team_mod.TeamRunner(self, tid).continue_run())

    def _apply_changeset(self, cs_id: int, finish: bool = True) -> dict:
        """Write a change set. finish=False keeps the chat open (team runs decide when they are done)."""
        cs = self.db.get_changeset(cs_id)
        if not cs or cs["status"] != "pending":
            raise TaskError("This change set is not waiting for approval.")
        task = self.db.get_task(cs["task_id"])
        ws = self.workspace(task["project_id"])
        backup_dir = self.backups_dir or Path(ws.root) / ".fusion" / "backups"
        names = ", ".join(f["path"] for f in cs["files"])
        self._act(task["id"], "tool.started", f"Writing change set #{cs_id} ({names}), with backups.", "system", "approval")
        try:
            backups = patches.apply_files(ws, cs["files"], backup_dir / f"task-{task['id']}")
        except patches.ConflictError as exc:
            self._event(task["id"], "error", str(exc))
            self._act(task["id"], "tool.completed", f"Could not write change set #{cs_id}: {exc}", "system", "approval",
                      outcome="failed")
            raise TaskError(str(exc)) from exc
        self._end_stage(task["id"], "approval", "completed", f"You applied change set #{cs_id}.", outcome="approved")
        self.db.update_task_state(task["id"], verification={"status": "not_run", "changeset": cs_id})
        self._act(task["id"], "tool.completed", f"Wrote {names}; {len(backups)} backup(s) saved.", "system", "approval",
                  outcome="completed")
        self.db.set_changeset_status(cs_id, "applied")
        if finish:  # Alternatives (compare workflow) are superseded; a team's other tasks are not alternatives.
            for other in self.db.list_changesets(task["id"], "pending"):
                self.db.set_changeset_status(other["id"], "superseded", f"Change set #{cs_id} was applied instead.")
        files = ", ".join(f["path"] for f in cs["files"])
        self.db.update_task_state(task["id"], decisions=[f"Applied change set #{cs_id} ({cs['backend']}): {files}"])
        self._event(task["id"], "applied", f"Applied change set #{cs_id} to: {files}. Backups: {len(backups)} file(s).")
        if finish:
            self._set(task["id"], status="completed", summary=f"Applied changes to {files}.")
        self.bus.publish({"type": "changeset", "task_id": task["id"], "changeset_id": cs_id})
        if finish:
            self._check_after_apply(task, ws)
        return self.db.get_changeset(cs_id)

    def _check_after_apply(self, task: dict, ws: Workspace) -> None:
        """Run the project's tests right after you apply a change, when that command needs no approval."""
        if not self.cfg["permissions"].get("test_after_apply", True):
            return
        candidates = list(task["state"].get("suggested_commands") or []) + ws.suggest_test_commands()
        for command in candidates:
            policy = cmdpolicy.classify(command, self.cfg["permissions"]["command_allowlist"])
            if policy.level == "allowlisted":
                try:
                    self.run_command(task["project_id"], command, task_id=task["id"])
                    self._act(task["id"], "test.auto", f"Running `{command}` to check the change you applied.",
                              "system", "testing", reason="Checks run after each applied change (Settings → Safety).")
                except TaskError as exc:
                    self._act(task["id"], "test.auto", f"Could not start `{command}`: {exc}", "system", "testing",
                              outcome="failed")
                return
        if candidates:
            self._event(task["id"], "note", f"`{candidates[0]}` needs your approval, so it was not run automatically. "
                                            "Use Run tests when you are ready.")

    def reject_changeset(self, cs_id: int, reason: str = "") -> dict:
        cs = self.db.get_changeset(cs_id)
        if not cs or cs["status"] != "pending":
            raise TaskError("This change set is not waiting for approval.")
        self.db.set_changeset_status(cs_id, "rejected", reason or "Rejected by you.")
        task_id = cs["task_id"]
        self.db.update_task_state(task_id, failures=[f"Change set #{cs_id} rejected: {reason or 'no reason given'}"])
        self._event(task_id, "rejected", f"You rejected change set #{cs_id}." + (f" Reason: {reason}" if reason else ""))
        task = self.db.get_task(task_id)
        if self._team_active(task):
            team_mod.TeamRunner(self, task_id).task_rejected(cs_id)
            if not self.db.list_changesets(task_id, "pending"):
                self._end_stage(task_id, "approval", "completed", f"You discarded change set #{cs_id}.", outcome="rejected")
            self._resume_team(task_id)
        elif not self.db.list_changesets(task_id, "pending"):
            self._end_stage(task_id, "approval", "completed", f"You discarded change set #{cs_id}.", outcome="rejected")
            self._set(task_id, status="completed", summary="Proposed change was rejected.")
        self.bus.publish({"type": "changeset", "task_id": task_id, "changeset_id": cs_id})
        return self.db.get_changeset(cs_id)

    def revert_changeset(self, cs_id: int) -> dict:
        cs = self.db.get_changeset(cs_id)
        if not cs or cs["status"] != "applied":
            raise TaskError("Only applied change sets can be reverted.")
        task = self.db.get_task(cs["task_id"])
        ws = self.workspace(task["project_id"])
        try:
            patches.revert_files(ws, cs["files"])
        except patches.ConflictError as exc:
            raise TaskError(str(exc)) from exc
        self.db.set_changeset_status(cs_id, "reverted")
        self._event(task["id"], "reverted", f"Reverted change set #{cs_id}.")
        self._act(task["id"], "patch.reverted", f"You undid change set #{cs_id}; the files are back as they were.",
                  "user", "approval", outcome="reverted")
        self.bus.publish({"type": "changeset", "task_id": task["id"], "changeset_id": cs_id})
        return self.db.get_changeset(cs_id)

    # Commands ---------------------------------------------------------------------------------

    def check_command(self, command: str) -> dict:
        return cmdpolicy.classify(command, self.cfg["permissions"]["command_allowlist"]).as_dict()

    def run_command(self, project_id: int, command: str, approved: bool = False, task_id: int | None = None,
                    wait: bool = False) -> dict:
        policy = cmdpolicy.classify(command, self.cfg["permissions"]["command_allowlist"])
        if policy.level == "blocked":
            raise TaskError("Blocked: " + " ".join(policy.reasons))
        if policy.level in ("approval", "high_risk") and not approved:
            return {"needs_approval": True, "policy": policy.as_dict()}
        ws = self.workspace(project_id)
        run_id = self.db.add_command_run(project_id, command, task_id)
        cancel = threading.Event()
        self._run_cancel[run_id] = cancel
        why = "You started this check" + (" and approved it." if approved else ".")
        step = None
        if task_id:
            self._event(task_id, "command", f"Running `{command}` ({policy.level}).")
            step = self._step(task_id, f"Run `{command}`", "system", "testing", why, kind="test.started")
        else:
            self._act(None, "tool.started", f"Running `{command}`.", "system", "testing", why)
        self.bus.publish({"type": "command", "run": self.db.get_command_run(run_id)})

        def work():
            result = cmdpolicy.run(policy.argv, ws.root, self.cfg["permissions"]["command_timeout_s"], cancel)
            self.db.finish_command_run(run_id, result.status, result.exit_code, result.stdout, result.stderr, result.duration_s)
            run = self.db.get_command_run(run_id)
            self.bus.publish({"type": "command", "run": run})
            note = f"{result.status} (exit code {result.exit_code}, {result.duration_s:.1f} s)"
            output = (result.stdout + "\n" + result.stderr).splitlines()
            last_line = next((ln.strip() for ln in reversed(output) if ln.strip()), "")
            if task_id:
                self.db.update_task_state(task_id, test_results=[{"command": command, "status": result.status, "exit_code": result.exit_code}])
                self._event(task_id, "command", f"`{command}` finished: {result.status} (exit code {result.exit_code}, {result.duration_s:.1f} s).")
                self.db.update_task_state(task_id, verification={"status": result.status, "command": command,
                                                                 "exit_code": result.exit_code, "at": time.time(),
                                                                 "detail": last_line[:200]})
                t = self.db.get_task(task_id)
                if result.status != "passed" and t["status"] == "completed":
                    # Never leave "done" standing on its own when the check says otherwise.
                    self._set(task_id, summary=f"Finished, but the check `{command}` failed: {last_line[:120]}")
                if result.status == "passed":
                    self._step_end(step, "completed", f"Passed {note}.", kind="test.completed", outcome="passed")
                else:
                    self._step_end(step, "cancelled" if result.status == "cancelled" else "failed",
                                   error=note + (f": {last_line[:200]}" if last_line else ""),
                                   kind="test.completed", outcome=result.status)
            else:
                self._act(None, "tool.completed", f"`{command}`: {note}.", "system", "testing", outcome=result.status)
            self._run_cancel.pop(run_id, None)

        if wait:
            work()
        else:
            self._spawn(work)
        return {"needs_approval": False, "run_id": run_id, "policy": policy.as_dict()}

    def cancel_command(self, run_id: int) -> None:
        ev = self._run_cancel.get(run_id)
        if ev:
            ev.set()

    def fix_failure(self, task_id: int, run_id: int) -> dict:
        """Hand a failed check back to the AI, escalating after the configured limit."""
        run = self.db.get_command_run(run_id)
        if not run or run["status"] == "passed":
            raise TaskError("That check did not fail.")
        missing = missing_tool((run["stdout"] or "") + "\n" + (run["stderr"] or ""))
        if missing:  # Nothing in the code to fix: asking a model would only produce a pointless change.
            raise TaskError(f"The check could not run because {missing} is not installed. Install it, then run the "
                            "tests again.")
        task = self._require(task_id)
        attempts = task["test_fix_attempts"] + 1
        self.db.update_task(task_id, test_fix_attempts=attempts, kind="edit")
        lines = (run["stdout"] + "\n" + run["stderr"]).strip().splitlines()
        tail = "\n".join(lines[-60:])
        report = f"After applying the change, `{run['command']}` failed (exit code {run['exit_code']}):\n{tail}"
        signature = _failure_signature(lines)
        repeated =bool(signature) and signature == task["state"].get("last_failure_sig")
        self.db.update_task_state(task_id, failures=[f"Check failed: {run['command']} (exit {run['exit_code']})"],
                                  invalid_outputs=0, backend_errors=0, last_failure_sig=signature,
                                  repeated_error=repeated)
        if repeated:
            self._act(task_id, "task.progress", "The same error came back after the previous fix.", "system", "testing",
                      reason=signature[:200])
        self._cancel[task_id] = threading.Event()
        self._act(task_id, "task.retry", f"You asked for a fix for the failed check `{run['command']}`.", "user", "testing",
                  reason=f"The check failed with exit code {run['exit_code']} (fix attempt {attempts}).")

        def work(tid: int) -> None:
            pack = self._gather(tid, task["objective"] + "\n" + tail)
            signals = self._signals(tid)
            available, _ = self.claude_available()
            block = self._cloud_block(tid)
            ok, reason = should_escalate(signals, self.cfg["routing"], task["workflow"] or "escalate", available)
            if block and ok:
                ok, reason = False, f"Claude might help, but {block}"
                self._act(tid, "task.not_escalated", "Not escalating to Claude.", "system", "testing", reason=reason)
            if ok:
                self._escalate(tid, reason, report=report, pack=pack)
                return
            self._set_owner(tid, LOCAL, f"Fixing a failed check (attempt {attempts}).")
            self._local_loop(tid, pack, feedback=report + "\nFix the cause. Change only what is needed.",
                             reason=f"Fixing a failed check (attempt {attempts}); {reason}")

        self._spawn(self._guard, task_id, work)
        return self.db.get_task(task_id)

    # Project summary ---------------------------------------------------------------------------

    def summarize_project(self, project_id: int) -> dict:
        ws = self.workspace(project_id)
        listing = "\n".join(sorted(ws.index)[:400])
        task = self.db.create_task(project_id, "Summarize project", "Summarize this project's structure and purpose.", "summary",
                                   state={})
        self._cancel[task["id"]] = threading.Event()

        def work(tid: int) -> None:
            key = "summary:" + sha256_text(listing)
            cached = self.db.cache_get(key)
            self._set_owner(tid, LOCAL, "Project summaries always run locally.")
            if cached:
                step = self._step(tid, "Reuse the saved summary", "system", "analysis",
                                  "The file list has not changed since the last summary.")
                self.db.set_project_summary(project_id, cached, LOCAL)
                self.db.add_message(tid, "assistant", cached, LOCAL)
                self._step_end(step, "completed", "Used the saved summary; no model request.")
                self._complete(tid, "Used the cached summary; the file list has not changed.")
                return
            step = self._step(tid, "Summarize the project", LOCAL, "local", "Project summaries always run locally.")
            pack = ctx.gather(ws, "readme main index init app server game config", self.cfg["context"])
            readme = [f for f in pack.files][:5]
            prompt = ("Write a concise summary of this project for another developer: purpose, main parts, "
                      "languages/frameworks, entry points, how to run tests. Max 250 words.\n\nFile list:\n"
                      + listing + "\n\n" + "\n\n".join(f"=== {f.path} ===\n{f.content[:3000]}" for f in readme))
            result = self._chat_local(tid, [ChatMessage("system", ctx.BASE_SYSTEM.format(project=ws.name)),
                                            ChatMessage("user", prompt)], "summary", step)
            self._step_end(step, "completed", "Wrote the summary" + self._result_note(result) + ".")
            self.db.cache_put(key, result.text)
            self.db.set_project_summary(project_id, result.text, LOCAL)
            self.db.add_message(tid, "assistant", result.text, LOCAL)
            self._complete(tid, _first_line(result.text))

        self._spawn(self._guard, task["id"], work)
        return self.db.get_task(task["id"])

    # Routing support -------------------------------------------------------------------------------

    def set_team(self, task_id: int, on: bool) -> dict:
        """TEAM ROLES for one chat. Switching off pauses a running team safely."""
        task = self._require(task_id)
        on = bool(on)
        self.db.update_task_state(task_id, team=on)
        run = task["state"].get("team_run") or {}
        self._act(task_id, "team.toggled", f"TEAM ROLES {'on' if on else 'off'} for this chat.", "user",
                  reason="Applies to your next messages." if not run else "")
        if not on and self._team_active(task) and task["status"] != "running":
            # Waiting for you (review or Claude question): pause right away. A running
            # step finishes first and the run pauses at its next checkpoint.
            team_mod.TeamRunner(self, task_id)._pause("TEAM ROLES was switched off for this chat.")
        if on and run.get("status") == "paused":
            for tt in self.db.team_tasks(task_id):
                if tt["status"] == "paused":
                    self.db.update_team_task(tt["id"], status="planned")
            team_mod.TeamRunner(self, task_id)._save_run(status="running")
            self._act(task_id, "team.resumed", "Team run resumed where it stopped.", "user")
            self._cancel[task_id] = threading.Event()
            self._spawn(self._guard, task_id, lambda tid: team_mod.TeamRunner(self, tid).continue_run())
        self._task_changed(task_id)
        return self.db.get_task(task_id)

    def team_view(self, task_id: int) -> dict:
        task = self._require(task_id)
        return {"run": task["state"].get("team_run") or {}, "team": bool(task["state"].get("team")),
                "tasks": self.db.team_tasks(task_id), "messages": self.db.team_messages(task_id, 100),
                "memory": self.db.memory(task["project_id"], None, 30, task_id)}

    def _close_team(self, task_id: int, status: str) -> None:
        task = self.db.get_task(task_id)
        run = task["state"].get("team_run") or {}
        if not run:
            return
        for tt in self.db.team_tasks(task_id):
            if tt["status"] in team_mod.open_statuses():
                self.db.update_team_task(tt["id"], status="cancelled", error=f"The run ended ({status}).")
        if run.get("status") not in ("completed", "failed", "cancelled"):
            run["status"] = status
            self.db.update_task_state(task_id, team_run=run)

    def _cloud_view(self, pack: ctx.ContextPack) -> tuple[ctx.ContextPack, set[str]]:
        """The part of a context pack Claude may see, and the lines that must not reach it."""
        globs = self.cfg["privacy"]["cloud_excluded_globs"]
        private = [f for f in pack.files if privacy.cloud_excluded(f.path, globs)]
        cloud_knowledge, private_knowledge = self._cloud_knowledge(pack)
        allowed = dataclasses.replace(pack, files=[f for f in pack.files if f not in private], knowledge=cloud_knowledge)
        return allowed, privacy.private_lines([f.content for f in private]) | private_knowledge

    def _cloud_knowledge(self, pack: ctx.ContextPack) -> tuple[list, set[str]]:
        """Knowledge excerpts Claude may see. Files marked private or matching the
        cloud exclusions stay on this PC, and their lines are scrubbed from the rest."""
        globs = self.cfg["privacy"]["cloud_excluded_globs"]
        keep, held = [], []
        for snip in pack.knowledge:
            (held if snip.private or privacy.cloud_excluded(snip.name, globs) else keep).append(snip)
        return keep, privacy.private_lines([snip.text for snip in held])

    # PC access (hardware, windows, opening pages and approved programs) -------------------

    def _pc_reply(self, task_id: int, text: str, kind: str, outcome: str, reason: str) -> bool:
        step = self._step(task_id, "PC request", "system", "analysis", reason)
        self._act(task_id, kind, text, "system", "analysis", reason=reason, outcome=outcome, step_id=step)
        self._step_end(step, "completed" if outcome in ("done", "read") else "blocked", text[:200], outcome=outcome)
        self.db.add_message(task_id, "assistant", text, "enoy")
        self._set(task_id, kind="pc", workflow="local_only", routing_reason="A PC request typed by you; no model was used.")
        self._complete(task_id, text.splitlines()[0])
        return True

    def _pc_request(self, task_id: int, text: str) -> bool:
        """Handle a direct PC request typed by the user. Returns True if handled here."""
        intent = pc.detect(text)
        if not intent and tools_mod.about_this_pc(text):
            intent = ("hardware", "")  # Any question about this PC's parts goes to Qwen with the hardware tools.
        if not intent:
            return False
        action, arg = intent
        basis = f"Typed by you; {pc.PROFILES[self.cfg['pc']['profile']][0]} profile."
        try:
            if action == "hardware":
                pc.check(self.cfg, "hardware.read")
                override = self.db.get_task(task_id)["state"].get("override") or LOCAL
                if self.cfg.get("tools", {}).get("enabled", True):
                    # Qwen gets the hardware tools and asks Windows for what it needs.
                    self.db.update_task_state(task_id, override=override)
                    self._act(task_id, "pc.hardware_tools", "Question about this PC: Qwen gets Enoy's read-only "
                              "hardware tools.", "system", "analysis", reason=basis, outcome="routed")
                    return False
                inv = hardware.inventory(self)
                facts = hardware.as_text(inv)
                self.db.update_task_state(task_id, hardware_facts=facts, override=override)
                self._act(task_id, "pc.hardware_read", "Read the hardware inventory from Windows (read-only).", "system",
                          "analysis", reason=basis, outcome="read")
                return False  # Qwen answers, with the measured facts in its context.
            if action == "list_windows":
                pc.check(self.cfg, "windows.observe")
                wins = pc.list_windows()
                lines = [f"- **{w.title}** ({w.program}{', minimized' if w.state == 'minimized' else ''})" for w in wins[:40]]
                return self._pc_reply(task_id, f"Open windows ({len(wins)}):\n\n" + "\n".join(lines), "pc.windows_listed", "read", basis)
            if action == "processes":
                pc.check(self.cfg, "processes.read")
                rows = pc.processes(15)
                lines = [f"| {r['name']} | {r['memory_mib']:,} MiB |" for r in rows]
                return self._pc_reply(task_id, "Programs using the most memory (private working set):\n\n| Program | Memory |\n|---|---:|\n"
                                      + "\n".join(lines), "pc.processes_read", "read", basis)
            if action in ("focus", "minimize", "maximize", "restore"):
                if not pc.list_windows():
                    return False
                pc.check(self.cfg, "windows.observe")
                win = pc.find_window(arg)
                if win is None:
                    return False  # Not a window: treat it as a normal message.
                pc.check(self.cfg, "windows.control")
                pc.window_action(action, win.hwnd)
                verb = {"focus": "Switched to", "minimize": "Minimized", "maximize": "Maximized", "restore": "Restored"}[action]
                return self._pc_reply(task_id, f"{verb} **{win.title}** ({win.program}).", "pc.window_" + action, "done", basis)
            if action == "open_url":
                try:
                    url = pc.check_url(arg)
                except ValueError:
                    return False
                if not pc.looks_like_site(url):
                    return False
                pc.check(self.cfg, "browser.navigate")
                pc.open_url(url)
                return self._pc_reply(task_id, f"Opened {url} in your default browser. Nothing was typed or submitted.",
                                      "pc.browser_navigate", "done", basis)
            if action == "open_app":
                app = pc.approved_app(self.cfg, arg)
                if app is None:
                    if arg.lower().strip() not in pc.KNOWN_APPS:
                        return False
                    return self._pc_reply(task_id, f"**{arg}** is not on your approved programs list, so Enoy did not start it. "
                                                   "Add it in Settings → Memory & PC.", "pc.app_not_approved", "denied", basis)
                pc.check(self.cfg, "apps.open")
                pc.open_app(app)
                return self._pc_reply(task_id, f"Started **{app['name']}**.", "pc.app_opened", "done", basis)
        except pc.PermissionDenied as exc:
            return self._pc_reply(task_id, f"Not done: {exc}", "pc.permission_denied", "denied", basis)
        except (OSError, ValueError) as exc:
            return self._pc_reply(task_id, f"Could not do that: {exc}", "pc.action_failed", "failed", basis)
        return False

    # Projects (workspaces with instructions, documents and artifacts) ---------------------

    def is_default_project(self, project: dict) -> bool:
        return project["path"] == str(Path(os.path.realpath(paths.general_chat_dir())))

    def create_space(self, name: str, description: str = "", instructions: str = "") -> dict:
        """A project without a code folder. It gets its own empty folder in the app data."""
        name = _clean_text(name, 120)
        if not name:
            raise TaskError("Give the project a name.")
        if self.db.space_name_taken(name):
            raise TaskError(f"A project named '{name}' already exists.")
        folder = paths.sub_dir("project-spaces") / f"p{int(time.time() * 1000)}-{secrets.token_hex(4)}"
        folder.mkdir(parents=True, exist_ok=False)  # Never reuse another project's space.
        project = self.db.upsert_project(str(Path(os.path.realpath(folder))), name, kind="space")
        return self.db.update_project(project["id"], description=_clean_text(description, 2000, multiline=True),
                                      instructions=_clean_text(instructions, PROJECT_INSTRUCTIONS_MAX, multiline=True))

    def update_project(self, project_id: int, name: str | None = None, description: str | None = None,
                       instructions: str | None = None) -> dict:
        project = self._require_project(project_id)
        if self.is_default_project(project) and (name is not None or description is not None or instructions is not None):
            raise TaskError("Plain chats have no project settings. Create a project for that.")
        if name is not None:
            name = _clean_text(name, 120)
            if not name:
                raise TaskError("The project name cannot be empty.")
            if project["kind"] == "space" and self.db.space_name_taken(name, project_id):
                raise TaskError(f"A project named '{name}' already exists.")
        if instructions is not None and len(instructions) > PROJECT_INSTRUCTIONS_MAX:
            raise TaskError(f"Instructions are limited to {PROJECT_INSTRUCTIONS_MAX:,} characters.")
        return self.db.update_project(
            project_id, name=name,
            description=None if description is None else _clean_text(description, 2000, multiline=True),
            instructions=None if instructions is None else _clean_text(instructions, PROJECT_INSTRUCTIONS_MAX, multiline=True))

    def delete_project(self, project_id: int) -> dict:
        """Chat project: its chats move to plain chats, its uploaded documents are deleted.
        Code (folder) project: removed from Enoy only; the folder on disk is never touched,
        and its chats and documents come back if the folder is opened again.
        Both: artifacts are kept in the library, without the project link."""
        project = self._require_project(project_id)
        if self.is_default_project(project):
            raise TaskError("Plain chats cannot be deleted as a project.")
        busy = [t for t in self.db.list_tasks(project_id, 500) if t["status"] in ("running", "queued", "awaiting_claude")]
        if busy:
            raise TaskError("A chat in this project is still working or waiting for you. Finish or cancel it first.")
        result = {"artifacts_kept": self.db.unlink_project_artifacts(project_id), "chats_moved": 0, "files_deleted": 0}
        if project["kind"] == "space":
            default = self.open_project(str(paths.general_chat_dir()))
            result["chats_moved"] = self.db.move_project_tasks(project_id, default["id"])
            result["files_deleted"] = self.db.delete_project_files(project_id)
            try:
                Path(project["path"]).rmdir()  # Only if empty; never removes anything else.
            except OSError:
                pass
        self.db.archive_project(project_id)
        self._workspaces.pop(project_id, None)
        return result

    def add_project_file(self, project_id: int, name: str, data: bytes) -> tuple[dict, bool]:
        self._require_project(project_id)
        clean, text = kb.validate(name, data)
        if len(self.db.project_files(project_id)) >= kb.MAX_FILES_PER_PROJECT:
            raise TaskError(f"A project can hold at most {kb.MAX_FILES_PER_PROJECT} documents.")
        return self.db.add_project_file(project_id, clean, text, len(data), sha256_text(text))

    def move_task(self, task_id: int, project_id: int) -> dict:
        """Move a chat between plain chats and chat projects. History and ids are kept."""
        task = self._require(task_id)
        target = self._require_project(project_id)
        source = self._require_project(task["project_id"])
        if task["status"] in ("running", "queued", "awaiting_claude", "awaiting_approval"):
            raise TaskError("This chat is still working or waiting for you.")
        for p in (source, target):
            if p["kind"] != "space" and not self.is_default_project(p):
                raise TaskError("Chats about a code folder stay with that folder: their changes belong to its files.")
        self.db.move_task(task_id, project_id)
        self._task_changed(task_id)
        return self.db.get_task(task_id)

    CHAT_TITLE_MAX = 200

    def rename_task(self, task_id: int, title: str) -> dict:
        self._require(task_id)
        title = " ".join(str(title or "").split())[:self.CHAT_TITLE_MAX]
        if not title:
            raise TaskError("Give the chat a name.")
        self.db.update_task(task_id, title=title)
        self._task_changed(task_id)
        return self.db.get_task(task_id)

    def delete_task(self, task_id: int) -> None:
        """Delete a chat. Files on disk, backups and artifacts are not touched."""
        task = self._require(task_id)
        if task["status"] in ("running", "queued"):
            raise TaskError("This chat is still working. Stop it first, then delete it.")
        self._cancel.pop(task_id, None)
        self._stop_reason.pop(task_id, None)
        self._partial.pop(task_id, None)
        self.db.delete_task(task_id)
        self.bus.publish({"type": "task_deleted", "task_id": task_id})

    def _require_project(self, project_id: int) -> dict:
        project = self.db.get_project(int(project_id))
        if not project or project.get("archived"):
            raise TaskError(f"Unknown project {project_id}")
        return project

    def _claude_direct(self, task_id: int, step_id: int | None, purpose: str, system: str, user: str,
                       files_meta: list[dict], private: set[str]) -> ChatResult:
        """One Claude request outside the single-task flow (team runs), with the same privacy rules."""
        block = self._cloud_block(task_id)
        if block:
            raise BackendError("unavailable", f"Not sent to Claude: {block}.")
        globs = self.cfg["privacy"]["cloud_excluded_globs"]
        allowed = [f for f in files_meta if not privacy.cloud_excluded(f["path"], globs)]
        check = privacy.CloudCheck(sent_files=[f["path"] for f in allowed],
                                   excluded_files=[f["path"] for f in files_meta if f not in allowed])
        prompt = privacy.finalize(user, private, check)
        system = redact(system)
        self._act(task_id, "privacy.checked", check.summary(), "system", outcome="passed", step_id=step_id)
        trans = self.db.add_transmission(task_id, CLAUDE, purpose, allowed, f"[system]\n{system}\n\n[user]\n{prompt}")
        self._act(task_id, "backend.request", f"Sent {len(prompt) + len(system):,} characters and {len(allowed)} file(s) "
                  "to Claude.", CLAUDE, step_id=step_id)
        self._busy[CLAUDE] = {"task_id": task_id, "step_id": step_id, "purpose": purpose, "since": time.time()}
        try:
            result = self.backends[CLAUDE].chat(ChatRequest(
                messages=[ChatMessage("system", system), ChatMessage("user", prompt)], purpose=purpose,
                cancel=self._cancel.setdefault(task_id, threading.Event())))
        except BackendError as exc:
            if exc.kind in ("limit", "unavailable"):
                self._mark_claude_unavailable(str(exc))
            raise
        finally:
            self._busy.pop(CLAUDE, None)
        task = self.db.get_task(task_id)
        self._set(task_id, claude_calls=task["claude_calls"] + 1)
        self._count_work(task_id, CLAUDE, result)
        if result.usage:
            note = ", ".join(f"{k}={v}" for k, v in result.usage.items())
            self.db.set_transmission_usage(trans, note)
            self._act(task_id, "backend.usage", f"Reported by Claude Code: {note}", CLAUDE, step_id=step_id)
        self.db.kv_set("claude_last_verified", time.time())
        return result

    def _count_work(self, task_id: int | None, backend: str, result: Any) -> None:
        """Tokens each model wrote for this chat: what the Claude-first share is measured on.

        Uses the counts the backends report (Ollama's eval_count, Claude Code's output_tokens); without one, the
        text length / 4 is the fallback, and the record says so."""
        if not task_id:
            return
        usage = getattr(result, "usage", None) or {}
        n = usage.get("eval_count" if backend == LOCAL else "output_tokens")
        estimated = not isinstance(n, int)
        if estimated:
            n = len(getattr(result, "text", "") or "") // 4
        with self._work_lock:
            work = dict(self.db.get_task(task_id)["state"].get("work") or {})
            work[backend] = int(work.get(backend, 0)) + int(n)
            if estimated:
                work["estimated"] = True
            self.db.update_task_state(task_id, work=work)

    def _cloud_block(self, task_id: int) -> str:
        task = self.db.get_task(task_id)
        return privacy.local_only_reason(self.cfg, self.db.get_project(task["project_id"]), task["state"])

    def set_task_local_only(self, task_id: int, on: bool) -> dict:
        self._require(task_id)
        self.db.update_task_state(task_id, local_only=bool(on))
        self._act(task_id, "privacy.changed", "This chat now stays on your PC." if on
                  else "This chat may use Claude again (it still asks first if Settings say so).", "user")
        self._task_changed(task_id)
        return self.db.get_task(task_id)

    def set_project_local_only(self, project_id: int, on: bool) -> dict:
        if not self.db.get_project(project_id):
            raise TaskError(f"Unknown project {project_id}")
        self.db.set_project_local_only(project_id, on)
        self._act(None, "privacy.changed", ("Project set to local-only: nothing from it is sent to Claude." if on
                                            else "Project may use Claude again for permitted files."), "user")
        return self.db.get_project(project_id)

    def _note_scope(self, task_id: int, paths: set[str]) -> None:
        """Re-check the routing assumption when an attempt reaches more files than expected."""
        route = self.db.get_task(task_id)["state"].get("route") or {}
        if len(paths) >= 3 and route.get("scope") in ("single function", "single file"):
            self.db.update_task_state(task_id, scope_grew=True)
            self._act(task_id, "task.progress", f"The change reaches {len(paths)} files, more than expected "
                      f"({route.get('scope')}).", "system", "local",
                      reason="Routing assumed a smaller change; Claude takes over if this attempt fails.")

    def _handoff_package(self, task_id: int, reason: str, report: str, user_requested: bool = False) -> str:
        """What the receiving model needs to continue safely, without the whole conversation."""
        task = self.db.get_task(task_id)
        s = task["state"]
        msgs = self.db.messages(task_id)
        followups = [m["content"] for m in msgs if m["role"] == "user"][1:]
        last_local = [m["content"] for m in msgs if m["role"] == "assistant" and m["backend"] == LOCAL][-1:]
        applied = [f["path"] for cs in self.db.list_changesets(task_id, "applied") for f in cs["files"]]
        pending = self.db.list_changesets(task_id, "pending")
        runs = self.db.list_command_runs(task["project_id"], task_id, 3)
        globs = self.cfg["privacy"]["cloud_excluded_globs"]
        out = ["## Handoff from Qwen (local model) to you",
               f"Original request:\n{task['objective'].strip()}"]
        if followups:
            out.append("Later messages from the user:\n" + "\n".join(f"- {t.strip()[:600]}" for t in followups[-3:]))
        out.append(f"Why you are receiving it: {reason}")
        out.append("Plan so far: " + (s.get("plan", "")[:3000] or "No written plan."))
        if s.get("decisions"):
            out.append("Decisions and findings:\n" + "\n".join(f"- {d}" for d in s["decisions"][-6:]))
        files = [f["path"] for f in s.get("relevant_files", []) if not privacy.cloud_excluded(f["path"], globs)]
        out.append("Files inspected: " + (", ".join(files) or "none"))
        out.append("Files already changed (applied by the user): " + (", ".join(applied) or "none"))
        if runs:
            out.append("Checks that were run (actual results):\n" + "\n".join(
                f"- `{r['command']}`: {r['status']} (exit code {r['exit_code']})\n"
                + "\n".join("    " + ln for ln in (r["stdout"] + "\n" + r["stderr"]).strip().splitlines()[-15:])
                for r in runs))
        if s.get("failures"):
            out.append("Errors so far:\n" + "\n".join(f"- {f}" for f in s["failures"][-6:]))
        if report:
            out.append(f"What failed in the last attempt:\n{report[:4000]}")
        if last_local:
            out.append("Qwen's last answer (excerpt):\n" + last_local[0][:5000])
        if pending:
            diffs = "\n".join(f["diff"] for f in pending[0]["files"] if not privacy.cloud_excluded(f["path"], globs))
            if diffs:
                out.append("Change proposed but not applied (unified diff):\n" + diffs[:6000])
        if s.get("uncertain"):
            out.append("Uncertainty: Qwen said it was not sure about its answer.")
        if user_requested and task["kind"] != "edit":
            out.append("Remaining: check the answer above for mistakes and give the correct, complete answer.")
        else:
            out.append("Remaining: produce a correct, complete result for the original request. Enoy will "
                       "verify any change and the user approves it before files are written.")
        return "\n\n".join(out)

    def _routing_history(self, limit: int = 100) -> dict:
        """Per category: tasks Qwen finished alone vs. tasks that needed Claude."""
        hist: dict[str, dict] = {}
        for t in self.db.finished_tasks(limit):
            route = t["state"].get("route") or {}
            if t["state"].get("initial_backend") != LOCAL or not route.get("category") or route.get("source") != "router":
                continue
            h = hist.setdefault(route["category"], {"local_ok": 0, "escalated": 0})
            if t["state"].get("escalations", 0) > 0:
                h["escalated"] += 1
            elif t["status"] == "completed":
                h["local_ok"] += 1
        return hist

    def routing_stats(self) -> dict:
        """Actual vs. target model share over the last finished tasks. No model calls."""
        r = self.cfg["routing"]
        window, target = r["stats_window"], r["target_local_share"]
        rows = self.db.finished_tasks(window)
        n = len(rows)
        claude_rows = [t for t in rows if t["claude_calls"] > 0]
        qwen = n - len(claude_rows)
        direct = [t for t in claude_rows if (t["state"].get("route") or {}).get("source") == "router"
                  and t["state"].get("initial_backend") == CLAUDE]
        manual = [t for t in claude_rows if (t["state"].get("route") or {}).get("source") == "manual"]
        escalated = [t for t in claude_rows if t["state"].get("escalations", 0) > 0]
        improved = [t for t in escalated if t["status"] == "completed"]
        cats: dict[str, int] = {}
        for t in direct:
            c = (t["state"].get("route") or {}).get("category", "other").replace("_", " ")
            cats[c] = cats.get(c, 0) + 1
        qwen_pct = round(100 * qwen / n) if n else None
        # Claude first touches every chat, so there the share of the writing is what counts, not tasks.
        written = {b: sum(int((t["state"].get("work") or {}).get(b, 0)) for t in rows) for b in (LOCAL, CLAUDE)}
        total = written[LOCAL] + written[CLAUDE]
        work = {"qwen_tokens": written[LOCAL], "claude_tokens": written[CLAUDE],
                "qwen_pct": round(100 * written[LOCAL] / total) if total else None,
                "estimated": any((t["state"].get("work") or {}).get("estimated") for t in rows),
                "rule": "Tokens each model wrote in these tasks, as Ollama and Claude Code report them."}
        if self._claude_first() and self.cfg["lead"]["enabled"]:
            if not total or n < 5:
                note = "Claude first: too few finished chats to compare Qwen's share of the writing with the target yet."
            elif work["qwen_pct"] >= target:
                note = f"Claude first: Qwen wrote {work['qwen_pct']}% of the text, at or above the {target}% target."
            else:
                note = (f"Claude first: Qwen wrote {work['qwen_pct']}% of the text, below the {target}% target. "
                        "Claude answered more itself (short or hard replies) than it handed over.")
        elif n < 5:
            note = "Too few finished tasks to compare with the target yet."
        elif abs(qwen_pct - target) <= 5:
            note = "On target."
        elif qwen_pct < target:
            parts = []
            if direct:
                parts.append(f"{len(direct)} went to Claude directly ({', '.join(f'{k} ×{v}' for k, v in cats.items())})")
            if escalated:
                parts.append(f"{len(escalated)} were escalated after Qwen's attempt failed")
            if manual:
                parts.append(f"you picked Claude {len(manual)} time(s)")
            note = ("Claude's share is above target because " + ", ".join(parts) + "."
                    if parts else "Claude's share is above target.")
        else:
            note = ("Claude's share is below target: recent tasks were routine. Enoy never calls Claude "
                    "just to reach the target.")
        return {
            "window": window, "counted": n, "qwen": qwen, "claude": len(claude_rows), "qwen_pct": qwen_pct,
            "target_qwen_pct": target, "direct": len(direct), "escalated": len(escalated),
            "escalations_completed": len(improved), "manual": len(manual), "note": note, "work": work,
            "rule": "Counts completed and failed tasks only. A task counts as Claude's if Claude handled any part of it.",
        }

    # Activity --------------------------------------------------------------------------------------

    def explain_workflow(self, task_id: int) -> dict:
        """Optional, on request only: one local Qwen request that explains a task's workflow.

        It reads the structured step log (titles, agents, reasons, results), never chat
        content or files, and it is not part of any task. Claude is never used here.
        """
        from . import activity

        task = self._require(task_id)
        log_text = activity.workflow_log(task, self.db.steps(task_id))
        if not self._local_lock.acquire(blocking=False):
            raise TaskError("Qwen is busy with a task right now. Try again when it is free.")
        start = time.time()
        self._busy[LOCAL] = {"task_id": None, "step_id": None, "purpose": "explain", "since": start, "chunks": 0,
                             "explaining": task_id}
        self._act(task_id, "explain.requested", "You asked Qwen to explain this workflow (one local request).", LOCAL,
                  reason="Explain workflow button. Not part of the task.")
        messages = [ChatMessage("system", EXPLAIN_SYSTEM), ChatMessage("user", log_text)]
        try:
            self.db.add_transmission(task_id, LOCAL, "explain", [], f"[system]\n{EXPLAIN_SYSTEM}\n\n[user]\n{log_text}")
            limits = self._memory_plan(None)
            if self.memory is not None:
                self.memory.begin(None, None, self.backends[LOCAL].settings["model"],
                                  limits["num_ctx"] or self.backends[LOCAL].settings["num_ctx"])
            result = self.backends[LOCAL].chat(ChatRequest(messages=messages, purpose="explain", **limits))
        except BackendError as exc:
            self._act(task_id, "explain.failed", f"Qwen could not explain the workflow: {exc}", LOCAL, outcome="failed")
            raise TaskError(f"Qwen could not explain the workflow: {exc}") from exc
        finally:
            if self.memory is not None:
                self.memory.end()
            self._busy.pop(LOCAL, None)
            self._local_lock.release()
        seconds = time.time() - start
        self._act(task_id, "explain.completed", f"Qwen explained the workflow ({seconds:.0f} s).", LOCAL, outcome="completed")
        return {"text": result.text.strip(), "seconds": round(seconds, 1), "log": log_text}

    # Continuation ---------------------------------------------------------------------------------

    def continuation_brief(self, task_id: int) -> str:
        task = self._require(task_id)
        project = self.db.get_project(task["project_id"])
        return ctx.continuation_brief(
            task, project, self.db.list_changesets(task_id),
            self.db.list_command_runs(task["project_id"], task_id), self.db.messages(task_id),
        )

    def handoff_to_claude_code(self, task_id: int) -> dict:
        """Manual fallback: write the brief into the project and open Claude Code on it."""
        from .backends.claude_code import find_executable

        task = self._require(task_id)
        ws = self.workspace(task["project_id"])
        folder = ws.root / ".fusion" / "handoff"
        folder.mkdir(parents=True, exist_ok=True)
        gitignore = ws.root / ".fusion" / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n", encoding="utf-8")
        brief_path = folder / f"task-{task_id}.md"
        brief_path.write_text(self.continuation_brief(task_id), encoding="utf-8")
        rel = brief_path.relative_to(ws.root).as_posix()
        exe = find_executable(self.cfg["claude"]["executable"])
        prompt = f"Read {rel} (a task brief exported from Enoy) and continue that task in this project."
        opened = False
        if exe and os.name == "nt":
            subprocess.Popen([exe, prompt], cwd=str(ws.root), creationflags=subprocess.CREATE_NEW_CONSOLE)
            opened = True
        self._event(task_id, "handoff", f"Wrote {rel}" + (" and opened Claude Code in a new window." if opened else "."))
        return {"path": str(brief_path), "opened": opened, "prompt": prompt}


class _Cancelled(Exception):
    pass


_ERROR_LINE = re.compile(r"(Error|Exception|FAILED|assert|^E\s|error:|Traceback)", re.I)
_TIMING = re.compile(r"\b\d+(\.\d+)?\s?(s|ms|seconds?)\b")


PROJECT_INSTRUCTIONS_MAX = 8000


_MISSING_TOOL = (re.compile(r"No module named '?([\w.-]+)'?"),
                 re.compile(r"'([\w.-]+)' is not recognized as an internal or external command"),
                 re.compile(r"([\w.-]+): (?:command )?not found"))


def missing_tool(output: str) -> str:
    """The tool a check needed but could not find (pytest, npm, ...), or ""."""
    for pattern in _MISSING_TOOL:
        m = pattern.search(output or "")
        if m:
            return m.group(1)
    return ""


def _fit_window(msgs: list[ChatMessage], budget: int) -> None:
    """Keep a tool conversation inside the model's window: shorten the oldest tool results first.

    Ollama would otherwise cut the start of the prompt, instructions included, without saying so."""
    def total() -> int:
        return sum(len(m.content) for m in msgs)
    for m in msgs[1:-2]:
        if total() <= budget:
            return
        if m.role == "tool" and len(m.content) > 300:
            m.content = m.content[:240] + " ... [older tool result shortened to fit the context window]"
    for m in msgs[1:-2]:
        if total() <= budget:
            return
        if m.role in ("user", "assistant") and len(m.content) > 600:
            m.content = m.content[:500] + " ... [shortened]"


def _clean_text(text, limit: int, multiline: bool = False) -> str:
    text = str(text or "")
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text) if multiline else re.sub(r"[\x00-\x1f]", " ", text)
    return text.strip()[:limit]


def _failure_signature(lines: list[str]) -> str:
    """The line that identifies a failure, without run-specific timings.

    Used to notice that the same error came back after a fix.
    """
    errors = [ln.strip() for ln in lines if ln.strip() and _ERROR_LINE.search(ln)]
    pick = errors[-1] if errors else next((ln.strip() for ln in reversed(lines) if ln.strip()), "")
    return _TIMING.sub("", pick).strip()[:300]


def _first_line(text: str) -> str:
    for line in text.strip().splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:200]
    return ""
