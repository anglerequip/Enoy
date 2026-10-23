"""Activity panel data: built only from stored tasks, steps and events.

Everything here is deterministic. Nothing in this module calls a model, so the
panel keeps working when Qwen and Claude are both offline.
"""

from __future__ import annotations

import time
from typing import Any

from . import privacy
from .backends.base import CLAUDE, LOCAL
from .storage import STEP_OPEN, TERMINAL_STATUSES

# Canonical stage order. The workflow view shows stages in the order they
# actually happened; stages that never ran are appended in this order.
STAGES = [
    ("request", "Request"),
    ("analysis", "Task analysis"),
    ("local", "Qwen (local)"),
    ("claude", "Claude"),
    ("patch", "Patch check"),
    ("review", "Claude review"),
    ("approval", "Your approval"),
    ("testing", "Checks / tests"),
    ("done", "Done"),
]
# A TEAM ROLES run has its own stages; each one appears only if the run used it.
TEAM_STAGES = [
    ("request", "Request"), ("analysis", "Understand"), ("plan", "Team plan"), ("implement", "Implementation"),
    ("integrate", "Integration"), ("testing", "Checks / tests"), ("review", "Review"), ("repair", "Repair"),
    ("done", "Done"),
]
STAGE_LABEL = dict(STAGES) | {k: v for k, v in TEAM_STAGES if k not in ("review",)} | {"review": "Review"}
EDIT_ONLY = {"patch", "approval", "testing", "review"}

TASK_STATUS_LABEL = {
    "queued": "Queued", "running": "Working", "awaiting_approval": "Needs your approval",
    "awaiting_claude": "Needs your OK for Claude", "completed": "Completed", "failed": "Failed",
    "cancelled": "Cancelled", "interrupted": "Interrupted",
}
AGENT_LABEL = {LOCAL: "Qwen", CLAUDE: "Claude", "system": "Enoy", "user": "You"}

# Timeline filter groups.
CATEGORY = {
    "task.assigned": "routing", "task.escalated": "routing", "task.classified": "routing", "task.handoff": "routing",
    "task.not_escalated": "routing", "privacy.checked": "routing", "privacy.changed": "routing", "task.queued": "routing", "task.dispatched": "routing",
    "backend.request": "model", "explain.requested": "model", "explain.completed": "model",
    "approval.requested": "approval", "approval.resolved": "approval", "patch.proposed": "approval",
    "patch.reverted": "approval",
    "test.started": "tests", "test.completed": "tests", "tool.started": "tests", "tool.completed": "tests",
    "backend.connected": "status", "backend.disconnected": "errors",
    "validation.failed": "errors", "step.failed": "errors", "task.failed": "errors", "explain.failed": "errors",
}


def category(ev: dict) -> str:
    kind = ev["kind"]
    if kind in CATEGORY:
        if kind.endswith("completed") and ev.get("outcome") in ("failed", "timeout", "error"):
            return "errors"
        return CATEGORY[kind]
    if kind.startswith("step.") and ev.get("agent") in (LOCAL, CLAUDE):
        return "model"
    return "status"


# Workflow -----------------------------------------------------------------------


def _stage_status(steps: list[dict]) -> str:
    if any(s["status"] == "working" for s in steps):
        return "active"
    if any(s["status"] in ("waiting", "queued", "blocked") for s in steps):
        return "waiting"
    last = steps[-1]["status"]
    return {"completed": "completed", "failed": "failed", "cancelled": "cancelled", "skipped": "skipped",
            "interrupted": "blocked"}.get(last, "queued")


def workflow(task: dict, steps: list[dict]) -> list[dict]:
    """Stages of one task, from its real steps."""
    order: list[str] = ["request", "analysis"]
    for s in steps:
        if s["stage"] not in order and s["stage"] != "done":
            order.append(s["stage"])
    for key, _ in (TEAM_STAGES if task.get("workflow") == "team" else STAGES):
        if key not in order and key != "done":
            order.append(key)
    order.append("done")
    terminal = task["status"] in TERMINAL_STATUSES
    out = []
    for key in order:
        mine = [s for s in steps if s["stage"] == key]
        note = ""
        if key == "request":
            status = "completed"
            note = "Received" + (" and queued while new tasks are paused." if task["state"].get("queued_action") else ".")
        elif key == "done":
            status = {"completed": "completed", "failed": "failed", "cancelled": "cancelled"}.get(task["status"], "queued")
            if task["status"] == "interrupted":
                status = "blocked"
            note = task.get("summary", "") if terminal else ""
        elif not steps and task["status"] != "queued":
            # Tasks from before step tracking existed: their stages were never recorded.
            status, note = "unrecorded", "Not recorded (this task ran before step tracking was added)."
        elif mine:
            status = _stage_status(mine)
            last = mine[-1]
            note = last["result"] or last["error"]
        elif task.get("workflow") == "team" and key in ("repair", "plan"):
            status, note = ("skipped", "Not needed in this run.") if terminal else ("optional", "Only if needed.")
        elif task["kind"] not in ("edit", "auto") and key in EDIT_ONLY:
            status, note = "skipped", "Not needed: this task does not change files."
        elif terminal or task["status"] in ("awaiting_approval", "awaiting_claude") and key not in ("approval", "testing"):
            status, note = "skipped", "Not used in this task."
        elif key in ("claude", "review", "testing"):
            status, note = "optional", "Only used if needed."
        else:
            status = "queued"
        out.append({
            "key": key, "label": STAGE_LABEL.get(key, key), "status": status, "note": note,
            "agents": sorted({s["agent"] for s in mine}), "steps": [s["id"] for s in mine],
        })
    return out


def current_stage(task: dict, stages: list[dict]) -> dict | None:
    for wanted in ("active", "waiting", "blocked"):
        for st in stages:
            if st["status"] == wanted:
                return st
    if task["status"] in TERMINAL_STATUSES:
        return stages[-1]
    done = [st for st in stages if st["status"] == "completed"]
    return done[-1] if done else stages[0]


def stage_label(task: dict, cur: dict | None) -> str:
    if task["status"] in TERMINAL_STATUSES:
        return TASK_STATUS_LABEL[task["status"]]
    return cur["label"] if cur else ""


def next_action(task: dict, steps: list[dict], paused: bool, changesets: list[dict] | None = None) -> str:
    """What happens next, from state alone. Empty when nothing is known."""
    st = task["status"]
    open_steps = [s for s in steps if s["status"] in STEP_OPEN]
    if st == "queued":
        return "Starts when you resume new tasks." if paused and task["state"].get("queued_action") else "Starting."
    if st == "awaiting_claude":
        return "Waiting for you: send the prepared request to Claude, or keep the task with Qwen."
    if st == "awaiting_approval":
        return "Waiting for you: apply or discard the proposed change."
    if st == "interrupted":
        return "The app stopped while this ran. Resume to continue from the saved state."
    if st == "failed":
        return "Stopped. You can retry, or ask Claude."
    if st == "cancelled":
        return "Cancelled. Nothing else will run for this task."
    if any(s["stage"] == "testing" for s in open_steps):
        return "A check is running; its result will appear in the chat."
    if st == "running":
        cur = open_steps[-1] if open_steps else None
        if cur is None:
            return "Working."
        if cur["stage"] == "analysis":
            return "Enoy is picking the relevant files and choosing a model."
        if cur["agent"] == LOCAL and cur["status"] == "waiting":
            return "Waiting for Qwen to finish another request; then this task starts."
        who = AGENT_LABEL.get(cur["agent"], cur["agent"])
        if cur["stage"] == "review":
            return "Claude is reviewing Qwen's change. Next: you decide whether to apply it."
        if task["kind"] == "edit":
            after = "Claude reviews it" if task["workflow"] == "local_then_review" and cur["agent"] == LOCAL else "you review it"
            return f"{who} is writing the change. Next: Enoy checks it, then {after}."
        return f"{who} is answering. Next: the answer appears in the chat."
    run = task["state"].get("team_run") or {}
    if st == "completed" and run:
        if run.get("verification") == "passed":
            return "Done. The team's changes passed the project's checks."
        return "Done, but not verified by tests: " + (run.get("verification_note") or "the project has no test command.")[:200]
    if st == "completed":
        applied = any(cs["status"] == "applied" for cs in (changesets or []))
        tests = [s for s in steps if s["stage"] == "testing"]
        tested = bool(tests)
        if tests and tests[-1]["status"] == "failed":
            return "The last check failed. Use “Fix it” in the chat, or open the log to see why."
        if applied and not tested:
            return "Done. Optional: run the project's checks."
        return "Done. Nothing else is planned."
    return ""


def workflow_log(task: dict, steps: list[dict]) -> str:
    """Plain-text step log for the optional 'Explain workflow' request."""
    lines = [f"Task: {task['title']}", f"Status: {TASK_STATUS_LABEL.get(task['status'], task['status'])}",
             f"Routing reason: {task.get('routing_reason') or 'Reason not recorded.'}"]
    if task.get("escalation_reason"):
        lines.append(f"Escalation reason: {task['escalation_reason']}")
    for s in steps:
        line = f"- [{s['status']}] {AGENT_LABEL.get(s['agent'], s['agent'])}: {s['title']} (stage: {STAGE_LABEL.get(s['stage'], s['stage'])})"
        if s["reason"]:
            line += f". Why: {s['reason']}"
        if s["result"] or s["error"]:
            line += f". Result: {s['result'] or s['error']}"
        lines.append(line)
    return "\n".join(lines)[:6000]


# Agents ------------------------------------------------------------------------------


def _agent_state(name: str, status: dict, busy: dict | None, open_steps: list[dict], last: dict | None) -> str:
    if busy:
        return "working"
    if any(s["agent"] == name and s["status"] == "working" for s in open_steps):
        return "working"
    if any(s["agent"] == name for s in open_steps):
        return "waiting"
    if not status.get("available"):
        # No version: the program or service could not be reached at all. With a
        # version it runs but cannot take work (model missing, not signed in, limit).
        return "blocked" if status.get("version") and status.get("enabled", True) else "offline"
    if last and last["status"] == "failed" and last.get("task_status") == "failed":
        return "failed"  # Its failure stopped the task. A failure that was handed on is shown as an error note.
    return "available"


def agent_card(orc: Any, name: str, status: dict, open_steps: list[dict]) -> dict:
    db = orc.db
    busy = orc._busy.get(name)
    recent = db.recent_steps(name, 1)
    last = recent[0] if recent else None
    if last:
        t = db.get_task(last["task_id"])
        last["task_status"] = t["status"] if t else ""
    mine = [s for s in open_steps if s["agent"] == name]
    current = None
    if busy and busy.get("purpose") == "explain":
        t = db.get_task(busy["explaining"]) if busy.get("explaining") else None
        current = {"title": "Explaining a workflow (you asked)", "status": "working", "since": busy["since"],
                   "task_id": busy.get("explaining"), "task_title": t["title"] if t else "",
                   "reason": "Explain workflow button. Not part of a task."}
    else:
        step = None
        if busy and busy.get("step_id"):
            step = db.get_step(busy["step_id"])
        if step is None and mine:
            step = mine[-1]
        if step:
            current = {"step_id": step["id"], "task_id": step["task_id"], "task_title": step.get("task_title", ""),
                       "title": step["title"], "status": step["status"], "stage": step["stage"],
                       "stage_label": STAGE_LABEL.get(step["stage"], step["stage"]),
                       "reason": step["reason"] or "Reason not recorded.", "since": step["started"] or step["created"],
                       "files": step["files"][:12]}
            if step.get("task_title") is None:
                t = db.get_task(step["task_id"])
                current["task_title"] = t["title"] if t else ""
    if current and busy and name == LOCAL and busy.get("purpose") != "explain":
        current["chunks"] = busy.get("chunks", 0)  # Explanations are not streamed.
    waiting_for = ""
    for s in mine:
        if s["status"] == "waiting" and s["needs_approval"]:
            waiting_for = "Your OK to send the prepared request."
        elif s["status"] == "waiting":
            waiting_for = "Qwen to finish another request."
        elif s["status"] == "queued":
            waiting_for = "Its turn to start."
    done = [s for s in db.recent_steps(name, 3, ("completed",))]
    failed = db.recent_steps(name, 1, ("failed",))
    error = None
    if failed and (not done or failed[0]["id"] > done[0]["id"]):
        f = failed[0]
        t = db.get_task(f["task_id"])
        error = {"task_id": f["task_id"], "task_title": f["task_title"], "title": f["title"], "error": f["error"],
                 "at": f["finished"], "task_status": t["status"] if t else "",
                 "handed_on": bool(t and t["status"] != "failed" and t["owner"] != name)}
    card = {
        "name": name, "label": AGENT_LABEL[name],
        "model": status.get("model") or "", "connection": status, "state": _agent_state(name, status, busy, open_steps, last),
        "current": current, "waiting_for": waiting_for, "error": error,
        "recent": [{"task_id": s["task_id"], "task_title": s["task_title"], "title": s["title"], "result": s["result"],
                    "at": s["finished"]} for s in done],
    }
    sent = db.transmissions(None, name, 1)
    if sent:
        x = sent[0]
        card["last_sent"] = {"task_id": x["task_id"], "purpose": x["purpose"], "chars": x["chars"],
                             "files": [f["path"] for f in x["files"]], "at": x["created"], "usage": x["usage"],
                             "id": x["id"]}
    if name == CLAUDE:
        pending = []
        for t in db.tasks_with_status("awaiting_claude"):
            p = t["state"].get("pending_claude") or {}
            if p:
                pending.append({"task_id": t["id"], "task_title": t["title"], "purpose": p.get("purpose"),
                                "chars": p.get("chars", 0), "files": [f["path"] for f in p.get("files", [])]})
        card["pending"] = pending
    return card


# Snapshot ---------------------------------------------------------------------------------


def _task_row(orc: Any, task: dict, paused: bool) -> dict:
    db = orc.db
    steps = db.steps(task["id"])
    stages = workflow(task, steps)
    cur = current_stage(task, stages)
    changesets = db.list_changesets(task["id"])
    pending = [cs["id"] for cs in changesets if cs["status"] == "pending"]
    st = task["status"]
    return {
        "id": task["id"], "project_id": task["project_id"], "title": task["title"], "status": st,
        "status_label": TASK_STATUS_LABEL.get(st, st), "owner": task["owner"], "workflow": task["workflow"],
        "kind": task["kind"], "routing_reason": task["routing_reason"] or "Reason not recorded.",
        "escalation_reason": task["escalation_reason"], "local_attempts": task["local_attempts"],
        "claude_calls": task["claude_calls"], "created": task["created"], "started": task.get("started"),
        "finished": task.get("finished"), "updated": task["updated"],
        "stage": stage_label(task, cur), "next": next_action(task, steps, paused, changesets),
        "pending_changesets": pending,
        "relevant_files": [f["path"] for f in task["state"].get("relevant_files", [])],
        "test_results": task["state"].get("test_results", [])[-3:],
        "verification": task["state"].get("verification"),
        "initial_backend": task["state"].get("initial_backend"),
        "assign_reason": task["state"].get("assign_reason", ""),
        "local_only": privacy.local_only_reason(orc.cfg, db.get_project(task["project_id"]), task["state"]),
        "can": {
            "cancel": st in ("running", "queued", "awaiting_claude", "interrupted"),
            "retry": st in ("failed", "interrupted", "cancelled"),
            "approve_claude": st == "awaiting_claude",
            "review": bool(pending),
        },
    }


def _team_block(orc: Any, task: dict) -> dict:
    view = orc.team_view(task["id"])
    tasks = view["tasks"]
    keys = {t["id"]: t["plan_key"] for t in tasks}
    active_roles = sorted({t["role"] for t in tasks if t["status"] in ("running", "waiting", "awaiting_review")})
    run = view["run"]
    return {
        "status": run.get("status", ""), "acceptance": run.get("acceptance", []), "planned_by": run.get("planned_by", ""),
        "plan_note": run.get("plan_note", ""), "verification": run.get("verification"), "repairs": run.get("repairs", 0),
        "model_calls": run.get("model_calls", 0), "claude_calls": run.get("claude_calls", 0),
        "claude_request": run.get("claude_request"), "auto_apply": run.get("auto_apply", False),
        "roles_used": sorted({t["role"] for t in tasks}), "active_roles": active_roles,
        "running_now": [t["plan_key"] for t in tasks if t["status"] == "running"],
        "tasks": [{"id": t["id"], "key": t["plan_key"], "role": t["role"], "specialty": t["specialty"],
                   "title": t["title"], "provider": t["provider"], "status": t["status"], "error": t["error"],
                   "route_reason": t["route_reason"] or "Reason not recorded.", "activation": t["activation_reason"],
                   "after": [keys.get(d, f"#{d}") for d in t["depends_on"]], "files": t["files"][:8],
                   "changed": t["result"].get("changed", []), "findings": t["result"].get("findings", [])[:8],
                   "commands": [{"command": c["command"], "status": c["status"]} for c in t["result"].get("commands", [])]}
                  for t in tasks],
        "messages": [{"from": m["from_role"], "to": m["to_role"], "type": m["type"], "body": m["body"], "at": m["created"]}
                     for m in view["messages"][:25]],
    }


def snapshot(orc: Any, focus_id: int | None = None, task_filter: int | None = None, limit: int = 150,
             refresh: bool = False) -> dict:
    db = orc.db
    paused = orc.dispatch_paused()
    statuses = orc.backend_status(refresh)
    open_steps = db.open_steps()
    counts = db.task_counts()
    active = db.tasks_with_status("running", "queued", "awaiting_approval", "awaiting_claude", "interrupted")
    recent = [t for t in db.list_tasks(None, 12) if t["status"] in TERMINAL_STATUSES][:6]
    focus = db.get_task(focus_id) if focus_id else None
    if focus is None and active:
        focus = sorted(active, key=lambda t: t["updated"])[-1]
    if focus is None:
        latest = db.list_tasks(None, 1)
        focus = latest[0] if latest else None
    focus_block = None
    if focus:
        steps = db.steps(focus["id"])
        stages = workflow(focus, steps)
        cur = current_stage(focus, stages)
        by_id = {s["id"]: s for s in steps}
        for s in steps:
            s["stage_label"] = STAGE_LABEL.get(s["stage"], s["stage"])
            s["after"] = [by_id[d]["title"] for d in s["depends_on"] if d in by_id]
            s["reason"] = s["reason"] or "Reason not recorded."
        last_ev = db.activity(focus["id"], 1)
        focus_block = {
            "task": _task_row(orc, focus, paused), "objective": focus["objective"][:600],
            "stages": stages, "stage": stage_label(focus, cur), "steps": steps,
            "last_activity": last_ev[0] if last_ev else None,
            "route": {k: v for k, v in (focus["state"].get("route") or {}).items() if k != "context"},
            "handoffs": focus["state"].get("handoff_log", []),
            "team": _team_block(orc, focus) if focus["state"].get("team_run") else None,
            "team_on": bool(focus["state"].get("team")),
        }
    if task_filter == -1:  # "The task shown above", whichever one that turned out to be.
        task_filter = focus["id"] if focus else None
    events = db.activity(task_filter, limit)
    for ev in events:
        ev["category"] = category(ev)
    running = counts.get("running", 0)
    return {
        "generated": time.time(),
        "paused": paused,
        "execution": "paused" if paused else ("working" if running or orc._busy else "idle"),
        "counts": {
            "active": running,
            "queued": counts.get("queued", 0),
            "needs_you": counts.get("awaiting_approval", 0) + counts.get("awaiting_claude", 0),
            "stopped": counts.get("failed", 0) + counts.get("interrupted", 0),
            "completed": counts.get("completed", 0),
            "cancelled": counts.get("cancelled", 0),
        },
        "routing": orc.routing_stats(),
        "agents": [agent_card(orc, LOCAL, statuses[LOCAL], open_steps),
                   agent_card(orc, CLAUDE, statuses[CLAUDE], open_steps)],
        "focus": focus_block,
        "tasks": [_task_row(orc, t, paused) for t in sorted(active, key=lambda t: -t["updated"])]
                 + [_task_row(orc, t, paused) for t in recent],
        "events": events,
    }
