"""Planning engine (T3.1-T3.4): plan schema, planner context, planner service, revision and approval."""

import copy
import json

import pytest

from enoy import devplan
from enoy.backends.base import CLAUDE, CODEX
from enoy.devplan import Planner, PlanError, planner_context, validate_plan
from enoy.devstore import DevStore
from enoy.project import Workspace
from test_orchestrator import open_project

ALLOW = ["python -m pytest", "pytest", "npm test"]
GOOD = {
    "summary": "Add multiply to the calculator.",
    "architecture": "Extend calculator.py with one function.",
    "risks": [], "questions": [],
    "tasks": [{
        "id": "T1", "title": "Add multiply", "objective": "calculator.multiply(a, b) returns a * b.",
        "depends_on": [], "allowed_files": ["calculator.py"], "test_files": ["tests/test_multiply.py"],
        "spec": "Add `def multiply(a, b)` to calculator.py returning a * b. Keep existing functions unchanged.",
        "inputs_outputs": "Two numbers in, their product out.", "edge_cases": ["negative numbers", "zero"],
        "tests": [{"name": "test_multiply", "file": "tests/test_multiply.py",
                   "expect_failure": "AttributeError: module 'calculator' has no attribute 'multiply'"}],
        "verify_commands": ["python -m pytest -q tests/test_multiply.py"],
        "regression_commands": ["python -m pytest -q"],
        "acceptance": ["multiply(2, 3) == 6", "existing tests still pass"],
        "completion": "Frozen tests and regression tests pass.",
        "blockers": ["calculator.py is missing or the tests cannot be imported"],
    }],
}


def plan(**task_changes):
    body = copy.deepcopy(GOOD)
    body["tasks"][0].update(task_changes)
    return body


def two_tasks(**second):
    body = copy.deepcopy(GOOD)
    t2 = copy.deepcopy(body["tasks"][0])
    t2.update(id="T2", title="Add divide", depends_on=["T1"], test_files=["tests/test_divide.py"],
              tests=[{"name": "test_divide", "file": "tests/test_divide.py", "expect_failure": "AttributeError"}],
              verify_commands=["python -m pytest -q tests/test_divide.py"])
    t2.update(second)
    body["tasks"].append(t2)
    return body


# T3.1 schema and validator ------------------------------------------------------------------

def test_good_plan_has_no_errors(project):
    errors, warnings = validate_plan(GOOD, Workspace(project), ALLOW)
    assert errors == [] and warnings == []


CODE = "```python\n" + "\n".join(f"x{i} = {i}" for i in range(20)) + "\n```"
BAD = {
    "missing spec": lambda: {k: v for k, v in plan().items()} | {"tasks": [{k: v for k, v in GOOD["tasks"][0].items() if k != "spec"}]},
    "no tasks": lambda: dict(GOOD, tasks=[]),
    "duplicate ids": lambda: two_tasks(id="T1"),
    "unknown dependency": lambda: plan(depends_on=["T9"]),
    "cycle": lambda: (lambda b: (b["tasks"][0].__setitem__("depends_on", ["T2"]), b)[1])(two_tasks()),
    "path escape": lambda: plan(allowed_files=["../outside.py"]),
    "absolute path": lambda: plan(allowed_files=["C:/Windows/win.ini"]),
    "sensitive file": lambda: plan(allowed_files=[".env"]),
    "excluded folder": lambda: plan(allowed_files=["node_modules/x.js"]),
    "test outside test_files": lambda: plan(tests=[{"name": "t", "file": "tests/other.py", "expect_failure": "x"}]),
    "file both test and production": lambda: plan(allowed_files=["calculator.py", "tests/test_multiply.py"]),
    "no tests": lambda: plan(tests=[]),
    "no verify command": lambda: plan(verify_commands=[]),
    "pipe in command": lambda: plan(verify_commands=["python -m pytest | tee out.txt"]),
    "network command": lambda: plan(regression_commands=["curl https://example.com"]),
    "production code block": lambda: plan(spec="Write this:\n" + CODE),
    "patch markers": lambda: plan(spec="<<<FILE calculator.py\ndef multiply(a, b):\n>>>END"),
    "too many files": lambda: plan(allowed_files=[f"src/m{i}.py" for i in range(12)]),
    "wrong type": lambda: plan(acceptance="multiply works"),
}


@pytest.mark.parametrize("case", sorted(BAD))
def test_bad_plans_are_rejected(project, case):
    errors, _ = validate_plan(BAD[case](), Workspace(project), ALLOW)
    assert errors, case


def test_independent_tasks_sharing_files_get_a_warning(project):
    errors, warnings = validate_plan(two_tasks(depends_on=[]), Workspace(project), ALLOW)
    assert errors == [] and any("calculator.py" in w and "parallel" in w for w in warnings)


def test_parse_reply_takes_the_json_block():
    text = "Plan below.\n```json\n" + json.dumps(GOOD) + "\n```\nDone."
    assert devplan.parse_reply(text) == GOOD
    assert devplan.parse_reply(json.dumps(GOOD)) == GOOD
    with pytest.raises(PlanError):
        devplan.parse_reply("I could not make a plan.")


# T3.2 planner context -----------------------------------------------------------------------

SECRET = "sk-SENTINEL-plan-0123456789"


def test_context_has_instructions_facts_and_tree_but_no_secrets(project):
    (project / "CLAUDE.md").write_text("Always use tabs in this project.")
    (project / "planning.md").write_text("- [x] T0 earlier work")
    (project / ".env").write_text("API_KEY=" + SECRET)
    text = planner_context(Workspace(project), "Add multiply", max_chars=60000)
    assert "Always use tabs" in text and "T0 earlier work" in text and "Add multiply" in text
    assert "calculator.py" in text and "Test commands" in text
    assert SECRET not in text and ".env" not in text
    assert "node_modules" not in text


def test_context_respects_the_size_limit(project):
    (project / "CLAUDE.md").write_text("rule\n" * 20000)
    for i in range(300):
        (project / f"mod_{i:03}.py").write_text("x = 1\n")
    text = planner_context(Workspace(project), "Goal", max_chars=6000)
    assert len(text) <= 6000 and "Goal" in text


# T3.3 planner service -----------------------------------------------------------------------

def reply(body):
    return "Here is the plan.\n```json\n" + json.dumps(body) + "\n```"


def setup(orc_factory, project, claude=None, codex=None, **settings):
    orc = orc_factory(qwen=codex or [], claude=claude or [], **settings)
    return orc, open_project(orc, project)


def test_claude_planner_gets_read_only_tools_and_stores_a_draft(orc_factory, project):
    orc, pid = setup(orc_factory, project, claude=[reply(GOOD)])
    p, v = Planner(orc).create(pid, "Add multiply")
    req = orc.backends[CLAUDE].requests[0]
    assert req.purpose == "plan" and not req.native_tools and not req.tools
    assert set(req.enoy_tools) <= {"list_files", "read_file", "search_code"} and req.enoy_tools
    assert req.enoy_env["ENOY_MCP_WORKSPACE"] == str(project.resolve()) or req.enoy_env["ENOY_MCP_WORKSPACE"] == str(project)
    assert "Add multiply" in req.messages[-1].content
    assert v["version"] == 1 and v["author"] == "claude-cli" and v["body"] == GOOD and v["approved"] is None
    assert p["status"] == "draft" and DevStore(orc.db).tasks(p["id"]) == []


def test_invalid_plan_is_repaired_once(orc_factory, project):
    bad = plan(allowed_files=["../outside.py"])
    orc, pid = setup(orc_factory, project, claude=[reply(bad), reply(GOOD)])
    p, v = Planner(orc).create(pid, "Add multiply")
    assert v["body"] == GOOD and len(orc.backends[CLAUDE].requests) == 2
    second = orc.backends[CLAUDE].requests[1].messages[-1].content
    assert "../outside.py" in second and "corrected" in second.lower()


def test_still_invalid_plan_is_reported_and_not_stored(orc_factory, project):
    bad = plan(allowed_files=["../outside.py"])
    orc, pid = setup(orc_factory, project, claude=[reply(bad), "no json at all"])
    with pytest.raises(PlanError) as exc:
        Planner(orc).create(pid, "Add multiply")
    assert exc.value.errors
    plans = DevStore(orc.db).plans(pid)
    assert len(plans) == 1 and DevStore(orc.db).versions(plans[0]["id"]) == []


def test_codex_planner_runs_without_native_tools(orc_factory, project):
    orc, pid = setup(orc_factory, project, codex=[reply(GOOD)])
    orc.cfg["build"]["roles"]["planner"] = "codex-cli"
    Planner(orc).create(pid, "Add multiply")
    req = orc.backends[CODEX].requests[0]
    assert req.purpose == "plan" and not req.native_tools and not req.tools and not orc.backends[CLAUDE].requests


def test_local_only_project_never_sends_the_goal_to_a_cloud_planner(orc_factory, project):
    orc, pid = setup(orc_factory, project, claude=[reply(GOOD)])
    orc.set_project_local_only(pid, True)
    with pytest.raises(PlanError, match="local"):
        Planner(orc).create(pid, "Add multiply")
    assert not orc.backends[CLAUDE].requests


def test_no_planner_assigned_is_a_clear_error(orc_factory, project):
    orc, pid = setup(orc_factory, project)
    orc.cfg["build"]["roles"]["planner"] = ""
    with pytest.raises(PlanError, match="planner"):
        Planner(orc).create(pid, "Add multiply")


# T3.4 revision and approval -----------------------------------------------------------------

def test_revision_creates_the_next_version_with_the_comment(orc_factory, project):
    revised = plan(title="Add multiply (with docs)")
    orc, pid = setup(orc_factory, project, claude=[reply(GOOD), reply(revised)])
    planner = Planner(orc)
    p, _ = planner.create(pid, "Add multiply")
    v2 = planner.revise(p["id"], "Also mention it in the README.")
    assert v2["version"] == 2 and v2["comment"] == "Also mention it in the README." and v2["body"] == revised
    prompt = orc.backends[CLAUDE].requests[1].messages[-1].content
    assert "Also mention it in the README." in prompt and "Add multiply" in prompt and '"T1"' in prompt


def test_only_the_latest_valid_version_can_be_approved(orc_factory, project):
    orc, pid = setup(orc_factory, project, claude=[reply(GOOD), reply(two_tasks())])
    planner = Planner(orc)
    p, _ = planner.create(pid, "Add multiply")
    planner.revise(p["id"], "Add divide too.")
    with pytest.raises(PlanError, match="latest"):
        planner.approve(p["id"], 1)
    approved = planner.approve(p["id"], 2)
    assert approved["approved_version"] == 2
    tasks = {t["task_key"]: t["status"] for t in DevStore(orc.db).tasks(p["id"])}
    assert tasks == {"T1": "ready", "T2": "pending"}
    with pytest.raises(PlanError):
        planner.revise(p["id"], "More please.")  # An approved plan is final; a change needs a new plan.


def test_a_version_that_fails_validation_now_cannot_be_approved(orc_factory, project):
    orc, pid = setup(orc_factory, project)
    store = DevStore(orc.db)
    p = store.create_plan(pid, "x", "y")
    store.add_version(p["id"], plan(allowed_files=[".env"]), author="user")
    with pytest.raises(PlanError) as exc:
        Planner(orc).approve(p["id"], 1)
    assert any(".env" in e for e in exc.value.errors)
    assert store.get_plan(p["id"])["approved_version"] is None
