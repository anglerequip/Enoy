"""Build workspace planning: the plan format, its validator, the planner context and the planner service.

A planner only reads the project and writes a plan. It gets no write tools, and the validator
rejects plans that contain production code, so planning stays separate from implementation.
Every task must be small enough for one fresh implementer session and must name its files,
its tests (written first) and the commands that verify it.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import PurePosixPath
from typing import Any

from . import commands as cmdpolicy, privacy
from .backends.base import CLAUDE, CODEX, BackendError, ChatMessage, ChatRequest
from .config import CLI_PROVIDERS
from .devstore import DevStore, TransitionError
from .project import PathError, Workspace
from .redact import redact

TASK_FIELDS: dict[str, type] = {
    "id": str, "title": str, "objective": str, "depends_on": list, "allowed_files": list, "test_files": list,
    "spec": str, "inputs_outputs": str, "edge_cases": list, "tests": list, "verify_commands": list,
    "regression_commands": list, "acceptance": list, "completion": str, "blockers": list,
}
NON_EMPTY = ("title", "objective", "spec", "tests", "verify_commands", "acceptance", "completion")
PLAN_FIELDS: dict[str, type] = {"summary": str, "architecture": str, "risks": list, "questions": list, "tasks": list}
MAX_TASKS = 30
MAX_FILES = 10            # allowed_files + test_files per task.
MAX_CODE_LINES = 15       # Longer code blocks are implementation, not specification.
PATCH_MARKERS = re.compile(r"^\s*(<<<FILE|<<<EDIT|<<<<<<<|>>>>>>>|>>>END|diff --git|@@ -\d)", re.M)
FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
READ_TOOLS = ("list_files", "read_file", "search_code")

SCHEMA_TEXT = """{
  "summary": "what will be built, in two or three sentences",
  "architecture": "how it fits the existing code: modules touched, data flow, conventions followed",
  "risks": ["architectural risk"], "questions": ["unresolved question for the user"],
  "tasks": [{
    "id": "T1", "title": "short title", "objective": "one-sentence outcome",
    "depends_on": ["ids of tasks that must finish first"],
    "allowed_files": ["exact production files this task may create or change"],
    "test_files": ["exact test files this task creates first"],
    "spec": "precise implementation specification: functions, signatures, behaviour, error handling",
    "inputs_outputs": "expected inputs and outputs", "edge_cases": ["edge case"],
    "tests": [{"name": "test function name", "file": "one of test_files", "expect_failure": "how it fails before the change"}],
    "verify_commands": ["command that runs this task's tests"],
    "regression_commands": ["command that runs the wider suite"],
    "acceptance": ["checkable acceptance criterion"], "completion": "when the task counts as done",
    "blockers": ["condition under which the implementer must stop and report"]
  }]
}"""

PLANNER_SYSTEM = f"""You are the planner for a software change. You do not write production code and you edit nothing.
Inspect the repository with the read-only tools you have (search first, then read only what you need) so the plan
matches the real code, its conventions and its tests. Then return ONE JSON object in a ```json block, in this format:
{SCHEMA_TEXT}
Rules Enoy enforces (a plan that breaks one is rejected):
- Small, atomic tasks: each one fits a fresh session of a cheaper coding model with no exploration, and lists at most
  {MAX_FILES} files in allowed_files plus test_files. A file is either a test file or a production file, never both.
- Paths are relative to the project root, inside it, and never secrets files such as .env or keys.
- Tests are written before the implementation: every test names its file from test_files and how it fails before the
  change. Implementers cannot change these tests afterwards.
- Commands run one program without a shell: no pipes, redirection, chaining, URLs or installs. Prefer the project's
  existing test commands.
- No production code: describe signatures and behaviour; do not include patches, diffs or code blocks longer than
  {MAX_CODE_LINES} lines.
- Order tasks with depends_on. Tasks without a dependency between them should not share allowed_files.
- Put real unknowns in "questions" instead of guessing."""


class PlanError(ValueError):
    def __init__(self, message: str, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = errors or []


# Validation ------------------------------------------------------------------------------

def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)


def _path_problem(path: Any, ws: Workspace | None) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return "is not a file path"
    clean = path.strip().replace("\\", "/")
    if clean.startswith("/") or re.match(r"^[A-Za-z]:", clean) or ".." in PurePosixPath(clean).parts:
        return "is outside the project folder"
    if ws is not None:
        try:
            ws.resolve(clean)
        except PathError as exc:
            return str(exc)
        if ws.is_sensitive(clean):
            return "is a secrets or credentials file"
        if ws.is_excluded(clean):
            return "is in a folder Enoy excludes"
    return None


def _cycle(tasks: dict[str, list[str]]) -> list[str] | None:
    state: dict[str, int] = {}

    def visit(k: str, path: list[str]) -> list[str] | None:
        if state.get(k) == 1:
            return path[path.index(k):] + [k]
        if state.get(k) == 2:
            return None
        state[k] = 1
        for d in tasks.get(k, []):
            if d in tasks:
                found = visit(d, path + [k])
                if found:
                    return found
        state[k] = 2
        return None

    for k in tasks:
        found = visit(k, [])
        if found:
            return found
    return None


def validate_plan(body: Any, ws: Workspace | None = None, allowlist: list[str] | tuple = (),
                  max_tasks: int = MAX_TASKS, max_files: int = MAX_FILES) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings). A plan with errors cannot be stored as valid or approved."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(body, dict):
        return ["The plan is not a JSON object."], []
    for k, typ in PLAN_FIELDS.items():
        if k in body and not isinstance(body[k], typ):
            errors.append(f"'{k}' must be a {typ.__name__}.")
    tasks = body.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return errors + ["The plan has no tasks."], warnings
    if len(tasks) > max_tasks:
        errors.append(f"The plan has {len(tasks)} tasks; the limit is {max_tasks}.")
    ids = [t.get("id") for t in tasks if isinstance(t, dict)]
    for dup in sorted(k for k, n in Counter(ids).items() if n > 1):
        errors.append(f"Task id {dup} is used more than once.")
    known = set(ids)
    for t in tasks:
        if not isinstance(t, dict):
            errors.append("Every task must be a JSON object.")
            continue
        tid = t.get("id") if isinstance(t.get("id"), str) else "?"
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,20}", tid):
            errors.append(f"Task id {tid!r} is not a short id such as T1.")
        missing = [k for k in TASK_FIELDS if k not in t]
        if missing:
            errors.append(f"{tid}: missing {', '.join(missing)}.")
        for k, typ in TASK_FIELDS.items():
            if k in t and not isinstance(t[k], typ):
                errors.append(f"{tid}: '{k}' must be a {typ.__name__}.")
        if any(k in t and isinstance(t[k], TASK_FIELDS[k]) and not t[k] for k in NON_EMPTY):
            errors.append(f"{tid}: {', '.join(k for k in NON_EMPTY if k in t and not t[k])} cannot be empty.")
        if any(k not in missing and not isinstance(t[k], TASK_FIELDS[k]) for k in TASK_FIELDS if k in t):
            continue  # Type errors above; the checks below need the right types.
        if missing:
            continue
        for d in t["depends_on"]:
            if d not in known:
                errors.append(f"{tid}: depends on unknown task {d}.")
        files, tests = list(t["allowed_files"]), list(t["test_files"])
        if not files and not tests:
            errors.append(f"{tid}: names no files.")
        if len(files) + len(tests) > max_files:
            errors.append(f"{tid}: lists {len(files) + len(tests)} files; the limit is {max_files}. Split the task.")
        for p in files + tests:
            problem = _path_problem(p, ws)
            if problem:
                errors.append(f"{tid}: '{p}' {problem}.")
        for p in sorted(set(files) & set(tests)):
            errors.append(f"{tid}: '{p}' cannot be both a test file and a production file.")
        for test in t["tests"]:
            if not isinstance(test, dict) or not all(isinstance(test.get(k), str) and test.get(k)
                                                     for k in ("name", "file", "expect_failure")):
                errors.append(f"{tid}: every test needs name, file and expect_failure.")
            elif test["file"] not in tests:
                errors.append(f"{tid}: test {test['name']} is in '{test['file']}', which is not in test_files.")
        for cmd in t["verify_commands"] + t["regression_commands"]:
            policy = cmdpolicy.classify(cmd, list(allowlist)) if isinstance(cmd, str) else None
            if policy is None or policy.level not in ("allowlisted", "approval"):
                errors.append(f"{tid}: command {cmd!r} is not allowed: "
                              f"{'; '.join(policy.reasons) if policy else 'not text'}")
        for text in _strings({k: t[k] for k in t if k not in ("id",)}):
            if PATCH_MARKERS.search(text):
                errors.append(f"{tid}: contains patch or diff markers. Plans describe changes; they do not make them.")
                break
            if any(len(block.strip().splitlines()) > MAX_CODE_LINES for block in FENCE.findall(text)):
                errors.append(f"{tid}: contains a code block longer than {MAX_CODE_LINES} lines. "
                              "Describe signatures and behaviour instead of writing the code.")
                break
    graph = {t["id"]: list(t.get("depends_on") or []) for t in tasks
             if isinstance(t, dict) and isinstance(t.get("id"), str) and isinstance(t.get("depends_on"), list)}
    cycle = _cycle(graph)
    if cycle:
        errors.append("Tasks depend on each other in a circle: " + " → ".join(cycle) + ".")
    elif not errors:
        warnings += _parallel_warnings(tasks, graph)
    return errors, warnings


def _parallel_warnings(tasks: list[dict], graph: dict[str, list[str]]) -> list[str]:
    def ancestors(k: str, seen: set[str]) -> set[str]:
        for d in graph.get(k, []):
            if d not in seen:
                seen.add(d)
                ancestors(d, seen)
        return seen

    out = []
    for i, a in enumerate(tasks):
        for b in tasks[i + 1:]:
            if a["id"] in ancestors(b["id"], set()) or b["id"] in ancestors(a["id"], set()):
                continue
            shared = sorted(set(a["allowed_files"] + a["test_files"]) & set(b["allowed_files"] + b["test_files"]))
            if shared:
                out.append(f"{a['id']} and {b['id']} both change {', '.join(shared)} but do not depend on each "
                           "other, so they cannot run in parallel.")
    return out


def parse_reply(text: str) -> dict:
    """The plan JSON from a planner reply: a ```json block, or the whole reply."""
    candidates = [m.group(1) for m in re.finditer(r"```(?:json)?\s*\n(.*?)```", text or "", re.S)] + [text or ""]
    for c in candidates:
        c = c.strip()
        start, end = c.find("{"), c.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            body = json.loads(c[start:end + 1])
        except ValueError:
            continue
        if isinstance(body, dict) and "tasks" in body:
            return body
    raise PlanError("The planner did not return a plan in JSON.", ["The reply contains no JSON plan object."])


# Planner context --------------------------------------------------------------------------

def planner_context(ws: Workspace, goal: str, max_chars: int = 60000) -> str:
    """What the planner sees before it starts reading: goal, instructions, facts, file list. Within max_chars."""
    head = f"# Goal\n{goal.strip()}\n"
    facts = ws.detect()
    langs = ", ".join(f"{ext or '(none)'} ×{n}" for ext, n in facts["languages"])
    tail = (f"\n# Project facts\nRoot folder name: {ws.name}\nGit repository: {'yes' if facts['git'] else 'no'}\n"
            f"File types: {langs}\nTest commands: {', '.join(facts['test_commands']) or 'none found'}\n")
    budget = max_chars - len(head) - len(tail) - 200
    parts = []
    docs = list(ws.instructions())
    try:
        text, _ = ws.read_text("planning.md")
        docs.append(("planning.md (current plan file)", text[:8000]))
    except (FileNotFoundError, PathError):
        pass
    for name, text in docs:
        block = f"\n# {name}\n{text.strip()}\n"
        if len(block) > budget // 2:
            block = block[:budget // 2] + "\n[shortened]\n"
        if len(block) <= budget:
            parts.append(block)
            budget -= len(block)
    files = sorted(ws.index)
    listing = "\n# Files (sensitive and excluded files are not listed)\n"
    for i, rel in enumerate(files):
        line = rel + "\n"
        if len(listing) + len(line) + 60 > budget:
            listing += f"[{len(files) - i} more files not listed; use the search tool]\n"
            break
        listing += line
    return redact(head + "".join(parts) + listing[:max(budget, 0)] + tail)[:max_chars]


# Asking a read-only model ------------------------------------------------------------------

def ask_model(orc, providers, model: dict, root, system: str, user: str, purpose: str, role: str) -> str:
    """One read-only request to a Build model (planner or reviewer). It never gets write tools.

    Claude through the CLI may read the folder with Enoy's read-only MCP tools; ChatGPT through the CLI gets
    no tools; API models go through Providers.complete, so spending limits apply.
    """
    messages = [ChatMessage("system", system), ChatMessage("user", user)]
    if model["provider"] == "claude_cli":
        env = orc._mcp_env()
        env["ENOY_MCP_WORKSPACE"] = str(root)
        env["ENOY_MCP_FOLDERS"] = {"current": str(root), "known": []}
        env["ENOY_MCP_PRIVATE"] = {"cloud_excluded_globs": orc.cfg["privacy"]["cloud_excluded_globs"],
                                   "extra_excluded_globs": orc.cfg["permissions"]["extra_excluded_globs"]}
        req = ChatRequest(messages, purpose=purpose, enoy_tools=list(READ_TOOLS), enoy_env=env,
                          model=model.get("model") or None)
        return orc.backends[CLAUDE].chat(req).text
    if model["provider"] == "codex_cli":
        return orc.backends[CODEX].chat(ChatRequest(messages, purpose=purpose, model=model.get("model") or None)).text
    return providers.complete(model["id"], messages, max_output=16000, role=role, purpose=purpose).text


# Planner service --------------------------------------------------------------------------

class Planner:
    def __init__(self, orc, providers=None, store: DevStore | None = None):
        from .providers import Providers
        self.orc = orc
        self.providers = providers or Providers(lambda: orc.cfg, orc.db)
        self.store = store or DevStore(orc.db)

    def _model(self) -> dict:
        mid = self.orc.cfg["build"]["roles"].get("planner")
        model = next((m for m in self.orc.cfg["build"]["models"] if m["id"] == mid), None)
        if not mid or model is None:
            raise PlanError("No planner model is chosen. Pick one in Settings → Providers → Roles.")
        return model

    def _workspace(self, project_id: int, cloud: bool) -> Workspace:
        project = self.orc.db.get_project(project_id)
        if project is None:
            raise PlanError(f"Unknown project {project_id}.")
        globs = list(self.orc.cfg["permissions"]["extra_excluded_globs"])
        if cloud:
            reason = privacy.local_only_reason(self.orc.cfg, project)
            if reason:
                raise PlanError(f"The planner runs in the cloud, but {reason}. Choose a local planner model.")
            globs += self.orc.cfg["privacy"]["cloud_excluded_globs"]
        return Workspace(project["path"], globs)

    def _is_cloud(self, model: dict) -> bool:
        if model["provider"] in CLI_PROVIDERS:
            return True
        return not self.orc.cfg["build"]["providers"][model["provider"]].get("local")

    def _ask(self, model: dict, ws: Workspace, user: str, system: str = PLANNER_SYSTEM) -> str:
        self.orc._act(None, "build.plan", f"Asking {model['id']} for a plan ({len(user):,} characters).", "system",
                      "plan")
        return ask_model(self.orc, self.providers, model, ws.root, system, user, "plan", "planner")

    def _generate(self, model: dict, ws: Workspace, user: str) -> dict:
        allow = self.orc.cfg["permissions"]["command_allowlist"]
        try:
            text = self._ask(model, ws, user)
        except BackendError as exc:
            raise PlanError(f"The planner could not answer: {exc}") from exc
        problems = []
        try:
            body = parse_reply(text)
            problems, _ = validate_plan(body, ws, allow)
        except PlanError as exc:
            problems = exc.errors
        if not problems:
            return body
        retry = (user + "\n\n# Your previous answer\n" + text[:20000] + "\n\n# It was rejected for these reasons\n" +
                 "\n".join("- " + p for p in problems) + "\n\nReturn the complete corrected plan JSON.")
        try:
            text = self._ask(model, ws, retry)
        except BackendError as exc:
            raise PlanError(f"The planner could not answer: {exc}", problems) from exc
        body = parse_reply(text)
        problems, _ = validate_plan(body, ws, allow)
        if problems:
            raise PlanError("The planner's plan still breaks Enoy's rules after one correction.", problems)
        return body

    def start(self, project_id: int, goal: str, title: str = "") -> dict:
        """Create the plan record after checking that a planner may see this project. No model request yet."""
        if not goal.strip():
            raise PlanError("Describe what should be built.")
        model = self._model()
        self._workspace(project_id, self._is_cloud(model))
        return self.store.create_plan(project_id, title or goal.strip().splitlines()[0][:80], goal.strip())

    def draft(self, plan_id: int) -> dict:
        """Ask the planner for the first version of a started plan."""
        plan = self.store.get_plan(plan_id)
        model = self._model()
        ws = self._workspace(plan["project_id"], self._is_cloud(model))
        body = self._generate(model, ws, planner_context(ws, plan["goal"]))
        version = self.store.add_version(plan_id, body, author=model["id"])
        self.orc._act(None, "build.plan", f"Plan {plan_id} version {version['version']} is ready for review "
                      f"({len(body['tasks'])} task(s)).", "system", "plan")
        return version

    def create(self, project_id: int, goal: str, title: str = "") -> tuple[dict, dict]:
        plan = self.start(project_id, goal, title)
        version = self.draft(plan["id"])
        return self.store.get_plan(plan["id"]), version

    def revise(self, plan_id: int, comment: str) -> dict:
        plan = self.store.get_plan(plan_id)
        if plan is None:
            raise PlanError(f"Unknown plan {plan_id}.")
        if plan["approved_version"] is not None:
            raise PlanError("This plan is approved and can no longer change. Start a new plan for further work.")
        latest = self.store.versions(plan_id)[-1]
        model = self._model()
        ws = self._workspace(plan["project_id"], self._is_cloud(model))
        user = (planner_context(ws, plan["goal"]) + "\n\n# Current plan (version " + str(latest["version"]) + ")\n```json\n"
                + json.dumps(latest["body"], indent=1) + "\n```\n\n# Requested changes\n" + comment.strip() +
                "\n\nReturn the complete revised plan JSON.")
        body = self._generate(model, ws, user)
        return self.store.add_version(plan_id, body, author=model["id"], comment=comment.strip())

    def approve(self, plan_id: int, version: int) -> dict:
        plan = self.store.get_plan(plan_id)
        if plan is None:
            raise PlanError(f"Unknown plan {plan_id}.")
        versions = self.store.versions(plan_id)
        if not versions or versions[-1]["version"] != version:
            raise PlanError("Only the latest version of a plan can be approved.")
        ws = Workspace(self.orc.db.get_project(plan["project_id"])["path"],
                       self.orc.cfg["permissions"]["extra_excluded_globs"])
        errors, _ = validate_plan(versions[-1]["body"], ws, self.orc.cfg["permissions"]["command_allowlist"])
        if errors:
            raise PlanError("This version breaks Enoy's plan rules and cannot be approved.", errors)
        try:
            approved = self.store.approve_version(plan_id, version)
        except TransitionError as exc:
            raise PlanError(str(exc)) from exc
        from . import planfile
        planfile.archive_version(self.store, plan_id, version)
        self.orc._act(None, "build.plan", f"You approved plan {plan_id} version {version}.", "user", "plan",
                      outcome="approved")
        return approved | {"planning_md": planfile.sync(self.orc, plan_id)}
