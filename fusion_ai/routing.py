"""Smart task router: classification, routing policy and escalation rules.

Pure functions with no I/O, so every routing rule is unit-tested. The
orchestrator calls classify() once per task, decide() to pick the first
backend, and should_escalate() after each failed or doubtful attempt.

Classification is rule-based and deterministic. It looks at what the request
means (what kind of work, how far it reaches, how costly a wrong answer would
be), not only at its length. No model is ever asked whether Claude is needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .backends.base import CLAUDE, LOCAL

LABEL = {LOCAL: "Qwen", CLAUDE: "Claude"}

EDIT_VERBS = re.compile(
    r"\b(add|implement|create|build|make|fix|refactor|rename|change|update|modify|rewrite|"
    r"remove|delete|convert|port|migrate|write|generate|replace|move|extract|optimi[sz]e|"
    r"rebuild|redo|rework|redesign|restyle|style|tweak|adjust|improve|hide|cent(er|re)|align|resize|"
    r"enable|disable|clean up|"
    r"lägg till|skapa|fixa|ändra|bygg|bygg om|skriv|gör|ta bort|byt)\b",
    re.I,
)
QUESTION_START = re.compile(
    r"^\s*(what|why|how|where|which|who|when|is|are|does|do|can|could|explain|describe|summari[sz]e|"
    r"compare|tell me|walk me through|vad|varför|hur)\b", re.I)
# "Write a tutorial / an email / a summary" asks for prose, not a file change.
PROSE = re.compile(
    r"\b(write|draft|give me|produce)\s+(me\s+)?(a|an|the|some)?\s*(short\s+|detailed\s+|\d+[- ]word\s+)*"
    r"(tutorial|guide|explanation|summary|overview|essay|email|e-mail|article|story|description|blog post|"
    r"letter|poem|walkthrough|comparison|answer|list of ideas|plan in words)\b", re.I)
INTO_FILE = re.compile(r"\b(in|into|to|as)\s+(a |the |new |our )?([\w./-]+\.(md|txt|rst)|file|readme|docs?)\b", re.I)
FILE_REF = re.compile(r"[\w./\\-]+\.(py|js|ts|tsx|jsx|lua|luau|json|md|toml|yaml|yml|css|html|cs|go|rs|java|cpp|c|h)\b", re.I)

# Category rules: (category, pattern, evidence points toward Claude, reason shown to the user).
# Several can match; the strongest one names the category, all of them add evidence.
CATEGORIES: list[tuple[str, re.Pattern, int, str]] = [
    ("architecture", re.compile(r"\b(architect\w*|system design|design (the|a|an) \w+ (system|service|architecture)|"
                                r"redesign|restructure the (app|project|codebase)|major feature)", re.I), 4,
     "architecture or major design decisions"),
    ("concurrency", re.compile(r"\b(race condition|deadlock|thread[- ]safe|concurren\w*|data race|mutex|"
                               r"async\w* state|state (gets|goes) out of sync|at the same time|at once|simultaneous\w*)", re.I), 4,
     "a concurrency or state-management problem"),
    ("complex_debugging", re.compile(r"\b(intermittent\w*|flaky|sometimes (fails|crashes|breaks|loses?|drops?|freezes)|only happens|"
                                     r"memory leak|heisenbug|can'?t reproduce|hard to reproduce|randomly (fails|crashes))", re.I), 4,
     "a hard-to-reproduce bug"),
    ("security", re.compile(r"\b(security|vulnerab\w*|exploit\w*|authentication|authori[sz]ation|encrypt\w*|"
                            r"injection|xss|csrf|anti-cheat|sanitiz\w*|permissions? model|log[- ]?in|sign[- ]?in|passwords?|session tokens?)", re.I), 3,
     "security-sensitive code, where a mistake is costly and hard to spot"),
    ("integration", re.compile(r"\b(dependency (conflict|hell|issue)|version conflict|integrat\w+ (with|between)|"
                               r"migrat\w+|upgrade (to|from) \w+ ?\d|breaking change|compatib\w+ (issue|problem))", re.I), 3,
     "a dependency, migration or integration problem"),
    ("data", re.compile(r"\b(datastore|database schema|schema change|data migration|persist\w* (data|state)|"
                        r"payments?|transactions?|save data)\b", re.I), 2,
     "persistent data, where errors can lose user data"),
    ("game_networking", re.compile(r"\b(remoteevent|remotefunction|replication|client[- ]server|server[- ]authoritative)", re.I), 2,
     "client/server game logic with a trust boundary"),
    ("performance", re.compile(r"\b(performance|too slow|speed up|optimi[sz]\w+|bottleneck|lag\w*)\b", re.I), 2,
     "performance analysis"),
    ("feature", re.compile(r"\b(implement|build|add|create)\s+(a |an |the )?(new )?(\w+ ){0,2}(feature|system|module|service|"
                           r"api|endpoint|screen|page|ui|subsystem|pipeline|integration)\b", re.I), 2,
     "a new feature"),
    ("refactor", re.compile(r"\b(refactor\w*|restructur\w*|reorgani[sz]\w*|split (up|into))\b", re.I), 1,
     "a refactor"),
    ("bug_fix", re.compile(r"\b(fix|bug|error|crash\w*|broken|doesn'?t work|not working|exception|traceback)\b", re.I), 1,
     "a bug fix"),
    ("tests", re.compile(r"\b(unit tests?|write tests?|add tests?|generate tests?|test cases?|tests? for)\b", re.I), 0,
     "tests for clearly specified behaviour"),
    ("luau_script", re.compile(r"\b(luau|roblox|lua script)\b", re.I), 0, "a Roblox/Luau script"),
    ("small_function", re.compile(r"\b(add|write|create|make)\s+(a |an )?(small |simple |helper )?([\w.]+(\([^)]*\))?\s+){0,2}"
                                  r"(function|method|helper|getter|setter)\b", re.I), 0,
     "a small, self-contained function"),
    ("boilerplate", re.compile(r"\b(boilerplate|scaffold\w*|template|stub)\b", re.I), 0, "boilerplate code"),
    ("docs_format", re.compile(r"\b(docstring|comments?|readme|documentation|typo|format\w*|rename|lint)\b", re.I), -1,
     "documentation, formatting or renaming"),
    ("explain", re.compile(r"\b(explain|what does|how does|describe|summari[sz]e|walk me through)\b", re.I), 0,
     "an explanation"),
]
ROUTINE_CATEGORIES = {"tests", "luau_script", "small_function", "boilerplate", "docs_format", "explain", "prose", "question"}
HIGH_RISK = {"security", "concurrency", "data", "complex_debugging", "integration"}

SCOPE_WIDE = re.compile(r"\b((across|several|multiple|many|all( the)?)\s+(files|modules|systems|services|components|"
                        r"scripts|classes|subsystems)|across\b[^.;]{0,60}\b(files|modules|systems|services|components|scripts)|"
                        r"whole (project|app|codebase|game)|entire (project|app|codebase|game)|"
                        r"everywhere|every file|end[- ]to[- ]end)\b", re.I)
INTERACTING = re.compile(r"\b(without breaking|while (also|keeping)|must (also|still)|backward[s]? compatib\w*|"
                         r"at the same time|keep(ing)? .{0,30} (working|compatible)|both .{0,40} and .{0,40} must)\b", re.I)
SMALL = re.compile(r"\b(small|simple|quick|tiny|one[- ]line|single (function|line|file)|just (add|change|rename))\b", re.I)
REPEATED = re.compile(r"\b(still (fails|failing|broken|doesn'?t work|crashes)|tried .{0,40}(didn'?t|did not|doesn'?t) work|"
                      r"keeps? (failing|crashing|happening)|same error again)\b", re.I)

UNCERTAINTY = re.compile(
    r"\b(i'?m not sure|i am not sure|not certain|cannot determine|can'?t determine|"
    r"i don'?t have enough|unable to|i cannot|without more (context|information)|"
    r"it'?s unclear|hard to say)\b",
    re.I,
)

# Evidence needed before Claude is chosen directly, per aggressiveness setting.
THRESHOLD = {"prefer_qwen": 5, "balanced": 4, "prefer_claude": 3}


@dataclass
class Classification:
    kind: str                     # "ask" (answer only) or "edit" (propose file changes)
    complexity: str               # low | medium | high
    score: int                    # Evidence that stronger reasoning would help.
    signals: list[str] = field(default_factory=list)
    category: str = "general"
    risk: str = "low"             # How costly or hard to detect a wrong result is.
    scope: str = "single file"    # Expected reach of the change.
    needs: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def classify(text: str, relevant_file_count: int = 0, kind_override: str | None = None,
             history: dict | None = None) -> Classification:
    """Classify a request. history: {category: {"local_ok": n, "escalated": m}} from past tasks."""
    lowered = text.lower()
    signals: list[str] = []
    prose = bool(PROSE.search(text)) and not INTO_FILE.search(text)
    if kind_override in ("ask", "edit"):
        kind = kind_override
        signals.append(f"task type chosen by you: {kind}")
    elif prose:
        kind = "ask"
    elif QUESTION_START.match(text) and not re.search(r"\b(please|then|and)\s+(add|fix|implement|change|update)", lowered):
        kind = "ask"
    elif EDIT_VERBS.search(text):
        kind = "edit"
    else:
        kind = "ask"

    score = 0
    matched: list[tuple[str, int, str]] = []
    for category, pattern, points, why in CATEGORIES:
        if pattern.search(text):
            matched.append((category, points, why))
    if prose:
        category = "prose"
    elif matched:
        category = max(matched, key=lambda m: m[1])[0]
    else:
        category = "question" if kind == "ask" else "general"
    for cat, points, why in matched:
        if points > 0:
            score += points
            signals.append(why)
    if matched and not any(points > 0 for _, points, _ in matched):
        signals.append(next(why for cat, _, why in matched if cat == category) if category != "prose" else "prose writing")
    elif category == "prose":
        signals.append("prose writing, no file changes")
    if any(points < 0 for _, points, _ in matched) and score <= 1:
        score -= 1

    files_named = {m.group(0).lower() for m in FILE_REF.finditer(text)}
    scope = "single file"
    if category in ("small_function", "docs_format"):
        scope = "single function"
    if SCOPE_WIDE.search(text):
        score += 3
        scope = "multiple modules"
        signals.append("reaches across several files or modules")
    elif len(files_named) >= 3:
        score += 2
        scope = "multiple modules"
        signals.append(f"names {len(files_named)} files")
    elif len(files_named) == 2 or category in ("feature", "refactor") and kind == "edit":
        score += 1 if len(files_named) == 2 else 0
        scope = "a few files"
    if relevant_file_count >= 6:
        score += 1
        signals.append(f"{relevant_file_count} relevant files found")
    if INTERACTING.search(text):
        score += 2
        signals.append("several requirements that must be reconciled")
    requirements = len(re.findall(r"(?m)^\s*([-*•]|\d+[.)])\s+\S", text))
    if requirements >= 4:
        score += 1
        signals.append(f"{requirements} listed requirements")
    if REPEATED.search(text):
        score += 3
        signals.append("earlier attempts did not work")
    if len(text) > 800:
        score += 1
        signals.append("long, detailed request")
    if SMALL.search(text) and score < 4:
        score -= 2
        signals.append("you described it as small")
    if kind == "ask" and category in ("question", "explain", "prose", "general"):
        score = min(score, 1)

    if history and category in history:
        h = history[category]
        total = h.get("local_ok", 0) + h.get("escalated", 0)
        if total >= 5:
            rate = h.get("escalated", 0) / total
            if rate >= 0.6:
                score += 1
                signals.append(f"history: Qwen needed Claude in {h['escalated']} of {total} recent {category} tasks")
            elif rate <= 0.1 and score > 0:
                score -= 1
                signals.append(f"history: Qwen finished {h['local_ok']} of {total} recent {category} tasks alone")

    risk = "high" if category in HIGH_RISK or any(c in HIGH_RISK for c, _, _ in matched) else (
        "medium" if kind == "edit" and category not in ROUTINE_CATEGORIES else "low")
    complexity = "high" if score >= 4 else "medium" if score >= 2 else "low"
    needs = {
        "edits": kind == "edit",
        "tests": kind == "edit" and category not in ("docs_format", "prose"),
        "multi_step": scope == "multiple modules" or category in ("architecture", "feature", "integration"),
    }
    return Classification(kind, complexity, score, signals, category, risk, scope, needs)


@dataclass
class Decision:
    backend: str | None           # LOCAL, CLAUDE, or None when the task cannot run
    workflow: str
    reason: str
    category: str = ""
    complexity: str = ""
    confidence: str = ""          # Rule margin (high/medium/low), not a probability. Empty for manual choices.
    scope: str = ""
    risk: str = ""
    signals: list[str] = field(default_factory=list)
    needs: dict = field(default_factory=dict)
    escalation: list[str] = field(default_factory=list)
    privacy: str = ""
    preferred: str | None = None  # What the router would pick with no restrictions.
    source: str = "router"        # router | manual | policy

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _confidence(score: int, threshold: int) -> str:
    margin = abs(score - (threshold - 0.5))
    return "high" if margin >= 2.5 else "medium" if margin >= 1.5 else "low"


def escalation_conditions(routing: dict, backend: str | None, workflow: str) -> list[str]:
    if backend == CLAUDE:
        return ["Claude handles this task; Enoy does not hand it back to Qwen unless you ask or Claude is unavailable."]
    if workflow in ("local_only", "claude_only") or not routing.get("failure_escalation", True):
        return ["No automatic escalation for this task."]
    return [
        f"Qwen's proposal fails Enoy's checks {routing['max_local_attempts']} time(s) in a row",
        f"checks still fail after {routing['max_test_fix_attempts']} fix attempt(s)",
        "the same error comes back after a fix",
        "the problem turns out to reach more files than expected and an attempt fails",
    ]


def decide(
    classification: Classification,
    routing: dict,
    claude_available: bool,
    override: str | None = None,
    local_only: str = "",
) -> Decision:
    """Choose the first backend for a task.

    local_only: empty, or the reason this task may not use the cloud (project
    setting, Settings). It overrides everything, including a manual choice.
    A manual override otherwise wins. Claude is never chosen when it is
    unavailable; if the user asked for it explicitly, the task does not run
    rather than silently switching to the local model.
    """
    c = classification
    mode = routing["mode"]
    workflow = routing["workflow"]
    base = dict(category=c.category, complexity=c.complexity, scope=c.scope, risk=c.risk,
                signals=list(c.signals), needs=dict(c.needs))
    privacy = f"Local only: {local_only}" if local_only else "Claude may receive the relevant, permitted files (secrets removed)."

    def out(backend, wf, reason, confidence="", preferred=None, source="router"):
        return Decision(backend, wf, reason, confidence=confidence, privacy=privacy,
                        escalation=escalation_conditions(routing, backend, wf),
                        preferred=preferred if preferred is not None else backend, source=source, **base)

    if override in (LOCAL, CLAUDE) and routing.get("manual_override", True):
        if override == CLAUDE and local_only:
            return out(None, workflow, f"You chose Claude, but this task must stay on your PC ({local_only}). "
                                       "Pick Qwen, or change the privacy setting.", preferred=CLAUDE, source="policy")
        if override == CLAUDE and not claude_available:
            return out(None, workflow, "You chose Claude, but Claude is not available. Pick Qwen or fix the Claude connection.",
                       preferred=CLAUDE, source="manual")
        # A manual choice runs as a single-backend task.
        wf = "local_only" if override == LOCAL else "claude_only"
        return out(override, wf, f"You chose {LABEL[override]} for this task.", source="manual")

    threshold = THRESHOLD.get(routing.get("aggressiveness", "balanced"), 4)
    evidence = ", ".join(c.signals[:3]) or "no special signals"
    wants_claude = c.score >= threshold or (c.score >= threshold - 2 and c.risk == "high")
    if c.score >= threshold:
        why_claude = f"{evidence}; stronger reasoning is likely to change the outcome"
    else:
        why_claude = f"{evidence}; a wrong result would be costly or hard to detect"
    confidence = _confidence(c.score, threshold)

    if local_only:
        note = f" Claude would likely help ({why_claude}), but" if wants_claude else ""
        return out(LOCAL, "local_only", f"Routed to Qwen.{note} this task stays on your PC: {local_only}.",
                   confidence, preferred=CLAUDE if wants_claude else LOCAL, source="policy")

    if mode == "manual":
        return out(LOCAL, "local_only", "Manual routing mode with no model chosen; using Qwen.", source="policy")

    if workflow == "local_only":
        return out(LOCAL, workflow, "Workflow is set to Qwen only.", source="policy")

    if mode == "claude_first":
        if claude_available:
            wf = "claude_only" if workflow in ("escalate", "local_then_review") else workflow
            return out(CLAUDE, wf, "Claude-first routing mode.", source="policy")
        return out(LOCAL, "local_only", "Claude-first mode, but Claude is not available, so this task runs on Qwen.",
                   preferred=CLAUDE, source="policy")

    if workflow == "claude_plans":
        if claude_available and c.kind == "edit":
            return out(CLAUDE, workflow, "Workflow: Claude writes the plan, Qwen implements it.", source="policy")
        return out(LOCAL, "escalate" if claude_available else "local_only",
                   "Question tasks skip the planning step." if claude_available else "Claude is not available.")
    if workflow == "compare":
        if claude_available and c.kind == "edit":
            return out(LOCAL, workflow, "Workflow: Qwen and Claude both propose a change; you pick one.", source="policy")
        return out(LOCAL, "local_only", "Compare needs an edit task and an available Claude; running on Qwen.")

    smart = mode == "auto" and routing.get("escalate_high_complexity", True)
    legacy_high = mode == "local_first" and routing.get("escalate_high_complexity", True) and c.complexity == "high"
    if (smart and wants_claude) or legacy_high:
        if claude_available:
            return out(CLAUDE, "claude_only", f"Routed to Claude: {why_claude}.", confidence)
        if routing.get("claude_fallback", "local") == "stop":
            return out(None, workflow, f"Claude would be the better choice ({why_claude}), but Claude is not available, "
                                       "and Settings say to stop rather than use Qwen.", confidence, preferred=CLAUDE, source="policy")
        return out(LOCAL, "local_only", f"Routed to Qwen. Claude would be the better choice ({why_claude}), "
                                        "but Claude is not available.", confidence, preferred=CLAUDE, source="policy")

    if c.score >= threshold - 2 and mode == "auto":
        why = (f"Routed to Qwen: borderline ({evidence}), but the change is easy to verify and a mistake is cheap to fix. "
               "Claude takes over if the attempt fails.")
    elif c.kind == "ask":
        why = f"Routed to Qwen: {c.signals[0] if c.signals else 'a question'} that can be answered locally."
    else:
        why = f"Routed to Qwen: {c.signals[0] if c.signals else 'a clearly scoped change'}, {c.scope}, low risk."
    note = "" if claude_available else " Claude is not available, so escalation is off."
    return out(LOCAL, workflow if claude_available else "local_only", why + note, confidence)


@dataclass
class AttemptSignals:
    """Objective facts about the attempts so far."""
    local_attempts: int = 0
    invalid_outputs: int = 0        # Missing blocks, failed validation, syntax errors.
    backend_errors: int = 0         # Timeouts, malformed responses.
    test_failures: int = 0          # Failed checks after applying a local change.
    uncertain: bool = False         # The model said it was unsure.
    escalations: int = 0
    user_requested: bool = False
    repeated_error: bool = False    # The same failing line came back after a fix.
    scope_grew: bool = False        # Attempts touched more files than the router expected.
    handoffs: int = 0               # Times the task changed model.


def should_escalate(signals: AttemptSignals, routing: dict, workflow: str, claude_available: bool) -> tuple[bool, str]:
    """Return (escalate?, reason). Self-reported doubt alone never escalates."""
    if workflow in ("local_only", "claude_only"):
        return False, "This task's workflow does not escalate."
    if not claude_available:
        return False, "Claude is not available."
    if signals.escalations >= routing["max_escalations"]:
        return False, f"Escalation limit reached ({routing['max_escalations']})."
    if signals.handoffs >= routing.get("max_handoffs", 3):
        return False, f"Model switch limit reached ({routing.get('max_handoffs', 3)}); stopping instead of switching again."
    if signals.user_requested:
        return True, "You asked for Claude."
    if not routing.get("failure_escalation", True):
        return False, "Escalation after failures is turned off in Settings."
    max_local = routing["max_local_attempts"]
    if signals.invalid_outputs >= max_local:
        return True, f"Qwen produced {signals.invalid_outputs} invalid result(s) in a row."
    if signals.backend_errors >= max_local:
        return True, f"Qwen failed {signals.backend_errors} time(s) (timeouts or bad responses)."
    if signals.repeated_error and signals.test_failures >= 1:
        return True, "The same error came back after a fix attempt."
    if signals.test_failures >= routing["max_test_fix_attempts"] and signals.test_failures > 0:
        return True, f"Checks still fail after {signals.test_failures} local fix attempt(s)."
    if signals.scope_grew and (signals.invalid_outputs + signals.test_failures) >= 1:
        return True, "The problem reaches more files than expected and a local attempt failed."
    if signals.uncertain and (signals.invalid_outputs + signals.test_failures + signals.backend_errors) >= 1:
        return True, "Qwen said it was unsure and an objective check also failed."
    return False, "No escalation condition met."


def sounds_uncertain(text: str) -> bool:
    return bool(UNCERTAINTY.search(text))
