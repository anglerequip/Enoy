"""Implementer execution (T6.1-T6.6): scoped tools, the tool loop, the task context, the test-first
phases and blockers. Real git and real pytest run in a temporary repository; the model is scripted."""

import hashlib
import json
import shutil
import subprocess
import threading

import pytest

from enoy import commands
from enoy.backends.base import BackendError, ChatResult
from enoy.devagent import (Scope, TaskExecution, ToolDenied, ToolLoop, check_expected_failure, task_context)
from enoy.devstore import DevStore
from enoy.gitwork import GitWork
from enoy.storage import Storage

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
ALLOW = ["python -m pytest", "pytest", "git status", "git diff"]


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


# A scripted model --------------------------------------------------------------------------

class Script:
    """Each step is a list of (tool, args) calls, a final text, a callable(messages) doing either, or an exception."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.seen = []

    def __call__(self, messages, tools):
        self.seen.append([m.content for m in messages])
        if not self.steps:
            return ChatResult("Done.", "fake", "fake")
        step = self.steps.pop(0)
        if callable(step):
            step = step(messages)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, str):
            return ChatResult(step, "fake", "fake", usage={"output_tokens": 10})
        calls = [{"id": f"c{len(self.seen)}_{i}", "name": n, "arguments": a} for i, (n, a) in enumerate(step)]
        return ChatResult("", "fake", "fake", tool_calls=calls, usage={"output_tokens": 10})


# Fixtures ----------------------------------------------------------------------------------

TEST_FILE = "tests/test_multiply.py"
GOOD_TEST = "from calc import multiply\n\n\ndef test_multiply():\n    assert multiply(2, 3) == 6\n"
TASK = {"id": "T1", "title": "Add multiply", "objective": "calc.multiply(a, b) returns a * b.", "depends_on": [],
        "allowed_files": ["calc.py"], "test_files": [TEST_FILE],
        "spec": "Add def multiply(a, b) to calc.py returning a * b.", "inputs_outputs": "numbers in, product out",
        "edge_cases": ["zero"], "tests": [{"name": "test_multiply", "file": TEST_FILE,
                                            "expect_failure": "ImportError: cannot import name 'multiply'"}],
        "verify_commands": ["python -m pytest -q tests/test_multiply.py"], "regression_commands": ["python -m pytest -q"],
        "acceptance": ["multiply(2, 3) == 6"], "completion": "tests pass", "blockers": ["calc.py missing"]}


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "calc"
    (r / "tests").mkdir(parents=True)
    git(tmp_path, "init", "-q", "-b", "main", str(r))
    git(r, "config", "user.name", "Test")
    git(r, "config", "user.email", "t@example.com")
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (r / "tests" / "test_add.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    (r / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    (r / "CLAUDE.md").write_text("Keep functions small.\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "initial")
    return r


@pytest.fixture
def env(repo, tmp_path):
    st = Storage(tmp_path / "enoy.db")
    store = DevStore(st)
    pid = st.upsert_project(str(repo), "calc")["id"]
    plan = store.create_plan(pid, "Calc", "Add multiply")
    body = {"summary": "s", "architecture": "a", "risks": [], "questions": [], "tasks": [TASK]}
    store.add_version(plan["id"], body)
    store.approve_version(plan["id"], 1)
    task = store.tasks(plan["id"])[0]
    claim = store.claim(task["id"])
    gw = GitWork(repo, tmp_path / "wt")
    return store, plan, store.get_task(task["id"]), claim, gw, body


def execution(env, script, **kw):
    store, plan, task, claim, gw, body = env
    kw.setdefault("allowlist", ALLOW)
    return TaskExecution(store=store, task=task, plan_body=body, git=gw, complete=script, **kw)


IMPL_GOOD = [("edit_file", {"path": "calc.py", "old_text": "    return a + b\n",
                            "new_text": "    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"})]
WRITE_TEST = [("write_file", {"path": TEST_FILE, "content": GOOD_TEST})]


# T6.1 scoped tools ---------------------------------------------------------------------------

@pytest.fixture
def scope(repo):
    (repo / ".env").write_text("API_KEY=secret\n")
    return Scope(repo, writable={"calc.py"}, frozen={TEST_FILE}, commands=["python -m pytest -q"], allowlist=ALLOW,
                 unattended=True)


def test_writes_outside_the_allowed_files_are_refused(scope, repo):
    with pytest.raises(ToolDenied, match="(?i)allowed"):
        scope.write("tests/test_add.py", "broken")
    assert "assert add(1, 2) == 3" in (repo / "tests" / "test_add.py").read_text()
    with pytest.raises(ToolDenied):
        scope.write("../outside.py", "x")


def test_frozen_tests_cannot_be_written(scope):
    with pytest.raises(ToolDenied, match="frozen"):
        scope.write(TEST_FILE, "def test_multiply():\n    assert True\n")


def test_secrets_cannot_be_read(scope):
    with pytest.raises(ToolDenied):
        scope.read(".env")


def test_unattended_commands_are_limited(scope):
    with pytest.raises(ToolDenied):
        scope.check_command("curl https://example.com")
    with pytest.raises(ToolDenied, match="approval"):
        scope.check_command("node build.js")
    scope.check_command("python -m pytest -q")  # The task's own command.
    scope.check_command("git status")             # Allowlisted.


def test_allowed_write_is_syntax_checked_and_recorded(scope, repo):
    with pytest.raises(ToolDenied, match="valid"):
        scope.write("calc.py", "def broken(:\n")
    scope.write("calc.py", "def add(a, b):\n    return b + a\n")
    assert scope.changed == {"calc.py"} and "b + a" in (repo / "calc.py").read_text()


def test_commands_use_the_projects_virtual_environment(scope, repo, monkeypatch):
    seen = {}
    monkeypatch.setattr(commands, "run", lambda argv, cwd, timeout_s, cancel=None, venv_root=None:
                        seen.update(argv=argv, venv_root=venv_root) or commands.RunResult("passed", 0, "ok", "", 0.1))
    scope.venv_root = repo.parent
    scope.run("python -m pytest -q")
    assert seen == {"argv": ["python", "-m", "pytest", "-q"], "venv_root": repo.parent}


# T6.2 tool loop ---------------------------------------------------------------------------------

def loop(scope, script, **kw):
    return ToolLoop(script, scope, "system", "user", **kw)


def test_loop_finishes_on_an_answer_without_tool_calls(scope):
    result = loop(scope, Script([("read_file", {"path": "calc.py"})], "All done.")).run()
    assert result.status == "finished" and result.summary == "All done." and result.steps == 2


def test_denied_tool_calls_are_reported_back_to_the_model(scope):
    script = Script([("write_file", {"path": "tests/test_add.py", "content": "x"})], "Stopped.")
    loop(scope, script).run()
    assert any("denied" in c.lower() or "allowed" in c.lower() for c in script.seen[1])


@pytest.mark.parametrize("error,status", [(BackendError("budget", "cap"), "limit"),
                                          (BackendError("unavailable", "offline"), "network"),
                                          (BackendError("failed", "boom"), "error")])
def test_loop_maps_model_errors(scope, error, status):
    assert loop(scope, Script(error)).run().status == status


def test_loop_stops_at_the_step_and_token_limits(scope):
    many = [[("list_files", {})] for _ in range(10)]
    assert loop(scope, Script(*many), max_steps=3).run().status == "limit"
    assert loop(scope, Script(*many), max_output_tokens=25).run().status == "limit"


def test_cancel_stops_the_loop(scope):
    cancel = threading.Event()
    cancel.set()
    assert loop(scope, Script([("list_files", {})]), cancel=cancel).run().status == "cancelled"


# T6.3 task context ---------------------------------------------------------------------------------

def test_context_holds_only_this_task(repo):
    # The packer receives only this task and its finished dependencies' summaries: no plan, no chat history.
    system, user = task_context(dict(TASK, id="T2"), "implement", repo, deps_done=[{"key": "T1", "title": "Add add",
                                "summary": "Added add()."}])
    assert "Add def multiply(a, b)" in user and "Added add()." in user and "T3" not in system + user
    assert "def add(a, b)" in user                     # The allowed file's current content.
    assert "Keep functions small." in system           # Project instructions.
    assert "python -m pytest -q tests/test_multiply.py" in user


def test_context_stays_within_its_size_limit(repo):
    (repo / "calc.py").write_text("x = 1\n" * 50000)
    system, user = task_context(TASK, "implement", repo, max_chars=12000)
    assert len(system) + len(user) <= 12000


# T6.4 test phase ---------------------------------------------------------------------------------

def test_expected_failure_checks():
    tests = TASK["tests"]
    assert check_expected_failure("1 passed", 0, tests)[2] == "passes"
    assert check_expected_failure('File "tests/test_multiply.py", line 3\nSyntaxError: invalid syntax', 2, tests)[2] == "broken"
    assert check_expected_failure("collected 0 items\nno tests ran", 5, tests)[2] == "broken"
    ok, _, kind = check_expected_failure("E   ImportError: cannot import name 'multiply' from 'calc'\n1 error", 2, tests)
    assert ok and kind == ""
    assert check_expected_failure("FAILED tests/test_x.py::test_other - KeyError: 1", 1, tests)[2] == "wrong"


def test_test_phase_writes_fails_as_expected_then_freezes(env):
    store, plan, task, claim, gw, body = env
    ex = execution(env, Script(WRITE_TEST, "Wrote the test.", IMPL_GOOD, "Implemented."))
    result = ex.run(claim)
    assert result.ok and result.status == "passed"
    frozen = store.frozen(task["id"])
    assert frozen == {TEST_FILE: hashlib.sha256(GOOD_TEST.encode()).hexdigest()}
    run = store.runs(task["id"])[-1]
    assert run["test_commit"] and run["impl_commit"] and run["status"] == "completed"
    assert git(gw.repo, "log", "-1", "--format=%s", run["test_commit"]).startswith("T1 tests")
    assert git(gw.repo, "show", "--name-only", "--format=", run["test_commit"]).splitlines() == [TEST_FILE]
    assert git(gw.repo, "show", "--name-only", "--format=", run["impl_commit"]).splitlines() == ["calc.py"]
    assert store.get_task(task["id"])["status"] == "verifying"
    assert "passed" in result.evidence["tests"] and "passed" in result.evidence["regression"]


def test_a_test_that_passes_before_the_change_blocks_the_task(env):
    store, plan, task, claim, gw, body = env
    passing = [("write_file", {"path": TEST_FILE, "content": "def test_multiply():\n    assert 2 * 3 == 6\n"})]
    result = execution(env, Script(passing, "Done.")).run(claim)
    assert not result.ok and result.status == "blocked" and "pass before" in result.reason
    assert store.get_task(task["id"])["status"] == "blocked" and store.frozen(task["id"]) == {}


def test_a_broken_test_gets_one_correction_round(env):
    store, plan, task, claim, gw, body = env
    broken = [("write_file", {"path": TEST_FILE, "content": "def test_multiply(:\n"})]
    script = Script(broken, "Done.", WRITE_TEST, "Fixed.", IMPL_GOOD, "Implemented.")
    result = execution(env, script).run(claim)
    # The syntax check refuses the broken file before it is written; the model then writes a valid test.
    assert result.ok
    assert any("valid" in c.lower() for step in script.seen for c in step)


# T6.5 implementation phase ---------------------------------------------------------------------------

def test_tampering_with_a_frozen_test_is_rejected_even_if_tests_pass(env):
    store, plan, task, claim, gw, body = env

    def sneaky(messages):
        wt = gw.base / "p{}-T1-a1".format(plan["id"])
        (wt / TEST_FILE).write_text("def test_multiply():\n    assert True\n")  # Bypasses the tools, like a script would.
        return "Implemented."

    result = execution(env, Script(WRITE_TEST, "Wrote it.", sneaky)).run(claim)
    assert not result.ok and result.status == "failed" and "frozen" in result.reason.lower()
    assert store.get_task(task["id"])["status"] == "failed" and not store.runs(task["id"])[-1]["impl_commit"]


def test_failing_frozen_tests_get_correction_rounds_then_fail(env):
    store, plan, task, claim, gw, body = env
    wrong = [("edit_file", {"path": "calc.py", "old_text": "    return a + b\n",
                            "new_text": "    return a + b\n\n\ndef multiply(a, b):\n    return a + b\n"})]
    script = Script(WRITE_TEST, "Wrote it.", wrong, "Done.", "Still done.", "Done again.")
    result = execution(env, script, max_fix_rounds=2).run(claim)
    assert not result.ok and result.status == "failed" and "test_multiply" in result.reason
    assert any("did not pass" in c for step in script.seen for c in step)


def test_failing_frozen_test_fixed_in_a_correction_round(env):
    store, plan, task, claim, gw, body = env
    wrong = [("edit_file", {"path": "calc.py", "old_text": "    return a + b\n",
                            "new_text": "    return a + b\n\n\ndef multiply(a, b):\n    return a + b\n"})]
    fix = [("edit_file", {"path": "calc.py", "old_text": "def multiply(a, b):\n    return a + b",
                          "new_text": "def multiply(a, b):\n    return a * b"})]
    result = execution(env, Script(WRITE_TEST, "Wrote it.", wrong, "Done.", fix, "Fixed.")).run(claim)
    assert result.ok


# T6.6 blockers --------------------------------------------------------------------------------------

def test_report_blocker_stops_the_task_for_a_plan_revision(env):
    store, plan, task, claim, gw, body = env
    blocker = [("report_blocker", {"kind": "test_spec_wrong",
                                   "details": "multiply already exists in another module; the test would pass."})]
    result = execution(env, Script(blocker)).run(claim)
    assert result.status == "blocked" and result.blocker_kind == "test_spec_wrong"
    t = store.get_task(task["id"])
    assert t["status"] == "blocked" and "plan revision" in t["blocker"] and "already exists" in t["blocker"]
    assert store.runs(task["id"])[-1]["status"] == "blocked"


def test_no_network_moves_the_task_to_waiting(env):
    store, plan, task, claim, gw, body = env
    result = execution(env, Script(BackendError("unavailable", "offline"))).run(claim)
    assert result.status == "network" and store.get_task(task["id"])["status"] == "waiting_network"
