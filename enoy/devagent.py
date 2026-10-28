"""Build implementers: one approved task, a fresh context, tests first, and limits enforced in code.

An implementer model runs in the task's own git worktree through Enoy's tools only. `Scope` decides
what each phase may write and run, so the rules hold whatever the model says:

1. Test phase: only the task's test files may be written. Enoy runs the tests itself and requires
   them to fail for the expected reason, then commits them and records their SHA-256 (frozen).
2. Implementation phase: only the task's allowed files may be written, never the frozen tests.
   Enoy re-checks the frozen fingerprints, runs the frozen tests and the regression commands
   itself, and commits only the allowed files.

A model that finds the task cannot be done as specified calls report_blocker instead of improvising.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import commands as commands_mod, patches
from .backends.base import BackendError, ChatMessage, ChatResult
from .devstore import DevStore
from .gitwork import GitError, GitWork
from .patches import _write_atomic
from .project import PathError, Workspace
from .redact import redact

MAX_TOOL_CHARS = 8000
MAX_READ_LINES = 400
BLOCKER_KINDS = ("test_spec_wrong", "missing_information", "environment", "other")


class ToolError(Exception):
    kind = "failed"


class ToolDenied(ToolError):
    kind = "denied"


# T6.1 Scope ------------------------------------------------------------------------------------

class Scope:
    """What one phase of one task may read, write and run. Enforced here, not by the prompt."""

    def __init__(self, root: Path, writable, frozen=(), commands=(), allowlist=(), unattended: bool = True,
                 venv_root: Path | None = None, timeout_s: float = 300, cancel: threading.Event | None = None):
        self.ws = Workspace(root)
        self.root = self.ws.root
        self.writable, self.frozen = set(writable), set(frozen)
        self.commands = [c.strip() for c in commands]
        self.allowlist = list(allowlist)
        self.unattended = unattended
        self.venv_root = venv_root
        self.timeout_s = timeout_s
        self.cancel = cancel
        self.changed: set[str] = set()

    def norm(self, path: str) -> str:
        try:
            rel = self.ws.rel(self.ws.resolve(str(path)))
        except PathError as exc:
            raise ToolDenied(str(exc)) from None
        if self.ws.is_sensitive(rel):
            raise ToolDenied(f"'{rel}' looks like a secrets file; Enoy does not read or change these.")
        if self.ws.is_excluded(rel):
            raise ToolDenied(f"'{rel}' is in a folder Enoy excludes.")
        return rel

    def read(self, path: str) -> str:
        rel = self.norm(path)
        try:
            return self.ws.read_text(rel)[0]
        except FileNotFoundError:
            raise ToolError(f"{rel} does not exist.") from None
        except PathError as exc:
            raise ToolDenied(str(exc)) from None

    def write(self, path: str, content: str) -> dict:
        rel = self.norm(path)
        if rel in self.frozen:
            raise ToolDenied(f"'{rel}' is a frozen test. Approved tests cannot be changed; if the test is wrong, "
                             "call report_blocker with kind test_spec_wrong.")
        if rel not in self.writable:
            raise ToolDenied(f"'{rel}' is not one of the files this phase may change. Allowed: "
                             + (", ".join(sorted(self.writable)) or "none") + ".")
        problems, _ = patches.check_syntax(rel, content)
        if problems:
            raise ToolDenied("Not written: the result would not be valid: " + "; ".join(problems[:5]))
        _write_atomic(self.root / rel, content)
        self.changed.add(rel)
        self.ws.invalidate()
        return {"path": rel, "written": True, "lines": len(content.splitlines())}

    def edit(self, path: str, old_text: str, new_text: str) -> dict:
        rel = self.norm(path)
        current = self.read(rel)
        new, errors = patches._apply_edits(current, [patches.Edit(old_text, new_text)], rel)
        if errors:
            raise ToolError(errors[0] + " Read the file again and copy old_text exactly, including indentation.")
        return self.write(rel, new)

    def check_command(self, command: str) -> list[str]:
        command = command.strip()
        policy = commands_mod.classify(command, self.allowlist)
        if policy.level in ("blocked", "high_risk"):
            raise ToolDenied("Not run: " + " ".join(policy.reasons))
        if command in self.commands or policy.level == "allowlisted":
            return policy.argv
        raise ToolDenied(f"Not run: `{command}` needs the user's approval" +
                         (", and this task runs unattended." if self.unattended else ".") +
                         " Use the task's verification commands instead.")

    def run(self, command: str) -> dict:
        argv = self.check_command(command)
        r = commands_mod.run(argv, self.root, self.timeout_s, cancel=self.cancel, venv_root=self.venv_root)
        self.ws.invalidate()
        out = (r.stdout + ("\n" + r.stderr if r.stderr else "")).strip()
        return {"command": command, "status": r.status, "exit_code": r.exit_code, "output": out}


# Tools offered to the model ----------------------------------------------------------------------

def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": props, "required": required}}}


_S = {"type": "string"}
TOOLS = [
    _fn("list_files", "List the project's files, optionally under a folder or matching a pattern such as *.py.",
        {"path": _S, "pattern": _S}, []),
    _fn("read_file", "Read a file with line numbers (up to 400 lines per call).",
        {"path": _S, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"]),
    _fn("search_code", "Find text in the project's files.", {"query": _S}, ["query"]),
    _fn("write_file", "Create a file or replace it completely. Only files allowed in this phase.",
        {"path": _S, "content": _S}, ["path", "content"]),
    _fn("edit_file", "Replace old_text (copied exactly from read_file, without line numbers) with new_text.",
        {"path": _S, "old_text": _S, "new_text": _S}, ["path", "old_text", "new_text"]),
    _fn("run_command", "Run one of the task's verification commands, or an allowlisted command, in the task folder.",
        {"command": _S}, ["command"]),
    _fn("report_blocker", "Stop: the task cannot be done as specified. Say exactly why.",
        {"kind": {"type": "string", "enum": list(BLOCKER_KINDS)}, "details": _S}, ["kind", "details"]),
]


def _list(scope: Scope, args: dict) -> dict:
    import fnmatch
    base = (args.get("path") or "").strip().strip("/")
    names = sorted(scope.ws.index)
    if base and base != ".":
        names = [n for n in names if n.startswith(base + "/")]
    pattern = args.get("pattern") or "*"
    names = [n for n in names if fnmatch.fnmatch(n.rsplit("/", 1)[-1], pattern) or fnmatch.fnmatch(n, pattern)]
    return {"count": len(names), "files": names[:300]}


def _read(scope: Scope, args: dict) -> dict:
    lines = scope.read(args["path"]).splitlines()
    start = max(1, int(args.get("start_line") or 1))
    end = min(int(args.get("end_line") or start + MAX_READ_LINES - 1), len(lines), start + MAX_READ_LINES - 1)
    return {"path": scope.norm(args["path"]), "lines": f"{start}-{end} of {len(lines)}",
            "content": "\n".join(f"{n:>4}| {lines[n - 1]}" for n in range(start, end + 1))}


def _dispatch(scope: Scope, name: str, args: dict) -> dict:
    if name == "list_files":
        return _list(scope, args)
    if name == "read_file":
        return _read(scope, args)
    if name == "search_code":
        hits = scope.ws.search(str(args.get("query", "")), max_results=40)
        return {"matches": [h for h in hits if not scope.ws.is_sensitive(h["path"])]}
    if name == "write_file":
        return scope.write(args["path"], args["content"])
    if name == "edit_file":
        return scope.edit(args["path"], args["old_text"], args["new_text"])
    if name == "run_command":
        r = scope.run(args["command"])
        return r | {"output": r["output"][-4000:]}
    raise ToolError(f"There is no tool called {name}.")


# T6.2 Tool loop ----------------------------------------------------------------------------------

@dataclass
class LoopResult:
    status: str                # finished | blocked | limit | network | cancelled | error
    summary: str = ""
    reason: str = ""
    blocker_kind: str = ""
    steps: int = 0


class ToolLoop:
    """Model → tools → model, until the model answers without a tool call. Can continue after add_user()."""

    def __init__(self, complete: Callable[[list[ChatMessage], list[dict]], ChatResult], scope: Scope, system: str,
                 user: str, max_steps: int = 30, max_output_tokens: int = 200_000,
                 cancel: threading.Event | None = None):
        self.complete, self.scope = complete, scope
        self.messages = [ChatMessage("system", system), ChatMessage("user", user)]
        self.max_steps, self.max_output_tokens = max_steps, max_output_tokens
        self.cancel = cancel or threading.Event()
        self.steps = 0
        self.output_tokens = 0

    def add_user(self, text: str) -> None:
        self.messages.append(ChatMessage("user", text))

    def run(self) -> LoopResult:
        while True:
            if self.cancel.is_set():
                return LoopResult("cancelled", reason="Stopped by the user.", steps=self.steps)
            if self.steps >= self.max_steps:
                return LoopResult("limit", reason=f"The implementer used its {self.max_steps} steps.", steps=self.steps)
            if self.output_tokens >= self.max_output_tokens:
                return LoopResult("limit", reason=f"The implementer used its {self.max_output_tokens:,} output tokens.",
                                  steps=self.steps)
            try:
                r = self.complete(self.messages, TOOLS)
            except BackendError as exc:
                status = {"cancelled": "cancelled", "budget": "limit", "limit": "limit", "unavailable": "network",
                          "timeout": "network"}.get(exc.kind, "error")
                return LoopResult(status, reason=str(exc), steps=self.steps)
            self.steps += 1
            self.output_tokens += (r.usage or {}).get("output_tokens", 0)
            if not r.tool_calls:
                self.messages.append(ChatMessage("assistant", r.text, provider_content=r.provider_content))
                return LoopResult("finished", summary=r.text.strip(), steps=self.steps)
            self.messages.append(ChatMessage("assistant", r.text, tool_calls=r.tool_calls,
                                             provider_content=r.provider_content))
            for call in r.tool_calls:
                args = call.get("arguments") or {}
                if call.get("name") == "report_blocker":
                    kind = args.get("kind") if args.get("kind") in BLOCKER_KINDS else "other"
                    return LoopResult("blocked", reason=str(args.get("details", ""))[:2000], blocker_kind=kind,
                                      steps=self.steps)
                if call.get("error"):
                    out = {"error": "invalid", "message": call["error"]}
                else:
                    try:
                        out = _dispatch(self.scope, call.get("name", ""), args)
                    except ToolError as exc:
                        out = {"error": exc.kind, "message": str(exc)}
                    except (KeyError, TypeError, ValueError) as exc:
                        out = {"error": "invalid", "message": f"Missing or wrong argument: {exc}"}
                text = redact(json.dumps(out, ensure_ascii=False))
                if len(text) > MAX_TOOL_CHARS:
                    text = text[:MAX_TOOL_CHARS] + " [shortened]"
                self.messages.append(ChatMessage("tool", text, tool_name=call.get("name", ""),
                                                 tool_call_id=call.get("id", "")))


# T6.3 Context ------------------------------------------------------------------------------------

ROLE = """You are an implementer working on exactly one approved task, in an isolated copy of the project.
Enoy enforces these rules in code: you can only write the files listed for this phase, approved tests are frozen,
and you can only run the task's verification commands or allowlisted ones. Use the tools to read what you need;
do not guess file contents. When the phase is done, reply with a short summary and no tool call. If the task cannot
be done as specified (for example a test in the specification is wrong, or information is missing), call
report_blocker instead of improvising or changing the plan."""
PHASE_RULES = {
    "tests": """Phase 1 of 2: write the tests. Create exactly the test files and tests listed below, and nothing else.
Do not change production code. The tests must fail now, for the reason given for each test, because the feature
does not exist yet. Run the verification command to check that they fail that way.""",
    "implement": """Phase 2 of 2: implement. The tests below are approved and frozen; you cannot change them. Change only
the allowed files so that the frozen tests pass, then run the verification and regression commands.
Make the smallest change that meets the specification and the acceptance criteria.""",
}


def _task_text(task: dict) -> str:
    from .planfile import _open_task
    return f"# Your task {task['id']}: {task['title']}\n" + "\n".join(_open_task(task)) + "\n"


def task_context(task: dict, phase: str, root: Path, deps_done=(), max_chars: int = 60000) -> tuple[str, str]:
    """(system, user) for one phase of one task. Only this task, its finished dependencies' summaries,
    the project's instruction files and the files it may touch: no plan, no other tasks, no chat history."""
    ws = Workspace(root)
    system = ROLE + "\n\n" + PHASE_RULES[phase]
    room = max_chars // 4
    for name, text in ws.instructions():
        block = f"\n\n# Project instructions ({name})\n{text.strip()}"
        if len(system) + len(block) > room:
            block = block[:max(room - len(system), 0)]
        system += block
    user = _task_text(task)
    if deps_done:
        user += "\n# Finished tasks this one builds on\n" + "".join(
            f"- {d['key']} {d['title']}: {d['summary']}\n" for d in deps_done)
    files = list(task["allowed_files"]) + list(task["test_files"])
    budget = max_chars - len(system) - len(user) - 200
    shown = "\n# Current files\n"
    for rel in files:
        try:
            text = ws.read_text(rel)[0]
        except (FileNotFoundError, PathError):
            block = f"\n## {rel}\n(does not exist yet)\n"
        else:
            block = f"\n## {rel}\n```\n{text}\n```\n"
        if len(shown) + len(block) > budget:
            block = f"\n## {rel}\n[too long to show here; use read_file]\n"
            if len(shown) + len(block) > budget:
                break
        shown += block
    return system[:max_chars], (user + shown)[:max(max_chars - len(system), 0)]


# T6.4 Expected failure -----------------------------------------------------------------------------

_BROKEN = re.compile(r"\b(SyntaxError|IndentationError|TabError)\b|collected 0 items|no tests ran", re.I)


def check_expected_failure(output: str, exit_code: int | None, tests: list[dict]) -> tuple[bool, str, str]:
    """Do the new tests fail for the reason the plan gave? Returns (ok, reason, kind).

    kind is "" when they do, "passes" when they pass already (the task is blocked), "broken" when the
    tests themselves do not run, and "wrong" when they fail for another reason.
    """
    if exit_code == 0:
        return False, ("The new tests pass before the change, so they do not test the missing behaviour, "
                       "or the behaviour exists already."), "passes"
    if _BROKEN.search(output or ""):
        return False, "The tests do not run: they have a syntax error or no tests were collected.", "broken"
    missing = []
    for t in tests:
        token = re.search(r"\b\w+(Error|Exception)\b", t.get("expect_failure", ""))
        if t["name"] not in output and not (token and token.group(0) in output):
            missing.append(t["name"])
    if missing:
        return False, ("These tests did not fail for the expected reason: " + ", ".join(missing) + "."), "wrong"
    return True, "The tests fail for the expected reason.", ""


# T6.4-T6.6 Execution -------------------------------------------------------------------------------

@dataclass
class PhaseResult:
    ok: bool
    status: str                 # passed | blocked | failed | network | limit | cancelled
    reason: str = ""
    summary: str = ""
    commit: str = ""
    blocker_kind: str = ""
    evidence: dict = field(default_factory=dict)


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _tail(text: str, n: int = 3000) -> str:
    return text[-n:]


def _last_line(text: str) -> str:
    lines = [l for l in text.strip().splitlines() if l.strip()]
    return lines[-1][:200] if lines else ""


class TaskExecution:
    def __init__(self, *, store: DevStore, task: dict, plan_body: dict, git: GitWork,
                 complete: Callable[[list[ChatMessage], list[dict]], ChatResult], allowlist,
                 unattended: bool = True, venv_root: Path | None = None, max_steps: int = 30, max_fix_rounds: int = 2,
                 max_output_tokens: int = 200_000, cancel: threading.Event | None = None, timeout_s: float = 300,
                 deps_done=(), provider: str = "", model: str = ""):
        self.store, self.row, self.git, self.complete = store, task, git, complete
        self.spec = task["spec"]
        self.plan_body = plan_body
        self.allowlist = list(allowlist)
        self.unattended, self.venv_root = unattended, venv_root
        self.max_steps, self.max_fix_rounds, self.max_output_tokens = max_steps, max_fix_rounds, max_output_tokens
        self.cancel = cancel or threading.Event()
        self.timeout_s = timeout_s
        self.deps_done = list(deps_done)
        self.provider, self.model = provider, model

    def _scope(self, wt: Path, writable, frozen, cmds) -> Scope:
        return Scope(wt, writable, frozen, cmds, self.allowlist, self.unattended, self.venv_root, self.timeout_s,
                     self.cancel)

    def _loop(self, scope: Scope, phase: str) -> ToolLoop:
        system, user = task_context(self.spec, phase, scope.root, self.deps_done)
        return ToolLoop(self.complete, scope, system, user, self.max_steps, self.max_output_tokens, self.cancel)

    @staticmethod
    def _from_loop(lr: LoopResult) -> PhaseResult:
        status = {"error": "failed"}.get(lr.status, lr.status)
        return PhaseResult(False, status, lr.reason, blocker_kind=lr.blocker_kind)

    @staticmethod
    def _run_all(scope: Scope, cmds: list[str]) -> tuple[str, int]:
        out, code = [], 0
        for c in cmds:
            r = scope.run(c)
            out.append(f"$ {c}\n{r['output']}")
            if r["exit_code"] != 0 and code == 0:
                code = r["exit_code"] if r["exit_code"] is not None else 1
        return "\n\n".join(out), code

    def _test_phase(self, wt: Path, run_id: int) -> PhaseResult:
        t = self.spec
        scope = self._scope(wt, t["test_files"], (), t["verify_commands"])
        loop = self._loop(scope, "tests")
        code = None
        for round_ in range(self.max_fix_rounds + 1):
            lr = loop.run()
            if lr.status != "finished":
                return self._from_loop(lr)
            missing = [f for f in t["test_files"] if not (wt / f).is_file()]
            if missing:
                problem = "These test files do not exist yet: " + ", ".join(missing) + "."
            else:
                out, code = self._run_all(scope, t["verify_commands"])
                ok, reason, kind = check_expected_failure(out, code, t["tests"])
                if ok:
                    break
                if kind == "passes":
                    return PhaseResult(False, "blocked", reason, blocker_kind="test_passes")
                problem = reason + "\n\nOutput:\n" + _tail(out)
            if round_ == self.max_fix_rounds:
                return PhaseResult(False, "failed", f"The tests were not ready after {self.max_fix_rounds} "
                                                    f"correction(s): {problem}")
            loop.add_user("Enoy ran the test-phase checks. " + problem +
                          "\nFix the test files so they fail for the expected reason, then reply with a short summary.")
        commit = self.git.commit_paths(wt, t["test_files"], f"{t['id']} tests: {t['title']}")
        hashes = {f: _sha(wt / f) for f in t["test_files"]}
        self.store.freeze(self.row["id"], run_id, hashes)
        return PhaseResult(True, "passed", summary=lr.summary, commit=commit,
                           evidence={"tests_before": f"failed as expected (exit {code})"})

    def _impl_phase(self, wt: Path) -> PhaseResult:
        t = self.spec
        frozen = self.store.frozen(self.row["id"])
        scope = self._scope(wt, t["allowed_files"], frozen, t["verify_commands"] + t["regression_commands"])
        loop = self._loop(scope, "implement")
        evidence = {}
        for round_ in range(self.max_fix_rounds + 1):
            lr = loop.run()
            if lr.status != "finished":
                return self._from_loop(lr)
            tampered = sorted(p for p, h in frozen.items() if _sha(wt / p) != h)
            if tampered:
                return PhaseResult(False, "failed", "Rejected: the frozen test file(s) " + ", ".join(tampered) +
                                   " were changed during implementation. The work was not committed.")
            out, code = self._run_all(scope, t["verify_commands"])
            if code != 0:
                problem = "The frozen tests did not pass.\n\nOutput:\n" + _tail(out)
            else:
                evidence["tests"] = f"passed (exit 0): {_last_line(out)}"
                rout, rcode = self._run_all(scope, t["regression_commands"])
                if rcode == 0:
                    evidence["regression"] = f"passed (exit 0): {_last_line(rout)}" if t["regression_commands"] \
                        else "no regression commands"
                    break
                problem = "The regression checks did not pass.\n\nOutput:\n" + _tail(rout)
            if round_ == self.max_fix_rounds:
                return PhaseResult(False, "failed", f"Still failing after {self.max_fix_rounds} correction(s). " + problem)
            loop.add_user("Enoy ran the checks. " + problem + "\nFix the allowed files (the tests are frozen), "
                          "then reply with a short summary.")
        try:
            commit = self.git.commit_paths(wt, t["allowed_files"], f"{t['id']}: {t['title']}")
        except GitError as exc:
            return PhaseResult(False, "failed", f"Nothing to commit: {exc}")
        return PhaseResult(True, "passed", summary=lr.summary, commit=commit, evidence=evidence)

    def _stop(self, res: PhaseResult, claim: str, run_id: int) -> PhaseResult:
        tid = self.row["id"]
        if res.status == "blocked" or res.status == "limit":
            if res.blocker_kind == "test_spec_wrong":
                blocker = "The implementer reports that the test specification is wrong; this task needs a plan " \
                          "revision. Details: " + res.reason
            elif res.blocker_kind:
                blocker = f"The implementer stopped ({res.blocker_kind.replace('_', ' ')}): {res.reason}"
            else:
                blocker = res.reason
            self.store.transition(tid, "blocked", claim=claim, blocker=blocker[:2000])
            self.store.finish_run(run_id, "blocked", error=blocker[:2000])
        elif res.status == "network":
            self.store.transition(tid, "waiting_network", claim=claim, blocker=res.reason[:2000])
            self.store.finish_run(run_id, "waiting_network", error=res.reason[:2000])
        elif res.status == "cancelled":
            self.store.transition(tid, "ready", claim=claim)
            self.store.finish_run(run_id, "cancelled", error=res.reason[:2000])
        else:
            self.store.transition(tid, "failed", claim=claim, blocker=res.reason[:2000])
            self.store.finish_run(run_id, "failed", error=res.reason[:2000])
        return res

    def run(self, claim: str) -> PhaseResult:
        """Run both phases for a claimed task. Leaves it verifying (success), or blocked, failed, waiting or ready."""
        t, tid = self.spec, self.row["id"]
        wt, branch = self.git.create_task_worktree(self.row["plan_id"], t["id"], self.row["attempts"])
        run_id = self.store.start_run(tid, "implementer", self.provider, self.model, phase="tests")
        self.store.update_run(run_id, worktree=str(wt), branch=branch)
        self.store.transition(tid, "testing", claim=claim)
        first = self._test_phase(wt, run_id)
        if not first.ok:
            return self._stop(first, claim, run_id)
        self.store.update_run(run_id, test_commit=first.commit, phase="implement")
        self.store.transition(tid, "implementing", claim=claim)
        second = self._impl_phase(wt)
        if not second.ok:
            return self._stop(second, claim, run_id)
        self.store.update_run(run_id, impl_commit=second.commit, phase="verify")
        self.store.transition(tid, "verifying", claim=claim)
        self.store.finish_run(run_id, "completed", summary=second.summary[:2000])
        second.evidence = first.evidence | second.evidence
        return second
