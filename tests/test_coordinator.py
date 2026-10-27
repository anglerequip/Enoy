"""Full collaboration workflow with scripted providers and real file/test tools."""
import json
import sys
import threading

import pytest

from conftest import FakeBackend
from test_lead import delegate, plan_task
from test_orchestrator import ADD_MULTIPLY, open_project
from enoy.backends.base import CODEX, CLAUDE, LOCAL, BackendError, ChatMessage, ChatRequest
from enoy.backends.codex import CodexBackend
from enoy.project import Workspace


def setup(orc_factory, codex, claude=None, **kwargs):
    orc = orc_factory(claude=claude, coordinator__enabled=True, permissions__auto_apply_patches=True, **kwargs)
    orc.backends[CODEX] = FakeBackend(CODEX, codex)
    return orc


def test_chatgpt_plans_claude_edits_real_checks_chatgpt_reviews(orc_factory, project, monkeypatch):
    monkeypatch.setattr(Workspace, "suggest_test_commands", lambda self: [f'"{sys.executable}" -m pytest -q'])
    def final(request):
        text = request.messages[-1].content
        assert "Shared state snapshot" in text and "calculator.py" in text
        assert "Checks: passed (real test run)" in text
        assert "+def multiply" in text and '"provider": "claude"' in text
        return "Added multiply; the actual tests passed.\nNEXT: done"
    orc = setup(orc_factory, [delegate(plan_task(model=CLAUDE)), final], [ADD_MULTIPLY])
    task = orc.start_task(open_project(orc, project), "Add multiply")
    saved = orc.db.get_task(task["id"])
    assert saved["status"] == "completed", saved["summary"]
    assert saved["workflow"] == "chatgpt_leads" and saved["owner"] == CODEX
    assert "def multiply" in (project / "calculator.py").read_text()
    assert not orc.backends[LOCAL].requests
    assert "Shared state snapshot" in orc.backends[CLAUDE].requests[0].messages[-1].content
    assert orc.db.messages(task["id"])[-1]["backend"] == CODEX


def test_codex_can_implement_and_followup_sees_chat(orc_factory, project):
    orc = setup(orc_factory, [delegate(plan_task(model=CODEX)), ADD_MULTIPLY, "Done.\nNEXT: done", "MODE: answer\nIt multiplies."])
    task = orc.start_task(open_project(orc, project), "Add multiply")
    assert orc.db.get_task(task["id"])["status"] == "completed"
    orc.send_message(task["id"], "What did you change?")
    assert orc.db.get_task(task["id"])["status"] == "completed"
    prompt = orc.backends[CODEX].requests[-1].messages[-1].content
    assert "What did you change?" in prompt and "Add multiply" in prompt
    assert not orc.backends[CLAUDE].requests and not orc.backends[LOCAL].requests


def test_existing_change_approval_resumes_coordinator(orc_factory, project):
    orc = setup(orc_factory, [delegate(plan_task(model=CODEX)), ADD_MULTIPLY, "Done.\nNEXT: done"])
    orc.cfg["permissions"]["auto_apply_patches"] = False
    task = orc.start_task(open_project(orc, project), "Add multiply")
    assert orc.db.get_task(task["id"])["status"] == "awaiting_approval"
    cs = orc.db.list_changesets(task["id"], "pending")[0]
    assert "def multiply" not in (project / "calculator.py").read_text()
    orc.approve_changeset(cs["id"])
    assert orc.db.get_task(task["id"])["status"] == "completed"
    assert "def multiply" in (project / "calculator.py").read_text()


def test_local_only_never_calls_coordinator(orc_factory, project):
    orc = setup(orc_factory, [], qwen=["Local answer."])
    task = orc.start_task(open_project(orc, project), "What does add do?", local_only=True)
    assert orc.db.get_task(task["id"])["status"] == "completed"
    assert not orc.backends[CODEX].requests


def test_shared_history_scrubs_private_file_quotes(orc_factory, project):
    secret = "A private business instruction that must stay on this computer."
    (project / "private.txt").write_text(secret)
    orc = setup(orc_factory, ["MODE: answer\nHello.", "MODE: answer\nFine."])
    orc.cfg["privacy"]["cloud_excluded_globs"] = ["private.txt"]
    task = orc.start_task(open_project(orc, project), "Hello")
    orc.db.add_message(task["id"], "assistant", secret, LOCAL)
    orc.send_message(task["id"], "Continue")
    assert secret not in orc.backends[CODEX].requests[-1].messages[-1].content


def test_codex_adapter_uses_official_jsonl_without_shell(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    fake = tmp_path / "fake_cli.py"
    fake.write_text('''import sys, json
assert "--sandbox" in sys.argv and "read-only" in sys.argv
assert "--ignore-user-config" in sys.argv
assert "features.shell_tool=false" in sys.argv
assert "hello" in sys.stdin.read()
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"hello back"}}))
print(json.dumps({"type":"turn.completed","usage":{"output_tokens":2}}))
''')
    backend = CodexBackend({"enabled": True, "timeout_s": 10}, [sys.executable, str(fake)])
    result = backend.chat(ChatRequest([ChatMessage("user", "hello")]))
    assert result.text == "hello back" and result.usage == {"output_tokens": 2}
    assert backend.verified
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(BackendError, match="cancelled"):
        backend.chat(ChatRequest([], cancel=cancel))


def test_codex_reads_more_files_without_exposing_excluded_files(orc_factory, project):
    (project / "notes.txt").write_text("A shared implementation note.")
    (project / "private.txt").write_text("Private content must never leave this computer.")
    orc = setup(orc_factory, ["<<<READ notes.txt>>>\n<<<READ private.txt>>>", "MODE: answer\nRead the note."])
    orc.cfg["context"]["max_files"] = 1
    orc.cfg["privacy"]["cloud_excluded_globs"] = ["private.txt"]
    task = orc.start_task(open_project(orc, project), "Hello")
    assert orc.db.get_task(task["id"])["status"] == "completed"
    prompt = orc.backends[CODEX].requests[-1].messages[-1].content
    assert "A shared implementation note." in prompt
    assert "Private content must never leave this computer." not in prompt


def test_both_workers_see_applied_dependency_results(orc_factory, project):
    tasks = [plan_task(model=CLAUDE), plan_task("T2", "Document multiply", ("MULTIPLY.md",),
                                             deps=("T1",), model=CODEX)]
    def document(request):
        prompt = request.messages[-1].content
        assert '"provider": "claude"' in prompt and '"status": "completed"' in prompt
        assert "def multiply" in (project / "calculator.py").read_text()
        return "<<<FILE MULTIPLY.md\nmultiply(a, b) returns the product.\n>>>END"
    orc = setup(orc_factory, [delegate(*tasks), document, "Both tasks finished.\nNEXT: done"], [ADD_MULTIPLY])
    task = orc.start_task(open_project(orc, project), "Add multiply and document it")
    assert orc.db.get_task(task["id"])["status"] == "completed"
    assert (project / "MULTIPLY.md").exists()


def test_unresolved_final_review_does_not_claim_completion(orc_factory, project):
    orc = setup(orc_factory, [delegate(plan_task(model=CODEX)), ADD_MULTIPLY,
                              "Missing an acceptance condition.\nNEXT: fix Add its edge cases"])
    orc.cfg["lead"]["max_followups"] = 0
    task = orc.start_task(open_project(orc, project), "Add multiply")
    saved = orc.db.get_task(task["id"])
    assert saved["status"] == "failed" and "unresolved" in saved["summary"]


def test_claude_quota_failure_hands_work_to_codex(orc_factory, project):
    orc = setup(orc_factory, [delegate(plan_task(model=CLAUDE)), ADD_MULTIPLY, "Codex completed it.\nNEXT: done"],
                [BackendError("limit", "You've hit your weekly limit")])
    task = orc.start_task(open_project(orc, project), "Add multiply")
    assert orc.db.get_task(task["id"])["status"] == "completed"
    dev = next(t for t in orc.db.team_tasks(task["id"]) if t["role"] == "developer")
    assert dev["provider"] == CODEX and "takes over" in dev["route_reason"]
    assert any(e["kind"] == "task.handoff" for e in orc.db.activity(task["id"]))


@pytest.mark.parametrize("claude_worker", [False, True])
def test_cancel_running_coordinator_stops_provider(orc_factory, project, claude_worker):
    started = threading.Event()
    def slow(request):
        started.set()
        request.cancel.wait(5)
        raise BackendError("cancelled", "Cancelled")
    orc = setup(orc_factory, [delegate(plan_task(model=CLAUDE))] if claude_worker else [slow],
                [slow] if claude_worker else None)
    orc.synchronous = False
    task = orc.start_task(open_project(orc, project), "Hello")
    assert started.wait(3)
    orc.cancel(task["id"])
    orc.wait_idle(5)
    assert orc.db.get_task(task["id"])["status"] == "cancelled"


def test_coordinator_work_is_not_counted_as_qwen(orc_factory, project):
    orc = setup(orc_factory, ["MODE: answer\nHello."])
    orc.start_task(open_project(orc, project), "Hello")
    stats = orc.routing_stats()
    assert stats["qwen"] == 0 and stats["counted"] == 0
