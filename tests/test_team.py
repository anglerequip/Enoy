"""TEAM ROLES: adaptive team runs with fake Qwen and fake Claude (no real model calls)."""

import json
import sys

import pytest

from fusion_ai import team as team_mod
from fusion_ai.backends.base import CLAUDE, LOCAL, BackendError
from fusion_ai.project import Workspace

from test_orchestrator import ADD_MULTIPLY, BREAK_SUBTRACT, FIX_SUBTRACT, open_project

PYTEST = f'"{sys.executable}" -m pytest -q'

ADD_POWER = """Adding power.
<<<EDIT calculator.py
<<<SEARCH
def divide(a, b):
===
def power(a, b):
    return a ** b


def divide(a, b):
>>>REPLACE
>>>END
"""

ADD_TEST = """Adding a test.
<<<EDIT tests/test_calculator.py
<<<SEARCH
def test_add():
    assert add(2, 3) == 5
===
def test_add():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-2, -3) == -5
>>>REPLACE
>>>END
"""

ADD_README = """Documenting.
<<<EDIT README.md
<<<SEARCH
# Sample project
===
# Sample project

Has add, subtract and divide.
>>>REPLACE
>>>END
"""

FEATURE = "Implement a new arithmetic feature across the calculator and test modules"


def plan(*tasks, acceptance=("The calculator gains the new functions", "All tests pass")):
    return json.dumps({"acceptance": list(acceptance), "architecture": "Pure functions in calculator.py.",
                       "tasks": [dict(t) for t in tasks]})


def task(key, title, files, deps=(), spec=""):
    return {"id": key, "title": title, "description": title, "files": list(files), "depends_on": list(deps),
            "specialty": spec}


@pytest.fixture(autouse=True)
def real_checks(monkeypatch):
    # The fixture project has no virtual environment; run its tests with this interpreter.
    monkeypatch.setattr(Workspace, "suggest_test_commands", lambda self: [PYTEST])


def team_orc(orc_factory, qwen=None, claude=None, **kw):
    kw.setdefault("permissions__auto_apply_patches", True)
    return orc_factory(qwen=qwen, claude=claude, **kw)


def run(orc, tid):
    return orc.db.get_task(tid)["state"]["team_run"]


def roles(orc, tid):
    return [(t["plan_key"], t["role"], t["provider"], t["status"]) for t in orc.db.team_tasks(tid)]


# Simple and complex -----------------------------------------------------------------------

def test_simple_change_uses_minimal_orchestration(orc_factory, project):
    orc = team_orc(orc_factory, qwen=[ADD_MULTIPLY], claude=["never"])
    t = orc.start_task(open_project(orc, project), "Add a multiply function to calculator.py", team=True)
    tasks = orc.db.team_tasks(t["id"])
    assert [x["role"] for x in tasks] == ["developer", "qa", "reviewer"]  # No architect, no specialists.
    assert tasks[0]["provider"] == LOCAL and tasks[1]["provider"] == "system" and tasks[2]["provider"] == "system"
    assert run(orc, t["id"])["status"] == "completed" and run(orc, t["id"])["verification"] == "passed"
    assert orc.db.get_task(t["id"])["status"] == "completed"
    assert len(orc.backends[LOCAL].requests) == 1 and orc.backends[CLAUDE].requests == []
    summary = orc.db.messages(t["id"])[-1]
    assert summary["backend"] == "team" and "passed (real test run)" in summary["content"]


def test_questions_stay_direct_even_with_team_on(orc_factory, project):
    orc = team_orc(orc_factory, qwen=["divide divides."])
    t = orc.start_task(open_project(orc, project), "Explain what divide does", team=True)
    assert orc.db.team_tasks(t["id"]) == [] and orc.db.get_task(t["id"])["status"] == "completed"


def test_complex_task_activates_architect_and_follows_dependencies(orc_factory, project):
    arch = plan(task("T1", "Add power", ["calculator.py"]), task("T2", "Add a negative add test", ["tests/test_calculator.py"], ["T1"]))
    orc = team_orc(orc_factory, qwen=[ADD_POWER, ADD_TEST], claude=[arch])
    t = orc.start_task(open_project(orc, project), FEATURE, team=True)
    tasks = orc.db.team_tasks(t["id"])
    assert tasks[0]["role"] == "architect" and tasks[0]["provider"] == CLAUDE and "Architect activated" in tasks[0]["activation_reason"]
    t1 = next(x for x in tasks if x["plan_key"] == "T1")
    t2 = next(x for x in tasks if x["plan_key"] == "T2")
    assert t2["depends_on"] == [t1["id"]] and t1["finished"] <= t2["started"]
    assert run(orc, t["id"])["acceptance"] == ["The calculator gains the new functions", "All tests pass"]
    assert run(orc, t["id"])["status"] == "completed"
    text = (project / "calculator.py").read_text(encoding="utf-8")
    assert "def power" in text
    # The developer brief carries the plan, not the conversation.
    t2_prompt = orc.backends[LOCAL].requests[1].messages[-1].content
    assert "Acceptance criteria" in t2_prompt and "Already done by teammates" in t2_prompt and "T1 Add power" in t2_prompt


def test_independent_tasks_run_and_shared_files_are_serialized():
    tasks, _, _, notes = team_mod.parse_plan(plan(
        task("T1", "Add power", ["calculator.py"]), task("T2", "Docs", ["README.md"]),
        task("T3", "Add multiply", ["calculator.py"])), 6)
    by = {t.key: t for t in tasks}
    assert by["T2"].depends_on == [] and by["T1"].depends_on == []
    assert by["T3"].depends_on == ["T1"]  # Same file: never at the same time.
    assert any("both change calculator.py" in n for n in notes)


def test_parallel_batch_uses_different_models_and_disjoint_files(orc_factory):
    orc = orc_factory()
    runner = team_mod.TeamRunner(orc, 0)
    ready = [{"id": 1, "provider": LOCAL, "files": ["a.py"]}, {"id": 2, "provider": LOCAL, "files": ["b.py"]},
             {"id": 3, "provider": CLAUDE, "files": ["a.py"]}, {"id": 4, "provider": CLAUDE, "files": ["c.py"]}]
    assert [t["id"] for t in runner._pick_batch(ready)] == [1, 4]


def test_plan_validation_rejects_cycles_duplicates_and_unsafe_paths():
    with pytest.raises(team_mod.PlanError):
        team_mod.parse_plan(plan(task("T1", "A", ["a.py"], ["T2"]), task("T2", "B", ["b.py"], ["T1"])), 6)
    tasks, _, _, notes = team_mod.parse_plan(plan(task("T1", "Same", ["a.py"]), task("T2", "same", ["b.py"]),
                                                  task("T3", "Escape", ["../../etc/passwd", "C:/x.py", "ok.py"])), 6)
    assert [t.key for t in tasks] == ["T1", "T3"] and tasks[1].files == ["ok.py"]
    assert any("duplicate" in n for n in notes) and any("unsafe path" in n for n in notes)
    many = plan(*[task(f"T{i}", f"Task {i}", [f"f{i}.py"]) for i in range(10)])
    tasks, _, _, notes = team_mod.parse_plan(many, 3)
    assert len(tasks) == 3 and any("task limit" in n for n in notes)
    with pytest.raises(team_mod.PlanError):
        team_mod.parse_plan("I think we should start with the database.", 3)


def test_unusable_architect_plan_falls_back_to_one_task(orc_factory, project):
    orc = team_orc(orc_factory, qwen=[], claude=["no json here", "still prose", ADD_POWER])
    t = orc.start_task(open_project(orc, project), FEATURE, team=True)
    tasks = orc.db.team_tasks(t["id"])
    assert tasks[0]["role"] == "architect" and tasks[0]["status"] == "failed"
    assert [x["role"] for x in tasks[1:]] == ["developer", "qa", "reviewer"]
    assert any(e["kind"] == "team.plan_rejected" for e in orc.db.activity(t["id"]))


def test_specialist_activates_only_when_justified(orc_factory, project):
    assert team_mod.detect_specialties("Add a multiply function") == {}
    found = team_mod.detect_specialties("Add a settings page with a dark theme to the UI")
    assert set(found) == {"ui"} and "UI/UX specialist" in found["ui"]
    arch = plan(task("T1", "Add a README section", ["README.md"], spec="docs"))
    orc = team_orc(orc_factory, qwen=[ADD_README], claude=[arch])
    t = orc.start_task(open_project(orc, project), FEATURE + " and update the documentation", team=True)
    dev = next(x for x in orc.db.team_tasks(t["id"]) if x["role"] == "developer")
    assert dev["specialty"] == "docs" and "Documentation" in dev["activation_reason"]
    assert "Specialist focus" in orc.backends[LOCAL].requests[0].messages[0].content


# Verification, repair, escalation ----------------------------------------------------------

def test_failing_checks_trigger_a_repair_and_completion_needs_passing_tests(orc_factory, project):
    orc = team_orc(orc_factory, qwen=[BREAK_SUBTRACT, FIX_SUBTRACT], claude_available=False)
    t = orc.start_task(open_project(orc, project), "Change subtract in calculator.py", team=True)
    keys = [(x["plan_key"], x["role"], x["status"]) for x in orc.db.team_tasks(t["id"])]
    assert ("QA", "qa", "failed") in keys and ("FIX1", "developer", "completed") in keys
    assert run(orc, t["id"])["status"] == "completed" and run(orc, t["id"])["verification"] == "passed"
    assert run(orc, t["id"])["repairs"] == 1


def test_repair_limit_stops_with_an_honest_failure(orc_factory, project):
    orc = team_orc(orc_factory, qwen=[BREAK_SUBTRACT, "no fix", "no fix", "no fix", "no fix"], claude_available=False,
                   team__max_repair_rounds=1)
    t = orc.start_task(open_project(orc, project), "Change subtract in calculator.py", team=True)
    task = orc.db.get_task(t["id"])
    assert task["status"] == "failed" and run(orc, t["id"])["status"] == "failed"
    summary = orc.db.messages(t["id"])[-1]["content"]
    assert "stopped with problems" in summary and "FAILED" in summary


def test_debugger_on_claude_after_repeated_failure(orc_factory, project):
    orc = team_orc(orc_factory, qwen=[BREAK_SUBTRACT, "<<<EDIT calculator.py\n<<<SEARCH\n\"\"\"Small arithmetic helpers.\"\"\"\n===\n\"\"\"Helpers.\"\"\"\n>>>REPLACE\n>>>END\n"],
                   claude=[FIX_SUBTRACT])
    t = orc.start_task(open_project(orc, project), "Change subtract in calculator.py", team=True)
    tasks = orc.db.team_tasks(t["id"])
    fix2 = next(x for x in tasks if x["plan_key"] == "FIX2")
    assert fix2["role"] == "debugger" and fix2["provider"] == CLAUDE and fix2["status"] == "completed"
    assert "Debugger activated" in fix2["activation_reason"]
    assert run(orc, t["id"])["status"] == "completed"


def test_developer_escalates_after_invalid_output(orc_factory, project):
    orc = team_orc(orc_factory, qwen=["no blocks", "still none"], claude=[ADD_MULTIPLY])
    t = orc.start_task(open_project(orc, project), "Add a multiply function to calculator.py", team=True)
    dev = orc.db.team_tasks(t["id"])[0]
    assert dev["provider"] == CLAUDE and dev["status"] == "completed" and dev["route_reason"].startswith("Escalated")
    assert any(m["type"] == "escalation" for m in orc.db.team_messages(t["id"]))


def test_review_blocks_leaked_credentials(orc_factory, project):
    leak = ADD_MULTIPLY.replace("    return a * b", '    API_KEY = "sk-ant-api03-FAKEFAKEFAKE1234567890abcdef"\n    return a * b')
    findings = team_mod.review_diff([{"path": "calculator.py", "diff": "@@ -1,1 +1,2 @@\n x\n+API_KEY = \"sk-ant-api03-FAKEFAKEFAKE1234567890abcdef\"\n"}])
    assert findings[0]["severity"] == "BLOCKER" and findings[0]["line"] == 2
    assert team_mod.parse_review("VERDICT: CHANGES_NEEDED\nBLOCKER | calc.py:3 | wrong sign")[1][0]["line"] == 3
    assert leak  # The leak itself is redacted before storage, so the deterministic check is tested directly.


def test_model_review_only_for_larger_changes(orc_factory, project):
    orc = team_orc(orc_factory, qwen=[ADD_MULTIPLY, "VERDICT: APPROVE"], claude_available=False, team__review_min_lines=1)
    t = orc.start_task(open_project(orc, project), "Add a multiply function to calculator.py", team=True)
    review = next(x for x in orc.db.team_tasks(t["id"]) if x["role"] == "reviewer")
    assert review["result"]["model_review"] is True and review["provider"] == LOCAL
    orc2 = team_orc(orc_factory, qwen=[ADD_MULTIPLY], team__review_min_lines=60)
    t2 = orc2.start_task(open_project(orc2, project), "Add a multiply function to calculator.py", team=True)
    review2 = next(x for x in orc2.db.team_tasks(t2["id"]) if x["role"] == "reviewer")
    assert review2["result"]["model_review"] is False and len(orc2.backends[LOCAL].requests) == 1


# Approvals, toggle, pause, cancel, resume ---------------------------------------------------

def test_changes_wait_for_approval_when_auto_apply_is_off(orc_factory, project):
    orc = orc_factory(qwen=[ADD_MULTIPLY])
    t = orc.start_task(open_project(orc, project), "Add a multiply function to calculator.py", team=True)
    assert orc.db.get_task(t["id"])["status"] == "awaiting_approval"
    assert orc.db.team_tasks(t["id"])[0]["status"] == "awaiting_review"
    assert "def multiply" not in (project / "calculator.py").read_text(encoding="utf-8")
    orc.approve_changeset(orc.db.list_changesets(t["id"], "pending")[0]["id"])
    assert run(orc, t["id"])["status"] == "completed" and orc.db.get_task(t["id"])["status"] == "completed"


def test_claude_is_asked_once_per_run(orc_factory, project):
    arch = plan(task("T1", "Add power", ["calculator.py"]))
    orc = team_orc(orc_factory, qwen=[ADD_POWER], claude=[arch], claude__confirm_before_send=True)
    t = orc.start_task(open_project(orc, project), FEATURE, team=True)
    assert orc.db.get_task(t["id"])["status"] == "awaiting_claude" and orc.backends[CLAUDE].requests == []
    assert run(orc, t["id"])["claude_request"]["tasks"][0].startswith("ARCH")
    orc.approve_claude(t["id"])
    assert len(orc.backends[CLAUDE].requests) == 1 and run(orc, t["id"])["status"] == "completed"


def test_declining_claude_keeps_the_run_on_qwen(orc_factory, project):
    orc = team_orc(orc_factory, qwen=["not json", "not json", ADD_POWER], claude=["never"], claude__confirm_before_send=True)
    t = orc.start_task(open_project(orc, project), FEATURE, team=True)
    orc.decline_claude(t["id"])
    assert orc.backends[CLAUDE].requests == []
    assert all(x["provider"] != CLAUDE for x in orc.db.team_tasks(t["id"]))
    assert run(orc, t["id"])["claude_declined"] is True


def test_team_off_bypasses_orchestration(orc_factory, project):
    orc = orc_factory(qwen=[ADD_MULTIPLY])
    t = orc.start_task(open_project(orc, project), "Add a multiply function to calculator.py", team=False)
    assert orc.db.team_tasks(t["id"]) == [] and orc.db.get_task(t["id"])["workflow"] != "team"
    assert "team_run" not in orc.db.get_task(t["id"])["state"]


def test_each_chat_keeps_its_own_setting(orc_factory, project):
    orc = orc_factory(qwen=["a", "b"], team__default_new_chats=False)
    pid = open_project(orc, project)
    a = orc.start_task(pid, "Explain add", team=True)
    b = orc.start_task(pid, "Explain subtract")
    assert orc.db.get_task(a["id"])["state"]["team"] is True and orc.db.get_task(b["id"])["state"]["team"] is False
    orc.set_team(b["id"], True)
    assert orc.db.get_task(a["id"])["state"]["team"] is True and orc.db.get_task(b["id"])["state"]["team"] is True
    orc.set_team(a["id"], False)
    assert orc.db.get_task(a["id"])["state"]["team"] is False and orc.db.get_task(b["id"])["state"]["team"] is True


def test_switching_off_pauses_and_switching_on_resumes_without_duplicates(orc_factory, project):
    arch = plan(task("T1", "Add power", ["calculator.py"]), task("T2", "Add a negative add test", ["tests/test_calculator.py"], ["T1"]))
    orc = orc_factory(qwen=[ADD_POWER, ADD_TEST], claude=[arch])
    t = orc.start_task(open_project(orc, project), FEATURE, team=True)
    assert orc.db.get_task(t["id"])["status"] == "awaiting_approval"  # T1 waits for review.
    orc.set_team(t["id"], False)
    assert orc.db.get_task(t["id"])["status"] == "paused" and run(orc, t["id"])["status"] == "paused"
    t2 = next(x for x in orc.db.team_tasks(t["id"]) if x["plan_key"] == "T2")
    assert t2["status"] == "paused"
    orc.approve_changeset(orc.db.list_changesets(t["id"], "pending")[0]["id"])  # Finished work is kept.
    orc.set_team(t["id"], True)
    orc.approve_changeset(orc.db.list_changesets(t["id"], "pending")[0]["id"])
    keys = [x["plan_key"] for x in orc.db.team_tasks(t["id"])]
    assert keys.count("T1") == 1 and keys.count("T2") == 1  # Nothing duplicated.
    assert run(orc, t["id"])["status"] == "completed"
    assert len(orc.backends[LOCAL].requests) == 2


def test_cancel_closes_the_team_graph(orc_factory, project):
    orc = orc_factory(qwen=[ADD_MULTIPLY])
    t = orc.start_task(open_project(orc, project), "Add a multiply function to calculator.py", team=True)
    orc.cancel(t["id"])
    assert orc.db.get_task(t["id"])["status"] == "cancelled"
    assert all(x["status"] not in team_mod.open_statuses() for x in orc.db.team_tasks(t["id"]))
    assert run(orc, t["id"])["status"] == "cancelled"


def test_interrupted_run_resumes_from_saved_graph(cfg, project, tmp_path, monkeypatch):
    from conftest import FakeBackend
    from fusion_ai.orchestrator import Orchestrator
    from fusion_ai.storage import Storage

    db = tmp_path / "f.db"
    cfg["permissions"]["auto_apply_patches"] = True
    orc = Orchestrator(Storage(db), cfg, {LOCAL: FakeBackend(LOCAL, [BackendError("cancelled", "stop")]),
                                          CLAUDE: FakeBackend(CLAUDE, available=False)}, synchronous=True)
    pid = orc.open_project(str(project))["id"]
    t = orc.start_task(pid, "Add a multiply function to calculator.py", team=True)
    # Simulate a crash in the middle: the run and one task were "running".
    orc.db.update_task(t["id"], status="running")
    dev = orc.db.team_tasks(t["id"])[0]
    orc.db.update_team_task(dev["id"], status="running")
    run_state = run(orc, t["id"]) | {"status": "running"}
    orc.db.update_task_state(t["id"], team_run=run_state)
    orc.db.close()
    storage = Storage(db)
    assert storage.mark_interrupted() == [t["id"]]
    assert storage.team_tasks(t["id"])[0]["status"] == "planned"
    orc = Orchestrator(storage, cfg, {LOCAL: FakeBackend(LOCAL, [ADD_MULTIPLY]), CLAUDE: FakeBackend(CLAUDE, available=False)},
                       synchronous=True)
    orc.resume(t["id"])
    assert run(orc, t["id"])["status"] == "completed"
    assert [x["plan_key"] for x in storage.team_tasks(t["id"])].count("T1") == 1


# Privacy, budgets, zero-paid, outages ----------------------------------------------------------

def test_local_only_project_keeps_the_whole_team_local(orc_factory, project):
    orc = team_orc(orc_factory, qwen=["not json", "not json", ADD_POWER], claude=["never"])
    pid = open_project(orc, project)
    orc.set_project_local_only(pid, True)
    t = orc.start_task(pid, FEATURE, team=True)
    assert orc.backends[CLAUDE].requests == []
    assert all(x["provider"] in (LOCAL, "system") for x in orc.db.team_tasks(t["id"]))


def test_zero_paid_mode_never_uses_claude_automatically(orc_factory, project):
    orc = team_orc(orc_factory, qwen=["not json", "not json", ADD_POWER], claude=["never"], routing__zero_paid_mode=True)
    t = orc.start_task(open_project(orc, project), FEATURE, team=True)
    assert orc.backends[CLAUDE].requests == []
    arch = orc.db.team_tasks(t["id"])[0]
    assert arch["provider"] == LOCAL and "zero-paid" in arch["route_reason"]
    orc2 = team_orc(orc_factory, qwen=["never"], claude=["Plan."], routing__zero_paid_mode=True)
    t2 = orc2.start_task(open_project(orc2, project), "Design the architecture for a plugin system", backend=CLAUDE)
    assert len(orc2.backends[CLAUDE].requests) == 1  # Your own explicit pick is not automatic.


def test_run_budget_stops_model_calls(orc_factory, project):
    orc = team_orc(orc_factory, qwen=["bad", "bad", "bad"], claude_available=False, team__max_model_calls=1)
    t = orc.start_task(open_project(orc, project), "Add a multiply function to calculator.py", team=True)
    assert len(orc.backends[LOCAL].requests) == 1
    dev = orc.db.team_tasks(t["id"])[0]
    assert dev["status"] == "failed" and "budget" in dev["error"]
    assert orc.db.get_task(t["id"])["status"] == "failed"


def test_qwen_outage_is_reported_not_hidden(orc_factory, project):
    orc = team_orc(orc_factory, qwen=[BackendError("unavailable", "Cannot reach Ollama")], claude_available=False)
    t = orc.start_task(open_project(orc, project), "Add a multiply function to calculator.py", team=True)
    assert orc.db.get_task(t["id"])["status"] == "failed"
    assert "Cannot reach Ollama" in orc.db.team_tasks(t["id"])[0]["error"]


def test_excluded_files_never_reach_claude_in_team_runs(orc_factory, project):
    arch = plan(task("T1", "Add power", ["calculator.py"]))
    orc = team_orc(orc_factory, qwen=[ADD_POWER], claude=[arch], privacy__cloud_excluded_globs=["calculator.py"])
    t = orc.start_task(open_project(orc, project), FEATURE + " (calculator.py)", team=True)
    sent = "\n".join(m.content for m in orc.backends[CLAUDE].requests[0].messages)
    assert "def subtract(a, b):" not in sent
    assert any(e["kind"] == "privacy.checked" for e in orc.db.activity(t["id"]))


# Records ----------------------------------------------------------------------------------------

def test_messages_memory_and_activity_reflect_the_run(orc_factory, project):
    orc = team_orc(orc_factory, qwen=[ADD_MULTIPLY])
    t = orc.start_task(open_project(orc, project), "Add a multiply function to calculator.py", team=True)
    types = [m["type"] for m in reversed(orc.db.team_messages(t["id"]))]
    for expected in ("assignment", "proposal", "approval", "completed", "handoff"):
        assert expected in types
    mem = {m["category"]: m for m in orc.db.memory(orc.db.get_task(t["id"])["project_id"], task_id=t["id"])}
    assert mem["requirements"]["verified"] == 0 and mem["implementation"]["verified"] == 1
    assert mem["test_results"]["verified"] == 1
    kinds = [e["kind"] for e in orc.db.activity(t["id"])]
    assert "team.started" in kinds and "test.completed" in kinds
    view = orc.team_view(t["id"])
    assert view["team"] is True and view["run"]["status"] == "completed" and len(view["tasks"]) == 3


def test_team_state_persists(cfg, project, tmp_path, monkeypatch):
    from conftest import FakeBackend
    from fusion_ai.orchestrator import Orchestrator
    from fusion_ai.storage import Storage

    db = tmp_path / "f.db"
    cfg["permissions"]["auto_apply_patches"] = True
    orc = Orchestrator(Storage(db), cfg, {LOCAL: FakeBackend(LOCAL, [ADD_MULTIPLY]), CLAUDE: FakeBackend(CLAUDE)},
                       synchronous=True)
    t = orc.start_task(orc.open_project(str(project))["id"], "Add a multiply function to calculator.py", team=True)
    orc.db.close()
    storage = Storage(db)
    assert storage.get_task(t["id"])["state"]["team"] is True
    assert [x["role"] for x in storage.team_tasks(t["id"])] == ["developer", "qa", "reviewer"]
    assert storage.team_messages(t["id"])


def test_activity_board_shows_the_real_team(orc_factory, project):
    from fusion_ai import activity

    arch = plan(task("T1", "Add power", ["calculator.py"]), task("T2", "Add a negative add test", ["tests/test_calculator.py"], ["T1"]))
    orc = team_orc(orc_factory, qwen=[ADD_POWER, ADD_TEST], claude=[arch])
    t = orc.start_task(open_project(orc, project), FEATURE, team=True)
    snap = activity.snapshot(orc, t["id"])
    team = snap["focus"]["team"]
    assert team["status"] == "completed" and team["roles_used"] == ["architect", "developer", "qa", "reviewer"]
    t2 = next(x for x in team["tasks"] if x["key"] == "T2")
    assert t2["after"] == ["T1"] and t2["provider"] == LOCAL and t2["changed"] == ["tests/test_calculator.py"]
    qa = next(x for x in team["tasks"] if x["role"] == "qa")
    assert qa["commands"][0]["status"] == "passed"
    keys = [s["key"] for s in snap["focus"]["stages"]]
    assert keys[:3] == ["request", "analysis", "plan"] and "implement" in keys and "testing" in keys
    assert next(s for s in snap["focus"]["stages"] if s["key"] == "repair")["status"] == "skipped"
    assert team["messages"] and team["model_calls"] == 3 and team["claude_calls"] == 1


def test_checks_that_cannot_run_are_reported_not_repaired(orc_factory, project, monkeypatch):
    monkeypatch.setattr(Workspace, "suggest_test_commands", lambda self: [f'"{sys.executable}" -c "import pytest_missing_xyz"'])
    monkeypatch.setattr(team_mod, "ENV_PROBLEM", __import__("re").compile("No module named"))
    orc = team_orc(orc_factory, qwen=[ADD_MULTIPLY], permissions__command_allowlist=["python -c"])
    t = orc.start_task(open_project(orc, project), "Add a multiply function to calculator.py", team=True)
    r = run(orc, t["id"])
    assert r["verification"] == "not_run" and r["repairs"] == 0 and "could not run" in r["verification_note"]
    assert "done, not verified by tests" in orc.db.messages(t["id"])[-1]["content"]
    assert "chosen by you" not in orc.db.team_tasks(t["id"])[0]["route_reason"]
    assert len(orc.backends[LOCAL].requests) == 1  # No repair attempt for an environment problem.
    assert "could not run" in orc.db.messages(t["id"])[-1]["content"]
