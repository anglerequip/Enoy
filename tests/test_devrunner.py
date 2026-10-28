"""Build scheduler (T8.1-T8.2): manual and sequential runs, retries, offline waits, cancel, recovery and
optional parallel runs. Real git and pytest; scripted implementer models."""

import threading
import time

import pytest

from enoy.backends.base import BackendError
from enoy.devrunner import BuildRunner
from enoy.devstore import DevStore
from enoy.gitwork import GitWork
from enoy.storage import Storage
from test_devagent import ALLOW, Script, git, repo  # noqa: F401


def task(key, title, files, test_file, test_name, expect, deps=(), verify=None):
    return {"id": key, "title": title, "objective": title, "depends_on": list(deps), "allowed_files": files,
            "test_files": [test_file], "spec": title, "inputs_outputs": "-", "edge_cases": [],
            "tests": [{"name": test_name, "file": test_file, "expect_failure": expect}],
            "verify_commands": [verify or f"python -m pytest -q {test_file}"], "regression_commands": [],
            "acceptance": [title], "completion": "tests pass", "blockers": []}


T1 = task("T1", "Add multiply", ["calc.py"], "tests/test_multiply.py", "test_multiply", "ImportError")
T2 = task("T2", "Add divide", ["calc.py"], "tests/test_divide.py", "test_divide", "ImportError", deps=["T1"])
T3 = task("T3", "Add subtract", ["ops.py"], "tests/test_subtract.py", "test_subtract", "ModuleNotFoundError")

MUL_TEST = "from calc import multiply\n\n\ndef test_multiply():\n    assert multiply(2, 3) == 6\n"
DIV_TEST = "from calc import divide\n\n\ndef test_divide():\n    assert divide(6, 3) == 2\n"
SUB_TEST = "from ops import subtract\n\n\ndef test_subtract():\n    assert subtract(5, 3) == 2\n"
ADD_SRC = "    return a + b\n"
MUL = [("edit_file", {"path": "calc.py", "old_text": ADD_SRC, "new_text": ADD_SRC + "\n\ndef multiply(a, b):\n    return a * b\n"})]
MUL_WRONG = [("edit_file", {"path": "calc.py", "old_text": ADD_SRC, "new_text": ADD_SRC + "\n\ndef multiply(a, b):\n    return a + b\n"})]
DIV = [("edit_file", {"path": "calc.py", "old_text": "    return a * b\n",
                      "new_text": "    return a * b\n\n\ndef divide(a, b):\n    return a / b\n"})]
SUB = [("write_file", {"path": "ops.py", "content": "def subtract(a, b):\n    return a - b\n"})]


def scripts():
    return {
        "T1": lambda: Script([("write_file", {"path": "tests/test_multiply.py", "content": MUL_TEST})], "Tests.", MUL, "Done."),
        "T2": lambda: Script([("write_file", {"path": "tests/test_divide.py", "content": DIV_TEST})], "Tests.", DIV, "Done."),
        "T3": lambda: Script([("write_file", {"path": "tests/test_subtract.py", "content": SUB_TEST})], "Tests.", SUB, "Done."),
    }


@pytest.fixture
def setup(repo, tmp_path):
    def make(tasks=(T1, T2, T3), plans=None, **kw):
        st = Storage(tmp_path / "enoy.db")
        store = DevStore(st)
        pid = st.upsert_project(str(repo), "calc")["id"]
        plan = store.create_plan(pid, "Calc", "Grow the calculator")
        store.add_version(plan["id"], {"summary": "s", "architecture": "a", "risks": [], "questions": [],
                                       "tasks": list(tasks)})
        store.approve_version(plan["id"], 1)
        made = {}
        script_for = plans or scripts()

        def complete_for(t):
            s = script_for[t["task_key"]]()
            made.setdefault(t["task_key"], []).append(s)
            return s

        synced = []
        runner = BuildRunner(store, GitWork(repo, tmp_path / "wt"), complete_for, ALLOW,
                             sync=lambda p: synced.append(p) or {"written": True}, **kw)
        return store, plan, runner, made, synced
    return make


def status(store, plan):
    return {t["task_key"]: t["status"] for t in store.tasks(plan["id"])}


# T8.1 ------------------------------------------------------------------------------------------

def test_manual_run_of_one_task(setup):
    store, plan, runner, made, synced = setup()
    t1 = next(t for t in store.tasks(plan["id"]) if t["task_key"] == "T1")
    result = runner.run_task(t1["id"])
    assert result["status"] == "completed"
    assert status(store, plan) == {"T1": "completed", "T2": "ready", "T3": "ready"}
    p = store.get_plan(plan["id"])
    assert p["integration_branch"] == f"enoy/plan-{plan['id']}" and p["base_commit"] and p["status"] == "running"


def test_sequential_run_completes_the_plan_in_order_with_fresh_contexts(setup, repo):
    store, plan, runner, made, synced = setup()
    report = runner.run_all(plan["id"])
    assert report.status == "completed" and [r["task"] for r in report.ran] == ["T1", "T2", "T3"]
    assert set(status(store, plan).values()) == {"completed"} and store.get_plan(plan["id"])["status"] == "completed"
    branch = f"enoy/plan-{plan['id']}"
    calc = git(repo, "show", f"{branch}:calc.py")
    assert "def multiply" in calc and "def divide" in calc and "def subtract" in git(repo, "show", f"{branch}:ops.py")
    assert "multiply" not in (repo / "calc.py").read_text()  # The user's checkout is untouched.
    assert synced.count(plan["id"]) == 3
    # Each task starts with only the system prompt and its own task; T2 also sees its dependency T1's summary.
    absent = {"T1": ["Add divide", "Add subtract"], "T2": ["Add subtract"], "T3": ["Add multiply", "Add divide"]}
    for key, made_scripts in made.items():
        first = made_scripts[0].seen[0]
        assert len(first) == 2 and f"# Your task {key}" in first[1]
        assert not any(title in first[0] + first[1] for title in absent[key])


def test_a_failed_attempt_is_retried_in_a_new_worktree(setup):
    attempts = []

    def t1():
        n = len(attempts)
        attempts.append(n)
        if n == 0:
            return Script([("write_file", {"path": "tests/test_multiply.py", "content": MUL_TEST})], "Tests.",
                          MUL_WRONG, "Done.")
        return Script(MUL, "Done.")  # The retry reuses the frozen tests and starts with the implementation.

    store, plan, runner, made, synced = setup(tasks=(T1,), plans={"T1": t1}, max_attempts=2, max_fix_rounds=0)
    report = runner.run_all(plan["id"])
    assert report.status == "completed", [(t["status"], t["blocker"]) for t in store.tasks(plan["id"])]
    t = store.tasks(plan["id"])[0]
    runs = store.runs(t["id"])
    assert t["attempts"] == 2 and [r["status"] for r in runs] == ["failed", "completed"]
    assert runs[0]["branch"].endswith("-a1") and runs[1]["branch"].endswith("-a2")
    assert runs[1]["test_commit"] != runs[0]["test_commit"]  # Copied onto the new branch, same frozen content.
    assert "Phase 2 of 2" in made["T1"][1].seen[0][0] and "Phase 1" not in made["T1"][1].seen[0][0]


def test_exhausted_attempts_stay_failed_and_independent_work_continues(setup):
    def bad():
        return Script([("write_file", {"path": "tests/test_multiply.py", "content": MUL_TEST})], "Tests.",
                      MUL_WRONG, "Done.")

    plans = scripts() | {"T1": bad}
    store, plan, runner, made, synced = setup(plans=plans, max_attempts=2, max_fix_rounds=0)
    report = runner.run_all(plan["id"])
    assert report.status == "attention"
    assert status(store, plan) == {"T1": "failed", "T2": "pending", "T3": "completed"}
    assert "test_multiply" in store.tasks(plan["id"])[0]["blocker"]


def test_offline_pauses_and_resume_continues(setup):
    online = {"now": False}

    def t1():
        if not online["now"]:
            return Script(BackendError("unavailable", "No connection."))
        return scripts()["T1"]()

    store, plan, runner, made, synced = setup(tasks=(T1,), plans={"T1": t1}, online=lambda: online["now"])
    report = runner.run_all(plan["id"])
    assert report.status == "offline" and status(store, plan) == {"T1": "waiting_network"}
    assert runner.resume(plan["id"])["waiting"] == 1 and status(store, plan) == {"T1": "waiting_network"}
    online["now"] = True
    assert runner.resume(plan["id"])["ready"] == 1
    assert runner.run_all(plan["id"]).status == "completed"


def test_stop_cancels_the_running_task_and_returns_it_to_ready(setup):
    holder = {}

    def t1():
        def stop(messages):
            holder["runner"].stop()
            return [("list_files", {})]
        return Script(stop, "never reached")

    store, plan, runner, made, synced = setup(tasks=(T1,), plans={"T1": t1})
    holder["runner"] = runner
    report = runner.run_all(plan["id"])
    assert report.status == "stopped" and status(store, plan) == {"T1": "ready"}
    assert store.runs(store.tasks(plan["id"])[0]["id"])[-1]["status"] == "cancelled"


def test_resume_after_restart_records_merged_work_and_requeues_the_rest(setup):
    store, plan, runner, made, synced = setup(tasks=(T1, T3))
    t1, t3 = store.tasks(plan["id"])
    runner.run_task(t1["id"])
    # T3 was in the middle of its run when Enoy stopped.
    claim = store.claim(t3["id"])
    store.transition(t3["id"], "testing", claim=claim)
    store.recover()
    counts = runner.resume(plan["id"])
    assert counts["requeued"] == 1 and status(store, plan) == {"T1": "completed", "T3": "ready"}


def test_the_same_task_is_never_run_twice_at_once(setup):
    store, plan, runner, made, synced = setup(tasks=(T1,))
    t1 = store.tasks(plan["id"])[0]
    results = []
    threads = [threading.Thread(target=lambda: results.append(runner.run_task(t1["id"])["status"])) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(results).count("completed") == 1 and results.count("not_ready") == 2


# T8.2 ------------------------------------------------------------------------------------------

def test_parallel_runs_are_off_by_default(setup):
    store, plan, runner, made, synced = setup()
    with pytest.raises(RuntimeError, match="off"):
        runner.run_parallel(plan["id"])


def test_batches_never_share_files(setup):
    t2_alone = dict(T2, depends_on=[])
    store, plan, runner, made, synced = setup(tasks=(T1, t2_alone, T3), parallel_allowed=True, max_parallel=3)
    keys = [t["task_key"] for t in runner.pick_batch(store.tasks(plan["id"]))]
    assert keys == ["T1", "T3"]  # T2 also changes calc.py, so it waits for the next batch.


def test_parallel_run_overlaps_independent_tasks_and_merges_them_one_by_one(setup, repo):
    times = {}

    def slow(key, base):
        def make():
            s = base()
            original = s.steps[0]

            def first(messages):
                times.setdefault(key, []).append(time.monotonic())
                time.sleep(0.8)
                return original
            s.steps[0] = first
            return s
        return make

    plans = scripts()
    plans = plans | {"T1": slow("T1", plans["T1"]), "T3": slow("T3", plans["T3"])}
    store, plan, runner, made, synced = setup(plans=plans, parallel_allowed=True, max_parallel=2)
    report = runner.run_parallel(plan["id"])
    assert report.status == "completed" and set(status(store, plan).values()) == {"completed"}
    assert abs(times["T1"][0] - times["T3"][0]) < 0.6  # Both started together.
    branch = f"enoy/plan-{plan['id']}"
    assert "def divide" in git(repo, "show", f"{branch}:calc.py") and "def subtract" in git(repo, "show", f"{branch}:ops.py")
    merges = git(repo, "log", "--merges", "--format=%s", branch).splitlines()
    assert sorted(merges) == ["T1: Add multiply", "T2: Add divide", "T3: Add subtract"]
