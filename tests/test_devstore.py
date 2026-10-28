"""Build workspace persistence: plans, versions, tasks, runs, frozen tests, model calls."""

import sqlite3
import time

import pytest

from enoy.storage import Storage

DEV_TABLES = ("dev_plans", "dev_plan_versions", "dev_tasks", "dev_runs", "dev_frozen_files", "model_calls")


def tables(path):
    with sqlite3.connect(path) as db:
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def counts(path):
    with sqlite3.connect(path) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: db.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names}


def old_database(path):
    """A database as Enoy 0.6.14 left it: user data, no Build tables."""
    st = Storage(path)
    pid = st.upsert_project("C:/code/app", "app")["id"]
    tid = st.create_task(pid, "Keep this chat", "Keep this chat", "chat")["id"]
    st.add_message(tid, "user", "Keep this message")
    st.close()
    with sqlite3.connect(path) as db:
        for name, in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'dev_%'").fetchall():
            db.execute(f"DROP TRIGGER {name}")
        for t in DEV_TABLES:
            db.execute(f"DROP TABLE IF EXISTS {t}")
    return path


def test_old_database_gains_build_tables_without_losing_rows(tmp_path):
    path = old_database(tmp_path / "enoy.db")
    before = counts(path)
    assert not set(DEV_TABLES) & tables(path)
    Storage(path).close()
    after = counts(path)
    assert set(DEV_TABLES) <= set(after)
    assert {k: v for k, v in after.items() if k in before} == before
    assert all(after[t] == 0 for t in DEV_TABLES)


def test_opening_twice_is_harmless(tmp_path):
    path = old_database(tmp_path / "enoy.db")
    Storage(path).close()
    first = counts(path)
    Storage(path).close()
    assert counts(path) == first


def _raw_plan(st):
    pid = st.upsert_project("C:/code/app", "app")["id"]
    now = time.time()
    plan = st._exec("INSERT INTO dev_plans (project_id, title, goal, status, created, updated) "
                    "VALUES (?, 'p', 'g', 'draft', ?, ?)", (pid, now, now)).lastrowid
    st._exec("INSERT INTO dev_plan_versions (plan_id, version, body, sha, created) VALUES (?, 1, '{}', 'abc', ?)",
             (plan, now))
    return plan


def test_approved_plan_version_cannot_change_or_be_deleted(tmp_path):
    st = Storage(tmp_path / "enoy.db")
    plan = _raw_plan(st)
    st._exec("UPDATE dev_plan_versions SET body = '{\"x\":1}', sha = 'def' WHERE plan_id = ?", (plan,))  # Drafts may change.
    st._exec("UPDATE dev_plan_versions SET approved = ? WHERE plan_id = ?", (time.time(), plan))
    with pytest.raises(sqlite3.IntegrityError, match="approved plan version"):
        st._exec("UPDATE dev_plan_versions SET body = '{}' WHERE plan_id = ?", (plan,))
    with pytest.raises(sqlite3.IntegrityError, match="approved plan version"):
        st._exec("UPDATE dev_plan_versions SET approved = NULL WHERE plan_id = ?", (plan,))
    with pytest.raises(sqlite3.IntegrityError, match="approved plan version"):
        st._exec("DELETE FROM dev_plan_versions WHERE plan_id = ?", (plan,))


def test_frozen_test_hashes_cannot_change_or_be_deleted(tmp_path):
    st = Storage(tmp_path / "enoy.db")
    plan = _raw_plan(st)
    now = time.time()
    task = st._exec("INSERT INTO dev_tasks (plan_id, version, task_key, title, status, created, updated) "
                    "VALUES (?, 1, 'T1', 't', 'ready', ?, ?)", (plan, now, now)).lastrowid
    st._exec("INSERT INTO dev_frozen_files (task_id, path, sha256, frozen) VALUES (?, 'tests/test_a.py', 'h1', ?)",
             (task, now))
    with pytest.raises(sqlite3.IntegrityError, match="frozen"):
        st._exec("UPDATE dev_frozen_files SET sha256 = 'h2' WHERE task_id = ?", (task,))
    with pytest.raises(sqlite3.IntegrityError, match="frozen"):
        st._exec("DELETE FROM dev_frozen_files WHERE task_id = ?", (task,))
    with pytest.raises(sqlite3.IntegrityError):
        st._exec("INSERT INTO dev_frozen_files (task_id, path, sha256, frozen) VALUES (?, 'tests/test_a.py', 'h3', ?)",
                 (task, now))


# T1.2 store API and state machine ----------------------------------------------------

import threading

from enoy import devstore
from enoy.devstore import DevStore, TransitionError

PLAN = {"tasks": [{"id": "T1", "title": "Add multiply", "depends_on": [], "allowed_files": ["calc.py"],
                   "test_files": ["tests/test_calc.py"]},
                  {"id": "T2", "title": "Add divide", "depends_on": ["T1"], "allowed_files": ["calc.py"],
                   "test_files": ["tests/test_div.py"]}]}


@pytest.fixture
def ds(tmp_path):
    st = Storage(tmp_path / "enoy.db")
    store = DevStore(st)
    pid = st.upsert_project("C:/code/app", "app")["id"]
    plan = store.create_plan(pid, "Calculator", "Add multiply and divide")
    store.add_version(plan["id"], PLAN, author="planner")
    return store, plan


def approved(ds):
    store, plan = ds
    store.approve_version(plan["id"], 1)
    return store, plan, {t["task_key"]: t for t in store.tasks(plan["id"])}


def test_versions_are_numbered_and_fingerprinted(ds):
    store, plan = ds
    v2 = store.add_version(plan["id"], {"tasks": []}, author="user", comment="Smaller please")
    assert v2["version"] == 2 and v2["comment"] == "Smaller please"
    assert store.version(plan["id"], 1)["sha"] == devstore.fingerprint(PLAN)
    assert store.version(plan["id"], 1)["body"] == PLAN


def test_approval_locks_the_version_and_creates_its_tasks(ds):
    store, plan, tasks = approved(ds)
    assert store.get_plan(plan["id"])["approved_version"] == 1
    assert tasks["T1"]["status"] == "ready" and tasks["T2"]["status"] == "pending"
    assert tasks["T1"]["allowed_files"] == ["calc.py"] and tasks["T2"]["depends_on"] == ["T1"]
    with pytest.raises(sqlite3.IntegrityError):
        store.st._exec("UPDATE dev_plan_versions SET body = '{}' WHERE plan_id = ?", (plan["id"],))
    with pytest.raises(TransitionError, match="already approved"):
        store.approve_version(plan["id"], 1)


def test_unapproved_version_tasks_are_never_created(ds):
    store, plan = ds
    with pytest.raises(TransitionError, match="not approved"):
        store.add_tasks(plan["id"], 1, PLAN["tasks"])
    assert store.tasks(plan["id"]) == []


def test_illegal_transitions_raise(ds):
    store, plan, tasks = approved(ds)
    with pytest.raises(TransitionError):
        store.transition(tasks["T1"]["id"], "implementing")  # Not claimed yet.
    with pytest.raises(TransitionError):
        store.transition(tasks["T2"]["id"], "claimed")  # Must be claimed through claim().
    with pytest.raises(TransitionError):
        store.transition("x", "ready")


def test_full_happy_path_and_dependency_release(ds):
    store, plan, tasks = approved(ds)
    t1 = tasks["T1"]["id"]
    token = store.claim(t1)
    assert token and store.get_task(t1)["attempts"] == 1
    for status in ("testing", "implementing", "verifying", "reviewing", "merging"):
        store.transition(t1, status, claim=token)
    with pytest.raises(TransitionError, match="commit"):
        store.transition(t1, "completed", claim=token)
    store.transition(t1, "completed", claim=token, commit_id="abc123", summary="Done", evidence={"tests": "2 passed"})
    done = store.get_task(t1)
    assert done["claim"] == "" and done["finished"] and done["evidence"] == {"tests": "2 passed"}
    with pytest.raises(TransitionError):
        store.transition(t1, "ready")  # Completed is final.
    assert store.refresh_ready(plan["id"]) == [tasks["T2"]["id"]]
    assert store.get_task(tasks["T2"]["id"])["status"] == "ready"


def test_stale_claim_cannot_change_a_task(ds):
    store, plan, tasks = approved(ds)
    t1 = tasks["T1"]["id"]
    token = store.claim(t1)
    with pytest.raises(TransitionError, match="claim"):
        store.transition(t1, "testing", claim="someone-else")
    store.transition(t1, "ready", claim=token)  # Released for a retry.
    second = store.claim(t1)
    assert second != token and store.get_task(t1)["attempts"] == 2
    with pytest.raises(TransitionError, match="claim"):
        store.transition(t1, "testing", claim=token)


def test_two_simultaneous_claims_have_exactly_one_winner(ds):
    store, plan, tasks = approved(ds)
    t1 = tasks["T1"]["id"]
    results, start = [], threading.Barrier(8)

    def grab():
        start.wait()
        results.append(store.claim(t1))

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len([r for r in results if r]) == 1


def test_runs_and_frozen_files(ds):
    store, plan, tasks = approved(ds)
    t1 = tasks["T1"]["id"]
    token = store.claim(t1)
    run = store.start_run(t1, "implementer", provider="deepseek", model="m", phase="tests")
    assert store.get_run(run)["attempt"] == 1 and store.get_run(run)["status"] == "running"
    store.freeze(t1, run, {"tests/test_calc.py": "h1"})
    assert store.frozen(t1) == {"tests/test_calc.py": "h1"}
    with pytest.raises(sqlite3.IntegrityError):
        store.freeze(t1, run, {"tests/test_calc.py": "h2"})
    store.finish_run(run, "completed", summary="Tests fail as expected", test_commit="t1c")
    assert store.get_run(run)["finished"] and store.get_run(run)["test_commit"] == "t1c"
    assert [r["id"] for r in store.runs(t1)] == [run]
    store.transition(t1, "testing", claim=token)


# T1.3 startup recovery ---------------------------------------------------------------

def test_recover_interrupts_running_work_and_keeps_completed(ds):
    store, plan, tasks = approved(ds)
    t1, t2 = tasks["T1"]["id"], tasks["T2"]["id"]
    token = store.claim(t1)
    store.transition(t1, "testing", claim=token)
    run = store.start_run(t1, "implementer", phase="tests")
    assert store.recover() == [t1]
    task = store.get_task(t1)
    assert task["status"] == "interrupted" and task["claim"] == ""
    assert store.get_run(run)["status"] == "interrupted" and store.get_run(run)["error"]
    assert store.get_task(t2)["status"] == "pending"
    store.transition(t1, "ready")
    token = store.claim(t1)
    for status in ("testing", "implementing", "verifying", "merging"):
        store.transition(t1, status, claim=token)
    store.transition(t1, "completed", claim=token, commit_id="c1")
    assert store.recover() == []
    assert store.get_task(t1)["status"] == "completed"


def test_app_startup_runs_build_recovery(monkeypatch, tmp_path):
    from enoy import app as app_mod
    calls = []
    monkeypatch.setattr(app_mod.devstore.DevStore, "recover", lambda self: calls.append(True) or [])
    app_mod.build(synchronous=True, save_config=False)
    assert calls == [True]
