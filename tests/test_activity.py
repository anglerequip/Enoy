"""Activity panel: structured steps, events, workflow and agent state (fake backends only)."""

import sys
import threading
import time

from fusion_ai import activity
from fusion_ai.backends.base import CLAUDE, LOCAL, BackendError
from fusion_ai.orchestrator import EventBus, Orchestrator
from fusion_ai.storage import Storage

from test_orchestrator import ADD_MULTIPLY, open_project

PYTEST = f'"{sys.executable}" -m pytest -q'


def kinds(orc, task_id):
    return [e["kind"] for e in reversed(orc.db.activity(task_id))]


def stage(snap, key):
    return next(s for s in snap["focus"]["stages"] if s["key"] == key)


def card(snap, name):
    return next(a for a in snap["agents"] if a["name"] == name)


def no_model_calls(orc):
    return orc.backends[LOCAL].requests == [] and orc.backends[CLAUDE].requests == []


def test_qwen_only_task_is_recorded_step_by_step(orc_factory, project):
    orc = orc_factory(qwen=["divide() raises ZeroDivisionError when b is 0."])
    task = orc.start_task(open_project(orc, project), "Explain what divide does")
    k = kinds(orc, task["id"])
    for expected in ("task.created", "step.started", "task.assigned", "backend.request", "step.completed", "task.completed"):
        assert expected in k
    assert k.index("task.created") < k.index("task.assigned") < k.index("backend.request") < k.index("task.completed")
    steps = orc.db.steps(task["id"])
    assert [(s["agent"], s["stage"], s["status"]) for s in steps] == [
        ("system", "analysis", "completed"), (LOCAL, "local", "completed")]
    assert steps[1]["reason"] and steps[1]["depends_on"] == [steps[0]["id"]]
    assigned = next(e for e in orc.db.activity(task["id"]) if e["kind"] == "task.assigned")
    assert assigned["agent"] == LOCAL and assigned["reason"]  # A real routing reason, recorded when it was made.

    snap = activity.snapshot(orc, task["id"])
    assert stage(snap, "local")["status"] == "completed"
    assert stage(snap, "claude")["status"] == "skipped"
    assert stage(snap, "approval")["status"] == "skipped"  # Questions never change files.
    assert stage(snap, "done")["status"] == "completed"
    assert snap["counts"]["completed"] == 1 and snap["counts"]["active"] == 0
    assert card(snap, LOCAL)["recent"][0]["task_id"] == task["id"]
    assert card(snap, CLAUDE)["state"] == "available" and card(snap, CLAUDE)["current"] is None
    assert orc.backends[CLAUDE].requests == []


def test_escalation_records_reason_and_handoff(orc_factory, project):
    orc = orc_factory(qwen=["no blocks", "still no blocks"], claude=[ADD_MULTIPLY])
    task = orc.start_task(open_project(orc, project), "Add a multiply function to calculator.py")
    events = orc.db.activity(task["id"])
    esc = next(e for e in events if e["kind"] == "task.escalated")
    assert esc["agent"] == CLAUDE and "invalid" in esc["reason"]
    steps = orc.db.steps(task["id"])
    local = [s for s in steps if s["agent"] == LOCAL]
    assert [s["status"] for s in local] == ["failed", "failed"]
    assert local[1]["reason"].startswith("Retry")
    claude = next(s for s in steps if s["agent"] == CLAUDE)
    assert claude["status"] == "completed" and claude["reason"].startswith("Escalated")
    assert any(e["kind"] == "validation.failed" for e in events)

    snap = activity.snapshot(orc, task["id"])
    assert stage(snap, "local")["status"] == "failed"
    assert stage(snap, "claude")["status"] == "completed"
    assert stage(snap, "approval")["status"] == "waiting"
    assert snap["focus"]["task"]["escalation_reason"]
    assert card(snap, CLAUDE)["last_sent"]["files"]  # What was sent is visible.
    assert card(snap, LOCAL)["state"] == "available"  # Qwen is idle; its task now waits for you, not for Qwen.


def test_waiting_for_approval_then_applied(orc_factory, project):
    orc = orc_factory(qwen=[ADD_MULTIPLY])
    task = orc.start_task(open_project(orc, project), "Add a multiply function to calculator.py")
    approval = [s for s in orc.db.open_steps(task["id"])]
    assert len(approval) == 1 and approval[0]["agent"] == "user" and approval[0]["needs_approval"]
    snap = activity.snapshot(orc, task["id"])
    assert snap["counts"]["needs_you"] == 1
    row = snap["focus"]["task"]
    assert row["can"]["review"] and row["pending_changesets"] and "apply or discard" in row["next"]
    assert stage(snap, "patch")["status"] == "completed"

    orc.approve_changeset(row["pending_changesets"][0])
    assert orc.db.open_steps(task["id"]) == []
    k = kinds(orc, task["id"])
    assert "approval.resolved" in k and "tool.started" in k and "tool.completed" in k and k[-1] == "task.completed"
    resolved = next(e for e in orc.db.activity(task["id"]) if e["kind"] == "approval.resolved")
    assert resolved["outcome"] == "approved"
    snap = activity.snapshot(orc, task["id"])
    assert stage(snap, "approval")["status"] == "completed"
    assert stage(snap, "testing")["status"] == "skipped"
    assert "run the project's checks" in snap["focus"]["task"]["next"]


def test_failed_task_shows_error_on_qwen_card(orc_factory, project):
    orc = orc_factory(qwen=["bad", "bad"], routing__workflow="local_only")
    task = orc.start_task(open_project(orc, project), "Add a multiply function to calculator.py")
    assert orc.db.get_task(task["id"])["status"] == "failed"
    snap = activity.snapshot(orc, task["id"])
    qwen = card(snap, LOCAL)
    assert qwen["state"] == "failed" and qwen["error"]["task_id"] == task["id"]
    assert snap["counts"]["stopped"] == 1
    assert snap["focus"]["task"]["can"]["retry"] and not snap["focus"]["task"]["can"]["cancel"]
    assert stage(snap, "done")["status"] == "failed"
    assert any(e["category"] == "errors" for e in snap["events"])


def test_claude_gate_shows_waiting_and_decline_is_recorded(orc_factory, project):
    orc = orc_factory(qwen=["bad", "bad"], claude=[ADD_MULTIPLY], claude__confirm_before_send=True)
    task = orc.start_task(open_project(orc, project), "Add a multiply function to calculator.py")
    snap = activity.snapshot(orc, task["id"])
    claude = card(snap, CLAUDE)
    assert claude["state"] == "waiting" and claude["waiting_for"].startswith("Your OK")
    assert claude["pending"][0]["task_id"] == task["id"] and claude["pending"][0]["chars"] > 0
    assert "last_sent" not in claude  # Nothing has been sent yet.
    assert any(e["kind"] == "approval.requested" for e in orc.db.activity(task["id"]))

    orc.decline_claude(task["id"])
    step = next(s for s in orc.db.steps(task["id"]) if s["agent"] == CLAUDE)
    assert step["status"] == "skipped"
    assert orc.backends[CLAUDE].requests == []
    declined = next(e for e in orc.db.activity(task["id"]) if e["kind"] == "approval.resolved")
    assert declined["outcome"] == "declined" and declined["agent"] == "user"


def test_backends_offline_dashboard_still_works(orc_factory, project):
    orc = orc_factory(qwen=[], claude_available=False)
    orc.backends[LOCAL].available = False
    snap = activity.snapshot(orc)
    assert card(snap, LOCAL)["state"] == "offline" and card(snap, CLAUDE)["state"] == "offline"
    assert snap["focus"] is None and snap["execution"] == "idle"
    k = [e["kind"] for e in orc.db.activity()]
    assert k.count("backend.disconnected") == 2
    activity.snapshot(orc)  # Unchanged state does not log again.
    assert [e["kind"] for e in orc.db.activity()].count("backend.disconnected") == 2
    orc.backends[LOCAL].available = True
    activity.snapshot(orc, refresh=True)
    assert orc.db.activity()[0]["kind"] == "backend.connected"
    assert no_model_calls(orc)


def test_task_with_unavailable_model_is_not_left_running(orc_factory, project):
    orc = orc_factory(qwen=[BackendError("unavailable", "Cannot reach Ollama")], claude_available=False)
    task = orc.start_task(open_project(orc, project), "Explain divide")
    assert orc.db.get_task(task["id"])["status"] == "failed"
    assert orc.db.open_steps() == []
    assert card(activity.snapshot(orc), LOCAL)["current"] is None


def test_monitoring_never_calls_a_model(orc_factory, project):
    orc = orc_factory(qwen=["An answer"], claude=["should never be used"])
    task = orc.start_task(open_project(orc, project), "Explain divide")
    before = len(orc.backends[LOCAL].requests)
    for _ in range(25):
        activity.snapshot(orc, task["id"], refresh=True)
        activity.snapshot(orc, None, task["id"])
    assert len(orc.backends[LOCAL].requests) == before
    assert orc.backends[CLAUDE].requests == []


def test_restart_keeps_history_and_marks_interrupted(cfg, project, tmp_path):
    from conftest import FakeBackend

    db_path = tmp_path / "fusion.db"
    storage = Storage(db_path)
    pid = storage.upsert_project(str(project), "project")["id"]
    task = storage.create_task(pid, "Explain divide", "Explain divide", "ask")
    storage.update_task(task["id"], status="running", owner=LOCAL)
    step = storage.add_step(task["id"], "Answer", LOCAL, "local", "Routine question.")
    storage.add_activity(task["id"], "step.started", "Answer", LOCAL, "local", step_id=step["id"])
    storage.close()

    storage = Storage(db_path)  # Restart.
    assert storage.mark_interrupted() == [task["id"]]
    assert storage.get_step(step["id"])["status"] == "interrupted"
    orc = Orchestrator(storage, cfg, {LOCAL: FakeBackend(LOCAL, ["divide divides."]), CLAUDE: FakeBackend(CLAUDE)},
                       synchronous=True)
    snap = activity.snapshot(orc)
    assert snap["focus"]["task"]["id"] == task["id"] and snap["focus"]["task"]["status"] == "interrupted"
    assert stage(snap, "local")["status"] == "blocked"
    assert "Resume" in snap["focus"]["task"]["next"]
    assert [e["kind"] for e in reversed(storage.activity(task["id"]))][:2] == ["step.started", "task.interrupted"]
    assert card(snap, LOCAL)["state"] == "available"  # An interrupted step is not "working".

    orc.resume(task["id"])
    assert storage.get_task(task["id"])["status"] == "completed"
    assert "task.retry" in kinds(orc, task["id"])


def test_dashboard_updates_while_a_task_runs(cfg, project, tmp_path):
    from conftest import FakeBackend

    cfg["tools"]["online"] = False  # Streaming test: do not wait on Windows Credential Manager/search setup.
    release = threading.Event()

    def slow(request):
        request.on_token("partial ")
        release.wait(5)
        return "divide() divides."

    bus = EventBus()
    seen = bus.subscribe()
    orc = Orchestrator(Storage(":memory:"), cfg, {LOCAL: FakeBackend(LOCAL, [slow]), CLAUDE: FakeBackend(CLAUDE)},
                       bus, backups_dir=tmp_path)
    task = orc.start_task(orc.open_project(str(project))["id"], "Explain divide")
    for _ in range(100):
        if orc._busy.get(LOCAL, {}).get("chunks"):
            break
        time.sleep(0.02)
    snap = activity.snapshot(orc, task["id"])
    qwen = card(snap, LOCAL)
    assert qwen["state"] == "working" and qwen["current"]["task_id"] == task["id"]
    assert qwen["current"]["chunks"] >= 1 and qwen["current"]["reason"]
    assert stage(snap, "local")["status"] == "active" and snap["counts"]["active"] == 1
    assert snap["execution"] == "working"
    release.set()
    orc.wait_idle(5)
    snap = activity.snapshot(orc, task["id"])
    assert card(snap, LOCAL)["state"] == "available" and snap["counts"]["active"] == 0
    published = []
    while not seen.empty():
        published.append(seen.get_nowait())
    assert any(e["type"] == "activity" and e["event"]["kind"] == "task.completed" for e in published)


def test_cancel_reports_what_was_stopped(cfg, project, tmp_path):
    from conftest import FakeBackend

    def wait_for_cancel(request):
        for _ in range(100):
            if request.cancel.is_set():
                raise BackendError("cancelled", "Cancelled.")
            time.sleep(0.05)
        return "finished anyway"

    orc = Orchestrator(Storage(":memory:"), cfg, {LOCAL: FakeBackend(LOCAL, [wait_for_cancel]), CLAUDE: FakeBackend(CLAUDE)},
                       backups_dir=tmp_path)
    task = orc.start_task(orc.open_project(str(project))["id"], "Explain divide")
    for _ in range(100):
        if LOCAL in orc._busy:
            break
        time.sleep(0.02)
    orc.cancel(task["id"])
    orc.wait_idle(5)
    events = orc.db.activity(task["id"])
    requested = next(e for e in events if e["kind"] == "task.cancel_requested")
    assert "Ollama" in requested["text"]
    assert events[0]["kind"] in ("task.cancelled", "step.cancelled") or "task.cancelled" in kinds(orc, task["id"])
    assert orc.db.open_steps() == [] and LOCAL not in orc._busy


def test_pause_queues_new_tasks_across_restart(cfg, project, tmp_path):
    from conftest import FakeBackend

    db_path = tmp_path / "fusion.db"
    backends = {LOCAL: FakeBackend(LOCAL, ["divide divides."]), CLAUDE: FakeBackend(CLAUDE)}
    orc = Orchestrator(Storage(db_path), cfg, backends, synchronous=True)
    pid = orc.open_project(str(project))["id"]
    orc.set_dispatch_paused(True)
    task = orc.start_task(pid, "Explain divide")
    assert orc.db.get_task(task["id"])["status"] == "queued"
    assert backends[LOCAL].requests == []
    snap = activity.snapshot(orc, task["id"])
    assert snap["paused"] and snap["counts"]["queued"] == 1
    assert "resume" in snap["focus"]["task"]["next"]
    orc.db.close()

    storage = Storage(db_path)  # Restart: the queued task did not start, so it is not "interrupted".
    assert storage.mark_interrupted() == []
    orc = Orchestrator(storage, cfg, backends, synchronous=True)
    assert orc.dispatch_paused()
    orc.set_dispatch_paused(False)
    assert storage.get_task(task["id"])["status"] == "completed"
    assert "task.dispatched" in kinds(orc, task["id"])


def test_check_run_is_a_testing_step(orc_factory, project):
    orc = orc_factory(qwen=[ADD_MULTIPLY])
    pid = open_project(orc, project)
    task = orc.start_task(pid, "Add a multiply function to calculator.py")
    orc.approve_changeset(orc.db.list_changesets(task["id"], "pending")[0]["id"])
    orc.run_command(pid, PYTEST, approved=True, task_id=task["id"])
    step = orc.db.steps(task["id"])[-1]
    assert step["stage"] == "testing" and step["status"] == "completed" and "Passed" in step["result"]
    k = kinds(orc, task["id"])
    assert "test.started" in k and "test.completed" in k
    snap = activity.snapshot(orc, task["id"])
    assert stage(snap, "testing")["status"] == "completed"
    assert snap["focus"]["task"]["next"] == "Done. Nothing else is planned."

    orc.run_command(pid, f'"{sys.executable}" -c "raise SystemExit(3)"', approved=True, task_id=task["id"])
    snap = activity.snapshot(orc, task["id"])
    assert stage(snap, "testing")["status"] == "failed"
    assert "check failed" in snap["focus"]["task"]["next"]


def test_explain_workflow_is_one_local_request_outside_the_task(orc_factory, project):
    orc = orc_factory(qwen=["divide divides.", "Qwen answered your question locally."], claude=["never"])
    task = orc.start_task(open_project(orc, project), "Explain divide")
    steps_before = len(orc.db.steps(task["id"]))
    out = orc.explain_workflow(task["id"])
    assert out["text"] == "Qwen answered your question locally."
    prompt = orc.backends[LOCAL].requests[-1].messages[-1].content
    assert "Answer" in prompt and "divide divides" not in prompt  # Step log only, no chat content.
    assert len(orc.db.steps(task["id"])) == steps_before
    assert "explain.completed" in kinds(orc, task["id"])
    assert orc.backends[CLAUDE].requests == []


def test_explain_refuses_while_qwen_is_busy(orc_factory, project):
    orc = orc_factory(qwen=["divide divides."])
    task = orc.start_task(open_project(orc, project), "Explain divide")
    orc._local_lock.acquire()
    try:
        try:
            orc.explain_workflow(task["id"])
            raise AssertionError("expected TaskError")
        except Exception as exc:
            assert "busy" in str(exc)
    finally:
        orc._local_lock.release()


def test_workflow_order_follows_real_steps(orc_factory, project):
    orc = orc_factory(qwen=[ADD_MULTIPLY], claude=["1. Add multiply after subtract."], routing__workflow="claude_plans")
    task = orc.start_task(open_project(orc, project), "Add a multiply function to calculator.py")
    keys = [s["key"] for s in activity.snapshot(orc, task["id"])["focus"]["stages"]]
    assert keys.index("claude") < keys.index("local")  # Claude planned before Qwen wrote.
    qwen_step = next(s for s in orc.db.steps(task["id"]) if s["agent"] == LOCAL)
    assert "plan" in qwen_step["reason"]
