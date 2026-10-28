"""Git isolation for Build tasks (T5.1-T5.3), on real temporary repositories."""

import shutil
import subprocess

import pytest

from enoy.gitwork import GitError, GitWork, init_repo, is_repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def run(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    run(r, "init", "-q", "-b", "main")
    run(r, "config", "user.name", "Test User")
    run(r, "config", "user.email", "user@example.com")
    (r / "a.py").write_text("x = 1\n")
    (r / "b.py").write_text("y = 2\n")
    run(r, "add", "-A")
    run(r, "commit", "-q", "-m", "initial")
    return r


@pytest.fixture
def gw(repo, tmp_path):
    return GitWork(repo, tmp_path / "worktrees")


def task_change(gw, plan, key, attempt, path, text, msg="change"):
    wt, branch = gw.create_task_worktree(plan, key, attempt)
    (wt / path).write_text(text)
    return wt, branch, gw.commit_all(wt, msg)


# T5.1 ----------------------------------------------------------------------------------------

def test_detects_repositories(repo, tmp_path):
    assert is_repo(repo) and not is_repo(tmp_path)
    (repo / "sub").mkdir()
    assert not is_repo(repo / "sub")  # Only the repository's own root counts.


def test_integration_branch_starts_at_head_without_touching_the_user(gw, repo):
    head = run(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text("x = 'my unsaved work'\n")
    branch, base = gw.ensure_integration(7)
    assert branch == "enoy/plan-7" and base == head
    assert run(repo, "rev-parse", "enoy/plan-7") == head
    assert run(repo, "branch", "--show-current") == "main"
    assert (repo / "a.py").read_text() == "x = 'my unsaved work'\n"
    assert gw.ensure_integration(7) == (branch, base)  # Idempotent.


def test_task_worktree_is_outside_the_repo_on_its_own_branch(gw, repo, tmp_path):
    gw.ensure_integration(1)
    wt, branch = gw.create_task_worktree(1, "T1", 1)
    assert branch == "enoy/p1/T1-a1" and wt.is_relative_to(tmp_path / "worktrees")
    assert (wt / "a.py").read_text() == "x = 1\n"
    assert run(wt, "branch", "--show-current") == branch


def test_commits_are_by_enoy_and_repository_hooks_never_run(gw, repo):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho ran > \"$GIT_DIR/../hook-ran.txt\"\nexit 1\n")
    gw.ensure_integration(1)
    wt, branch, sha = task_change(gw, 1, "T1", 1, "a.py", "x = 10\n")
    assert run(wt, "log", "-1", "--format=%an <%ae>", sha) == "Enoy agent <enoy-agent@localhost>"
    assert not list(repo.rglob("hook-ran.txt"))
    assert gw.diff_names("enoy/plan-1", branch) == ["a.py"]


def test_commit_with_nothing_to_commit_is_an_error(gw):
    gw.ensure_integration(1)
    wt, _ = gw.create_task_worktree(1, "T1", 1)
    with pytest.raises(GitError, match="nothing"):
        gw.commit_all(wt, "empty")


def test_merge_into_integration_leaves_the_user_branch_alone(gw, repo):
    main = run(repo, "rev-parse", "main")
    gw.ensure_integration(1)
    _, branch, sha = task_change(gw, 1, "T1", 1, "a.py", "x = 10\n")
    result = gw.merge(1, branch, "T1: change a")
    assert result.ok and result.conflicts == []
    assert run(repo, "show", "enoy/plan-1:a.py") == "x = 10"
    assert run(repo, "rev-parse", "main") == main and (repo / "a.py").read_text() == "x = 1\n"
    assert run(repo, "merge-base", "--is-ancestor", sha, "enoy/plan-1") == ""


def test_conflict_is_detected_aborted_and_keeps_both_branches(gw, repo):
    gw.ensure_integration(1)
    _, b1, s1 = task_change(gw, 1, "T1", 1, "a.py", "x = 'one'\n")
    _, b2, s2 = task_change(gw, 1, "T2", 1, "a.py", "x = 'two'\n")
    assert gw.merge(1, b1, "T1").ok
    integration = run(repo, "rev-parse", "enoy/plan-1")
    result = gw.merge(1, b2, "T2")
    assert not result.ok and result.conflicts == ["a.py"]
    assert run(repo, "rev-parse", "enoy/plan-1") == integration
    assert run(repo, "rev-parse", b2) == s2 and run(repo, "rev-parse", b1) == s1
    assert run(gw.integration_worktree(1), "status", "--porcelain") == ""


# T5.2 ----------------------------------------------------------------------------------------

def test_worktree_with_uncommitted_changes_is_kept_unless_confirmed(gw):
    gw.ensure_integration(1)
    wt, _ = gw.create_task_worktree(1, "T1", 1)
    (wt / "a.py").write_text("unsaved\n")
    with pytest.raises(GitError, match="uncommitted"):
        gw.remove_worktree(wt)
    assert wt.exists()
    stale = gw.worktrees()
    assert any(w["path"] == wt and w["dirty"] for w in stale)
    gw.remove_worktree(wt, confirm=True)
    assert not wt.exists() and all(w["path"] != wt for w in gw.worktrees())


def test_clean_worktree_is_removed_and_prune_forgets_missing_ones(gw):
    gw.ensure_integration(1)
    wt, branch = gw.create_task_worktree(1, "T1", 1)
    gw.remove_worktree(wt)
    assert not wt.exists() and run(gw.repo, "rev-parse", "--verify", branch)  # The branch stays as evidence.
    wt2, _ = gw.create_task_worktree(1, "T2", 1)
    shutil.rmtree(wt2)
    gw.prune()
    assert all(w["path"] != wt2 for w in gw.worktrees())


# T5.3 ----------------------------------------------------------------------------------------

def test_final_merge_refuses_when_user_changes_overlap(gw, repo):
    gw.ensure_integration(1)
    _, b1, _ = task_change(gw, 1, "T1", 1, "a.py", "x = 10\n")
    gw.merge(1, b1, "T1")
    (repo / "a.py").write_text("x = 'mine'\n")
    result = gw.merge_into_current(1)
    assert not result.ok and "a.py" in result.reason
    assert (repo / "a.py").read_text() == "x = 'mine'\n"


def test_final_merge_into_current_branch_and_it_can_be_reverted(gw, repo):
    gw.ensure_integration(1)
    _, b1, _ = task_change(gw, 1, "T1", 1, "a.py", "x = 10\n")
    gw.merge(1, b1, "T1")
    (repo / "b.py").write_text("y = 'unrelated unsaved work'\n")
    result = gw.merge_into_current(1)
    assert result.ok and (repo / "a.py").read_text() == "x = 10\n"
    assert (repo / "b.py").read_text() == "y = 'unrelated unsaved work'\n"
    assert run(repo, "rev-parse", "HEAD") == result.commit and run(repo, "branch", "--show-current") == "main"
    run(repo, "revert", "--no-edit", "-m", "1", "HEAD")
    assert (repo / "a.py").read_text() == "x = 1\n"


def test_final_merge_refuses_detached_head(gw, repo):
    gw.ensure_integration(1)
    run(repo, "checkout", "-q", "--detach")
    result = gw.merge_into_current(1)
    assert not result.ok and "branch" in result.reason


def test_final_merge_conflict_is_aborted(gw, repo):
    gw.ensure_integration(1)
    _, b1, _ = task_change(gw, 1, "T1", 1, "a.py", "x = 'enoy'\n")
    gw.merge(1, b1, "T1")
    (repo / "a.py").write_text("x = 'user'\n")
    run(repo, "commit", "-q", "-am", "user change")
    head = run(repo, "rev-parse", "HEAD")
    result = gw.merge_into_current(1)
    assert not result.ok and result.conflicts == ["a.py"]
    assert run(repo, "rev-parse", "HEAD") == head and run(repo, "status", "--porcelain") == ""


# git init offer (decision 5) ---------------------------------------------------------------------

def test_init_repo_needs_confirmation_and_leaves_secrets_out(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "app.py").write_text("print(1)\n")
    (folder / ".env").write_text("API_KEY=secret\n")
    with pytest.raises(GitError, match="confirm"):
        init_repo(folder)
    assert not (folder / ".git").exists()
    init_repo(folder, confirm=True)
    assert is_repo(folder)
    assert run(folder, "ls-files").splitlines() == ["app.py"]
    with pytest.raises(GitError, match="already"):
        init_repo(folder, confirm=True)
