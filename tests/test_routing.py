from enoy import config as config_mod
from enoy.backends.base import CLAUDE, LOCAL
from enoy.routing import AttemptSignals, classify, decide, should_escalate, sounds_uncertain


def routing(**kw):
    r = dict(config_mod.DEFAULTS["routing"])
    r.update(kw)
    return r


def test_classify_questions_and_edits():
    assert classify("Explain how divide() works").kind == "ask"
    assert classify("What does Inventory.count return?").kind == "ask"
    assert classify("Add a removeItem function to the inventory").kind == "edit"
    assert classify("Fix the failing test in test_calculator").kind == "edit"
    assert classify("Explain this", kind_override="edit").kind == "edit"


def test_classify_complexity():
    assert classify("Rename the variable x to total").complexity == "low"
    high = classify("Redesign the architecture of the RemoteEvent handling to stop exploiters and fix the race condition")
    assert high.complexity == "high"
    assert "client/server game logic with a trust boundary" in high.signals and high.risk == "high"


def test_local_first_routes_routine_work_to_qwen():
    d = decide(classify("Add a docstring to add()"), routing(), claude_available=True)
    assert d.backend == LOCAL and d.workflow == "escalate"


def test_local_first_sends_high_complexity_to_claude():
    cls = classify("Design the architecture for a secure trading system with DataStore persistence and anti-cheat")
    assert cls.complexity == "high"
    d = decide(cls, routing(), claude_available=True)
    assert d.backend == CLAUDE
    # ...but not when that rule is off, or Claude is unavailable.
    assert decide(cls, routing(escalate_high_complexity=False), True).backend == LOCAL
    assert decide(cls, routing(), claude_available=False).backend == LOCAL


def test_manual_override_always_wins():
    cls = classify("Design the architecture for secure authentication and encryption")
    d = decide(cls, routing(), claude_available=True, override=LOCAL)
    assert d.backend == LOCAL and d.workflow == "local_only"
    d = decide(classify("Rename x"), routing(mode="local_first"), claude_available=True, override=CLAUDE)
    assert d.backend == CLAUDE


def test_override_to_unavailable_claude_does_not_silently_switch():
    d = decide(classify("Rename x"), routing(), claude_available=False, override=CLAUDE)
    assert d.backend is None
    assert "not available" in d.reason


def test_claude_first_and_manual_modes():
    assert decide(classify("Rename x"), routing(mode="claude_first"), True).backend == CLAUDE
    d = decide(classify("Rename x"), routing(mode="claude_first"), False)
    assert d.backend == LOCAL and "not available" in d.reason
    d = decide(classify("Design the architecture"), routing(mode="manual"), True)
    assert d.backend == LOCAL and d.workflow == "local_only"


def test_local_only_workflow_never_uses_claude():
    cls = classify("Design the architecture for secure authentication and encryption")
    assert decide(cls, routing(workflow="local_only"), True).backend == LOCAL
    ok, _ = should_escalate(AttemptSignals(invalid_outputs=9), routing(), "local_only", True)
    assert not ok


def test_escalation_after_repeated_local_failure():
    r = routing(max_local_attempts=2)
    assert not should_escalate(AttemptSignals(invalid_outputs=1), r, "escalate", True)[0]
    ok, reason = should_escalate(AttemptSignals(invalid_outputs=2), r, "escalate", True)
    assert ok and "invalid" in reason
    assert should_escalate(AttemptSignals(backend_errors=2), r, "escalate", True)[0]


def test_escalation_after_test_failures():
    r = routing(max_test_fix_attempts=2)
    assert not should_escalate(AttemptSignals(test_failures=1), r, "escalate", True)[0]
    assert should_escalate(AttemptSignals(test_failures=2), r, "escalate", True)[0]


def test_self_reported_uncertainty_alone_does_not_escalate():
    r = routing()
    assert not should_escalate(AttemptSignals(uncertain=True), r, "escalate", True)[0]
    assert should_escalate(AttemptSignals(uncertain=True, invalid_outputs=1), r, "escalate", True)[0]


def test_escalation_limit_prevents_loops():
    r = routing(max_escalations=1)
    ok, reason = should_escalate(AttemptSignals(invalid_outputs=5, escalations=1), r, "escalate", True)
    assert not ok and "limit" in reason


def test_no_escalation_when_claude_unavailable():
    ok, reason = should_escalate(AttemptSignals(invalid_outputs=5), routing(), "escalate", False)
    assert not ok and "not available" in reason


def test_uncertainty_detection():
    assert sounds_uncertain("I'm not sure which file handles this.")
    assert not sounds_uncertain("The function returns the sum of a and b.")
