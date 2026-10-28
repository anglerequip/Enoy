"""Git isolation for Build tasks: an integration branch per plan, a worktree and branch per task attempt.

Every task works in its own worktree outside the project folder, on its own branch, so agents never
touch the user's checkout or each other's files. Finished tasks merge into the plan's integration
branch; the user's own branch changes only when the user asks for `merge_into_current`.

Git runs without a shell and with the repository's hooks and fsmonitor turned off, because both can
run programs from the repository. Branches are kept after their worktree is removed, as evidence.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import gitinfo, paths
from .commands import safe_env
from .project import EXCLUDED_DIRS, SENSITIVE_GLOBS

AGENT = ("Enoy agent", "enoy-agent@localhost")
TIMEOUT_S = 120


class GitError(RuntimeError):
    pass


@dataclass
class MergeResult:
    ok: bool
    commit: str = ""
    conflicts: list[str] = field(default_factory=list)
    reason: str = ""


def _env() -> dict:
    env = {k: v for k, v in safe_env().items() if not k.upper().startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(cwd: Path, *args: str, check: bool = True, agent: bool = False) -> subprocess.CompletedProcess:
    exe = shutil.which("git")
    if not exe:
        raise GitError("Git is not installed. Install Git for Windows to use the Build workspace.")
    no_hooks = paths.app_home() / "no-git-hooks"  # Never created, so Git finds no hooks.
    argv = [exe, "-C", str(cwd), "-c", f"core.hooksPath={no_hooks}", "-c", "core.fsmonitor=false"]
    if agent:
        argv += ["-c", f"user.name={AGENT[0]}", "-c", f"user.email={AGENT[1]}", "-c", "commit.gpgsign=false"]
    try:
        out = subprocess.run(argv + list(args), capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=TIMEOUT_S, env=_env(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} did not finish within {TIMEOUT_S} s.") from None
    if check and out.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed: {(out.stderr or out.stdout).strip()[:500]}")
    return out


def _same(a: Path | str, b: Path | str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def is_repo(path: Path | str) -> bool:
    """True when the folder is the root of a Git repository (a subfolder does not count)."""
    try:
        out = _git(Path(path), "rev-parse", "--show-toplevel", check=False)
    except GitError:
        return False
    return out.returncode == 0 and _same(out.stdout.strip(), path)


def _user_identity(cwd: Path) -> list[str]:
    """The user's own Git identity when configured; Enoy's otherwise (a commit needs one)."""
    name = _git(cwd, "config", "user.name", check=False).stdout.strip()
    email = _git(cwd, "config", "user.email", check=False).stdout.strip()
    return [] if name and email else ["-c", "user.name=Enoy", "-c", "user.email=enoy@localhost"]


def init_repo(folder: Path | str, confirm: bool = False) -> None:
    """Create a Git repository with a first commit. Secrets and excluded folders are left out of it."""
    folder = Path(folder)
    if (folder / ".git").exists() or is_repo(folder):
        raise GitError("This folder is already a Git repository.")
    if not confirm:
        raise GitError("Creating a Git repository in this folder needs your confirmation.")
    _git(folder, "init", "-q", "-b", "main")
    exclude = folder / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as fh:
        fh.write("\n# Added by Enoy: never commit secrets or generated folders.\n")
        fh.write("".join(f"{g}\n" for g in SENSITIVE_GLOBS) + "".join(f"{d}/\n" for d in sorted(EXCLUDED_DIRS)))
    _git(folder, "add", "-A")
    if _git(folder, "diff", "--cached", "--quiet", check=False).returncode != 0:
        _git(folder, *_user_identity(folder), "commit", "-q", "--no-verify", "-m", "Initial commit (created by Enoy)")


class GitWork:
    def __init__(self, repo: Path | str, root: Path | str | None = None):
        self.repo = Path(repo).resolve()
        key = hashlib.sha1(os.path.normcase(str(self.repo)).encode()).hexdigest()[:8]
        self.base = (Path(root) if root else paths.sub_dir("worktrees")).resolve() / f"{self.repo.name}-{key}"

    def _branch_exists(self, branch: str) -> bool:
        return _git(self.repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0

    def head(self, cwd: Path | None = None) -> str:
        out = _git(cwd or self.repo, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
        if out.returncode != 0:
            raise GitError("The repository has no commits yet. Make a first commit before running Build tasks.")
        return out.stdout.strip()

    @staticmethod
    def integration_branch(plan_id: int) -> str:
        return f"enoy/plan-{plan_id}"

    def ensure_integration(self, plan_id: int) -> tuple[str, str]:
        """The plan's integration branch, created at HEAD if needed, and the commit it started from."""
        branch = self.integration_branch(plan_id)
        if not self._branch_exists(branch):
            base = self.head()
            _git(self.repo, "branch", branch, base)
            return branch, base
        return branch, _git(self.repo, "merge-base", branch, "HEAD").stdout.strip()

    def integration_worktree(self, plan_id: int) -> Path:
        branch, _ = self.ensure_integration(plan_id)
        path = self.base / f"plan-{plan_id}"
        if not path.exists():
            self.base.mkdir(parents=True, exist_ok=True)
            self.prune()
            _git(self.repo, "worktree", "add", "-q", str(path), branch)
        return path

    def create_task_worktree(self, plan_id: int, task_key: str, attempt: int) -> tuple[Path, str]:
        start, _ = self.ensure_integration(plan_id)
        branch = f"enoy/p{plan_id}/{task_key}-a{attempt}"
        path = self.base / f"p{plan_id}-{task_key}-a{attempt}"
        if path.exists() or self._branch_exists(branch):
            raise GitError(f"A worktree or branch for {task_key} attempt {attempt} already exists.")
        self.base.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "worktree", "add", "-q", "-b", branch, str(path), start)
        return path, branch

    def commit_all(self, worktree: Path, message: str) -> str:
        _git(worktree, "add", "-A")
        if _git(worktree, "diff", "--cached", "--quiet", check=False).returncode == 0:
            raise GitError("There is nothing to commit.")
        _git(worktree, "commit", "-q", "--no-verify", "-m", message, agent=True)
        return self.head(worktree)

    def diff_names(self, a: str, b: str) -> list[str]:
        out = _git(self.repo, "-c", "core.quotepath=false", "diff", "--name-only", a, b).stdout
        return sorted(line.strip() for line in out.splitlines() if line.strip())

    def _merge(self, cwd: Path, branch: str, message: str, agent: bool) -> MergeResult:
        ident = [] if agent else _user_identity(cwd)
        out = _git(cwd, *ident, "merge", "--no-ff", "--no-edit", "-m", message, branch, check=False, agent=agent)
        if out.returncode == 0:
            return MergeResult(True, self.head(cwd))
        conflicts = sorted(x for x in _git(cwd, "diff", "--name-only", "--diff-filter=U", check=False).stdout.splitlines()
                           if x.strip())
        _git(cwd, "merge", "--abort", check=False)
        reason = ("The changes conflict with work already merged." if conflicts else
                  (out.stderr or out.stdout).strip()[:500])
        return MergeResult(False, conflicts=conflicts, reason=reason)

    def merge(self, plan_id: int, branch: str, message: str) -> MergeResult:
        """Merge a task branch into the plan's integration branch. A conflict is aborted; both branches stay."""
        return self._merge(self.integration_worktree(plan_id), branch, message, agent=True)

    # Cleanup --------------------------------------------------------------------------------

    def worktrees(self) -> list[dict]:
        """Enoy's worktrees for this repository, with whether each has uncommitted changes."""
        out = _git(self.repo, "worktree", "list", "--porcelain").stdout
        found = []
        for block in out.strip().split("\n\n"):
            info = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
            if "worktree" not in info:
                continue
            path = Path(info["worktree"]).resolve()
            if not path.is_relative_to(self.base):
                continue
            exists = path.exists()
            dirty = exists and bool(_git(path, "status", "--porcelain", check=False).stdout.strip())
            found.append({"path": path, "branch": info.get("branch", "").removeprefix("refs/heads/"),
                          "exists": exists, "dirty": dirty})
        return found

    def remove_worktree(self, path: Path, confirm: bool = False) -> None:
        """Remove a task worktree. One with uncommitted changes is removed only with confirm=True."""
        path = Path(path).resolve()
        if not path.is_relative_to(self.base):
            raise GitError("Enoy only removes worktrees it created.")
        if path.exists() and _git(path, "status", "--porcelain", check=False).stdout.strip() and not confirm:
            raise GitError("This worktree has uncommitted changes. Confirm to discard them.")
        _git(self.repo, "worktree", "remove", *(["--force"] if confirm else []), str(path))

    def prune(self) -> None:
        _git(self.repo, "worktree", "prune")

    # The user's branch -------------------------------------------------------------------------

    def merge_into_current(self, plan_id: int) -> MergeResult:
        """Merge the plan's integration branch into the branch the user has checked out. Only on request.

        Refused when the repository is not on a branch, or when the user has uncommitted changes in a file
        the plan changed. A conflict is aborted. The merge commit can be undone with `git revert -m 1`.
        """
        current = _git(self.repo, "branch", "--show-current").stdout.strip()
        if not current:
            return MergeResult(False, reason="The repository is not on a branch (detached HEAD). "
                                             "Check out the branch to merge into first.")
        integration = self.integration_branch(plan_id)
        if not self._branch_exists(integration):
            return MergeResult(False, reason="This plan has no integration branch yet.")
        base = _git(self.repo, "merge-base", "HEAD", integration).stdout.strip()
        changed = set(self.diff_names(base, integration))
        overlap = sorted(changed & set(gitinfo.changed_files(self.repo) or []))
        if overlap:
            return MergeResult(False, reason="You have uncommitted changes in " + ", ".join(overlap) +
                               ", which the plan also changed. Commit or set them aside first.")
        return self._merge(self.repo, integration, f"Merge Enoy plan {plan_id}", agent=False)
