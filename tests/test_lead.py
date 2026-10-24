"""Claude as the lead: it reads each message first, answers or plans, Qwen and the team execute, Claude checks.

Fake Qwen and fake Claude only: no model or cloud calls.
"""

import json
import subprocess
import sys

import pytest

from fusion_ai import activity
from fusion_ai import context as ctx
from fusion_ai.backends.base import CLAUDE, LOCAL, BackendError
from fusion_ai.project import Workspace

from test_orchestrator import ADD_MULTIPLY, BREAK_SUBTRACT, FIX_SUBTRACT, open_project

PYTEST = f'"{sys.executable}" -m pytest -q'


@pytest.fixture(autouse=True)
def real_checks(monkeypatch):
    monkeypatch.setattr(Workspace, "suggest_test_commands", lambda self: [PYTEST])


def lead_orc(orc_factory, qwen=None, claude=None, **kw):
    kw.setdefault("lead__enabled", True)
    kw.setdefault("lead__style", "balanced")  # The Claude-first tests below set claude_first themselves.
    kw.setdefault("permissions__auto_apply_patches", True)
    return orc_factory(qwen=qwen, claude=claude, **kw)


def delegate(*tasks, message="The team will add it.", acceptance=("multiply(a, b) returns a * b", "All tests pass")):
    plan = {"acceptance": list(acceptance), "architecture": "Pure functions in calculator.py.", "tasks": list(tasks)}
    return f"MODE: delegate\n{message}\n```json\n{json.dumps(plan)}\n```"


def plan_task(key="T1", title="Add multiply to calculator.py", files=("calculator.py",), deps=(), model="qwen", **extra):
    return {"id": key, "title": title, "description": title, "files": list(files), "depends_on": list(deps),
            "model": model, "why": "Small, clear function.", "expected_output": "An EDIT block for calculator.py",
            "constraints": ["Keep the existing functions unchanged"], **extra}


def answers(orc, task_id):
    return [(m["backend"], m["content"]) for m in orc.db.messages(task_id) if m["role"] == "assistant"]


def kinds(orc, task_id):
    return [a["kind"] for a in orc.db.activity(task_id, 500)]


# Entry point ------------------------------------------------------------------------

def test_claude_answers_a_harder_question_itself(orc_factory, project):
    orc = lead_orc(orc_factory, claude=["MODE: answer\ndivide() has no shared state, so it is thread-safe."])
    task = orc.start_task(open_project(orc, project), "Is divide thread-safe? Explain the race conditions.")
    t = orc.db.get_task(task["id"])
    assert t["status"] == "completed" and t["workflow"] == "claude_leads" and t["owner"] == CLAUDE
    assert answers(orc, task["id"]) == [(CLAUDE, "divide() has no shared state, so it is thread-safe.")]
    assert orc.backends[LOCAL].requests == [] and orc.db.team_tasks(task["id"]) == []
    assert len(orc.backends[CLAUDE].requests) == 1
    prompt = orc.backends[CLAUDE].requests[0].messages[-1].content
    assert "MODE: answer" in prompt and "MODE: delegate" in prompt
    assert "lead.answer" in kinds(orc, task["id"])


def test_casual_messages_skip_the_lead(orc_factory, project):
    orc = lead_orc(orc_factory, qwen=["Doing well!", "divide() raises ZeroDivisionError when b is 0."])
    pid = open_project(orc, project)
    for text in ("hey, how are you?", "What does divide do with zero?"):
        task = orc.start_task(pid, text)
        assert orc.db.get_task(task["id"])["workflow"] != "claude_leads"
        assert "lead.skipped" in kinds(orc, task["id"])
    assert orc.backends[CLAUDE].requests == [] and len(orc.backends[LOCAL].requests) == 2


def test_claude_can_ask_a_clarifying_question(orc_factory, project):
    orc = lead_orc(orc_factory, claude=["MODE: clarify\nShould multiply accept floats too?"])
    task = orc.start_task(open_project(orc, project), "make the maths better")
    assert answers(orc, task["id"]) == [(CLAUDE, "Should multiply accept floats too?")]
    assert orc.backends[LOCAL].requests == [] and "lead.clarify" in kinds(orc, task["id"])


def test_reply_without_mode_line_is_an_answer():
    assert ctx.parse_lead("Just text.") == ("answer", "Just text.", None)
    mode, msg, plan = ctx.parse_lead(delegate(plan_task()))
    assert mode == "delegate" and msg == "The team will add it." and json.loads(plan)["tasks"][0]["id"] == "T1"


def test_general_chat_never_delegates(orc_factory):
    orc = lead_orc(orc_factory, claude=[delegate(plan_task())])
    space = orc.create_space("Ideas")
    task = orc.start_task(space["id"], "Compare the architecture options for a party planner app")
    assert ctx.LEAD_NO_DELEGATE in orc.backends[CLAUDE].requests[0].messages[-1].content
    assert orc.db.team_tasks(task["id"]) == [] and orc.db.get_task(task["id"])["status"] == "completed"
    assert "no project code to change" in answers(orc, task["id"])[0][1]


# Plan and delegate ------------------------------------------------------------------

def test_claude_plans_qwen_implements_qa_verifies_claude_answers(orc_factory, project):
    review = "Multiply was added and the real test run passed.\nNEXT: done"
    orc = lead_orc(orc_factory, qwen=[ADD_MULTIPLY], claude=[delegate(plan_task()), review])
    task = orc.start_task(open_project(orc, project), "Add a multiply function")
    t = orc.db.get_task(task["id"])
    assert t["status"] == "completed", t["summary"]
    assert "def multiply" in (project / "calculator.py").read_text(encoding="utf-8")
    tts = {x["plan_key"]: x for x in orc.db.team_tasks(task["id"])}
    assert tts["PLAN"]["role"] == "architect" and tts["PLAN"]["provider"] == CLAUDE
    dev = tts["T1"]
    assert dev["provider"] == LOCAL and dev["status"] == "completed" and "Claude gave this to Qwen" in dev["route_reason"]
    assert dev["spec"]["expected_output"] == "An EDIT block for calculator.py"
    assert dev["spec"]["verification"] == "checks passed"
    assert tts["QA"]["status"] == "completed"
    # Qwen got a structured task package, not the conversation.
    brief = orc.backends[LOCAL].requests[0].messages[-1].content
    assert "Team task T1 (Developer)" in brief and "Expected output: An EDIT block" in brief
    assert "- Keep the existing functions unchanged" in brief and "Do not claim to have run tests" in brief
    assert "multiply(a, b) returns a * b" in brief  # Acceptance criteria.
    # One coherent reply: Claude's plan message and Claude's final answer; Qwen's raw output stays in the panel.
    msgs = answers(orc, task["id"])
    assert msgs == [(CLAUDE, "The team will add it."), (CLAUDE, "Multiply was added and the real test run passed.")]
    assert "multiply" in dev["result"]["output"]
    # Claude saw the real results.
    check = orc.backends[CLAUDE].requests[1].messages[-1].content
    assert "Checks: passed (real test run)" in check and "+def multiply" in check
    assert len(orc.backends[CLAUDE].requests) == 2


def test_claude_can_keep_a_hard_part_for_itself(orc_factory, project):
    orc = lead_orc(orc_factory, claude=[delegate(plan_task(model="claude")), ADD_MULTIPLY, "Done.\nNEXT: done"])
    task = orc.start_task(open_project(orc, project), "Add a multiply function")
    dev = next(x for x in orc.db.team_tasks(task["id"]) if x["plan_key"] == "T1")
    assert dev["provider"] == CLAUDE and orc.backends[LOCAL].requests == []
    assert orc.db.get_task(task["id"])["status"] == "completed"


def test_dependencies_run_in_order(orc_factory, project):
    add_test = ("<<<EDIT tests/test_calculator.py\n<<<SEARCH\ndef test_add():\n===\ndef test_add_again():\n"
                "    assert add(1, 1) == 2\n\n\ndef test_add():\n>>>REPLACE\n>>>END\n")
    tasks = [plan_task("T2", "Add a test", ("tests/test_calculator.py",), deps=("T1",)), plan_task()]
    orc = lead_orc(orc_factory, qwen=[ADD_MULTIPLY, add_test], claude=[delegate(*tasks), "Done.\nNEXT: done"])
    task = orc.start_task(open_project(orc, project), "Add multiply and a test")
    reqs = orc.backends[LOCAL].requests
    assert "Team task T1" in reqs[0].messages[-1].content and "Team task T2" in reqs[1].messages[-1].content
    assert orc.db.get_task(task["id"])["status"] == "completed"


def test_failed_qwen_task_is_retried_then_escalated_to_claude(orc_factory, project):
    bad = "I added it."  # No change blocks.
    orc = lead_orc(orc_factory, qwen=[bad, bad], claude=[delegate(plan_task()), ADD_MULTIPLY, "Fixed.\nNEXT: done"])
    task = orc.start_task(open_project(orc, project), "Add a multiply function")
    dev = next(x for x in orc.db.team_tasks(task["id"]) if x["plan_key"] == "T1")
    assert len(orc.backends[LOCAL].requests) == 2  # Retry limit.
    assert dev["provider"] == CLAUDE and dev["status"] == "completed"
    assert "task.escalated" in kinds(orc, task["id"])
    assert "Could not be applied" in orc.backends[CLAUDE].requests[1].messages[-1].content \
        or "could not be applied" in orc.backends[CLAUDE].requests[1].messages[-1].content


def test_failing_checks_send_a_fix_then_claude_checks(orc_factory, project):
    orc = lead_orc(orc_factory, qwen=[BREAK_SUBTRACT, FIX_SUBTRACT],
                   claude=[delegate(plan_task(title="Change subtract")), "Fixed after one repair.\nNEXT: done"])
    task = orc.start_task(open_project(orc, project), "Change subtract")
    keys = [x["plan_key"] for x in orc.db.team_tasks(task["id"])]
    assert "FIX1" in keys and orc.db.get_task(task["id"])["status"] == "completed"


def test_claude_orders_a_fix_after_its_check_and_it_is_bounded(orc_factory, project):
    orc = lead_orc(orc_factory, qwen=[ADD_MULTIPLY, "UNRESOLVED: none\nNothing more to change."],
                   claude=[delegate(plan_task()), "Missing a docstring.\nNEXT: fix Add a docstring to multiply",
                           "Still not fixed.\nNEXT: fix Add the docstring"])
    task = orc.start_task(open_project(orc, project), "Add a multiply function")
    keys = [x["plan_key"] for x in orc.db.team_tasks(task["id"])]
    assert "LEAD1" in keys and "LEAD2" not in keys  # max_followups = 1
    assert len(orc.backends[CLAUDE].requests) == 3
    assert "lead.fix_ordered" in kinds(orc, task["id"])


def test_unusable_plan_changes_nothing(orc_factory, project):
    orc = lead_orc(orc_factory, claude=["MODE: delegate\nOn it.\n```json\n{\"tasks\": []}\n```"])
    before = (project / "calculator.py").read_text(encoding="utf-8")
    task = orc.start_task(open_project(orc, project), "Add a multiply function")
    assert "could not use Claude's plan" in answers(orc, task["id"])[0][1]
    assert (project / "calculator.py").read_text(encoding="utf-8") == before and orc.backends[LOCAL].requests == []


# Claude unavailable, cost and your control ------------------------------------------------

def test_unavailable_claude_is_said_and_qwen_answers_labeled(orc_factory, project):
    orc = lead_orc(orc_factory, qwen=["divide raises on zero."], claude_available=False)
    task = orc.start_task(open_project(orc, project), "Is divide thread-safe? Explain the race conditions.")
    assert answers(orc, task["id"]) == [(LOCAL, "divide raises on zero.")]
    notes = [e["text"] for e in orc.db.events(task["id"]) if e["type"] == "note"]
    assert any("Claude cannot lead this message" in n and "labeled Qwen" in n for n in notes)
    assert orc.backends[CLAUDE].requests == [] and "lead.unavailable" in kinds(orc, task["id"])


def test_unavailable_claude_stops_when_settings_say_so(orc_factory, project):
    orc = lead_orc(orc_factory, claude_available=False, routing__claude_fallback="stop")
    task = orc.start_task(open_project(orc, project), "Is divide thread-safe? Explain the race conditions.")
    t = orc.db.get_task(task["id"])
    assert t["status"] == "failed" and "Claude cannot lead" in t["summary"]
    assert orc.backends[LOCAL].requests == [] and orc.backends[CLAUDE].requests == []


def test_claude_failing_mid_request_falls_back_to_qwen(orc_factory, project):
    orc = lead_orc(orc_factory, qwen=["Local answer."], claude=[BackendError("limit", "usage limit reached")])
    task = orc.start_task(open_project(orc, project), "Is divide thread-safe? Explain the race conditions.")
    assert answers(orc, task["id"])[-1] == (LOCAL, "Local answer.")


def test_zero_paid_mode_keeps_claude_out(orc_factory, project):
    orc = lead_orc(orc_factory, qwen=["Local answer."], routing__zero_paid_mode=True)
    orc.start_task(open_project(orc, project), "Is divide thread-safe? Explain the race conditions.")
    assert orc.backends[CLAUDE].requests == []


def test_picking_a_model_skips_the_lead(orc_factory, project):
    orc = lead_orc(orc_factory, qwen=["Local answer."])
    task = orc.start_task(open_project(orc, project), "Is divide thread-safe? Explain the race conditions.", backend=LOCAL)
    assert orc.backends[CLAUDE].requests == [] and orc.db.get_task(task["id"])["workflow"] != "claude_leads"


def test_ask_first_setting_holds_the_lead_request(orc_factory, project):
    orc = lead_orc(orc_factory, claude=["MODE: answer\nOk."], claude__confirm_before_send=True)
    task = orc.start_task(open_project(orc, project), "Is divide thread-safe? Explain the race conditions.")
    assert orc.db.get_task(task["id"])["status"] == "awaiting_claude" and orc.backends[CLAUDE].requests == []
    orc.approve_claude(task["id"])
    assert answers(orc, task["id"]) == [(CLAUDE, "Ok.")]


def test_follow_up_goes_to_the_lead_again(orc_factory, project):
    orc = lead_orc(orc_factory, claude=["MODE: answer\nFirst.", "MODE: answer\nSecond."])
    task = orc.start_task(open_project(orc, project), "Is divide thread-safe? Explain the race conditions.")
    orc.send_message(task["id"], "And is subtract thread-safe? Explain the race conditions.")
    assert [m for _, m in answers(orc, task["id"])] == ["First.", "Second."]
    assert "Follow-up from the user:\nAnd is subtract thread-safe?" in orc.backends[CLAUDE].requests[1].messages[-1].content


def test_claude_unable_to_check_results_leaves_enoys_summary(orc_factory, project):
    orc = lead_orc(orc_factory, qwen=[ADD_MULTIPLY], claude=[delegate(plan_task())], lead__final_review=False)
    task = orc.start_task(open_project(orc, project), "Add a multiply function")
    last = answers(orc, task["id"])[-1]
    assert last[0] == "team" and "Team result: done" in last[1]
    assert "lead.review_skipped" in kinds(orc, task["id"]) and len(orc.backends[CLAUDE].requests) == 1


def test_team_task_controls(orc_factory, project):
    tasks = [plan_task(), plan_task("T2", "Add a test", ("tests/test_calculator.py",), deps=("T1",))]
    orc = lead_orc(orc_factory, qwen=["no blocks", "no blocks"], claude=[delegate(*tasks), "Stopped.\nNEXT: done"],
                   permissions__auto_apply_patches=True)
    orc.backends[CLAUDE].available = True
    task = orc.start_task(open_project(orc, project), "Add multiply and a test")
    tts = {x["plan_key"]: x for x in orc.db.team_tasks(task["id"])}
    # T1 failed on Qwen and was escalated; Claude had no reply left, so it failed; T2 is blocked.
    assert tts["T1"]["status"] == "failed" and tts["T2"]["status"] == "blocked"
    with pytest.raises(Exception):
        orc.team_task_action(tts["T1"]["id"], "cancel")  # Not cancellable once it ran.
    orc.team_task_action(tts["T1"]["id"], "model", LOCAL)
    assert orc.db.get_team_task(tts["T1"]["id"])["route_reason"] == "You picked Qwen (local)."
    orc.backends[LOCAL].replies = [ADD_MULTIPLY, "UNRESOLVED: none"]
    orc.backends[CLAUDE].replies = ["Done now.\nNEXT: done"]
    orc.team_task_action(tts["T1"]["id"], "retry")
    assert orc.db.get_team_task(tts["T1"]["id"])["status"] == "completed"


def test_pause_delegation_holds_new_tasks(orc_factory, project):
    tasks = [plan_task(), plan_task("T2", "Add a test", ("tests/test_calculator.py",), deps=("T1",))]
    orc = lead_orc(orc_factory, claude=[delegate(*tasks)], permissions__auto_apply_patches=False)
    task = orc.start_task(open_project(orc, project), "Add multiply and a test")
    # No Qwen replies were scripted: T1 failed. Hold, then check nothing new starts.
    orc.hold_delegation(task["id"], True)
    assert orc.db.get_task(task["id"])["state"]["team_run"]["hold"] is True


# Memory, git and the Activity panel ----------------------------------------------------------

def test_project_memory_reaches_the_lead_and_a_checkpoint_is_saved(orc_factory, project):
    orc = lead_orc(orc_factory, qwen=[ADD_MULTIPLY], claude=["MODE: answer\nOk.", delegate(plan_task()), "Done.\nNEXT: done"])
    pid = open_project(orc, project)
    orc.db.add_memory(pid, "decisions", "Keep calculator functions pure.", True, "you")
    orc.start_task(pid, "Is divide thread-safe? Explain the race conditions.")
    assert "Keep calculator functions pure." in orc.backends[CLAUDE].requests[0].messages[-1].content
    assert "verified" in orc.backends[CLAUDE].requests[0].messages[-1].content
    task = orc.start_task(pid, "Add a multiply function")
    cps = orc.db.memory(pid, "checkpoint", 5, task["id"])
    assert cps and "Changed: calculator.py" in cps[0]["content"] and "Verified: passed" in cps[0]["content"]
    assert orc.db.memory(pid, "decisions", 5, task["id"])  # The lead's decision.


def git(project, *args):
    subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True)


def test_uncommitted_user_changes_are_never_auto_applied(orc_factory, project):
    git(project, "init", "-q")
    git(project, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git(project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "start")
    path = project / "calculator.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n# my own work in progress\n", encoding="utf-8")
    orc = lead_orc(orc_factory, qwen=[ADD_MULTIPLY], claude=[delegate(plan_task())])
    task = orc.start_task(open_project(orc, project), "Add a multiply function")
    dev = next(x for x in orc.db.team_tasks(task["id"]) if x["plan_key"] == "T1")
    assert dev["status"] == "awaiting_review"
    assert "def multiply" not in path.read_text(encoding="utf-8")
    assert {"git.checked", "git.protected"} <= set(kinds(orc, task["id"]))


def test_activity_shows_the_lead_the_tasks_and_controls(orc_factory, project):
    orc = lead_orc(orc_factory, qwen=[ADD_MULTIPLY], claude=[delegate(plan_task()), "Done.\nNEXT: done"])
    task = orc.start_task(open_project(orc, project), "Add a multiply function")
    snap = activity.snapshot(orc, task["id"])
    assert snap["lead"]["enabled"] is True
    team = snap["focus"]["team"]
    assert team["lead"] and team["lead_state"] == "Claude handed the result to you."
    t1 = next(x for x in team["tasks"] if x["key"] == "T1")
    assert t1["provider"] == LOCAL and t1["verification"] == "checks passed" and t1["spec"]["why"]
    assert t1["can"] == {"cancel": False, "retry": False, "model": False}
    events = kinds(orc, task["id"])
    for k in ("lead.started", "team.planned", "backend.request", "lead.reviewed", "task.completed"):
        assert k in events


def test_one_qwen_backend_serves_every_role(orc_factory, project):
    """Roles share the single local backend and its lock; no second Qwen is ever created."""
    orc = lead_orc(orc_factory, qwen=[ADD_MULTIPLY], claude=[delegate(plan_task()), "Done.\nNEXT: done"])
    backend = orc.backends[LOCAL]
    orc.start_task(open_project(orc, project), "Add a multiply function")
    assert orc.backends[LOCAL] is backend and len(orc.backends) == 2
    assert orc._local_lock.acquire(blocking=False)
    orc._local_lock.release()


# The chat Claude sees, empty folders and "no changes" replies ---------------------------------

def test_follow_up_sends_claude_the_replies_it_did_not_write(orc_factory, tmp_path):
    """Qwen answered the first message; Claude must see that answer, not answer the first message again."""
    empty = tmp_path / "Enoy General"
    empty.mkdir()
    orc = lead_orc(orc_factory, qwen=["Doing well! Trollhattan: overcast, 10.2 C (SMHI)."],
                   claude=["MODE: answer\nThis folder is empty; open the folder with Enoy's code."])
    task = orc.start_task(open_project(orc, empty), "hey, how are you?")
    orc.send_message(task["id"], "Add a dark mode toggle to my website's sidebar.")
    prompt = orc.backends[CLAUDE].requests[0].messages[-1].content
    assert "First message of this chat:\nhey, how are you?" in prompt
    assert ctx.CONVERSATION_NOTE in prompt and "[Qwen]\nDoing well! Trollhattan: overcast" in prompt
    assert prompt.count("Add a dark mode toggle") == 1  # Only as the follow-up, not also in the conversation.
    assert ctx.CLAUDE_EMPTY_FOLDER.format(path=empty) in prompt
    assert orc.db.get_task(task["id"])["status"] == "completed"


def test_resumed_claude_session_gets_only_what_came_after_its_reply(orc_factory, project):
    orc = lead_orc(orc_factory, claude=["MODE: answer\nFirst."])
    task = orc.start_task(open_project(orc, project), "Is divide thread-safe? Explain the race conditions.")
    orc.db.add_message(task["id"], "assistant", "Qwen's later reply.", LOCAL)
    orc.db.add_message(task["id"], "user", "And subtract?")
    talk = orc._conversation_for_claude(task["id"], "Is divide thread-safe? Explain the race conditions.", "sess-1")
    assert "[Qwen]\nQwen's later reply." in talk and "First." not in talk and "And subtract?" not in talk


def test_claude_explaining_instead_of_editing_is_an_answer(orc_factory, project):
    orc = orc_factory(claude=["I can't see that code in this project, so nothing was changed."])
    before = (project / "calculator.py").read_text(encoding="utf-8")
    task = orc.start_task(open_project(orc, project), "Add an Ollama status dot to the sidebar", backend=CLAUDE)
    t = orc.db.get_task(task["id"])
    assert t["status"] == "completed", t["summary"]
    assert "failed validation" not in t["summary"]
    assert answers(orc, task["id"])[-1] == (CLAUDE, "I can't see that code in this project, so nothing was changed.")
    assert (project / "calculator.py").read_text(encoding="utf-8") == before


def test_team_does_not_resend_when_claude_changes_nothing_and_retry_asks_the_lead(orc_factory, project):
    orc = lead_orc(orc_factory, claude=[delegate(plan_task(model="claude", title="Add an Ollama dot to the sidebar")),
                                        "The sidebar code is not in this project, so I changed nothing.",
                                        "Nothing was changed: the sidebar code is not here.\nNEXT: done",
                                        "MODE: answer\nOpen the folder that holds the app's code."])
    task = orc.start_task(open_project(orc, project), "Add an Ollama status dot to the sidebar")
    dev = next(x for x in orc.db.team_tasks(task["id"]) if x["plan_key"] == "T1")
    assert dev["status"] == "failed" and "Claude made no changes" in dev["error"]
    edits = [r for r in orc.backends[CLAUDE].requests if "<<<FILE" in r.messages[-1].content]
    assert len(edits) == 1  # Not asked again with the same files.
    orc.resume(task["id"])
    last = orc.backends[CLAUDE].requests[-1].messages[-1].content
    assert "MODE: delegate" in last and "Continuation brief" not in last
    assert answers(orc, task["id"])[-1] == (CLAUDE, "Open the folder that holds the app's code.")


# Qwen's team tasks in agent mode -------------------------------------------------------------

def _tool(name, **args):
    from fusion_ai.backends.base import ChatResult
    return ChatResult(text="", backend=LOCAL, model="fake", tool_calls=[{"name": name, "arguments": args}])


def test_qwen_team_task_works_step_by_step_with_tools(orc_factory, project):
    edits = [_tool("search_code", query="def add"), _tool("read_file", path="calculator.py"),
             _tool("edit_file", path="calculator.py", old_text="def add(a, b):\n    return a + b\n",
                   new_text="def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"),
             "Added multiply() after add().\nUNRESOLVED: none"]
    orc = lead_orc(orc_factory, qwen=edits, claude=[delegate(plan_task())], tools__agent=True,
                   lead__style="claude_first")
    task = orc.start_task(open_project(orc, project), "Add a multiply function")
    dev = next(x for x in orc.db.team_tasks(task["id"]) if x["plan_key"] == "T1")
    assert dev["status"] == "completed" and dev["provider"] == LOCAL, dev["error"]
    assert "def multiply(a, b):" in (project / "calculator.py").read_text(encoding="utf-8")
    first = orc.backends[LOCAL].requests[0]
    assert {t["function"]["name"] for t in first.tools} == {"list_files", "read_file", "search_code", "edit_file",
                                                            "write_file"}
    assert "Team task T1" in first.messages[1].content and "Do not run tests" in first.messages[0].content
    assert orc.db.get_task(task["id"])["status"] == "completed"
    assert len(orc.backends[CLAUDE].requests) == 1  # The plan only: clean run, no review by Claude.


def test_qwen_team_edit_that_fails_is_retried_with_feedback(orc_factory, project):
    bad = [_tool("edit_file", path="calculator.py", old_text="def nope():\n", new_text="x"),
           "I could not find it.\nUNRESOLVED: the edit"]
    good = [_tool("read_file", path="calculator.py"),
            _tool("edit_file", path="calculator.py", old_text="def add(a, b):\n    return a + b\n",
                  new_text="def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"),
            "Added multiply().\nUNRESOLVED: none"]
    orc = lead_orc(orc_factory, qwen=bad + good, claude=[delegate(plan_task())], tools__agent=True,
                   lead__style="claude_first")
    task = orc.start_task(open_project(orc, project), "Add a multiply function")
    dev = next(x for x in orc.db.team_tasks(task["id"]) if x["plan_key"] == "T1")
    assert dev["status"] == "completed" and dev["attempts"] == 2
    retry = [m.content for m in orc.backends[LOCAL].requests[2].messages if m.role == "user"][0]
    assert "could not be applied" in retry and "no file changes" in retry and "edit_file" in retry


def test_mode_line_after_narration_still_counts():
    """A real run: with file tools, Claude wrote a sentence before 'MODE: delegate', and the plan was lost."""
    text = "I've finished mapping the code; here's the plan.\n\n" + delegate(plan_task())
    mode, msg, plan = ctx.parse_lead(text)
    assert mode == "delegate" and msg == "The team will add it." and json.loads(plan)["tasks"][0]["id"] == "T1"
    assert ctx.parse_lead("Looked around.\nMODE: folder C:/Code/app\nIt is there.")[2] == "C:/Code/app"
