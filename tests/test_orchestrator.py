"""End-to-end orchestration with scripted fake backends (no model or cloud calls)."""

import sys
import time

from fusion_ai.backends.base import CLAUDE, LOCAL, BackendError
from fusion_ai.orchestrator import Orchestrator
from fusion_ai.storage import Storage

ADD_MULTIPLY = """Adding multiply.
<<<EDIT calculator.py
<<<SEARCH
def subtract(a, b):
    return a - b
===
def subtract(a, b):
    return a - b


def multiply(a, b):
    return a * b
>>>REPLACE
>>>END
"""

BREAK_SUBTRACT = """<<<EDIT calculator.py
<<<SEARCH
    return a - b
===
    return a + b
>>>REPLACE
>>>END
"""

HARMLESS_COMMENT = """<<<EDIT calculator.py
<<<SEARCH
\"\"\"Small arithmetic helpers.\"\"\"
===
\"\"\"Small arithmetic helpers (checked).\"\"\"
>>>REPLACE
>>>END
"""

FIX_SUBTRACT = """<<<EDIT calculator.py
<<<SEARCH
def subtract(a, b):
    return a + b
===
def subtract(a, b):
    return a - b
>>>REPLACE
>>>END
"""

PYTEST = f'"{sys.executable}" -m pytest -q'


def open_project(orc, project):
    return orc.open_project(str(project))["id"]


def test_local_question_completes_without_claude(orc_factory, project):
    orc = orc_factory(qwen=["divide() raises ZeroDivisionError when b is 0."])
    pid = open_project(orc, project)
    task = orc.start_task(pid, "Explain what divide does")
    task = orc.db.get_task(task["id"])
    assert task["status"] == "completed" and task["owner"] == LOCAL and task["kind"] == "ask"
    assert orc.backends[CLAUDE].requests == []
    msgs = orc.db.messages(task["id"])
    assert msgs[-1]["backend"] == LOCAL and "ZeroDivisionError" in msgs[-1]["content"]
    assert orc.db.transmissions(task["id"], LOCAL)


def test_edit_needs_approval_then_applies(orc_factory, project):
    orc = orc_factory(qwen=[ADD_MULTIPLY])
    pid = open_project(orc, project)
    before = (project / "calculator.py").read_text(encoding="utf-8")
    task = orc.start_task(pid, "Add a multiply function to calculator.py")
    task = orc.db.get_task(task["id"])
    assert task["status"] == "awaiting_approval"
    assert (project / "calculator.py").read_text(encoding="utf-8") == before  # Nothing written yet.
    cs = orc.db.list_changesets(task["id"], "pending")[0]
    assert cs["backend"] == LOCAL and "+def multiply" in cs["files"][0]["diff"]
    orc.approve_changeset(cs["id"])
    assert "def multiply" in (project / "calculator.py").read_text(encoding="utf-8")
    assert orc.db.get_task(task["id"])["status"] == "completed"
    orc.revert_changeset(cs["id"])
    assert (project / "calculator.py").read_text(encoding="utf-8") == before


def test_reject_records_reason(orc_factory, project):
    orc = orc_factory(qwen=[ADD_MULTIPLY])
    pid = open_project(orc, project)
    task = orc.start_task(pid, "Add a multiply function")
    cs = orc.db.list_changesets(task["id"], "pending")[0]
    orc.reject_changeset(cs["id"], "not needed")
    assert orc.db.get_changeset(cs["id"])["status"] == "rejected"
    assert "def multiply" not in (project / "calculator.py").read_text(encoding="utf-8")
    assert any("not needed" in f for f in orc.db.get_task(task["id"])["state"]["failures"])


def test_invalid_local_output_is_retried_with_feedback(orc_factory, project):
    orc = orc_factory(qwen=["Sure, just add a multiply function!", ADD_MULTIPLY])
    pid = open_project(orc, project)
    task = orc.start_task(pid, "Add a multiply function to calculator.py")
    task = orc.db.get_task(task["id"])
    assert task["status"] == "awaiting_approval" and task["local_attempts"] == 2
    second = orc.backends[LOCAL].requests[1].messages[-1].content
    assert "could not be applied" in second
    assert orc.backends[CLAUDE].requests == []


def test_escalates_to_claude_after_repeated_local_failure(orc_factory, project):
    orc = orc_factory(qwen=["no blocks", "still no blocks"], claude=[ADD_MULTIPLY])
    pid = open_project(orc, project)
    task = orc.start_task(pid, "Add a multiply function to calculator.py")
    task = orc.db.get_task(task["id"])
    assert task["owner"] == CLAUDE and "invalid" in task["escalation_reason"]
    assert task["status"] == "awaiting_approval"
    assert orc.db.list_changesets(task["id"], "pending")[0]["backend"] == CLAUDE
    prompt = orc.backends[CLAUDE].requests[0].messages[-1].content
    assert "Handoff from Qwen" in prompt and "What failed in the last attempt" in prompt
    assert "fake-test-value" not in prompt  # .env is never sent.
    events = [e["text"] for e in orc.db.events(task["id"]) if e["type"] == "owner"]
    assert any("Claude" in e for e in events)  # The switch is announced, not silent.


def test_retry_limit_is_respected_local_only(orc_factory, project):
    orc = orc_factory(qwen=["bad"] * 10, routing__max_local_attempts=3, routing__workflow="local_only")
    pid = open_project(orc, project)
    task = orc.start_task(pid, "Add a multiply function")
    task = orc.db.get_task(task["id"])
    assert task["status"] == "failed"
    assert len(orc.backends[LOCAL].requests) == 3
    assert orc.backends[CLAUDE].requests == []


def test_no_escalation_when_claude_unavailable(orc_factory, project):
    orc = orc_factory(qwen=["bad", "bad"], claude=[ADD_MULTIPLY], claude_available=False)
    pid = open_project(orc, project)
    task = orc.db.get_task(orc.start_task(pid, "Add a multiply function")["id"])
    assert task["status"] == "failed"
    assert orc.backends[CLAUDE].requests == []
    assert "could not produce a valid change" in task["summary"]


def test_manual_claude_override_when_unavailable_does_not_fall_back(orc_factory, project):
    orc = orc_factory(qwen=["should not be used"], claude_available=False)
    pid = open_project(orc, project)
    task = orc.db.get_task(orc.start_task(pid, "Explain divide", backend=CLAUDE)["id"])
    assert task["status"] == "failed" and "not available" in task["summary"]
    assert orc.backends[LOCAL].requests == []


def test_manual_local_override_for_complex_task(orc_factory, project):
    orc = orc_factory(qwen=["An answer"])
    pid = open_project(orc, project)
    t = orc.start_task(pid, "Design the architecture for secure authentication and encryption", backend=LOCAL)
    t = orc.db.get_task(t["id"])
    assert t["owner"] == LOCAL and t["status"] == "completed"
    assert orc.backends[CLAUDE].requests == []


def test_confirmation_gate_before_claude(orc_factory, project):
    orc = orc_factory(qwen=["bad", "bad"], claude=[ADD_MULTIPLY], claude__confirm_before_send=True)
    pid = open_project(orc, project)
    task_id = orc.start_task(pid, "Add a multiply function to calculator.py")["id"]
    task = orc.db.get_task(task_id)
    assert task["status"] == "awaiting_claude"
    assert orc.backends[CLAUDE].requests == []  # Nothing sent before approval.
    pending = task["state"]["pending_claude"]
    assert pending["chars"] > 0 and any(f["path"] == "calculator.py" for f in pending["files"])
    orc.approve_claude(task_id)
    assert len(orc.backends[CLAUDE].requests) == 1
    assert orc.db.get_task(task_id)["status"] == "awaiting_approval"
    trans = orc.db.transmissions(task_id, CLAUDE)
    assert len(trans) == 1 and trans[0]["files"]


def test_declining_claude_sends_nothing(orc_factory, project):
    orc = orc_factory(qwen=["bad", "bad"], claude=[ADD_MULTIPLY], claude__confirm_before_send=True)
    pid = open_project(orc, project)
    task_id = orc.start_task(pid, "Add a multiply function")["id"]
    orc.decline_claude(task_id)
    assert orc.backends[CLAUDE].requests == []
    assert orc.db.transmissions(task_id, CLAUDE) == []
    assert orc.db.get_task(task_id)["status"] == "failed"


def test_declining_claude_after_applied_change_keeps_task_completed(orc_factory, project):
    orc = orc_factory(qwen=[ADD_MULTIPLY], claude__confirm_before_send=True)
    pid = open_project(orc, project)
    task_id = orc.start_task(pid, "Add a multiply function")["id"]
    orc.approve_changeset(orc.db.list_changesets(task_id, "pending")[0]["id"])
    orc.ask_claude(task_id)
    assert orc.db.get_task(task_id)["status"] == "awaiting_claude"
    orc.decline_claude(task_id)
    task = orc.db.get_task(task_id)
    assert task["status"] == "completed" and "stays in place" in task["summary"]
    assert task["owner"] == LOCAL  # Claude never ran, so it is not shown as the owner.
    assert orc.backends[CLAUDE].requests == []


def test_claude_usage_limit_is_handled(orc_factory, project):
    orc = orc_factory(qwen=["bad", "bad"], claude=[BackendError("limit", "Claude usage limit reached")])
    pid = open_project(orc, project)
    task = orc.db.get_task(orc.start_task(pid, "Add a multiply function")["id"])
    assert task["status"] == "failed" and "usage limit" in task["summary"]
    assert orc.claude_available()[0] is False  # Remembered, so later tasks stay local.
    assert task["state"]["limitations"]


def test_review_workflow_bounded_rounds(orc_factory, project):
    orc = orc_factory(
        qwen=[BREAK_SUBTRACT, ADD_MULTIPLY],
        claude=["VERDICT: CHANGES_NEEDED\n- subtract now adds; revert that and add multiply instead."],
        routing__workflow="local_then_review", routing__max_review_rounds=1,
    )
    pid = open_project(orc, project)
    task_id = orc.start_task(pid, "Add a multiply function to calculator.py")["id"]
    sets = orc.db.list_changesets(task_id)
    statuses = sorted(c["status"] for c in sets)
    assert statuses == ["pending", "superseded"]
    assert sets[-1]["review"].startswith("VERDICT: CHANGES_NEEDED")  # The first set carries the review.
    assert len(orc.backends[CLAUDE].requests) == 1  # No second review: round limit.
    assert "VERDICT" in orc.backends[CLAUDE].requests[0].messages[-1].content
    assert "reviewer found problems" in orc.backends[LOCAL].requests[1].messages[-1].content


def test_claude_plans_qwen_implements(orc_factory, project):
    orc = orc_factory(qwen=[ADD_MULTIPLY], claude=["1. In calculator.py add multiply(a, b) after subtract."],
                      routing__workflow="claude_plans")
    pid = open_project(orc, project)
    task_id = orc.start_task(pid, "Add a multiply function to calculator.py")["id"]
    task = orc.db.get_task(task_id)
    assert task["status"] == "awaiting_approval"
    assert orc.db.list_changesets(task_id, "pending")[0]["backend"] == LOCAL
    assert "implementation plan" in orc.backends[LOCAL].requests[0].messages[-1].content
    assert task["state"]["plan"].startswith("1. In calculator.py")


def test_compare_workflow_offers_both(orc_factory, project):
    orc = orc_factory(qwen=[ADD_MULTIPLY], claude=[HARMLESS_COMMENT], routing__workflow="compare")
    pid = open_project(orc, project)
    task_id = orc.start_task(pid, "Add a multiply function to calculator.py")["id"]
    pending = orc.db.list_changesets(task_id, "pending")
    assert {c["backend"] for c in pending} == {LOCAL, CLAUDE}
    orc.approve_changeset(pending[0]["id"])
    assert orc.db.get_changeset(pending[1]["id"])["status"] == "superseded"


def test_failed_check_fix_loop_escalates_after_limit(orc_factory, project):
    orc = orc_factory(qwen=[BREAK_SUBTRACT, HARMLESS_COMMENT], claude=[FIX_SUBTRACT], routing__max_test_fix_attempts=2)
    pid = open_project(orc, project)
    task_id = orc.start_task(pid, "Change subtract", kind="edit")["id"]
    orc.approve_changeset(orc.db.list_changesets(task_id, "pending")[0]["id"])

    run = orc.run_command(pid, PYTEST, task_id=task_id)
    assert run["needs_approval"] is False  # pytest is allowlisted.
    first = orc.db.get_command_run(run["run_id"])
    assert first["status"] == "failed" and first["exit_code"] != 0

    orc.fix_failure(task_id, first["id"])  # Attempt 1: local.
    assert orc.db.get_task(task_id)["owner"] == LOCAL
    orc.approve_changeset(orc.db.list_changesets(task_id, "pending")[0]["id"])
    second = orc.db.get_command_run(orc.run_command(pid, PYTEST, task_id=task_id)["run_id"])
    assert second["status"] == "failed"

    orc.fix_failure(task_id, second["id"])  # Attempt 2: limit reached, escalate.
    task = orc.db.get_task(task_id)
    assert task["owner"] == CLAUDE and ("Checks still fail" in task["escalation_reason"] or "same error" in task["escalation_reason"])
    assert "failed (exit code" in orc.backends[CLAUDE].requests[0].messages[-1].content
    orc.approve_changeset(orc.db.list_changesets(task_id, "pending")[0]["id"])
    third = orc.db.get_command_run(orc.run_command(pid, PYTEST, task_id=task_id)["run_id"])
    assert third["status"] == "passed" and third["exit_code"] == 0


def test_risky_command_needs_approval_and_blocked_never_runs(orc_factory, project):
    orc = orc_factory()
    pid = open_project(orc, project)
    r = orc.run_command(pid, f'"{sys.executable}" -c "print(1)"')
    assert r["needs_approval"] is True and r["policy"]["level"] == "approval"
    assert orc.db.list_command_runs(pid) == []
    r = orc.run_command(pid, f'"{sys.executable}" -c "print(1)"', approved=True)
    assert orc.db.get_command_run(r["run_id"])["status"] == "passed"
    import pytest as _pytest
    from fusion_ai.orchestrator import TaskError
    with _pytest.raises(TaskError):
        orc.run_command(pid, "format C:", approved=True)


def test_secrets_are_redacted_before_claude(orc_factory, project):
    (project / "settings.py").write_text('API_KEY = "sk-ant-api03-REALLOOKINGKEY1234567890"\nDEBUG = True\n', encoding="utf-8")
    orc = orc_factory(claude=["Looks fine."])
    pid = open_project(orc, project)
    task_id = orc.start_task(pid, "Review settings.py", backend=CLAUDE)["id"]
    prompt = orc.backends[CLAUDE].requests[0].messages[-1].content
    assert "settings.py" in prompt
    assert "REALLOOKINGKEY" not in prompt and "[REDACTED]" in prompt
    stored = orc.db.transmissions(task_id, CLAUDE)[0]["content"]
    assert "REALLOOKINGKEY" not in stored


def test_claude_followup_does_not_resend_unchanged_files(orc_factory, project):
    orc = orc_factory(claude=["First answer.", "Second answer."])
    pid = open_project(orc, project)
    task_id = orc.start_task(pid, "Explain calculator.py divide", backend=CLAUDE)["id"]
    first = orc.backends[CLAUDE].requests[0]
    assert "def divide" in first.messages[-1].content and first.resume_session is None
    orc.send_message(task_id, "And subtract?", backend=CLAUDE)
    second = orc.backends[CLAUDE].requests[1]
    assert second.resume_session == "sess-1"
    assert "def divide" not in second.messages[-1].content
    assert "already have" in second.messages[-1].content


def test_local_model_offline_fails_clearly(orc_factory, project):
    orc = orc_factory(qwen=[BackendError("unavailable", "Cannot reach Ollama. Is Ollama running?")],
                      routing__workflow="local_only")
    pid = open_project(orc, project)
    task = orc.db.get_task(orc.start_task(pid, "Explain divide")["id"])
    assert task["status"] == "failed" and "Is Ollama running" in task["summary"]
    assert len(orc.backends[LOCAL].requests) == 1  # No pointless retries when the service is down.


def test_timeouts_and_malformed_responses_retry_then_stop(orc_factory, project):
    orc = orc_factory(qwen=[BackendError("timeout", "too slow"), BackendError("malformed", "garbage")],
                      routing__workflow="local_only")
    pid = open_project(orc, project)
    task = orc.db.get_task(orc.start_task(pid, "Explain divide")["id"])
    assert task["status"] == "failed" and len(orc.backends[LOCAL].requests) == 2


def test_cancellation(cfg, project, tmp_path):
    from conftest import FakeBackend

    def wait_for_cancel(request):
        for _ in range(100):
            if request.cancel.is_set():
                raise BackendError("cancelled", "Cancelled.")
            time.sleep(0.05)
        return "finished anyway"

    backends = {LOCAL: FakeBackend(LOCAL, [wait_for_cancel]), CLAUDE: FakeBackend(CLAUDE)}
    orc = Orchestrator(Storage(":memory:"), cfg, backends, backups_dir=tmp_path)
    pid = orc.open_project(str(project))["id"]
    task_id = orc.start_task(pid, "Explain divide")["id"]
    time.sleep(0.4)
    orc.cancel(task_id)
    orc.wait_idle(10)
    assert orc.db.get_task(task_id)["status"] == "cancelled"


def test_task_state_survives_restart_and_resumes(cfg, project, tmp_path):
    from conftest import FakeBackend

    db_path = tmp_path / "fusion.db"
    storage = Storage(db_path)
    pid = storage.upsert_project(str(project), "project")["id"]
    task = storage.create_task(pid, "Explain divide", "Explain divide", "ask")
    storage.update_task(task["id"], status="running", owner=LOCAL)
    storage.update_task_state(task["id"], decisions=["Use the local model"])
    storage.close()

    storage = Storage(db_path)  # "Restart".
    assert storage.mark_interrupted() == [task["id"]]
    restored = storage.get_task(task["id"])
    assert restored["status"] == "interrupted" and restored["state"]["decisions"] == ["Use the local model"]

    orc = Orchestrator(storage, cfg, {LOCAL: FakeBackend(LOCAL, ["divide divides."]), CLAUDE: FakeBackend(CLAUDE)},
                       backups_dir=tmp_path, synchronous=True)
    brief = orc.continuation_brief(task["id"])
    assert "Explain divide" in brief and "Use the local model" in brief
    orc.resume(task["id"])
    assert storage.get_task(task["id"])["status"] == "completed"
    assert "resuming an interrupted task" in orc.backends[LOCAL].requests[0].messages[-1].content


def test_auto_apply_setting(orc_factory, project):
    orc = orc_factory(qwen=[ADD_MULTIPLY], permissions__auto_apply_patches=True)
    pid = open_project(orc, project)
    task = orc.db.get_task(orc.start_task(pid, "Add a multiply function")["id"])
    assert task["status"] == "completed"
    assert "def multiply" in (project / "calculator.py").read_text(encoding="utf-8")


def test_concurrent_user_edit_blocks_apply(orc_factory, project):
    orc = orc_factory(qwen=[ADD_MULTIPLY])
    pid = open_project(orc, project)
    task_id = orc.start_task(pid, "Add a multiply function")["id"]
    (project / "calculator.py").write_text("# the user changed this\n", encoding="utf-8")
    cs = orc.db.list_changesets(task_id, "pending")[0]
    import pytest as _pytest
    from fusion_ai.orchestrator import TaskError
    with _pytest.raises(TaskError):
        orc.approve_changeset(cs["id"])
    assert (project / "calculator.py").read_text(encoding="utf-8") == "# the user changed this\n"
