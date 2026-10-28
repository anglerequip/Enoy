"""Verification gate, independent review and completion for Build tasks.

The gate is Enoy's own check of a finished task and does not depend on what any model reports:
the commits stay inside the task's files, the frozen tests are byte-for-byte unchanged, the tests
and regression commands pass again in a fresh checkout, and the changed code has no syntax errors,
lost content or unfinished markers. A review by a different model can add findings but can never
approve work that failed the gate. Only then is the task merged into the plan's integration branch.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import patches
from .backends.base import BackendError
from .devagent import Scope, TaskExecution, content_sha
from .devstore import DevStore
from .gitwork import GitError, GitWork

UNFINISHED = re.compile(r"\b(TODO|FIXME|XXX)\b|NotImplementedError")
MAX_REVIEW_DIFF = 60000


class ReviewError(ValueError):
    pass


@dataclass
class GateResult:
    ok: bool
    checks: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def failures(self) -> str:
        return "; ".join(f"{c['name']}: {c['detail']}" for c in self.checks if not c["ok"])


@dataclass
class ReviewResult:
    approved: bool
    skipped: bool = False
    reason: str = ""
    findings: list[dict] = field(default_factory=list)
    reviewer: str = ""


@dataclass
class FinishResult:
    ok: bool
    status: str
    reason: str = ""


def _text(data: bytes | None) -> str | None:
    return None if data is None else data.decode("utf-8", "replace").replace("\r\n", "\n")


def _check(checks: list, name: str, problems: list[str], ok_detail: str) -> None:
    checks.append({"name": name, "ok": not problems, "detail": "; ".join(problems[:10]) if problems else ok_detail})


# T7.1 -----------------------------------------------------------------------------------------

def run_gate(store: DevStore, task: dict, git: GitWork, allowlist, venv_root: Path | None = None,
             timeout_s: float = 300) -> GateResult:
    spec = task["spec"]
    run = store.runs(task["id"])[-1] if store.runs(task["id"]) else {}
    tc, ic = run.get("test_commit"), run.get("impl_commit")
    if not tc or not ic:
        return GateResult(False, [{"name": "commits", "ok": False, "detail": "The run has no test or implementation commit."}])
    checks: list[dict] = []
    start = git.parent(tc)
    test_changes, impl_changes = git.diff_names(start, tc), git.diff_names(tc, ic)
    outside = [f"{p} (test commit)" for p in test_changes if p not in spec["test_files"]] + \
              [f"{p} (implementation)" for p in impl_changes if p not in spec["allowed_files"]]
    _check(checks, "scope", [f"changed outside the task: {p}" for p in outside], "only the task's files changed")

    frozen = store.frozen(task["id"])
    changed = [p for p, h in frozen.items() if (b := git.file_at(ic, p)) is None or content_sha(b) != h]
    if not frozen:
        changed = ["no frozen tests were recorded"]
    _check(checks, "frozen_tests", [f"{p} differs from the approved test" for p in changed],
           f"{len(frozen)} frozen test file(s) unchanged")

    syntax, lost = [], []
    for p in impl_changes:
        after = _text(git.file_at(ic, p))
        if after is None:
            continue
        problems, _ = patches.check_syntax(p, after)
        syntax += [f"{p}: {x}" for x in problems[:3]]
        why = patches.lost_content(p, _text(git.file_at(tc, p)), after)
        if why:
            lost.append(f"{p}: {why}")
    _check(checks, "syntax", syntax, "changed files parse")
    _check(checks, "lost_content", lost, "no code was replaced by placeholders")

    unfinished, current = [], ""
    for line in git.diff_text(tc, ic).splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("+") and UNFINISHED.search(line):
            unfinished.append(f"{current}: {line[1:].strip()[:120]}")
    _check(checks, "unfinished", unfinished, "no TODO, FIXME or NotImplementedError added")

    fresh = git.temp_worktree(ic, f"gate-{task['id']}")
    try:
        scope = Scope(fresh, (), (), spec["verify_commands"] + spec["regression_commands"], allowlist, True,
                      venv_root, timeout_s)
        out, code = TaskExecution._run_all(scope, spec["verify_commands"])
        _check(checks, "tests", [] if code == 0 else [f"the frozen tests failed (exit {code}): " + out[-1500:]],
               "frozen tests pass in a fresh checkout")
        rout, rcode = TaskExecution._run_all(scope, spec["regression_commands"]) if spec["regression_commands"] else ("", 0)
        _check(checks, "regression", [] if rcode == 0 else [f"regression checks failed (exit {rcode}): " + rout[-1500:]],
               "regression checks pass in a fresh checkout")
    finally:
        try:
            git.remove_worktree(fresh, confirm=True)
        except GitError:
            pass
    last = lambda text: ([l for l in text.strip().splitlines() if l.strip()] or [""])[-1][:200]
    evidence = {"tests": f"passed in a fresh checkout: {last(out)}" if code == 0 else "failed",
                "regression": (f"passed in a fresh checkout: {last(rout)}" if spec["regression_commands"]
                               else "no regression commands") if rcode == 0 else "failed"}
    return GateResult(all(c["ok"] for c in checks), checks, evidence)


# T7.2 -----------------------------------------------------------------------------------------

REVIEW_SYSTEM = """You review one finished task of an approved plan. You edit nothing.
Enoy's own checks have already passed (listed below): the change stays inside the task's files, the approved
tests are unchanged and pass, and the regression checks pass. Look for what those checks cannot see: acceptance
criteria that are not really met, tests that pass without testing the behaviour, obvious defects, missed edge
cases, and changes that do not fit the project's conventions. Return ONE JSON object in a ```json block:
{"verdict": "approve" or "changes", "findings": [{"severity": "high|medium|low", "file": "path", "issue": "what"}]}
Approve only when every acceptance criterion is met; low-severity notes may accompany an approval."""


def review_task(ask: Callable[[str, str], str], reviewer_id: str, implementer_id: str, spec: dict, diff: str,
                gate: GateResult) -> ReviewResult:
    if reviewer_id and reviewer_id == implementer_id:
        raise ReviewError("The reviewer must be a different model from the implementer.")
    if not gate.ok:
        return ReviewResult(False, True, "Enoy's checks failed, so the review was not run: " + gate.failures(),
                            reviewer=reviewer_id)
    facts = {k: spec[k] for k in ("id", "title", "objective", "spec", "edge_cases", "acceptance", "allowed_files",
                                  "test_files") if k in spec}
    user = ("# Task\n```json\n" + json.dumps(facts, indent=1) + "\n```\n\n# Enoy's checks\n" +
            "\n".join(f"- {c['name']}: {'passed' if c['ok'] else 'FAILED'} ({c['detail'][:200]})" for c in gate.checks) +
            "\n\n# Change (tests first, then implementation)\n```diff\n" + diff[:MAX_REVIEW_DIFF] + "\n```")
    text = ask(REVIEW_SYSTEM, user)
    for block in [m.group(1) for m in re.finditer(r"```(?:json)?\s*\n(.*?)```", text or "", re.S)] + [text or ""]:
        start, end = block.find("{"), block.rfind("}")
        try:
            data = json.loads(block[start:end + 1]) if start >= 0 else None
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("verdict") in ("approve", "changes"):
            findings = [f for f in data.get("findings") or [] if isinstance(f, dict)]
            return ReviewResult(data["verdict"] == "approve", reason="" if data["verdict"] == "approve" else
                                "; ".join(f"{f.get('file', '')}: {f.get('issue', '')}" for f in findings)[:2000],
                                findings=findings, reviewer=reviewer_id)
    return ReviewResult(False, reason="The reviewer gave no clear verdict (approve or changes).", reviewer=reviewer_id)


# T7.3 -----------------------------------------------------------------------------------------

def finish_task(store: DevStore, task: dict, claim: str, git: GitWork, allowlist,
                review: Callable[[dict, str, GateResult], ReviewResult] | None = None,
                sync: Callable[[int], dict] | None = None, venv_root: Path | None = None) -> FinishResult:
    """Gate, optional review, merge into the integration branch, record the result. Task must be verifying."""
    tid, spec = task["id"], task["spec"]
    run = store.runs(tid)[-1]
    gate = run_gate(store, task, git, allowlist, venv_root)
    evidence = dict(gate.evidence)
    if not gate.ok:
        reason = "Enoy's checks failed: " + gate.failures()
        store.transition(tid, "failed", claim=claim, blocker=reason[:2000], evidence=evidence)
        return FinishResult(False, "failed", reason)
    evidence["checks"] = "all passed: " + ", ".join(c["name"] for c in gate.checks)
    if review is not None:
        store.transition(tid, "reviewing", claim=claim)
        try:
            rv = review(spec, git.diff_text(git.parent(run["test_commit"]), run["impl_commit"]), gate)
        except BackendError as exc:
            if exc.kind in ("unavailable", "timeout"):
                store.transition(tid, "waiting_network", claim=claim, blocker=f"The review could not run: {exc}")
                return FinishResult(False, "network", str(exc))
            store.transition(tid, "failed", claim=claim, blocker=f"The review failed: {exc}"[:2000])
            return FinishResult(False, "failed", str(exc))
        except ReviewError as exc:
            store.transition(tid, "failed", claim=claim, blocker=str(exc))
            return FinishResult(False, "failed", str(exc))
        if not rv.approved:
            reason = "The reviewer asked for changes: " + (rv.reason or "no details")
            store.transition(tid, "failed", claim=claim, blocker=reason[:2000], evidence=evidence)
            return FinishResult(False, "failed", reason)
        evidence["review"] = f"approved by {rv.reviewer}"
    else:
        evidence["review"] = "no review model; Enoy's checks only"
    store.transition(tid, "merging", claim=claim)
    merged = git.merge(task["plan_id"], run["branch"], f"{spec['id']}: {spec['title']}")
    if not merged.ok:
        reason = ("Merge conflict in " + (", ".join(merged.conflicts) or "the integration branch") +
                  ". The task will run again on top of the latest integration branch.")
        store.transition(tid, "ready", claim=claim, blocker=reason)
        return FinishResult(False, "conflict", reason)
    evidence["merge"] = merged.commit[:12]
    store.transition(tid, "completed", claim=claim, commit_id=run["impl_commit"], blocker="",
                     summary=(run["summary"] or spec["title"])[:2000], evidence=evidence)
    _after_completion(store, task["plan_id"], git, run, sync)
    return FinishResult(True, "completed")


def _after_completion(store: DevStore, plan_id: int, git: GitWork, run: dict, sync) -> None:
    store.refresh_ready(plan_id)
    if sync is not None:
        try:
            sync(plan_id)
        except Exception:  # planning.md is a copy; failing to write it must not undo a recorded result.
            pass
    if run.get("worktree"):
        try:
            git.remove_worktree(Path(run["worktree"]))
        except GitError:
            pass  # Kept when it has uncommitted changes; cleanup lists it.


def record_if_merged(store: DevStore, task: dict, git: GitWork, claim: str | None = None,
                     sync: Callable[[int], dict] | None = None) -> bool:
    """After a restart: if this task's work is already on the integration branch, record it as completed
    instead of running it again. Returns whether it did."""
    runs = [r for r in store.runs(task["id"]) if r["impl_commit"]]
    if not runs or task["status"] == "completed":
        return False
    run = runs[-1]
    if not git.is_ancestor(run["impl_commit"], git.integration_branch(task["plan_id"])):
        return False
    store.transition(task["id"], "completed", claim=claim, commit_id=run["impl_commit"], blocker="",
                     summary=(run["summary"] or task["title"])[:2000],
                     evidence={"merge": "found on the integration branch after a restart"})
    _after_completion(store, task["plan_id"], git, run, sync)
    return True
