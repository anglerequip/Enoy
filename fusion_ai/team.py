"""Adaptive team workflow (TEAM ROLES).

Roles describe work; providers run it. The orchestrator role is this module:
plain code that owns the plan, the task graph, scheduling, retries and the
final verdict. Model calls happen only for work a model must do (a plan for a
complex change, code, a deep review, a diagnosis), and each one is routed to
Qwen or Claude by the smart router.

A run never trusts a model's claim of success: changed files come from
validated change sets, verification from real check runs, and a run only
counts as done when the required checks passed and no review blocker is open.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import commands as cmdpolicy
from . import context as ctx
from . import gitinfo
from . import patches
from . import privacy
from .backends.base import CLAUDE, LOCAL, BackendError, ChatMessage
from .redact import contains_secret
from .routing import classify, decide
from .storage import TEAM_OPEN

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

ROLE_LABEL = {
    "lead": "Claude (lead)", "orchestrator": "Orchestrator", "architect": "Architect", "developer": "Developer", "qa": "QA",
    "reviewer": "Reviewer", "debugger": "Debugger",
}
STAGE_OF_ROLE = {"architect": "plan", "developer": "implement", "qa": "verify", "reviewer": "review",
                 "debugger": "repair"}

# Optional specialists: a developer task with extra instructions, activated only
# when the request or the plan mentions their area.
SPECIALISTS = {
    "ui": ("UI/UX", re.compile(r"\b(ui|ux|screen|page|layout|css|button|menu|dialog|theme|design system|frontend)\b", re.I),
           "Focus on a clear, consistent interface that matches the existing look; keep markup accessible."),
    "database": ("Database", re.compile(r"\b(database|sql|sqlite|schema|migration|datastore|query)\b", re.I),
                 "Keep data changes safe: no data loss, backward-compatible reads, explicit migrations."),
    "api": ("API integration", re.compile(r"\b(api|endpoint|http|rest|webhook|oauth|request handler)\b", re.I),
            "Validate inputs at the boundary, handle errors and timeouts, never log credentials."),
    "luau": ("Roblox/Luau", re.compile(r"\b(luau|roblox|remoteevent|remotefunction|datastore|modulescript)\b", re.I),
             ctx.LUAU_NOTES),
    "performance": ("Performance", re.compile(r"\b(performance|slow|optimi[sz]e|latency|memory use)\b", re.I),
                    "Measure before and after where possible; avoid changing behaviour."),
    "docs": ("Documentation", re.compile(r"\b(readme|documentation|docs|user guide)\b", re.I),
             "Write short, accurate documentation that matches the code."),
    "accessibility": ("Accessibility", re.compile(r"\b(accessib\w*|a11y|screen reader|keyboard navigation|contrast)\b", re.I),
                      "Keyboard access, labels, focus order and contrast."),
}

ARCHITECT_PROMPT = """\
You are the architect on a small software team. Plan the work for the request
below for developers who see only their own task. Reply with ONLY a JSON object:
{"acceptance": ["verifiable criterion", ...],
 "architecture": "short notes: modules, interfaces, shared types, risks",
 "tasks": [{"id": "T1", "title": "...", "description": "what to change and how",
            "files": ["path/in/project", ...], "depends_on": [], "specialty": ""}]}
Rules: use the fewest tasks that keep each one focused (1 to {max_tasks}).
Tasks that change the same file must depend on each other. Name only files that
exist or must be created. "specialty" may be one of: ui, database, api, luau,
performance, docs, accessibility, or empty."""

REVIEW_PROMPT = ctx.REVIEW_INSTRUCTIONS + """
List each problem on its own line as: SEVERITY | file:line | problem
(SEVERITY is BLOCKER or MINOR). Check correctness, missed requirements, security,
unsafe file or command use, leaked credentials and unfinished code."""

DEBUG_PROMPT = """\
You are the debugger. A check failed after the team's changes. Find the root
cause from the output and the code, then fix it with the smallest change.
Explain the cause in one or two sentences before the blocks."""

UNSAFE = [
    (re.compile(r"\b(os\.system|subprocess\.(call|run|Popen)\([^)]*shell\s*=\s*True)"), "runs a shell command"),
    (re.compile(r"\beval\(|\bexec\("), "evaluates dynamic code"),
    (re.compile(r"\bloadstring\("), "loads code from a string (Luau)"),
    (re.compile(r"\b(rm\s+-rf|shutil\.rmtree|Remove-Item\s+-Recurse)"), "deletes files recursively"),
    (re.compile(r"verify\s*=\s*False"), "turns off TLS certificate checks"),
]
ENV_PROBLEM = re.compile(r"No module named (pytest|unittest)\b|is not recognized as an internal or external command|"
                         r"command not found|ModuleNotFoundError: No module named '?(pytest|_pytest)|"
                         r"Cannot find module|npm ERR! missing script|not installed", re.I)
UNFINISHED = re.compile(r"\b(TODO|FIXME|NotImplementedError|XXX)\b|^\s*pass\s*$|--\s*TODO", re.I)


@dataclass
class PlanTask:
    key: str
    title: str
    description: str
    files: list[str]
    depends_on: list[str]
    specialty: str = ""
    extra: dict = field(default_factory=dict)  # From a lead plan: model, why, expected_output, constraints.


class PlanError(ValueError):
    pass


def eligible(cls_kind: str, general: bool) -> bool:
    """TEAM ROLES only handles development work; questions stay direct."""
    return cls_kind == "edit" and not general


def needs_architect(cls) -> tuple[bool, str]:
    if cls.category in ("architecture", "feature", "integration", "refactor"):
        return True, f"the request is {cls.category.replace('_', ' ')} work"
    if cls.scope == "multiple modules":
        return True, "the change reaches several modules"
    if cls.complexity == "high":
        return True, "the request was classified as high complexity"
    return False, ""


def detect_specialties(text: str) -> dict[str, str]:
    found = {}
    for key, (label, pattern, _) in SPECIALISTS.items():
        m = pattern.search(text)
        if m:
            found[key] = f'{label} specialist: the request mentions "{m.group(0)}"'
    return found


def parse_plan(text: str, max_tasks: int) -> tuple[list[PlanTask], list[str], str, list[str]]:
    """Validate a plan from the architect. Returns (tasks, acceptance, architecture, notes).

    Deterministic checks: valid JSON, task limit, unique ids and titles, known
    dependencies, no cycles, safe relative paths. Tasks that touch the same file
    are chained so they never run at the same time.
    """
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise PlanError("The plan was not JSON.")
    try:
        data = json.loads(m.group(0))
    except ValueError as exc:
        raise PlanError(f"The plan was not valid JSON ({exc}).") from exc
    raw = data.get("tasks") or []
    if not isinstance(raw, list) or not raw:
        raise PlanError("The plan has no tasks.")
    notes: list[str] = []
    if len(raw) > max_tasks:
        notes.append(f"The plan had {len(raw)} tasks; kept the first {max_tasks} (task limit).")
        raw = raw[:max_tasks]
    tasks: list[PlanTask] = []
    seen_keys, seen_titles = set(), set()
    for i, t in enumerate(raw):
        if not isinstance(t, dict):
            continue
        key = str(t.get("id") or f"T{i + 1}")[:12]
        title = str(t.get("title") or "").strip()[:160]
        if not title or key in seen_keys or title.lower() in seen_titles:
            notes.append(f"Dropped a duplicate or empty task ({key}).")
            continue
        files = []
        for f in t.get("files") or []:
            f = str(f).replace("\\", "/").strip()
            while f.startswith("./"):  # Only the "./" prefix; "../" must stay visible to the check below.
                f = f[2:]
            if not f or f.startswith("/") or ".." in f.split("/") or re.match(r"^[A-Za-z]:", f):
                notes.append(f"Ignored an unsafe path in {key}: {f}")
                continue
            files.append(f)
        spec = str(t.get("specialty") or "").strip().lower()
        extra = {k: t[k] for k in ("model", "why", "expected_output", "constraints") if t.get(k)}
        if not isinstance(extra.get("constraints", []), list):
            extra["constraints"] = [extra["constraints"]]
        extra = {k: ([str(c)[:300] for c in v][:8] if k == "constraints" else str(v)[:600]) for k, v in extra.items()}
        tasks.append(PlanTask(key, title, str(t.get("description") or "")[:3000], files,
                              [str(d) for d in (t.get("depends_on") or [])], spec if spec in SPECIALISTS else "", extra))
        seen_keys.add(key)
        seen_titles.add(title.lower())
    if not tasks:
        raise PlanError("No usable tasks in the plan.")
    keys = {t.key for t in tasks}
    for t in tasks:
        unknown = [d for d in t.depends_on if d not in keys or d == t.key]
        if unknown:
            notes.append(f"Removed unknown dependencies of {t.key}: {', '.join(unknown)}")
            t.depends_on = [d for d in t.depends_on if d in keys and d != t.key]
    # Same file in two tasks: the later one waits for the earlier one.
    for i, t in enumerate(tasks):
        for earlier in tasks[:i]:
            if set(t.files) & set(earlier.files) and earlier.key not in t.depends_on:
                t.depends_on.append(earlier.key)
                notes.append(f"{t.key} waits for {earlier.key}: both change {', '.join(sorted(set(t.files) & set(earlier.files)))}.")
    _check_cycles(tasks)
    acceptance = [str(a)[:300] for a in (data.get("acceptance") or []) if str(a).strip()][:10]
    return tasks, acceptance, str(data.get("architecture") or "")[:4000], notes


def _check_cycles(tasks: list[PlanTask]) -> None:
    graph = {t.key: t.depends_on for t in tasks}
    state: dict[str, int] = {}

    def visit(k: str) -> None:
        if state.get(k) == 1:
            raise PlanError(f"The plan has a circular dependency at {k}.")
        if state.get(k) == 2:
            return
        state[k] = 1
        for d in graph.get(k, []):
            visit(d)
        state[k] = 2

    for k in graph:
        visit(k)


def review_diff(files: list[dict]) -> list[dict]:
    """Deterministic review of added lines. Returns findings with file and line."""
    findings = []
    for f in files:
        line_no = 0
        for raw in (f.get("diff") or "").splitlines():
            hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", raw)
            if hunk:
                line_no = int(hunk.group(1)) - 1
                continue
            if raw.startswith("+++") or raw.startswith("---"):
                continue
            if raw.startswith("+"):
                line_no += 1
                added = raw[1:]
                if contains_secret(added):
                    findings.append({"severity": "BLOCKER", "file": f["path"], "line": line_no,
                                     "problem": "A credential-like value is written into the code.", "source": "check"})
                for pattern, what in UNSAFE:
                    if pattern.search(added):
                        findings.append({"severity": "MINOR", "file": f["path"], "line": line_no,
                                         "problem": f"New code {what}; confirm it is intended.", "source": "check"})
                if UNFINISHED.search(added):
                    findings.append({"severity": "MINOR", "file": f["path"], "line": line_no,
                                     "problem": "Unfinished code (TODO, pass or NotImplemented).", "source": "check"})
            elif not raw.startswith("-"):
                line_no += 1
    return findings


def parse_review(text: str) -> tuple[str, list[dict]]:
    verdict = "CHANGES_NEEDED" if re.search(r"VERDICT:\s*CHANGES_NEEDED", text, re.I) else (
        "APPROVE" if re.search(r"VERDICT:\s*APPROVE", text, re.I) else "UNCLEAR")
    findings = []
    for line in text.splitlines():
        m = re.match(r"\s*[-*]?\s*(BLOCKER|MINOR)\s*\|\s*([^|]*)\|\s*(.+)", line, re.I)
        if m:
            loc = m.group(2).strip()
            file, _, ln = loc.partition(":")
            findings.append({"severity": m.group(1).upper(), "file": file.strip(), "line": int(ln) if ln.strip().isdigit() else None,
                             "problem": m.group(3).strip()[:400], "source": "model"})
    if verdict == "CHANGES_NEEDED" and not any(f["severity"] == "BLOCKER" for f in findings):
        findings.append({"severity": "BLOCKER", "file": "", "line": None, "source": "model",
                         "problem": "The reviewer asked for changes: " + text.strip().splitlines()[-1][:300] if text.strip() else ""})
    return verdict, findings


class TeamRunner:
    """Runs one TEAM ROLES workflow for a chat task. One runner per task at a time."""

    def __init__(self, orc: "Orchestrator", task_id: int):
        self.orc = orc
        self.db = orc.db
        self.task_id = task_id
        self.cfg = orc.cfg["team"]

    # Shared helpers --------------------------------------------------------------

    @property
    def task(self) -> dict:
        return self.db.get_task(self.task_id)

    @property
    def run(self) -> dict:
        return self.task["state"].get("team_run") or {}

    def _save_run(self, **changes: Any) -> dict:
        run = dict(self.run)
        run.update(changes)
        self.db.update_task_state(self.task_id, team_run=run)
        self.orc.bus.publish({"type": "team", "task_id": self.task_id})
        return run

    def msg(self, from_role: str, to_role: str, type_: str, body: str, tt: int | None = None) -> None:
        m = self.db.add_team_message(self.task_id, from_role, to_role, type_, body, tt)
        self.orc._act(self.task_id, f"team.{type_}", f"{ROLE_LABEL.get(from_role, from_role)} → "
                      f"{ROLE_LABEL.get(to_role, to_role)}: {body[:220]}", "system", "team")
        self.orc.bus.publish({"type": "team", "task_id": self.task_id, "message": m})

    def _memory(self, category: str, content: str, verified: bool, source: str) -> None:
        self.db.add_memory(self.task["project_id"], category, content, verified, source, self.task_id)

    def _budget_left(self, provider: str) -> str:
        run = self.run
        if run.get("model_calls", 0) >= self.cfg["max_model_calls"]:
            return f"the run used its budget of {self.cfg['max_model_calls']} model requests"
        if provider == CLAUDE and run.get("claude_calls", 0) >= self.cfg["max_claude_calls"]:
            return f"the run used its budget of {self.cfg['max_claude_calls']} Claude requests"
        return ""

    def _claude_allowed(self) -> str:
        """Empty when Claude may be used automatically in this run, else the reason it may not."""
        block = self.orc._cloud_block(self.task_id)
        if block:
            return block
        if self.orc.cfg["routing"].get("zero_paid_mode"):
            return "zero-paid mode is on (Claude is never used automatically)"
        if self.run.get("claude_declined"):
            return "you chose to keep this run on Qwen"
        ok, why = self.orc.claude_available()
        return "" if ok else f"Claude is not available ({why})"

    def route(self, text: str, role: str, files: int) -> tuple[str, str]:
        """Provider for one piece of team work, from the smart router."""
        cls = classify(text, files, "edit" if role in ("developer", "debugger") else "ask")
        # The team set the task type, not the user; keep the reason about the work itself.
        cls.signals = [s for s in cls.signals if not s.startswith("task type chosen by you")]
        if role == "architect" and cls.complexity == "low":
            cls.complexity = "medium"
        blocked = self._claude_allowed()
        d = decide(cls, self.orc.cfg["routing"], not blocked, None, local_only="")
        if d.backend is None:
            return LOCAL, d.reason
        if blocked and d.preferred == CLAUDE:
            return LOCAL, f"Routed to Qwen. Claude would help here, but {blocked}."
        return d.backend, d.reason

    # Model calls -------------------------------------------------------------------

    def call(self, tt: dict, provider: str, system: str, user: str, purpose: str, files_meta: list[dict],
             private: set[str] | None = None) -> str:
        """One model request for a team task, with budgets, privacy and records."""
        over = self._budget_left(provider)
        if over:
            raise _Stop(f"Stopped: {over}.")
        run = self.run
        self._save_run(model_calls=run.get("model_calls", 0) + 1,
                       claude_calls=run.get("claude_calls", 0) + (1 if provider == CLAUDE else 0))
        step = self.db.get_team_task(tt["id"]).get("result", {}).get("step")
        if provider == LOCAL:
            result = self.orc._chat_local(self.task_id, [ChatMessage("system", system), ChatMessage("user", user)],
                                          purpose, step)
            return result.text
        return self.orc._claude_direct(self.task_id, step, purpose, system, user, files_meta, private or set()).text

    # Run ------------------------------------------------------------------------------

    def start(self, objective: str) -> None:
        orc = self.orc
        ws = orc.workspace(self.task["project_id"])
        self._save_run(status="planning", started=time.time(), model_calls=0, claude_calls=0, repairs=0,
                       objective=objective, auto_apply=bool(orc.cfg["permissions"]["auto_apply_patches"]))
        self._git_snapshot()
        orc._act(self.task_id, "team.started", "TEAM ROLES run started.", "system", "plan",
                 reason="TEAM ROLES is on for this chat and the request asks for code changes.")
        pack, analysis = orc._analyze(self.task_id, objective, "Understand the request")
        cls = classify(objective, len(pack.files))
        specialties = detect_specialties(objective)
        arch, why_arch = needs_architect(cls)
        orc._step_end(analysis, "completed", f"{cls.category.replace('_', ' ')}, {cls.complexity} complexity, {cls.scope}; "
                      + ("architect needed" if arch else "small enough for a single developer task") + ".")
        self._memory("requirements", objective, False, "user request")
        self._save_run(specialties=specialties)
        if arch:
            provider, reason = self.route(objective, "architect", len(pack.files))
            tt = self.db.add_team_task(self.task_id, "architect", "Plan the work and the interfaces", plan_key="ARCH",
                                       description=objective, provider=provider, route_reason=reason,
                                       activation_reason=f"Architect activated because {why_arch}.")
            self.msg("orchestrator", "architect", "assignment", f"Plan: {objective[:200]}", tt["id"])
        else:
            self._minimal_plan(objective, pack, cls, specialties)
        self.continue_run()

    def _minimal_plan(self, objective: str, pack: ctx.ContextPack, cls, specialties: dict) -> None:
        spec = next(iter(specialties), "")
        files = [f.path for f in pack.files][:6]
        acceptance = ["The requested change is made.", "Existing checks still pass (when the project has checks)."]
        provider, reason = self.route(objective, "developer", len(files))
        tt = self.db.add_team_task(self.task_id, "developer", objective.splitlines()[0][:120], plan_key="T1",
                                   specialty=spec, description=objective, acceptance=acceptance, files=files,
                                   provider=provider, route_reason=reason,
                                   activation_reason=specialties.get(spec, "Every code change needs a developer."))
        self._save_run(status="running", acceptance=acceptance, planned_by="orchestrator",
                       plan_note="Small, clearly scoped request: one developer task, no separate architect.")
        self.orc._act(self.task_id, "team.planned", "Plan: 1 developer task (no architect needed).", "system", "plan",
                      reason="Small, clearly scoped request; planning a single task needs no model.")
        self.msg("orchestrator", "developer", "assignment", f"{tt['title']} ({ROLE_LABEL['developer']}, "
                 f"{'Qwen' if provider == LOCAL else 'Claude'})", tt["id"])
        self._memory("task_plan", f"T1 developer: {tt['title']}", True, "orchestrator")

    def _architect(self, tt: dict) -> None:
        orc = self.orc
        ws = orc.workspace(self.task["project_id"])
        objective = tt["description"]
        specialties = self.run.get("specialties") or {}
        provider, reason = tt["provider"], tt["route_reason"]
        pack = orc._gather(self.task_id, objective)
        self.db.update_team_task(tt["id"], status="running", attempts=tt["attempts"] + 1)
        step = orc._step(self.task_id, "Architect: plan the work", provider, "plan", reason)
        self.db.update_team_task(tt["id"], result={"step": step})
        system = ARCHITECT_PROMPT.replace("{max_tasks}", str(self.cfg["max_tasks"]))
        view, private = orc._cloud_view(pack) if provider == CLAUDE else (pack, set())
        user = ctx.project_header(ws, view) + "\n\n" + ctx.task_message(objective, view)
        if specialties:
            user += "\n\nAreas mentioned: " + ", ".join(SPECIALISTS[k][0] for k in specialties)
        plan = None
        for attempt in (1, 2):
            try:
                text = self.call(tt, provider, system, user, "plan", [{"path": f.path, "sha": f.sha, "chars": len(f.content)}
                                                                        for f in view.files], private)
                plan = parse_plan(text, self.cfg["max_tasks"])
                break
            except PlanError as exc:
                orc._act(self.task_id, "team.plan_rejected", f"The architect's plan was not usable: {exc}", "system", "plan")
                user += f"\n\nYour previous plan was rejected: {exc} Reply with only the JSON object."
            except BackendError as exc:
                if exc.kind == "cancelled":
                    raise
                orc._step_end(step, "failed", error=str(exc))
                break
        if plan is None:
            orc._step_end(step, "failed", error="No usable plan; falling back to a single developer task.")
            self.db.update_team_task(tt["id"], status="failed", error="No usable plan; continuing with a single task.")
            self._minimal_plan(objective, pack, classify(objective, len(pack.files)), specialties)
            return
        tasks, acceptance, architecture, notes = plan
        key_to_id = self._add_plan_tasks(tasks, acceptance, specialties)
        orc._step_end(step, "completed", f"{len(tasks)} task(s), {len(acceptance)} acceptance criteria.")
        self.db.update_team_task(tt["id"], status="completed", result={"step": step, "tasks": len(tasks), "notes": notes})
        self._save_run(status="running", acceptance=acceptance, architecture=architecture, planned_by="architect",
                       plan_note="; ".join(notes))
        self._memory("architecture", architecture or "(no notes)", False, f"architect ({'Qwen' if provider == LOCAL else 'Claude'})")
        self._memory("task_plan", "\n".join(f"{p.key}: {p.title} (after {', '.join(p.depends_on) or 'nothing'})" for p in tasks),
                     True, "orchestrator")
        self.msg("architect", "orchestrator", "completed", f"Plan with {len(tasks)} task(s); "
                 f"acceptance: {'; '.join(acceptance[:3]) or 'not stated'}", tt["id"])
        for note in notes:
            orc._act(self.task_id, "team.plan_adjusted", note, "system", "plan", reason="Plan checks by the orchestrator.")
        for p in tasks:
            row = self.db.get_team_task(key_to_id[p.key])
            self.msg("orchestrator", "developer", "assignment",
                     f"{p.key} {p.title} → {'Qwen' if row['provider'] == LOCAL else 'Claude'}"
                     + (f" ({SPECIALISTS[row['specialty']][0]})" if row["specialty"] else ""), row["id"])

    def _add_plan_tasks(self, tasks: list[PlanTask], acceptance: list[str], specialties: dict,
                        lead: bool = False) -> dict[str, int]:
        """Developer tasks for a plan, each with its task specification. Returns plan key -> team task id."""
        key_to_id: dict[str, int] = {}
        memory = self.orc.memory
        for p in tasks:
            spec = p.specialty or next((k for k in specialties if SPECIALISTS[k][1].search(p.title + " " + p.description)), "")
            if lead:
                prov, why_route = self._lead_route(p)
                act_reason = p.extra.get("why") or "Implementation task from Claude's plan."
            else:
                prov, why_route = self.route(f"{p.title}. {p.description}", "developer", len(p.files))
                act_reason = (specialties.get(spec) or f"{SPECIALISTS[spec][0]} specialist: the plan assigns this area.") \
                    if spec else "Implementation task from the architect's plan."
            row = self.db.add_team_task(
                self.task_id, "developer", p.title, plan_key=p.key, specialty=spec, description=p.description,
                acceptance=acceptance, files=p.files, depends_on=[key_to_id[d] for d in p.depends_on if d in key_to_id],
                provider=prov, route_reason=why_route, activation_reason=act_reason,
                spec={"parent": self.task_id, "project": self.task["project_id"], "why": p.extra.get("why", ""),
                      "expected_output": p.extra.get("expected_output")
                      or "Change blocks that Enoy validates and applies, plus a one-line summary.",
                      "constraints": list(p.extra.get("constraints") or []) + [
                          "Change only the files this task owns unless required.",
                          "Do not claim to have run tests or changed files: Enoy applies the change and runs the checks."],
                      "model_requested": p.extra.get("model", ""),
                      "privacy": "stays on this PC" if prov == LOCAL else "sent to Claude without private files",
                      "memory_mode": f"{memory.mode} ({memory.ceiling_mib:,} MiB)" if memory is not None else "",
                      "tests": "QA runs the project's checks after all tasks are applied.",
                      "verification": "not verified yet"})
            key_to_id[p.key] = row["id"]
        # Dependencies may point forward in the list; resolve them now that all ids exist.
        for p in tasks:
            self.db.update_team_task(key_to_id[p.key], depends_on=[key_to_id[d] for d in p.depends_on if d in key_to_id])
        return key_to_id

    def _lead_route(self, p: PlanTask) -> tuple[str, str]:
        """Claude's choice of model for one task, within what Enoy allows."""
        why = p.extra.get("why", "")
        if p.extra.get("model") == CLAUDE:
            blocked = self._claude_allowed()
            if not blocked:
                return CLAUDE, f"Claude kept this part for itself: {why or 'it needs stronger reasoning'}."
            return LOCAL, f"Claude wanted this part for itself, but {blocked}; Qwen does it."
        return LOCAL, f"Claude gave this to Qwen: {why or 'clear, routine work'}."

    def start_from_lead(self, objective: str, plan: tuple) -> None:
        """Run a plan Claude wrote as the lead: the team carries it out and Claude checks the result."""
        orc = self.orc
        tasks, acceptance, architecture, notes = plan
        self._save_run(status="planning", started=time.time(), model_calls=0, claude_calls=0, repairs=0, lead=True,
                       lead_reviews=0, objective=objective, planned_by="Claude (lead)",
                       auto_apply=bool(orc.cfg["permissions"]["auto_apply_patches"]))
        self._git_snapshot()
        self.db.add_team_task(self.task_id, "architect", "Plan the work (Claude as lead)", plan_key="PLAN",
                              description=objective, provider=CLAUDE, status="completed", acceptance=acceptance,
                              route_reason="Claude leads: its first reply contained the plan.",
                              activation_reason="The lead's plan stands in for a separate architect step.")
        specialties = detect_specialties(objective)
        key_to_id = self._add_plan_tasks(tasks, acceptance, specialties, lead=True)
        self._save_run(status="running", acceptance=acceptance, architecture=architecture, specialties=specialties,
                       plan_note="; ".join(notes))
        orc._act(self.task_id, "team.planned", f"Claude planned {len(tasks)} task(s) for the team.", CLAUDE, "plan",
                 reason="Claude leads: it decided this request needs file changes.")
        self._memory("task_plan", "\n".join(f"{p.key}: {p.title} (after {', '.join(p.depends_on) or 'nothing'})"
                                             for p in tasks), True, "Claude (lead)")
        if architecture:
            self._memory("architecture", architecture, False, "Claude (lead)")
        for note in notes:
            orc._act(self.task_id, "team.plan_adjusted", note, "system", "plan", reason="Plan checks by the orchestrator.")
        for p in tasks:
            row = self.db.get_team_task(key_to_id[p.key])
            self.msg("lead", "developer", "assignment", f"{p.key} {p.title} → {'Qwen' if row['provider'] == LOCAL else 'Claude'}"
                     f" ({row['route_reason'][:120]})", row["id"])
        self.continue_run()

    def lead_fix(self, instruction: str) -> None:
        """Claude's final check found a defect: one more developer task for it, then verify again."""
        run = self._save_run(lead_reviews=self.run.get("lead_reviews", 0) + 1, status="running")
        devs = [t for t in self.db.team_tasks(self.task_id) if t["role"] == "developer" and t["status"] == "completed"]
        files = sorted({f for t in devs for f in t["result"].get("changed", [])})
        key = f"LEAD{run['lead_reviews']}"
        tt = self.db.add_team_task(self.task_id, "developer", f"Fix: {instruction[:100]}", plan_key=key,
                                   description=instruction, files=files, acceptance=run.get("acceptance", []),
                                   provider=LOCAL, route_reason="Claude's check found a defect; Qwen fixes it.",
                                   activation_reason="Claude (lead) asked for a fix after checking the results.",
                                   spec={"parent": self.task_id, "why": "Claude's final check",
                                         "expected_output": "Change blocks fixing exactly this defect.",
                                         "verification": "not verified yet"})
        self.msg("lead", "developer", "assignment", f"{key} {tt['title']}", tt["id"])
        self.orc._act(self.task_id, "lead.fix_ordered", f"Claude ordered a fix: {instruction[:200]}", CLAUDE, "repair",
                      reason="Claude's final check found a defect the team can fix.")
        self.continue_run()

    def _git_snapshot(self) -> None:
        """Remember which files already had uncommitted changes of yours; the team never auto-applies over them."""
        ws = self.orc.workspace(self.task["project_id"])
        changed = gitinfo.changed_files(ws.root)
        if changed is None:
            self._save_run(git=None)
            return
        self._save_run(git={"user_changes": changed[:500]})
        if changed:
            self.orc._act(self.task_id, "git.checked", f"Git: {len(changed)} file(s) have uncommitted changes of yours. "
                          "Changes to them always wait for your review.", "system", "plan",
                          reason="Enoy checks git status before the team edits anything.")

    # Scheduling ---------------------------------------------------------------------

    def continue_run(self) -> None:
        """Advance the run as far as possible. Safe to call again after a pause or approval."""
        orc = self.orc
        while True:
            orc._check_cancel(self.task_id)
            task = self.task
            if self.run.get("hold"):
                self._pause("You paused delegation for this chat.")
                return
            if not task["state"].get("team") and not self.run.get("lead"):
                self._pause("TEAM ROLES was switched off for this chat.")
                return
            if self.run.get("status") in ("completed", "failed", "cancelled"):
                return
            tasks = self.db.team_tasks(self.task_id)
            by_id = {t["id"]: t for t in tasks}
            # Dependencies that can no longer finish block their dependents.
            for t in tasks:
                if t["status"] in ("planned", "ready", "paused") and any(
                        by_id.get(d, {}).get("status") in ("failed", "blocked", "cancelled") for d in t["depends_on"]):
                    self.db.update_team_task(t["id"], status="blocked", error="A task it depends on did not finish.")
                    self.msg("orchestrator", t["role"], "dependency_request", f"{t['plan_key']} is blocked: a task it depends on did not finish.", t["id"])
            tasks = self.db.team_tasks(self.task_id)
            arch = next((t for t in tasks if t["role"] == "architect" and t["status"] in ("planned", "paused")), None)
            if arch:
                if self._gate([arch]):
                    return
                self._architect(arch)
                continue
            if any(t["status"] == "awaiting_review" for t in tasks):
                self._wait_for_review()
                return
            ready = [t for t in tasks if t["role"] in ("developer", "debugger") and t["status"] in ("planned", "ready", "paused")
                     and all(by_id[d]["status"] == "completed" for d in t["depends_on"] if d in by_id)]
            if not ready:
                if any(t["status"] in ("planned", "ready", "running", "waiting") for t in tasks if t["role"] in ("developer", "debugger")):
                    return  # Others are still running in parallel; they continue the run when done.
                self._verify_and_finish()
                return
            batch = self._pick_batch(ready)
            if self._gate(batch):
                return
            self._run_batch(batch)

    def _gate(self, batch: list[dict]) -> bool:
        """Ask once per run before the team's first Claude request, if Settings say to ask.

        Returns True when the run must wait for the answer. The question lists every
        Claude use planned so far; the answer covers this run only, within its budget.
        """
        if not any(t["provider"] == CLAUDE for t in batch):
            return False
        run = self.run
        if run.get("claude_ok") or not self.orc.cfg["claude"]["confirm_before_send"]:
            return False
        blocked = self._claude_allowed()
        if blocked:
            for t in self.db.team_tasks(self.task_id):
                if t["provider"] == CLAUDE and t["status"] in TEAM_OPEN:
                    self.db.update_team_task(t["id"], provider=LOCAL, route_reason=f"Moved to Qwen: {blocked}.")
            return False
        planned = [t for t in self.db.team_tasks(self.task_id) if t["provider"] == CLAUDE and t["status"] in TEAM_OPEN]
        self._save_run(status="awaiting_claude", claude_request={
            "tasks": [f"{t['plan_key']} {ROLE_LABEL[t['role']]}: {t['title'][:80]}" for t in planned],
            "budget": self.cfg["max_claude_calls"]})
        self.msg("orchestrator", "user", "question", f"May the team use Claude for {len(planned)} task(s) in this run "
                 f"(at most {self.cfg['max_claude_calls']} requests)?")
        self.orc._act(self.task_id, "approval.requested", f"Waiting for your OK to use Claude in this run "
                      f"({len(planned)} task(s)).", "user", "team", reason="Settings: ask before sending anything to Claude.")
        self.orc._set(self.task_id, status="awaiting_claude")
        return True

    def answer_claude(self, allow: bool) -> None:
        if allow:
            self._save_run(claude_ok=True, status="running", claude_request=None)
            self.orc._act(self.task_id, "approval.resolved", "You allowed Claude for this run.", "user", "team", outcome="approved")
        else:
            for t in self.db.team_tasks(self.task_id):
                if t["provider"] == CLAUDE and t["status"] in TEAM_OPEN:
                    self.db.update_team_task(t["id"], provider=LOCAL, route_reason="You chose to keep this run on Qwen.")
            self._save_run(claude_declined=True, status="running", claude_request=None)
            self.orc._act(self.task_id, "approval.resolved", "You kept this run on Qwen; nothing was sent to Claude.",
                          "user", "team", outcome="declined")

    def _pick_batch(self, ready: list[dict]) -> list[dict]:
        """Independent tasks with disjoint files, at most max_parallel, at most one per provider.

        Qwen runs one request at a time anyway, so two Qwen tasks in parallel would
        only queue. Different providers can genuinely work at the same time.
        """
        batch: list[dict] = []
        used_files: set[str] = set()
        used_providers: set[str] = set()
        for t in ready:
            if len(batch) >= self.cfg["max_parallel"]:
                break
            if t["provider"] in used_providers or used_files & set(t["files"]):
                continue
            batch.append(t)
            used_files |= set(t["files"])
            used_providers.add(t["provider"])
        return batch or ready[:1]

    def _run_batch(self, batch: list[dict]) -> None:
        if len(batch) == 1 or self.orc.synchronous:
            for t in batch:
                self._develop(t)
            return
        names = ", ".join(t["plan_key"] for t in batch)
        self.orc._act(self.task_id, "team.parallel", f"Running {names} at the same time.", "system", "implement",
                      reason="They do not depend on each other, change different files, and use different models.")
        errors: list[BaseException] = []

        def work(t: dict) -> None:
            try:
                self._develop(t)
            except BaseException as exc:  # Re-raised in the run thread below.
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(t,), daemon=True) for t in batch]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        if errors:
            raise errors[0]

    # Developer and debugger work ---------------------------------------------------------

    def _develop(self, tt: dict, feedback: str = "") -> None:
        orc = self.orc
        ws = orc.workspace(self.task["project_id"])
        role = tt["role"]
        provider = tt["provider"]
        self.db.update_team_task(tt["id"], status="running", attempts=tt["attempts"] + 1)
        label = f"{ROLE_LABEL[role]}{' (' + SPECIALISTS[tt['specialty']][0] + ')' if tt['specialty'] else ''}"
        step = orc._step(self.task_id, f"{label}: {tt['title'][:90]}", provider, STAGE_OF_ROLE[role], tt["route_reason"],
                         files=tt["files"])
        result = dict(tt["result"])
        result["step"] = step
        self.db.update_team_task(tt["id"], result=result)
        pack = orc._gather(self.task_id, tt["title"] + "\n" + tt["description"])
        if tt["files"]:
            extra = [f for f in tt["files"] if f not in {p.path for p in pack.files}]
            if extra:
                pack = ctx.gather(ws, tt["title"] + "\n" + tt["description"], orc.cfg["context"], extra)
        brief = self._brief(tt)
        if provider == LOCAL:  # Only what fits Qwen's context window; Ollama would cut the rest unseen.
            budget = ctx.prompt_budget(*orc._local_window())
            pack = ctx.fit_files(pack, budget - len(brief) - len(feedback) - 4000)
        if role == "debugger":
            system = (ctx.local_system_prompt(ws, pack, "edit") if provider == LOCAL else ctx.CLAUDE_SYSTEM) + "\n\n" + DEBUG_PROMPT
        else:
            system = ctx.local_system_prompt(ws, pack, "edit") if provider == LOCAL else ctx.CLAUDE_SYSTEM
        if tt["specialty"]:
            system += "\n\nSpecialist focus: " + SPECIALISTS[tt["specialty"]][2]
        # Claude sees only the files it may receive; Qwen sees the whole pack.
        view, private = orc._cloud_view(pack) if provider == CLAUDE else (pack, set())
        user = ctx.task_message(brief, view, feedback)
        if provider == CLAUDE:
            user = ctx.project_header(ws, view) + "\n\n" + user + "\n\n" + patches.FORMAT_INSTRUCTIONS
        files_meta = [{"path": f.path, "sha": f.sha, "chars": len(f.content)} for f in view.files]
        versions = {f.path: f.sha for f in pack.files}
        try:
            text = self.call(self.db.get_team_task(tt["id"]), provider, system, user, "edit", files_meta, private)
        except BackendError as exc:
            if exc.kind == "cancelled":
                raise
            orc._step_end(step, "failed", error=str(exc))
            who = "" if exc.kind == "budget" else f"{'Qwen' if provider == LOCAL else 'Claude'} failed: "
            self._task_failed(tt, f"{who}{exc}", step_done=True)
            return
        if self.run.get("lead"):  # Claude answers the user at the end; the work stays in the team panel.
            result["output"] = text[:4000]
            self.db.update_team_task(tt["id"], result=result)
        else:
            self.db.add_message(self.task_id, "assistant", f"**{label}** · {tt['title']}\n\n{text}", provider)
            orc.bus.publish({"type": "message_done", "task_id": self.task_id, "backend": provider})
        parsed = patches.parse(text)
        validation = patches.validate(ws, parsed.changes, versions)
        problems = parsed.errors + validation.errors
        if not validation.files and not problems:
            problems = ["No usable file changes were produced."]
        if provider == CLAUDE and not parsed.changes and not parsed.errors:
            # Claude explained why it changed nothing (the code is not in this project...). Asking again with
            # the same files gets the same answer and costs another request, so its reason ends the task.
            why = _first_line(parsed.explanation or text)[:300]
            orc._step_end(step, "failed", error=f"Claude made no changes: {why}")
            self._task_failed(tt, f"Claude made no changes: {why}", step_done=True)
            return
        if problems:
            orc._step_end(step, "failed", error="Proposal failed Enoy's checks: " + "; ".join(problems[:3]))
            tt = self.db.get_team_task(tt["id"])
            fb = ("Your previous answer could not be applied because:\n- " + "\n- ".join(problems[:6])
                  + "\nSend corrected blocks for ALL intended changes, following the block format exactly.")
            self.msg("orchestrator", role, "rejection", f"{tt['plan_key']} proposal rejected: {problems[0][:160]}", tt["id"])
            if tt["attempts"] < self.orc.cfg["routing"]["max_local_attempts"]:
                self._develop(tt, fb)
                return
            if provider == LOCAL and not self._claude_allowed() and self.orc.cfg["routing"].get("failure_escalation", True):
                reason = f"Qwen's proposal failed Enoy's checks {tt['attempts']} time(s)."
                self.db.update_team_task(tt["id"], provider=CLAUDE, route_reason=f"Escalated: {reason}", attempts=0)
                self.msg("orchestrator", role, "escalation", f"{tt['plan_key']} moves to Claude: {reason}", tt["id"])
                orc._act(self.task_id, "task.escalated", f"{tt['plan_key']} escalated from Qwen to Claude.", CLAUDE,
                         STAGE_OF_ROLE[role], reason=reason)
                self._develop(self.db.get_team_task(tt["id"]), fb)
                return
            self._task_failed(tt, "Could not produce a valid change: " + "; ".join(problems[:2]), step_done=True)
            return
        cs_id = orc._propose(self.task_id, provider, validation, parsed.explanation)
        names = [f.path for f in validation.files]
        orc._step_end(step, "completed", f"Proposed change set #{cs_id}: {', '.join(names)}.")
        result.update({"changeset": cs_id, "changed": names, "summary": _first_line(parsed.explanation or text)})
        self.db.update_team_task(tt["id"], changeset_id=cs_id, result=result)
        self.msg(role, "orchestrator", "proposal", f"{tt['plan_key']} proposes change set #{cs_id} ({', '.join(names)})", tt["id"])
        self._apply_or_wait(self.db.get_team_task(tt["id"]))

    def _apply_or_wait(self, tt: dict) -> None:
        if not self.run.get("auto_apply"):
            self.db.update_team_task(tt["id"], status="awaiting_review")
            return
        mine = set((self.run.get("git") or {}).get("user_changes") or [])
        touched = mine & set(tt["result"].get("changed", []))
        if touched:
            self.db.update_team_task(tt["id"], status="awaiting_review")
            self.orc._act(self.task_id, "git.protected", f"{tt['plan_key']} changes {', '.join(sorted(touched))}, which "
                          "had uncommitted changes of yours, so it waits for your review.", "user", "implement",
                          reason="Changes over your own uncommitted work are never applied automatically.")
            return
        try:
            self.orc._apply_changeset(tt["changeset_id"], finish=False)
        except Exception as exc:  # Conflict: the files changed since the proposal.
            if tt["attempts"] < 2:
                self.msg("orchestrator", tt["role"], "rejection", f"{tt['plan_key']}: files changed meanwhile; redoing it on the current files.", tt["id"])
                self._develop(self.db.get_team_task(tt["id"]), f"Your change could not be applied: {exc} Redo it against the current file contents.")
                return
            self._task_failed(tt, f"Could not apply the change: {exc}")
            return
        self.task_applied(tt["changeset_id"])

    def task_applied(self, cs_id: int) -> None:
        """A team change set was applied (automatically or by the user)."""
        tt = next((t for t in self.db.team_tasks(self.task_id) if t["changeset_id"] == cs_id), None)
        if not tt:
            return
        spec = dict(tt.get("spec") or {})
        if spec:
            spec["verification"] = "applied; checks run after all tasks"
        self.db.update_team_task(tt["id"], status="completed", spec=spec)
        self.msg("orchestrator", tt["role"], "approval", f"{tt['plan_key']} applied: {', '.join(tt['result'].get('changed', []))}", tt["id"])
        self._memory("implementation", f"{tt['plan_key']} {tt['title']}: changed {', '.join(tt['result'].get('changed', []))}",
                     True, f"change set #{cs_id}")

    def task_rejected(self, cs_id: int) -> None:
        tt = next((t for t in self.db.team_tasks(self.task_id) if t["changeset_id"] == cs_id), None)
        if tt:
            self.db.update_team_task(tt["id"], status="cancelled", error="You discarded its change.")
            self.msg("orchestrator", tt["role"], "rejection", f"{tt['plan_key']}: you discarded the change; tasks that depend on it are blocked.", tt["id"])

    def _task_failed(self, tt: dict, error: str, step_done: bool = False) -> None:
        self.db.update_team_task(tt["id"], status="failed", error=error)
        self.msg(tt["role"], "orchestrator", "test_failure" if tt["role"] == "qa" else "completed",
                 f"{tt['plan_key']} failed: {error[:200]}", tt["id"])
        self._memory("known_issues", f"{tt['plan_key']} {tt['title']}: {error}", True, "orchestrator")

    def _brief(self, tt: dict) -> str:
        """The task-specific package: no chat history, only what this task needs."""
        run = self.run
        parts = [f"Team task {tt['plan_key']} ({ROLE_LABEL[tt['role']]}): {tt['title']}", tt["description"] or ""]
        parts.append(f"Overall goal: {run.get('objective', '')[:1500]}")
        if run.get("acceptance"):
            parts.append("Acceptance criteria:\n" + "\n".join(f"- {a}" for a in run["acceptance"]))
        if run.get("architecture"):
            parts.append("Architecture notes (from the architect; a proposal, the code is authoritative):\n" + run["architecture"][:2500])
        done = [t for t in self.db.team_tasks(self.task_id) if t["id"] in tt["depends_on"] and t["status"] == "completed"]
        if done:
            parts.append("Already done by teammates:\n" + "\n".join(
                f"- {t['plan_key']} {t['title']}: changed {', '.join(t['result'].get('changed', [])) or 'nothing'}" for t in done))
        if tt["files"]:
            parts.append("Files this task owns: " + ", ".join(tt["files"]) + ". Do not change other files unless required.")
        spec = tt.get("spec") or {}
        if spec.get("expected_output"):
            parts.append("Expected output: " + spec["expected_output"])
        if spec.get("constraints"):
            parts.append("Constraints:\n" + "\n".join(f"- {c}" for c in spec["constraints"]))
        parts.append("End with one line: UNRESOLVED: <anything you could not finish, or none>.")
        return "\n\n".join(p for p in parts if p)

    # Review gate, verification, repair ----------------------------------------------------

    def _wait_for_review(self) -> None:
        waiting = [t for t in self.db.team_tasks(self.task_id) if t["status"] == "awaiting_review"]
        self._save_run(status="awaiting_review")
        # A team task waits only when auto-apply is off or the change touches your own uncommitted work.
        self.orc._await_approval(self.task_id, ask=True)
        self.msg("orchestrator", "user", "approval", f"Waiting for you to review {len(waiting)} change(s): "
                 + ", ".join(t["plan_key"] for t in waiting))

    def _verify_and_finish(self) -> None:
        orc = self.orc
        tasks = self.db.team_tasks(self.task_id)
        devs = [t for t in tasks if t["role"] in ("developer", "debugger")]
        if not any(t["status"] == "completed" for t in devs):
            self._finish("failed", "No task finished, so there is nothing to verify.")
            return
        self._save_run(status="verifying")
        # Integrate: every finished task's change must actually be on disk.
        applied = [orc.db.get_changeset(t["changeset_id"]) for t in devs if t["changeset_id"]]
        missing = [c["id"] for c in applied if c and c["status"] != "applied"]
        integ = orc._step(self.task_id, "Integrate: confirm every change is applied", "system", "integrate",
                          "Enoy checks that the team's changes are all in the project before testing.")
        if missing:
            orc._step_end(integ, "failed", error=f"Change set(s) not applied: {missing}")
            self._finish("failed", f"Some changes were not applied ({missing}).")
            return
        orc._step_end(integ, "completed", f"{len(applied)} change set(s) applied.")
        qa_ok, qa_note = self._qa()
        blockers = self._review([c for c in applied if c])
        failed_devs = [t for t in devs if t["status"] in ("failed", "blocked", "cancelled")]
        if (not qa_ok or blockers) and self.run.get("repairs", 0) < self.cfg["max_repair_rounds"]:
            self._repair(qa_note if not qa_ok else "", blockers)
            return
        if not qa_ok or blockers or failed_devs:
            problems = []
            if not qa_ok:
                problems.append(qa_note)
            if blockers:
                problems.append(f"{len(blockers)} review blocker(s) open")
            if failed_devs:
                problems.append(f"{len(failed_devs)} task(s) did not finish")
            self._finish("failed", "Stopped with problems: " + "; ".join(problems) + ".")
            return
        self._finish("completed", qa_note)

    def _qa(self) -> tuple[bool, str]:
        orc = self.orc
        task = self.task
        ws = orc.workspace(task["project_id"])
        allow = orc.cfg["permissions"]["command_allowlist"]
        commands = [c for c in ws.suggest_test_commands() if cmdpolicy.classify(c, allow).level == "allowlisted"][:2]
        tt = self.db.add_team_task(self.task_id, "qa", "Run the project's checks", plan_key="QA", provider="system",
                                   route_reason="Real test tools first; no model needed to run them.",
                                   activation_reason="Every team run is verified before it is called done.",
                                   status="running", acceptance=self.run.get("acceptance", []))
        if not commands:
            note = "No allowlisted test command was found, so only Enoy's syntax checks verified the change."
            self.db.update_team_task(tt["id"], status="completed", result={"commands": [], "note": note})
            self.msg("qa", "orchestrator", "completed", note, tt["id"])
            self._memory("test_results", note, True, "QA")
            self._save_run(verification="not_run")
            return True, note
        results = []
        for cmd in commands:
            run = orc.run_command(task["project_id"], cmd, approved=False, task_id=self.task_id, wait=True)
            r = orc.db.get_command_run(run["run_id"]) if run.get("run_id") else None
            results.append({"command": cmd, "status": r["status"] if r else "not run",
                            "exit_code": r["exit_code"] if r else None,
                            "tail": "\n".join(((r["stdout"] + "\n" + r["stderr"]) if r else "").strip().splitlines()[-25:])})
        env = [r for r in results if r["status"] != "passed" and ENV_PROBLEM.search(r["tail"])]
        if env and len(env) == len([r for r in results if r["status"] != "passed"]):
            # The check itself could not run (missing tool or package). That says nothing
            # about the code, so it is reported as unverified instead of starting repairs.
            line = next((ln for ln in env[0]["tail"].splitlines() if ENV_PROBLEM.search(ln)), "")
            note = f"The checks could not run: {line.strip()[:200]}. The change is not verified by tests."
            self.db.update_team_task(tt["id"], status="failed", result={"commands": results}, error=note)
            self.msg("qa", "orchestrator", "test_failure", note, tt["id"])
            self._memory("known_issues", note, True, "QA (real check runs)")
            self._save_run(verification="not_run", verification_note=note)
            return True, note
        ok = all(r["status"] == "passed" for r in results)
        note = "; ".join(f"`{r['command']}` {r['status']}" for r in results)
        self.db.update_team_task(tt["id"], status="completed" if ok else "failed", result={"commands": results},
                                 error="" if ok else note)
        self.msg("qa", "orchestrator", "completed" if ok else "test_failure", note, tt["id"])
        self._memory("test_results", note, True, "QA (real check runs)")
        self._save_run(verification="passed" if ok else "failed", last_failure="\n\n".join(
            f"$ {r['command']}\n{r['tail']}" for r in results if r["status"] != "passed"))
        return ok, note

    def _review(self, changesets: list[dict]) -> list[dict]:
        orc = self.orc
        files = [f for c in changesets for f in c["files"]]
        tt = self.db.add_team_task(self.task_id, "reviewer", "Review the team's changes", plan_key="REVIEW",
                                   provider="system", status="running",
                                   route_reason="Deterministic checks first (secrets, unsafe calls, unfinished code).",
                                   activation_reason="Every team run gets a review; a model review only for larger or risky changes.")
        findings = review_diff(files)
        added = sum(1 for f in files for ln in (f.get("diff") or "").splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        risky = (self.task["state"].get("route") or {}).get("risk") == "high" or classify(self.run.get("objective", "")).risk == "high"
        model_review = added >= self.cfg["review_min_lines"] or risky
        if model_review:
            why = f"{added} added lines" if added >= self.cfg["review_min_lines"] else "a high-risk change"
            provider, reason = self.route("Review a risky multi-file change for correctness and security", "reviewer", len(files))
            if provider == CLAUDE and orc.cfg["claude"]["confirm_before_send"] and not self.run.get("claude_ok"):
                provider, reason = LOCAL, "Reviewed by Qwen: Claude was not approved for this run, and asking now would pause after the tests already ran."
            self.db.update_team_task(tt["id"], provider=provider, route_reason=f"Model review because of {why}. {reason}")
            step = orc._step(self.task_id, "Reviewer: independent code review", provider, "review", f"Model review because of {why}.")
            res = dict(self.db.get_team_task(tt["id"])["result"])
            res["step"] = step
            self.db.update_team_task(tt["id"], result=res)
            globs = orc.cfg["privacy"]["cloud_excluded_globs"]
            diff = "\n".join(f["diff"] for f in files
                             if provider == LOCAL or not privacy.cloud_excluded(f["path"], globs))[:20000]
            user = (f"Task: {self.run.get('objective', '')[:1500]}\nAcceptance criteria:\n"
                    + "\n".join(f"- {a}" for a in self.run.get("acceptance", [])) + "\n\nChanges (unified diff):\n" + diff)
            try:
                text = self.call(self.db.get_team_task(tt["id"]), provider, REVIEW_PROMPT, user, "review", [])
                verdict, model_findings = parse_review(text)
                findings += model_findings
                orc._step_end(step, "completed", f"Verdict: {verdict}; {len(model_findings)} finding(s).")
            except BackendError as exc:
                if exc.kind == "cancelled":
                    raise
                orc._step_end(step, "failed", error=str(exc))
                findings.append({"severity": "MINOR", "file": "", "line": None, "source": "check",
                                 "problem": f"The model review could not run ({exc}); only automatic checks were done."})
        blockers = [f for f in findings if f["severity"] == "BLOCKER"]
        self.db.update_team_task(tt["id"], status="completed" if not blockers else "failed",
                                 result={"findings": findings, "model_review": model_review,
                                         "step": self.db.get_team_task(tt["id"])["result"].get("step")},
                                 error=f"{len(blockers)} blocker(s)" if blockers else "")
        for f in findings[:8]:
            loc = f"{f['file']}:{f['line']}" if f.get("file") else "general"
            self.msg("reviewer", "orchestrator", "review_finding", f"{f['severity']} {loc}: {f['problem']}", tt["id"])
        if not findings:
            self.msg("reviewer", "orchestrator", "completed", "No findings.", tt["id"])
        self._memory("review_findings", json.dumps(findings)[:3000] if findings else "No findings.", True, "reviewer")
        return blockers

    def _repair(self, failure: str, blockers: list[dict]) -> None:
        orc = self.orc
        run = self._save_run(repairs=self.run.get("repairs", 0) + 1, status="repairing")
        signature = (run.get("last_failure") or "").strip().splitlines()[-1:] or [""]
        repeated = bool(signature[0]) and signature[0] == run.get("prev_failure_sig")
        self._save_run(prev_failure_sig=signature[0])
        straightforward = run["repairs"] == 1 and not repeated and not blockers
        devs = [t for t in self.db.team_tasks(self.task_id) if t["role"] == "developer" and t["status"] == "completed"]
        files = sorted({f for t in devs for f in t["result"].get("changed", [])})
        report = (run.get("last_failure") or failure)[:4000]
        if blockers:
            report += "\n\nReview blockers:\n" + "\n".join(f"- {b['file']}:{b['line']} {b['problem']}" for b in blockers)
        if straightforward:
            role, reason = "developer", "A first, straightforward failure: the developer fixes it."
            provider = devs[-1]["provider"] if devs else LOCAL
            route_reason = f"Same provider as the original change. {reason}"
        else:
            role = "debugger"
            reason = ("The same error came back after a fix." if repeated else
                      "Repair round 2 or later." if run["repairs"] > 1 else "Review blockers need investigation.")
            provider, route_reason = self.route("Debug a failing check across modules; the same error repeats", "debugger", len(files))
            route_reason = f"{reason} {route_reason}"
        tt = self.db.add_team_task(self.task_id, role, f"Fix: {(failure or 'review blockers')[:100]}",
                                   plan_key=f"FIX{run['repairs']}", description=report, files=files,
                                   acceptance=self.run.get("acceptance", []), provider=provider, route_reason=route_reason,
                                   activation_reason=f"{ROLE_LABEL[role]} activated: {reason}")
        orc._act(self.task_id, "team.repair", f"Repair round {run['repairs']}: {ROLE_LABEL[role]} on "
                 f"{'Qwen' if provider == LOCAL else 'Claude'}.", provider, "repair", reason=reason)
        self.msg("orchestrator", role, "assignment", f"{tt['plan_key']} {tt['title']}", tt["id"])
        self._save_run(status="running")
        self.continue_run()

    def _finish(self, status: str, note: str) -> None:
        orc = self.orc
        tasks = self.db.team_tasks(self.task_id)
        changed = sorted({f for t in tasks for f in t["result"].get("changed", []) if t["status"] == "completed"})
        run = self._save_run(status=status, finished=time.time())
        verified = self.run.get("verification") == "passed"
        headline = ("done" if verified else "done, not verified by tests") if status == "completed" else "stopped with problems"
        lines = [f"**Team result: {headline}**", ""]
        for t in tasks:
            mark = {"completed": "✓", "failed": "✕", "blocked": "⛔", "cancelled": "–"}.get(t["status"], "…")
            who = {"qwen": "Qwen", "claude": "Claude"}.get(t["provider"], "Enoy")
            lines.append(f"- {mark} {t['plan_key']} {ROLE_LABEL[t['role']]} ({who}): {t['title']}"
                         + (f" — {t['error']}" if t["error"] else ""))
        lines.append("")
        lines.append("Changed files: " + (", ".join(changed) or "none"))
        verif = run.get("verification")
        lines.append("Checks: " + ({"passed": "passed (real test run)", "failed": "FAILED", "not_run": "not verified by tests (see below)"}
                                   .get(verif, "not run")) + ".")
        criteria = run.get("acceptance") or []
        if criteria:
            tested = verif == "passed"
            lines.append("Acceptance criteria " + ("(covered only as far as the project's tests check them):" if tested
                                                  else "(not verified by tests):"))
            lines += [f"- {c}" for c in criteria]
        lines.append(f"Model requests: {run.get('model_calls', 0)} ({run.get('claude_calls', 0)} to Claude).")
        if note:
            lines.append("")
            lines.append(note)
        summary = "\n".join(lines)
        self._checkpoint(status, changed, note)
        for t in tasks:
            if t["role"] == "developer" and t["status"] == "completed" and t.get("spec"):
                self.db.update_team_task(t["id"], spec=dict(t["spec"], verification={
                    "passed": "checks passed", "failed": "checks failed"}.get(verif, "applied, not verified by tests")))
        if run.get("lead") and self._lead_review(status, note, summary, tasks):
            return
        orc.db.add_message(self.task_id, "assistant", summary, "team")
        orc.bus.publish({"type": "message_done", "task_id": self.task_id, "backend": "team"})
        self.msg("orchestrator", "user", "handoff", f"Run {status}: {note[:200]}")
        if status == "completed":
            orc._complete(self.task_id, f"Team run done. Changed {', '.join(changed) or 'nothing'}. {note}"[:300])
        else:
            orc._fail(self.task_id, note)

    def _checkpoint(self, status: str, changed: list[str], note: str) -> None:
        """A concise record of the run in the project's shared memory (no model text, no thinking)."""
        run = self.run
        tasks = self.db.team_tasks(self.task_id)
        names = {LOCAL: "Qwen", CLAUDE: "Claude"}
        lines = [f"Request: {run.get('objective', '')[:300]}", f"Planned by: {run.get('planned_by') or 'orchestrator'}",
                 ("Tasks: " + "; ".join(f"{t['plan_key']} {ROLE_LABEL.get(t['role'], t['role'])} on "
                                        f"{names.get(t['provider'], 'Enoy')}: {t['status']}" for t in tasks))[:1200],
                 f"Changed: {', '.join(changed) or 'nothing'}",
                 f"Verified: {run.get('verification') or 'not run'}", f"Outcome: {status}. {note[:300]}"]
        failed = [t for t in tasks if t["status"] in ("failed", "blocked")]
        if failed:
            lines.append("Remaining: " + "; ".join(f"{t['plan_key']} {t['error'][:120]}" for t in failed))
        self._memory("checkpoint", "\n".join(lines), True, "Enoy (recorded results)")

    def _lead_review(self, status: str, note: str, summary: str, tasks: list[dict]) -> bool:
        """Ask Claude to check the results and write the answer. False when it cannot; the summary is used then."""
        orc = self.orc
        run = self.run
        why = "the final check by Claude is off in Settings" if not orc.cfg["lead"]["final_review"] else self._claude_allowed()
        if why:
            orc._act(self.task_id, "lead.review_skipped", f"Claude did not check the results: {why}.", "system", "team")
            self._save_run(lead_review_note=f"Claude did not check the results: {why}.")
            return False
        globs = orc.cfg["privacy"]["cloud_excluded_globs"]
        diffs = []
        for t in tasks:
            cs = orc.db.get_changeset(t["changeset_id"]) if t["changeset_id"] else None
            for f in (cs or {}).get("files", []):
                if not privacy.cloud_excluded(f["path"], globs):
                    diffs.append(f["diff"])
        report = (summary + "\n\nChanges (unified diff):\n" + ("\n".join(diffs)[:12000] or "none")
                  + (f"\n\nLast failing check output:\n{run['last_failure'][:3000]}" if run.get("last_failure") else ""))
        self._save_run(status="lead_review", final_status=status, final_note=note, report=summary)
        self.msg("orchestrator", "lead", "handoff", "Results ready for Claude's check.")
        orc._request_claude(self.task_id, "lead_review", ctx.ContextPack(), extra=report,
                            reason="Claude leads: it checks the team's results and writes the answer.")
        return True

    def finish_after_lead(self, verdict: str, instruction: str, answered: bool) -> None:
        """Claude checked the results. Order one more fix if it asks and the limit allows, else end the run."""
        if (verdict == "fix" and instruction and self.run.get("lead_reviews", 0) < self.orc.cfg["lead"]["max_followups"]
                and self.orc.workspace(self.task["project_id"]).index):
            self.lead_fix(instruction)
            return
        self.end_lead_run(answered)

    def end_lead_run(self, answered: bool, why: str = "") -> None:
        """End a lead run. Without Claude's answer, Enoy's own summary of the results is the reply."""
        run = self._save_run(status=self.run.get("final_status", "completed"), finished=time.time())
        note = run.get("final_note", "")
        if not answered:
            text = run.get("report", "") + (f"\n\nClaude did not check these results: {why}" if why else "")
            self.orc.db.add_message(self.task_id, "assistant", text, "team")
            self.orc.bus.publish({"type": "message_done", "task_id": self.task_id, "backend": "team"})
        self.msg("orchestrator", "user", "handoff", f"Run {run['status']}: {note[:200]}")
        if run["status"] == "completed":
            changed = sorted({f for t in self.db.team_tasks(self.task_id) for f in t["result"].get("changed", [])
                              if t["status"] == "completed"})
            self.orc._complete(self.task_id, f"Team run done. Changed {', '.join(changed) or 'nothing'}. {note}"[:300])
        else:
            self.orc._fail(self.task_id, note)

    def _pause(self, why: str) -> None:
        for t in self.db.team_tasks(self.task_id):
            if t["status"] in ("planned", "ready"):
                self.db.update_team_task(t["id"], status="paused")
        self._save_run(status="paused", paused_reason=why)
        self.orc._act(self.task_id, "team.paused", "Team run paused. Finished work is kept.", "user", "team", reason=why)
        self.orc._set(self.task_id, status="paused", summary="Team run paused. Turn TEAM ROLES on to continue.")


class _Stop(BackendError):
    """A run budget was used up."""

    def __init__(self, message: str):
        super().__init__("budget", message)


def _first_line(text: str) -> str:
    for line in (text or "").strip().splitlines():
        line = line.strip().lstrip("#").strip()
        if line and not line.startswith("<<<"):
            return line[:200]
    return ""


def open_statuses() -> tuple[str, ...]:
    return TEAM_OPEN
