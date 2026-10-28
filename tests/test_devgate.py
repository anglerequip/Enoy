"""Verification gate, independent review and completion (T7.1-T7.3). Real git and pytest; scripted models."""

import json

import pytest

from enoy.devagent import TaskExecution
from enoy.devgate import ReviewError, finish_task, record_if_merged, review_task, run_gate
from test_devagent import (ALLOW, IMPL_GOOD, TEST_FILE, WRITE_TEST, Script, env, execution, git,  # noqa: F401
                           repo)


@pytest.fixture
def verified(env):
    store, plan, task, claim, gw, body = env
    result = execution(env, Script(WRITE_TEST, "Wrote the test.", IMPL_GOOD, "Implemented multiply.")).run(claim)
    assert result.ok
    return env


def recommit(env, path, text, message="later change"):
    store, plan, task, claim, gw, body = env
    run = store.runs(task["id"])[-1]
    wt = gw.base / f"p{plan['id']}-T1-a1"
    (wt / path).parent.mkdir(parents=True, exist_ok=True)
    (wt / path).write_text(text)
    sha = gw.commit_paths(wt, [path], message)
    store.update_run(run["id"], impl_commit=sha)
    return sha


def gate(env):
    store, plan, task, claim, gw, body = env
    return run_gate(store, store.get_task(task["id"]), gw, ALLOW)


def failed_checks(result):
    return {c["name"] for c in result.checks if not c["ok"]}


# T7.1 gate ------------------------------------------------------------------------------------

def test_gate_passes_for_good_work(verified):
    result = gate(verified)
    assert result.ok and failed_checks(result) == set()
    names = {c["name"] for c in result.checks}
    assert names == {"scope", "frozen_tests", "tests", "regression", "syntax", "lost_content", "unfinished"}


def test_gate_catches_files_outside_the_task(verified):
    recommit(verified, "README.md", "changed\n")
    assert "scope" in failed_checks(gate(verified))


def test_gate_catches_a_changed_frozen_test(verified):
    recommit(verified, TEST_FILE, "def test_multiply():\n    assert True\n")
    assert {"frozen_tests", "scope"} <= failed_checks(gate(verified))


def test_gate_reruns_the_tests_in_a_fresh_checkout(verified):
    recommit(verified, "calc.py", "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return 0\n")
    failed = failed_checks(gate(verified))
    assert "tests" in failed and "regression" in failed


def test_gate_catches_syntax_errors_and_unfinished_markers(verified):
    recommit(verified, "calc.py", "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n"
                                  "    # TODO handle floats\n    return a * b\n")
    assert failed_checks(gate(verified)) == {"unfinished"}
    recommit(verified, "calc.py", "def add(a, b:\n")
    assert "syntax" in failed_checks(gate(verified))


def test_gate_catches_lost_content(verified):
    recommit(verified, "calc.py", "# ... existing code ...\n\ndef multiply(a, b):\n    return a * b\n")
    assert "lost_content" in failed_checks(gate(verified))


# T7.2 review ----------------------------------------------------------------------------------

def verdict(v, findings=()):
    return "```json\n" + json.dumps({"verdict": v, "findings": list(findings)}) + "\n```"


def test_reviewer_must_differ_from_the_implementer(verified):
    with pytest.raises(ReviewError, match="different"):
        review_task(lambda s, u: verdict("approve"), "ds", "ds", {}, "diff", gate(verified))


def test_review_never_overrides_a_failed_gate(verified):
    recommit(verified, "README.md", "changed\n")
    asked = []
    result = review_task(lambda s, u: asked.append(u) or verdict("approve"), "codex-cli", "ds", {}, "diff",
                         gate(verified))
    assert not result.approved and result.skipped and asked == []


def test_review_approves_or_requests_changes(verified):
    g = gate(verified)
    seen = []
    ok = review_task(lambda s, u: seen.append(u) or verdict("approve"), "codex-cli", "ds",
                     {"id": "T1", "acceptance": ["multiply(2, 3) == 6"]}, "+def multiply", g)
    assert ok.approved and "multiply(2, 3) == 6" in seen[0] and "+def multiply" in seen[0] and "scope" in seen[0]
    no = review_task(lambda s, u: verdict("changes", [{"severity": "high", "file": "calc.py", "issue": "No floats."}]),
                     "codex-cli", "ds", {"id": "T1"}, "diff", g)
    assert not no.approved and no.findings[0]["issue"] == "No floats."
    junk = review_task(lambda s, u: "Looks fine to me!", "codex-cli", "ds", {"id": "T1"}, "diff", g)
    assert not junk.approved and "verdict" in junk.reason


# T7.3 completion ------------------------------------------------------------------------------

def finish(env, review=None, synced=None):
    store, plan, task, claim, gw, body = env
    return finish_task(store, store.get_task(task["id"]), claim, gw, ALLOW, review=review,
                       sync=(lambda pid: synced.append(pid) or {"written": True}) if synced is not None else None)


def test_finish_merges_records_and_releases(verified):
    store, plan, task, claim, gw, body = verified
    synced = []
    result = finish(verified, review=lambda spec, diff, g: review_task(lambda s, u: verdict("approve"), "codex-cli",
                                                                      "ds", spec, diff, g), synced=synced)
    assert result.ok
    t = store.get_task(task["id"])
    run = store.runs(task["id"])[-1]
    assert t["status"] == "completed" and t["commit_id"] == run["impl_commit"]
    assert t["evidence"]["review"] == "approved by codex-cli" and "passed" in t["evidence"]["tests"]
    assert git(gw.repo, "merge-base", "--is-ancestor", run["impl_commit"], "enoy/plan-%d" % plan["id"]) == ""
    assert "def multiply" in git(gw.repo, "show", "enoy/plan-%d:calc.py" % plan["id"])
    assert git(gw.repo, "rev-parse", "main") == git(gw.repo, "rev-parse", "main~0")  # User branch untouched.
    assert "multiply" not in (gw.repo / "calc.py").read_text()
    assert synced == [plan["id"]]
    assert not (gw.base / f"p{plan['id']}-T1-a1").exists()  # Clean worktree removed; branch kept.


def test_finish_without_a_reviewer_relies_on_the_gate(verified):
    store, plan, task, claim, gw, body = verified
    assert finish(verified).ok
    assert store.get_task(task["id"])["evidence"]["review"] == "no review model; Enoy's checks only"


def test_failed_gate_fails_the_task_and_merges_nothing(verified):
    store, plan, task, claim, gw, body = verified
    recommit(verified, "README.md", "changed\n")
    result = finish(verified)
    assert not result.ok and store.get_task(task["id"])["status"] == "failed"
    assert "scope" in store.get_task(task["id"])["blocker"]
    assert git(gw.repo, "rev-parse", "enoy/plan-%d" % plan["id"]) == git(gw.repo, "rev-parse", "main")


def test_requested_changes_fail_the_task_with_findings(verified):
    store, plan, task, claim, gw, body = verified
    result = finish(verified, review=lambda spec, diff, g: review_task(
        lambda s, u: verdict("changes", [{"severity": "high", "file": "calc.py", "issue": "No floats."}]),
        "codex-cli", "ds", spec, diff, g))
    assert not result.ok and "No floats." in store.get_task(task["id"])["blocker"]


def test_merge_conflict_sends_the_task_back_for_a_new_attempt(verified):
    store, plan, task, claim, gw, body = verified
    integration = gw.integration_worktree(plan["id"])
    (integration / "calc.py").write_text("def add(a, b):\n    return b + a  # someone else\n")
    gw.commit_paths(integration, ["calc.py"], "conflicting change")
    result = finish(verified)
    assert not result.ok and result.status == "conflict"
    t = store.get_task(task["id"])
    assert t["status"] == "ready" and "conflict" in t["blocker"].lower() and "calc.py" in t["blocker"]
    run = store.runs(task["id"])[-1]
    assert git(gw.repo, "rev-parse", run["branch"]) == run["impl_commit"]  # Task branch kept intact.


def test_merged_work_is_recorded_after_a_restart_without_running_again(verified):
    store, plan, task, claim, gw, body = verified
    run = store.runs(task["id"])[-1]
    gw.merge(plan["id"], run["branch"], "merged before the crash")
    store.recover()  # Enoy stopped before recording the result.
    assert store.get_task(task["id"])["status"] == "interrupted"
    assert record_if_merged(store, store.get_task(task["id"]), gw)
    t = store.get_task(task["id"])
    assert t["status"] == "completed" and t["commit_id"] == run["impl_commit"]
    assert len(store.runs(task["id"])) == 1


def test_unmerged_interrupted_work_is_not_recorded(verified):
    store, plan, task, claim, gw, body = verified
    store.recover()
    assert not record_if_merged(store, store.get_task(task["id"]), gw)
    assert store.get_task(task["id"])["status"] == "interrupted"
