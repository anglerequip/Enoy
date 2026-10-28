"""planning.md, the approved-plan archive and CLAUDE.md generation (T4.1-T4.4)."""

import copy
import json
import os
import stat

import pytest

from enoy import paths, planfile
from enoy.backends.base import CLAUDE
from enoy.devplan import Planner, PlanError
from enoy.devstore import DevStore
from enoy.planfile import ArchiveError
from enoy.project import Workspace
from test_devplan import GOOD, reply, setup, two_tasks
from test_orchestrator import open_project


def approved_plan(orc, pid, body=None):
    store = DevStore(orc.db)
    p = store.create_plan(pid, "Calculator", "Add multiply and divide")
    store.add_version(p["id"], body or two_tasks(), author="claude-cli")
    store.approve_version(p["id"], 1)
    return store, p


# T4.1 renderer ---------------------------------------------------------------------------

def test_render_matches_the_expected_file(orc_factory, project):
    orc, pid = setup(orc_factory, project)
    body = two_tasks(edge_cases=["zero divisor"], acceptance=["divide(6, 3) == 2"],
                     spec="Add `def divide(a, b)`.\nRaise ZeroDivisionError for b == 0.")
    store, p = approved_plan(orc, pid, body)
    t1 = {t["task_key"]: t for t in store.tasks(p["id"])}["T1"]
    token = store.claim(t1["id"])
    for s in ("testing", "implementing", "verifying", "merging"):
        store.transition(t1["id"], s, claim=token)
    store.transition(t1["id"], "completed", claim=token, commit_id="abc1234", summary="Added multiply.",
                     evidence={"tests": "2 passed", "regression": "34 passed"})
    sha = store.version(p["id"], 1)["sha"]
    expected = f"""<!-- enoy:planning plan={p['id']} version=1 sha={sha[:12]} -->
# Calculator

Goal: Add multiply and divide

Status: approved (version 1). 1 of 2 tasks completed.

Enoy writes this file from the plan it keeps. Change the plan in Enoy's Build workspace; edits made here are not read back.

## Summary

Add multiply to the calculator.

## Architecture

Extend calculator.py with one function.

## Risks

- None listed.

## Open questions

- None listed.

## Tasks

- [x] **T1 Add multiply** (completed)
  - Summary: Added multiply.
  - Commit: `abc1234`
  - Evidence: regression: 34 passed; tests: 2 passed
- [ ] **T2 Add divide** (pending; depends on T1)
  - Objective: calculator.multiply(a, b) returns a * b.
  - Allowed files: `calculator.py`
  - Test files: `tests/test_divide.py`
  - Specification: Add `def divide(a, b)`.
    Raise ZeroDivisionError for b == 0.
  - Inputs and outputs: Two numbers in, their product out.
  - Edge cases: zero divisor
  - Tests first:
    - `test_divide` in `tests/test_divide.py`; fails before the change with: AttributeError
  - Verify: `python -m pytest -q tests/test_divide.py`
  - Regression: `python -m pytest -q`
  - Acceptance:
    - divide(6, 3) == 2
  - Done when: Frozen tests and regression tests pass.
  - Stop and report if: calculator.py is missing or the tests cannot be imported

Approved specification: version 1, SHA-256 {sha}. The full text is archived by Enoy.
"""
    assert planfile.render(store, p["id"]) == expected
    assert planfile.render(store, p["id"]) == expected  # Same input, same bytes.


def test_draft_plan_renders_latest_version_waiting_for_approval(orc_factory, project):
    orc, pid = setup(orc_factory, project)
    store = DevStore(orc.db)
    p = store.create_plan(pid, "Calc", "Goal")
    store.add_version(p["id"], GOOD)
    store.add_version(p["id"], two_tasks())
    text = planfile.render(store, p["id"])
    assert "Status: draft (version 2), waiting for your approval." in text
    assert "- [ ] **T2 Add divide** (waiting for approval; depends on T1)" in text
    assert "Approved specification" not in text


def test_blocked_task_shows_its_blocker(orc_factory, project):
    orc, pid = setup(orc_factory, project)
    store, p = approved_plan(orc, pid)
    t1 = {t["task_key"]: t for t in store.tasks(p["id"])}["T1"]
    token = store.claim(t1["id"])
    store.transition(t1["id"], "testing", claim=token)
    store.transition(t1["id"], "blocked", claim=token, blocker="The test spec expects a function that exists already.")
    assert "- [ ] **T1 Add multiply** (blocked)\n  - Blocker: The test spec expects" in planfile.render(store, p["id"])


# T4.2 archive -----------------------------------------------------------------------------

def test_archive_is_written_read_only_and_verified(orc_factory, project):
    orc, pid = setup(orc_factory, project)
    store, p = approved_plan(orc, pid)
    path = planfile.archive_version(store, p["id"], 1)
    assert path.parent == paths.sub_dir("plans") / str(p["id"])
    assert not os.access(path, os.W_OK)
    assert planfile.load_archive(store, p["id"], 1) == store.version(p["id"], 1)["body"]
    assert path.with_suffix(".sha256").read_text().strip() == store.version(p["id"], 1)["sha"]


def test_tampered_archive_is_detected(orc_factory, project):
    orc, pid = setup(orc_factory, project)
    store, p = approved_plan(orc, pid)
    path = planfile.archive_version(store, p["id"], 1)
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    data = json.loads(path.read_text())
    data["body"]["tasks"][0]["spec"] = "Something else."
    path.write_text(json.dumps(data))
    with pytest.raises(ArchiveError):
        planfile.load_archive(store, p["id"], 1)


def test_unapproved_versions_are_not_archived(orc_factory, project):
    orc, pid = setup(orc_factory, project)
    store = DevStore(orc.db)
    p = store.create_plan(pid, "x", "y")
    store.add_version(p["id"], GOOD)
    with pytest.raises(ArchiveError):
        planfile.archive_version(store, p["id"], 1)


# T4.3 safe writer --------------------------------------------------------------------------

def test_absent_file_is_written_and_unchanged_file_is_updated(orc_factory, project):
    orc, pid = setup(orc_factory, project)
    store, p = approved_plan(orc, pid)
    first = planfile.sync(orc, p["id"])
    target = project / "planning.md"
    assert first["written"] and target.read_text(encoding="utf-8") == planfile.render(store, p["id"])
    t1 = {t["task_key"]: t for t in store.tasks(p["id"])}["T1"]
    store.transition(t1["id"], "cancelled")
    second = planfile.sync(orc, p["id"])
    assert second["written"] and "(cancelled)" in target.read_text(encoding="utf-8")


def test_user_edits_are_never_overwritten_without_confirmation(orc_factory, project):
    orc, pid = setup(orc_factory, project)
    store, p = approved_plan(orc, pid)
    planfile.sync(orc, p["id"])
    target = project / "planning.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nMy own note.\n", encoding="utf-8")
    result = planfile.sync(orc, p["id"])
    assert not result["written"] and result["reason"] == "changed" and "+My own note." not in result["diff"]
    assert "-My own note." in result["diff"] and "My own note." in target.read_text(encoding="utf-8")
    confirmed = planfile.sync(orc, p["id"], confirm=True)
    assert confirmed["written"] and "My own note." not in target.read_text(encoding="utf-8")
    assert "My own note." in confirmed["backup"].read_text(encoding="utf-8")


def test_existing_file_not_written_by_enoy_needs_confirmation(orc_factory, project):
    orc, pid = setup(orc_factory, project)
    store, p = approved_plan(orc, pid)
    (project / "planning.md").write_text("# My own plan\n", encoding="utf-8")
    result = planfile.sync(orc, p["id"])
    assert not result["written"] and result["reason"] == "not_enoy"
    assert (project / "planning.md").read_text(encoding="utf-8") == "# My own plan\n"


def test_approval_archives_and_writes_planning_md(orc_factory, project):
    orc, pid = setup(orc_factory, project, claude=[reply(GOOD)])
    planner = Planner(orc)
    p, _ = planner.create(pid, "Add multiply")
    approved = planner.approve(p["id"], 1)
    assert approved["planning_md"]["written"]
    assert (project / "planning.md").exists()
    assert planfile.load_archive(DevStore(orc.db), p["id"], 1) == GOOD


# T4.4 CLAUDE.md --------------------------------------------------------------------------

def test_claude_md_draft_comes_from_the_planner_and_writes_nothing(orc_factory, project):
    draft = "# Project instructions\n\n- Run `python -m pytest -q` before finishing.\n"
    orc, pid = setup(orc_factory, project, claude=["Here it is:\n```markdown\n" + draft + "```"])
    text = planfile.draft_instructions(Planner(orc), pid)
    assert text == draft and not (project / "CLAUDE.md").exists()
    req = orc.backends[CLAUDE].requests[0]
    assert not req.native_tools and set(req.enoy_tools) <= {"list_files", "read_file", "search_code"}


def test_existing_claude_md_is_untouched_without_confirmation(orc_factory, project):
    orc, pid = setup(orc_factory, project)
    ws = Workspace(project)
    (project / "CLAUDE.md").write_text("Keep my rules.\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        planfile.save_instructions(ws, "New rules.\n")
    assert (project / "CLAUDE.md").read_text(encoding="utf-8") == "Keep my rules.\n"
    result = planfile.save_instructions(ws, "New rules.\n", confirm=True)
    assert (project / "CLAUDE.md").read_text(encoding="utf-8") == "New rules.\n"
    assert result["backup"].read_text(encoding="utf-8") == "Keep my rules.\n"


def test_absent_claude_md_is_written(orc_factory, project):
    result = planfile.save_instructions(Workspace(project), "Rules.\n")
    assert (project / "CLAUDE.md").read_text(encoding="utf-8") == "Rules.\n" and result["backup"] is None
