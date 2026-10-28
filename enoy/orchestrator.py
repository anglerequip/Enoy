"""Task orchestrator: runs tasks, routes them, escalates, validates, records.

Owns task scheduling, shared state, native-provider access policies, retries,
activity and approval of proposals in filtered context mode. Native providers
can inspect and edit actual files and run commands directly.
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
from . import patches
from . import privacy
from . import context_usage
from . import team as team_mod
from . import tools as tools_mod
from .tools import folders as folders_mod
from .backends.base import CLAUDE, CODEX, BackendError, ChatMessage, ChatRequest, ChatResult
from .project import PathError, Workspace, sha256_text
from .redact import redact
from .routing import classify, sounds_uncertain
from .storage import STEP_OPEN, TERMINAL_STATUSES, Storage

log = logging.getLogger("enoy.orchestrator")

BACKEND_LABEL = {CLAUDE: "Claude", CODEX: "Codex"}
MAX_READ_ROUNDS = 3  # How many times ChatGPT may ask for more project files in one answer.
CLAUDE_STEP_TITLE = {"plan": "Write an implementation plan", "review": "Review ChatGPT's change",
                     "edit": "Write the change", "ask": "Answer", "lead": "Read the request and decide (lead)",
                     "lead_review": "Check the team's results (lead)"}
CANCEL_NOTE = {
    CODEX: "Cancel requested. Enoy stops the Codex process at the current request.",
    CODEX: "Cancel requested. Enoy closes its connection to local model service; local model service stops generating when the connection closes.",
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
        self.memory = None  # Started by the app; tests run without it.
        self._stop_reason: dict[int, str] = {}
        self._partial: dict[int, str] = {}  # Text streamed so far, kept if the user stops the answer.
        self._work_lock = threading.Lock()  # ChatGPT and Claude can finish at once in a team run.
        self._cancel: dict[int, threading.Event] = {}
        self._run_cancel: dict[int, threading.Event] = {}
        self._local_lock = threading.Lock()
        self._claude_status: tuple[float, bool, str] | None = None
        self._threads: list[threading.Thread] = []
        # Requests in flight right now, per backend. Only set while a real request runs.
        self._busy: dict[str, dict] = {}
        self._backend_seen: dict[str, bool] = {}
        self._local_status: tuple[float, dict] | None = None
        self._codex_status: tuple[float, dict] | None = None
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
            if status == "completed" and self.cfg["agent"].get("self_check"):
                self._self_check_usage(task_id)
        self._task_changed(task_id)

    def _self_check_usage(self, task_id: int) -> None:
        """Cheap local usage review. Only actionable patterns produce Activity notes."""
        rows = self.db.transmissions(task_id=task_id, limit=100)
        complete = [r for r in rows if r.get("usage")]
        if (len(complete) >= 4 and sum(r.get("chars", 0) for r in complete) > 20_000
                and not any(r.get("history_saved_chars", 0) for r in complete)):
            self._event(task_id, "usage.check", "Usage check: four or more model requests recorded no context savings. Review Activity for repeated context or tool calls.")

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
            section = backend.name
            backend.settings = cfg[section]
            setattr(self, "_status_" + backend.name, None)
        self._claude_status = None
        self._codex_status = None
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
        now = time.time()
        statuses = {}
        for name in (CODEX, CLAUDE):
            backend = self.backends.get(name)
            if backend is None:
                continue
            attr = '_status_' + name
            cache = getattr(self, attr, None)
            if refresh or not cache or now-cache[0] > 60:
                cache = (now, backend.status().as_dict())
                setattr(self, attr, cache)
            statuses[name] = dict(cache[1])
        return statuses

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
                   team: bool | None = None, images: list[str] | None = None, starting_model: dict | None = None) -> dict:
        objective = objective.strip()
        if not objective:
            raise TaskError("The task is empty.")
        from .model_policy import validate_start
        starting_model = validate_start(starting_model)
        image_ids = imgs.valid_ids(images)
        title = objective.splitlines()[0][:80]
        task = self.db.create_task(project_id, title, objective, kind or "auto",
                                   state={"starting_model": starting_model, "override": backend, "kind_override": kind, "extra_paths": files or [],
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
                     files: list[str] | None = None, images: list[str] | None = None, starting_model: dict | None = None) -> dict:
        """Follow-up message in an existing task (conversation continues)."""
        task = self._require(task_id)
        if task["status"] in ("running", "queued"):
            raise TaskError("This task is still running.")
        if not text.strip():
            raise TaskError("The message is empty.")
        from .model_policy import validate_start
        starting_model = validate_start(starting_model)
        self.db.add_message(task_id, "user", text, images=imgs.valid_ids(images))
        new_files = [f for f in (files or []) if f not in task["state"].get("extra_paths", [])]
        self.db.update_task_state(task_id, followup=text, extra_paths=new_files, starting_model=starting_model)
        self._act(task_id, "task.message", "Follow-up message received.", "user", "request")
        self._cancel[task_id] = threading.Event()
        if self._queue_if_paused(task_id, {"type": "followup", "text": text, "backend": backend}):
            return self.db.get_task(task_id)
        self._spawn(self._guard, task_id, lambda tid: self._run_followup(tid, text, backend))
        return self.db.get_task(task_id)

    def compact_task(self, task_id: int, focus: str = "") -> dict:
        """Compact provider context while keeping the complete chat transcript visible."""
        task = self._require(task_id)
        if task["status"] in ("running", "queued"):
            raise TaskError("Wait until this chat finishes before compacting it.")
        messages = self.db.messages(task_id)
        if not messages:
            raise TaskError("There is no conversation to compact yet.")
        previous = task["state"].get("context_compaction") or {}
        parts = []
        if previous.get("summary"):
            parts += ["## Earlier compacted context", previous["summary"][-4000:]]
        current = self.continuation_brief(task_id)
        if len(current) > 9000:
            current = current[:7000] + "\n[Middle of the continuation brief omitted.]\n" + current[-2000:]
        parts += ["## Current task state", current]
        recent = messages[-8:]
        if recent:
            lines = [f"- {m['role'].capitalize()}: {m['content'].replace(chr(10), ' ')[:1000]}"
                     for m in recent]
            parts += ["## Recent conversation", *lines]
        brief = "\n\n".join(parts)
        focus = focus.strip()[:1000]
        if focus:
            brief += "\n\n## Keep in mind while continuing\n" + focus
        cutoff = messages[-1]["id"]
        self.db.update_task_state(task_id, context_compaction={
            "summary": brief,
            "through_message_id": cutoff,
            "created": time.time(),
        }, native_sessions={})
        self._act(task_id, "context.compacted", "Compacted provider context and reset native provider sessions.",
                  "user", "context")
        self._task_changed(task_id)
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
        task = self._require(task_id)
        return self.send_message(task_id, 'Review the latest request and results in this conversation.', backend=CLAUDE)

    def approve_claude(self, task_id: int) -> None:
        from . import coordinator
        task = self._require(task_id)
        if (task['state'].get('team_run') or {}).get('status') == 'awaiting_claude':
            team_mod.TeamRunner(self, task_id).answer_claude(True)
            self._spawn(self._guard, task_id, lambda tid: team_mod.TeamRunner(self, tid).continue_run())
            return
        if not task['state'].get('pending_claude'):
            raise TaskError('No Claude request is waiting.')
        self._spawn(self._guard, task_id, lambda tid: coordinator.send_direct(self, tid))

    def decline_claude(self, task_id: int) -> None:
        from . import coordinator
        task = self._require(task_id)
        if (task['state'].get('team_run') or {}).get('status') == 'awaiting_claude':
            team_mod.TeamRunner(self, task_id).answer_claude(False)
            self._spawn(self._guard, task_id, lambda tid: team_mod.TeamRunner(self, tid).continue_run())
            return
        pending = task['state'].get('pending_claude')
        if not pending:
            raise TaskError('No Claude request is waiting.')
        self.db.update_task_state(task_id, pending_claude=None)
        self._spawn(self._guard, task_id, lambda tid: coordinator.start(self, tid, pending['objective'], CODEX))

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
            partial = self._partial.pop(task_id, '')
            if partial:
                self.db.add_message(task_id, 'assistant', partial + '\n\n*Stopped before the answer was finished.*', CODEX)
            why = self._stop_reason.pop(task_id, None)
            if why:  # Stopped by the memory supervisor, not by the user.
                self._set(task_id, status="failed", summary=why)
                self._event(task_id, "error", why, CODEX)
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
        if not ctx.is_general(ws) and not ws.index and not task["state"].get("empty_note"):
            self.db.update_task_state(task_id, empty_note=True)  # Once per chat, not under every message.
            self._event(task_id, "note", f"This chat's folder ({ws.root}) is empty. Ask about code anywhere on this PC "
                        "(Enoy itself too) and Enoy offers to move the chat to that folder.")
        return pack

    def _analyze(self, task_id: int, objective: str, title: str) -> tuple[ctx.ContextPack, int]:
        step = self._step(task_id, title, "system", "analysis",
                          "Every task starts with a quick local analysis by Enoy itself (no model).")
        pack = self._gather(task_id, objective)
        self.db.update_step(step, files=[f.path for f in pack.files])
        return pack, step

    def _run_new(self, task_id: int) -> None:
        from . import coordinator
        task = self.db.get_task(task_id)
        if self._pc_request(task_id, task['objective']) or self._enoy_request(task_id, task['objective']):
            return
        coordinator.start(self, task_id, task['objective'], task['state'].get('override'))

    def _run_followup(self, task_id: int, text: str, backend: str | None) -> None:
        from . import coordinator
        self.db.update_task_state(task_id, override=backend)
        if self._pc_request(task_id, text) or self._enoy_request(task_id, text):
            return
        coordinator.start(self, task_id, text, backend)

    def _run_resume(self, task_id: int) -> None:
        task = self._require(task_id)
        run = task['state'].get('team_run') or {}
        if run and run.get('status') not in ('completed','failed','cancelled'):
            team_mod.TeamRunner(self, task_id).continue_run()
        else:
            self._run_followup(task_id, task['state'].get('followup') or task['objective'], None)

    # Claude as the lead -------------------------------------------------------------------










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
            if provider not in (CLAUDE, CODEX):
                raise TaskError("Pick Codex or Claude.")
            if provider not in self.backends:
                raise TaskError("That provider is not configured.")
            if tt["status"] not in ("planned", "ready", "paused", "blocked", "failed") or tt["role"] not in ("developer", "debugger"):
                raise TaskError("The model can only be changed for a developer task that is not running or finished.")
            if provider in (CLAUDE, CODEX) and self._cloud_block(task_id):
                raise TaskError(f"Not sent to {BACKEND_LABEL[provider]}: {self._cloud_block(task_id)}.")
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


    def _chat_codex(self, task_id: int, messages: list[ChatMessage], purpose: str, step_id: int | None = None, tools=None):
        from .coordinator import call
        system = '\n\n'.join(m.content for m in messages if m.role == 'system')
        user = '\n\n'.join(m.content for m in messages if m.role != 'system')
        return call(self, task_id, CODEX, system, user, purpose, step=step_id)

    # ------------------------------------------------------------ tools



    def _tools_wanted(self, text: str) -> bool:
        s = self.cfg.get("tools", {})
        return bool(s.get("enabled", True) and tools_mod.wants_tools(text))

    def _tool_runtime(self) -> dict:
        return {'providers': [CODEX, CLAUDE], 'local_models': False}

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

    def _mcp_env(self, files_task: int | None = None) -> dict:
        """Settings for Enoy's MCP server. With files_task, also that chat's project folder and privacy rules."""
        try:
            runtime = self._tool_runtime()
        except Exception as exc:
            runtime = {"error": str(exc)}
        env = {"ENOY_MCP_CFG": {"tools": self.cfg.get("tools", {}), "pc": self.cfg.get("pc", {})},
               "ENOY_MCP_RUNTIME": runtime}
        if files_task:
            svc = self._folder_service(self.db.get_task(files_task))
            env["ENOY_MCP_FOLDERS"] = {"current": svc.current, "known": svc.known}
            env["ENOY_MCP_WORKSPACE"] = str(self.workspace(self.db.get_task(files_task)["project_id"]).root)
            env["ENOY_MCP_PRIVATE"] = {"cloud_excluded_globs": self.cfg["privacy"]["cloud_excluded_globs"],
                                       "extra_excluded_globs": self.cfg["permissions"]["extra_excluded_globs"]}
        return env

    def _claude_file_tools(self, task_id: int) -> list[str]:
        """Read-only project tools for Claude: only with a project that has files and nothing keeping it local."""
        s = self.cfg.get("tools", {})
        if not (s.get("enabled", True) and s.get("claude_files", True)) or self._cloud_block(task_id):
            return []
        ws = self.workspace(self.db.get_task(task_id)["project_id"])
        if ctx.is_general(ws) or not ws.index:
            return []
        runner = tools_mod.ToolRunner(self.cfg, tools_mod.registry(), offer=set(tools_mod.CLAUDE_FILES),
                                      services={"files": None})
        return sorted(t.name for t in runner.available())

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
            self._act(task_id, "tool.finished", "Claude had Enoy's tools available but did not use them.", CLAUDE, "tools",
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


    def _conversation(self, task_id: int, limit: int = 6) -> list[ChatMessage]:
        msgs = self.db.messages(task_id)[:-1]  # Latest user message is rebuilt with context.
        return [ChatMessage(m["role"], m["content"]) for m in msgs[-limit:] if m["role"] in ("user", "assistant")]


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





    # Claude requests ---------------------------------------------------------------------






    # Results ---------------------------------------------------------------------------------


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
        return "Reported by local model service: " + ", ".join(bits) if bits else ""

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
        backup_dir = self.backups_dir or Path(ws.root) / ".enoy" / "backups"
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
        run = self.db.get_command_run(run_id)
        if not run or run['task_id'] != task_id or run['status'] == 'passed':
            raise TaskError('That failed check does not belong to this chat.')
        return self.send_message(task_id, 'Fix the failed check: '+run['command']+'\n'+run['stdout']+'\n'+run['stderr'])

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
            self._set_owner(tid, CODEX, "Project summary using Codex.")
            if cached:
                step = self._step(tid, "Reuse the saved summary", "system", "analysis",
                                  "The file list has not changed since the last summary.")
                self.db.set_project_summary(project_id, cached, CODEX)
                self.db.add_message(tid, "assistant", cached, CODEX)
                self._step_end(step, "completed", "Used the saved summary; no model request.")
                self._complete(tid, "Used the cached summary; the file list has not changed.")
                return
            step = self._step(tid, "Summarize the project", CODEX, "local", "Project summary using Codex.")
            pack = ctx.gather(ws, "readme main index init app server game config", self.cfg["context"])
            readme = [f for f in pack.files][:5]
            prompt = ("Write a concise summary of this project for another developer: purpose, main parts, "
                      "languages/frameworks, entry points, how to run tests. Max 250 words.\n\nFile list:\n"
                      + listing + "\n\n" + "\n\n".join(f"=== {f.path} ===\n{f.content[:3000]}" for f in readme))
            result = self._chat_codex(tid, [ChatMessage("system", "Summarize the supplied project facts accurately."),
                                            ChatMessage("user", prompt)], "summary", step)
            self._step_end(step, "completed", "Wrote the summary" + self._result_note(result) + ".")
            self.db.cache_put(key, result.text)
            self.db.set_project_summary(project_id, result.text, CODEX)
            self.db.add_message(tid, "assistant", result.text, CODEX)
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

    def _enoy_request(self, task_id: int, text: str) -> bool:
        """A change to Enoy itself asked from a chat without its code: offer to move there, without a model.

        Small models guess here (ChatGPT once started a new tkinter app in an empty folder), and asking Claude
        costs a request, so Enoy recognises these itself. True when it offered the move."""
        from . import native
        if native.mode(self) == "full" and native.allowed(self, task_id, "ask"):
            return False  # Full Access explicitly authorizes discovery and work outside this chat's folder.
        source = folders_mod.enoy_source()
        task = self.db.get_task(task_id)
        if task["state"].get("skip_enoy_check"):  # You chose "Stay here" for this message.
            self.db.update_task_state(task_id, skip_enoy_check=False)
            return False
        if not source or not folders_mod.about_enoy_change(text):
            return False
        ws = self.workspace(task["project_id"])
        if ws.index and not ctx.is_general(ws):
            return False  # A real project is open: the request may well be about its code.
        if not self._request_folder(task_id, source, "This is about Enoy itself, and its source code is in that "
                                                     "folder.", "system"):
            return False
        self.db.add_message(task_id, "assistant", "This is a change to Enoy itself. Its source code is not in this "
                            f"chat's folder but in `{source}`. Move the chat there and the work starts.", "enoy")
        self.bus.publish({"type": "message_done", "task_id": task_id, "backend": "enoy"})
        self._complete(task_id, "Offered to move this chat to Enoy's source code.")
        return True

    def _pc_request(self, task_id: int, text: str) -> bool:
        """Handle a direct PC request typed by the user. Returns True if handled here."""
        intent = pc.detect(text)
        if not intent and tools_mod.about_this_pc(text):
            intent = ("hardware", "")  # Any question about this PC's parts goes to ChatGPT with the hardware tools.
        if not intent:
            return False
        action, arg = intent
        basis = f"Typed by you; {pc.PROFILES[self.cfg['pc']['profile']][0]} profile."
        try:
            if action == "hardware":
                pc.check(self.cfg, "hardware.read")
                override = self.db.get_task(task_id)["state"].get("override") or CODEX
                inv = hardware.inventory(self)
                facts = hardware.as_text(inv)
                self.db.update_task_state(task_id, hardware_facts=facts, override=override)
                self._act(task_id, "pc.hardware_read", "Read the hardware inventory from Windows (read-only).", "system",
                          "analysis", reason=basis, outcome="read")
                return False  # ChatGPT answers, with the measured facts in its context.
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
        candidate = Path(os.path.realpath(project["path"]))
        if candidate == Path(os.path.realpath(paths.general_chat_dir())):
            return True
        # Imported chats can retain the MSIX app's earlier virtual data path.
        # Recognize only known app-data layouts, not arbitrary folders with this name.
        try:
            parts = tuple(part.lower() for part in candidate.relative_to(Path.home() / "AppData" / "Local").parts)
        except ValueError:
            return False
        return (parts in (("enoy", "general-chat"), ("fusionai", "general-chat")) or
                len(parts) == 6 and parts[0] == "packages" and parts[2:4] == ("localcache", "local")
                and parts[4] in ("enoy", "fusionai") and parts[5] == "general-chat")

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

    # Working in the right folder ------------------------------------------------------------

    def _folder_service(self, task: dict) -> Any:
        """What the folder tools and prompts know: this chat's folder, Enoy's own source, folders opened before."""
        ws = self.workspace(task["project_id"])
        known = [(p["name"], p["path"]) for p in self.db.list_projects()
                 if p.get("kind", "folder") == "folder" and not self.is_default_project(p)][:10]
        return tools_mod.FolderService("" if ctx.is_general(ws) else str(ws.root), known, folders_mod.enoy_source())

    def _folder_context(self, svc: Any) -> str:
        lines = ["Folders on this PC that you may need:"]
        if svc.source:
            lines.append(f"- Enoy's own source code (this app: its window, sidebar, status dots, chat, settings, "
                         f"styles; Python in enoy/, the interface in enoy/ui/): {svc.source}")
        lines += [f"- {name}: {path}" for name, path in svc.known
                  if os.path.normcase(path) not in (os.path.normcase(svc.source), os.path.normcase(svc.current))]
        lines.append(f"This chat works in: {svc.current or 'no folder (general chat)'}. Work on code in another "
                     "folder by moving the chat there (the user confirms first).")
        return "\n".join(lines)

    def _request_folder(self, task_id: int, path: str, reason: str, by: str) -> bool:
        """A model asked to move this chat to another folder: show the user a card. False if the folder is unusable."""
        task = self.db.get_task(task_id)
        svc = self._folder_service(task)
        try:
            target = folders_mod.check_target(path, svc.current)
        except tools_mod.ToolError as exc:
            self._event(task_id, "note", f"{BACKEND_LABEL.get(by, 'Enoy')} suggested another folder, but {exc}", by)
            return False
        users = [m["content"] for m in self.db.messages(task_id) if m["role"] == "user"]
        self.db.update_task_state(task_id, pending_folder={
            "path": str(target), "name": target.name, "reason": reason, "by": by,
            "is_enoy": bool(svc.source) and os.path.normcase(str(target)) == os.path.normcase(svc.source),
            "text": users[-1] if users else task["objective"]})
        self._act(task_id, "folder.requested", f"{BACKEND_LABEL.get(by, 'Enoy')} asks to move this chat to {target}.", by,
                  "analysis", reason=reason or "The request is about code in that folder.", outcome="waiting")
        self._task_changed(task_id)
        return True

    def answer_folder(self, task_id: int, accept: bool) -> dict:
        """Your answer to a folder request: move the chat there and continue, or stay."""
        task = self._require(task_id)
        req = task["state"].get("pending_folder")
        if not req:
            raise TaskError("Nothing is waiting for a folder decision.")
        if task["status"] in ("running", "queued", "awaiting_claude", "awaiting_approval"):
            raise TaskError("This chat is still working or waiting for you.")
        self.db.update_task_state(task_id, pending_folder=None)
        if not accept:
            self._act(task_id, "folder.declined", f"You kept this chat in its folder (not moved to {req['path']}).", "user")
            if req.get("by") == "system":
                # Enoy offered the move before any model saw the message: answer it here after all.
                self.db.update_task_state(task_id, skip_enoy_check=True)
                self._set(task_id, status="running")
                self._cancel[task_id] = threading.Event()
                self._spawn(self._guard, task_id, lambda tid: self._run_followup(tid, req["text"], None))
            else:
                self._task_changed(task_id)
            return self.db.get_task(task_id)
        target = self.open_project(req["path"])
        if self.db.list_changesets(task_id):
            # Changes made here belong to this folder's files (and its Undo): the work continues in a new chat.
            new = self.start_task(target["id"], req["text"])
            self._event(task_id, "note", f"Continued in a new chat in {req['path']}, because this chat already changed "
                                         "files in its own folder.")
            self._task_changed(task_id)
            return self.db.get_task(new["id"])
        self.db.move_task(task_id, target["id"])
        # A resumed Claude session still believes it is in the old folder: start fresh with the new project.
        self.db.update_task_state(task_id, claude_session=None, claude_sent={}, empty_note=False)
        self._act(task_id, "folder.moved", f"Moved this chat to {req['path']} and continued there.", "user",
                  reason=req.get("reason", ""))
        self._event(task_id, "status", f"Moved to {req['path']}.")
        self._set(task_id, status="running")
        self._cancel[task_id] = threading.Event()
        self._spawn(self._guard, task_id, lambda tid: self._run_followup(tid, req["text"], None))
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
                       files_meta: list[dict], private: set[str], images: list | None = None) -> ChatResult:
        """One Claude request outside the single-task flow (team runs), with the same privacy rules."""
        block = self._cloud_block(task_id)
        if block:
            raise BackendError("unavailable", f"Not sent to Claude: {block}.")
        globs = self.cfg["privacy"]["cloud_excluded_globs"]
        allowed = [f for f in files_meta if not privacy.cloud_excluded(f["path"], globs)]
        check = privacy.CloudCheck(sent_files=[f["path"] for f in allowed],
                                   excluded_files=[f["path"] for f in files_meta if f not in allowed])
        prompt = privacy.finalize(user, private, check)
        from . import native, model_policy, blender
        task_objective = self.db.get_task(task_id).get('objective', '')
        profile = model_policy.claude_profile(self.cfg, task_objective, purpose)
        model_policy.announce(self, task_id, CLAUDE, purpose, profile)
        native_options = native.options(self, task_id, CLAUDE, purpose, profile)
        native_active = bool(native_options.get('native_tools'))
        native_options.setdefault('model', profile['model'])
        native_options.setdefault('effort', profile['effort'])
        files = [] if native_active else self._claude_file_tools(task_id)
        if native.SELF_RELIANCE not in system:  # Team requests that skip the coordinator still get the rule.
            system += "\n\n" + native.SELF_RELIANCE
        if files:
            system += "\n\n" + tools_mod.CLAUDE_FILES_NOTE
            if purpose in tools_mod.CLAUDE_REVIEW_PURPOSES:
                system += "\n\n" + tools_mod.CLAUDE_REVIEW_NOTE
        if not native_active and self.cfg.get("tools", {}).get("enabled", True):
            files = files + ["find_folders"]
        system = redact(system)
        self._act(task_id, "privacy.checked", check.summary(), "system", outcome="passed", step_id=step_id)
        trans = self.db.add_transmission(task_id, CLAUDE, purpose, allowed, f"[system]\n{system}\n\n[user]\n{prompt}")
        self._act(task_id, "backend.request", f"Sent {len(prompt) + len(system):,} characters and {len(allowed)} file(s) "
                  "to Claude.", CLAUDE, step_id=step_id)
        self._busy[CLAUDE] = {"task_id": task_id, "step_id": step_id, "purpose": purpose, "since": time.time()}
        result = None
        try:
            result = self.backends[CLAUDE].chat(ChatRequest(
                messages=[ChatMessage("system", system), ChatMessage("user", prompt, images=images or [])], purpose=purpose,
                cancel=self._cancel.setdefault(task_id, threading.Event()), enoy_tools=files,
                mcp_servers=blender.servers(self, task_id, CLAUDE, purpose),
                on_thinking=lambda text: model_policy.progress(self, task_id, text),
                enoy_env=self._mcp_env(task_id) if files else {}, **native_options))
        except BackendError as exc:
            if exc.kind in ("limit", "unavailable"):
                self._mark_claude_unavailable(str(exc))
            raise
        finally:
            self._busy.pop(CLAUDE, None)
            native.remember(self, task_id, CLAUDE, native_options, result)
        task = self.db.get_task(task_id)
        self._set(task_id, claude_calls=task["claude_calls"] + 1)
        self._count_work(task_id, CLAUDE, result)
        if files:
            self._record_claude_tools(task_id, result.tool_events, step_id)
        if result.usage:
            note = ", ".join(f"{k}={v}" for k, v in result.usage.items())
            self._act(task_id, "backend.usage", f"Reported by Claude Code: {note}", CLAUDE, step_id=step_id)
        usage_record = {**result.usage, 'model':result.model, 'effort':profile['effort'],
            'progress':self.db.get_task(task_id)['state'].get('active_agent', {}).get('progress', [])}
        reading = context_usage.context_reading(CLAUDE, result.model, result.usage)
        if reading:
            usage_record['context'] = reading
        self.db.set_transmission_usage(trans, json.dumps(usage_record))
        self.db.kv_set("claude_last_verified", time.time())
        return result

    def _count_work(self, task_id: int | None, backend: str, result: Any) -> None:
        """Tokens each model wrote for this chat: what the Claude-first share is measured on.

        Uses the counts the backends report (local model service's eval_count, Claude Code's output_tokens); without one, the
        text length / 4 is the fallback, and the record says so."""
        if not task_id:
            return
        usage = getattr(result, "usage", None) or {}
        n = usage.get("eval_count" if backend == CODEX else "output_tokens")
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



    def routing_stats(self) -> dict:
        rows = self.db.finished_tasks(50)
        counts = {p: sum(int((t['state'].get('work') or {}).get(p,0)) for t in rows) for p in (CODEX, CLAUDE)}
        return {'window':50,'counted':len(rows),'providers':counts,'note':'Only Codex and Claude are available.','work':{},'rule':'Recorded provider usage.'}

    # Activity --------------------------------------------------------------------------------------

    def explain_workflow(self, task_id: int) -> dict:
        from . import activity
        from .coordinator import call
        task = self._require(task_id)
        log_text = activity.workflow_log(task, self.db.steps(task_id))
        result = call(self, task_id, CODEX, EXPLAIN_SYSTEM, log_text, 'explain')
        return {'text':result.text,'seconds':round(result.duration_s,1),'log':log_text}

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
        folder = ws.root / ".enoy" / "handoff"
        folder.mkdir(parents=True, exist_ok=True)
        gitignore = ws.root / ".enoy" / ".gitignore"
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


_MORE = re.compile(r"^(now |next,? |first,? )?(let me(?! know)|let's|i'll now|i will now|i'm going to)\b", re.I)


def _announces_more(text: str) -> bool:
    """The reply ends by announcing a next step ("Now let me write the tests:") instead of finishing.

    "Let me know if..." and offers ("If you want, I'll add tests.") are endings, not announcements."""
    tail = (text or "").strip()
    if not tail:
        return False
    last = re.split(r"(?<=[.!?])\s+|\n", tail)[-1].strip()
    return tail.endswith(":") or bool(_MORE.match(last))


def _fit_window(msgs: list[ChatMessage], budget: int) -> None:
    """Keep a tool conversation inside the model's window: shorten the oldest tool results first.

    local model service would otherwise cut the start of the prompt, instructions included, without saying so."""
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
