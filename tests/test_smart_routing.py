"""Smart router: classification, routing policy, privacy, handoff and statistics.

All providers are fakes; nothing reaches Ollama or Claude.
"""

import sys

import pytest

from enoy import config as config_mod
from enoy import privacy
from enoy.backends.base import CLAUDE, LOCAL, BackendError
from enoy.orchestrator import TaskError
from enoy.routing import AttemptSignals, classify, decide, should_escalate

from test_orchestrator import ADD_MULTIPLY, open_project

PRIVATE_LINE = 'assert(amount > 0, "amount must be positive")'
FAKE_KEY = "sk-ant-api03-FAKEFAKEFAKEFAKE1234567890abcdef"


def routing(**kw):
    r = dict(config_mod.DEFAULTS["routing"])
    r.update(kw)
    return r


def route(text, files=2, **kw):
    return decide(classify(text, files), routing(**kw), claude_available=True)


def kinds(orc, task_id):
    return [e["kind"] for e in reversed(orc.db.activity(task_id))]


# 1-4: classification and direct routing ---------------------------------------------------

def test_simple_explanation_routes_to_qwen():
    d = route("Explain what divide() does")
    assert d.backend == LOCAL and d.category == "explain" and d.confidence == "high"
    assert d.reason.startswith("Routed to Qwen")


def test_prose_request_is_not_a_file_change():
    c = classify("Write a detailed 600-word tutorial explaining every function in calculator.py")
    assert c.kind == "ask" and c.category == "prose"
    assert classify("Write a short guide into docs/guide.md").kind == "edit"


def test_small_isolated_coding_tasks_route_to_qwen():
    for text in ("Add a multiply(a, b) function to calculator.py", "Write a Luau function that shuffles a table",
                 "Add unit tests for the subtract function", "Fix the off-by-one error in Inventory.count"):
        d = route(text)
        assert d.backend == LOCAL, (text, d.reason)
        assert d.risk in ("low", "medium")


def test_complex_multi_file_feature_routes_directly_to_claude():
    d = route("Implement a trading system across the inventory, datastore and UI modules")
    assert d.backend == CLAUDE and d.workflow == "claude_only" and d.scope == "multiple modules"
    assert "Routed to Claude" in d.reason


def test_cross_module_bug_routes_directly_to_claude():
    d = route("Players sometimes lose items when two trades happen at once; the bug spans Inventory.luau, "
              "Trade.luau and DataStore.luau")
    assert d.backend == CLAUDE and d.risk == "high"
    assert any("concurrency" in s for s in d.signals)


def test_short_sentence_can_still_be_complex():
    assert route("Fix the race condition in saving").backend == CLAUDE
    assert route("The login still fails after the last fix, same error again").backend == CLAUDE


def test_aggressiveness_shifts_borderline_cases_only():
    text = "Add a settings page with dark mode"
    assert route(text).backend == LOCAL
    assert route(text, aggressiveness="prefer_claude").backend == LOCAL  # Still not enough evidence.
    assert route("Design the architecture for a plugin system", aggressiveness="prefer_qwen").backend == LOCAL
    assert route("Design the architecture for a plugin system").backend == CLAUDE


def test_decision_record_is_complete():
    d = route("Implement a trading system across the inventory, datastore and UI modules")
    record = d.as_dict()
    for key in ("backend", "category", "complexity", "confidence", "reason", "scope", "risk", "needs",
                "escalation", "privacy", "signals"):
        assert key in record
    assert record["needs"]["multi_step"] and record["escalation"]


def test_history_tilts_only_borderline_cases():
    hist = {"feature": {"local_ok": 1, "escalated": 5}}
    c = classify("Add a settings page with dark mode", 2, history=hist)
    assert any(s.startswith("history:") for s in c.signals)
    assert decide(c, routing(), True).backend == LOCAL  # History alone does not flip a clear local task.
    small = classify("Explain divide()", 2, history={"explain": {"local_ok": 0, "escalated": 9}})
    assert decide(small, routing(), True).backend == LOCAL


# 5-6: outcome-aware escalation -------------------------------------------------------------

def test_repeated_failure_escalates_with_handoff_package(orc_factory, project):
    orc = orc_factory(qwen=["no blocks", "still no blocks"], claude=[ADD_MULTIPLY])
    task = orc.start_task(open_project(orc, project), "Add a multiply function to calculator.py")
    task = orc.db.get_task(task["id"])
    assert task["state"]["initial_backend"] == LOCAL and task["owner"] == CLAUDE
    prompt = orc.backends[CLAUDE].requests[0].messages[-1].content
    for part in ("Handoff from Qwen", "Original request:", "Why you are receiving it:", "Files inspected:",
                 "Errors so far:", "Qwen's last answer", "Remaining:"):
        assert part in prompt
    k = kinds(orc, task["id"])
    assert k.index("task.escalated") < k.index("task.handoff") < k.index("privacy.checked") < k.index("backend.request", k.index("task.handoff"))
    assert task["state"]["handoffs"] == 1 and task["state"]["handoff_log"][0]["to"] == CLAUDE


def test_passing_checks_do_not_escalate(orc_factory, project):
    orc = orc_factory(qwen=[ADD_MULTIPLY])
    pid = open_project(orc, project)
    task = orc.start_task(pid, "Add a multiply function to calculator.py")
    orc.approve_changeset(orc.db.list_changesets(task["id"], "pending")[0]["id"])
    orc.run_command(pid, f'"{sys.executable}" -m pytest -q', approved=True, task_id=task["id"])
    t = orc.db.get_task(task["id"])
    assert t["state"]["verification"]["status"] == "passed"
    assert t["owner"] == LOCAL and orc.backends[CLAUDE].requests == []
    assert "task.escalated" not in kinds(orc, task["id"])


def test_same_error_after_fix_escalates(orc_factory, project):
    from test_orchestrator import BREAK_SUBTRACT, HARMLESS_COMMENT

    orc = orc_factory(qwen=[BREAK_SUBTRACT, HARMLESS_COMMENT], claude=[ADD_MULTIPLY], routing__max_test_fix_attempts=3)
    pid = open_project(orc, project)
    task = orc.start_task(pid, "Change subtract in calculator.py")
    orc.approve_changeset(orc.db.list_changesets(task["id"], "pending")[0]["id"])
    cmd = f'"{sys.executable}" -m pytest -q'
    run = orc.run_command(pid, cmd, approved=True, task_id=task["id"])
    orc.fix_failure(task["id"], run["run_id"])  # Qwen "fixes" it with an unrelated change.
    orc.approve_changeset(orc.db.list_changesets(task["id"], "pending")[0]["id"])
    run = orc.run_command(pid, cmd, approved=True, task_id=task["id"])
    assert orc.db.get_task(task["id"])["state"]["verification"]["status"] == "failed"
    orc.fix_failure(task["id"], run["run_id"])
    t = orc.db.get_task(task["id"])
    assert t["state"]["repeated_error"] is True
    assert t["owner"] == CLAUDE and "same error" in t["escalation_reason"]
    assert "Finished, but the check" not in t["summary"] or t["status"] != "completed"


def test_failed_check_is_never_reported_as_plain_success(orc_factory, project):
    from test_orchestrator import BREAK_SUBTRACT

    orc = orc_factory(qwen=[BREAK_SUBTRACT])
    pid = open_project(orc, project)
    task = orc.start_task(pid, "Change subtract in calculator.py")
    orc.approve_changeset(orc.db.list_changesets(task["id"], "pending")[0]["id"])
    orc.run_command(pid, f'"{sys.executable}" -m pytest -q', approved=True, task_id=task["id"])
    t = orc.db.get_task(task["id"])
    assert t["state"]["verification"]["status"] == "failed"
    assert t["summary"].startswith("Finished, but the check")


# 7-8: manual override -------------------------------------------------------------------------

def test_manual_qwen_overrides_automatic_routing(orc_factory, project):
    orc = orc_factory(qwen=["An answer."], claude=["never"])
    task = orc.start_task(open_project(orc, project), "Design the architecture for secure authentication", backend=LOCAL)
    t = orc.db.get_task(task["id"])
    assert t["owner"] == LOCAL and t["workflow"] == "local_only" and t["state"]["route"]["source"] == "manual"
    assert orc.backends[CLAUDE].requests == []


def test_manual_claude_overrides_automatic_routing(orc_factory, project):
    orc = orc_factory(qwen=["never"], claude=["divide divides."])
    task = orc.start_task(open_project(orc, project), "Explain what divide does", backend=CLAUDE)
    t = orc.db.get_task(task["id"])
    assert t["owner"] == CLAUDE and t["status"] == "completed" and orc.backends[LOCAL].requests == []


def test_manual_override_can_be_disabled():
    d = decide(classify("Explain divide()"), routing(manual_override=False), True, override=CLAUDE)
    assert d.backend == LOCAL and d.source == "router"


# 9-11: privacy ------------------------------------------------------------------------------------

def test_local_only_project_never_sends_code_to_claude(orc_factory, project):
    orc = orc_factory(qwen=["bad", "bad", "An answer."], claude=[ADD_MULTIPLY])
    pid = open_project(orc, project)
    orc.set_project_local_only(pid, True)
    task = orc.start_task(pid, "Implement a trading system across the inventory, datastore and UI modules")
    t = orc.db.get_task(task["id"])
    assert t["owner"] == LOCAL and "local-only" in t["routing_reason"]
    assert t["state"]["route"]["preferred"] == CLAUDE  # The router says why Claude would have helped.
    assert orc.backends[CLAUDE].requests == []
    assert any(e["kind"] == "task.not_escalated" for e in orc.db.activity(task["id"]))
    with pytest.raises(TaskError):
        orc.ask_claude(task["id"])
    t2 = orc.start_task(pid, "Explain divide", backend=CLAUDE)  # Even a manual choice is refused.
    assert orc.db.get_task(t2["id"])["status"] == "failed" and orc.backends[CLAUDE].requests == []


def test_per_chat_local_only_and_global_never(orc_factory, project):
    orc = orc_factory(qwen=["ok", "ok"], claude=["never"])
    pid = open_project(orc, project)
    task = orc.start_task(pid, "Design the architecture for a plugin system", local_only=True)
    assert orc.db.get_task(task["id"])["owner"] == LOCAL
    orc.cfg["privacy"]["cloud_policy"] = "never"
    task = orc.start_task(pid, "Design the architecture for a plugin system")
    assert orc.db.get_task(task["id"])["owner"] == LOCAL and orc.backends[CLAUDE].requests == []


def test_secrets_are_redacted_before_the_request_is_built(orc_factory, project):
    calc = project / "calculator.py"
    calc.write_text(calc.read_text(encoding="utf-8") + f'\nAPI_KEY = "{FAKE_KEY}"\n', encoding="utf-8")
    orc = orc_factory(claude=["Here is the design."])
    task = orc.start_task(open_project(orc, project), "Design the architecture for calculator.py and its API_KEY handling")
    req = orc.backends[CLAUDE].requests[0]
    sent = "\n".join(m.content for m in req.messages)
    assert FAKE_KEY not in sent and "[REDACTED]" in sent
    assert FAKE_KEY not in orc.db.transmissions(task["id"], CLAUDE)[0]["content"]
    check = next(e for e in orc.db.activity(task["id"]) if e["kind"] == "privacy.checked")
    assert "replaced with [REDACTED]" in check["text"]


def test_excluded_files_stay_out_of_cloud_context_and_handoff(orc_factory, project):
    quoting = f"I looked at the module:\n{PRIVATE_LINE}\nbut I could not finish."
    orc = orc_factory(qwen=[quoting, quoting], claude=[ADD_MULTIPLY],
                      privacy__cloud_excluded_globs=["src/shared/**"])
    task = orc.start_task(open_project(orc, project), "Add a removeItem method to the Inventory module in src/shared/Inventory.luau")
    qwen_saw = orc.backends[LOCAL].requests[0].messages[-1].content
    assert PRIVATE_LINE in qwen_saw  # Qwen may read it locally.
    assert orc.backends[CLAUDE].requests, "expected escalation"
    sent = "\n".join(m.content for m in orc.backends[CLAUDE].requests[0].messages)
    assert PRIVATE_LINE not in sent  # Not as a file, and not inside Qwen's quoted answer.
    assert "local Inventory = {}" not in sent
    pending_files = orc.db.transmissions(task["id"], CLAUDE)[0]["files"]
    assert all(not f["path"].startswith("src/shared/") for f in pending_files)
    check = next(e for e in orc.db.activity(task["id"]) if e["kind"] == "privacy.checked")
    assert "private file(s) left out" in check["text"] and "quoting private files removed" in check["text"]


def test_cloud_excluded_patterns():
    globs = ["src/shared/**", "*.secret.json", "private"]
    assert privacy.cloud_excluded("src/shared/Inventory.luau", globs)
    assert privacy.cloud_excluded("config/app.secret.json", globs)
    assert privacy.cloud_excluded("private/notes.md", globs)
    assert not privacy.cloud_excluded("src/client/Main.luau", globs)
    assert privacy.cloud_excluded("./.secrets/key.txt", [".secrets/**"])  # Leading dots are kept.


# 12-13: outages and limits ------------------------------------------------------------------------

def test_provider_outage_falls_back_honestly(orc_factory, project):
    orc = orc_factory(qwen=["An answer."], claude_available=False)
    task = orc.start_task(open_project(orc, project), "Design the architecture for a plugin system")
    t = orc.db.get_task(task["id"])
    assert t["owner"] == LOCAL and "Claude is not available" in t["routing_reason"]
    assert t["state"]["route"]["preferred"] == CLAUDE


def test_provider_outage_with_stop_policy_does_not_claim_success(orc_factory, project):
    orc = orc_factory(qwen=["never"], claude_available=False, routing__claude_fallback="stop")
    task = orc.start_task(open_project(orc, project), "Design the architecture for a plugin system")
    t = orc.db.get_task(task["id"])
    assert t["status"] == "failed" and "not available" in t["summary"]
    assert orc.backends[LOCAL].requests == []


def test_claude_failure_mid_task_is_not_success(orc_factory, project):
    orc = orc_factory(qwen=[], claude=[BackendError("unavailable", "Claude Code stopped responding")],
                      routing__claude_fallback="stop")
    task = orc.start_task(open_project(orc, project), "Implement a trading system across the inventory, datastore and UI modules")
    t = orc.db.get_task(task["id"])
    assert t["status"] == "failed" and "Claude failed" in t["summary"]
    assert any(s["agent"] == CLAUDE and s["status"] == "failed" for s in orc.db.steps(task["id"]))


def test_claude_failure_falls_back_to_qwen_once(orc_factory, project):
    orc = orc_factory(qwen=["An analysis from Qwen."], claude=[BackendError("limit", "Claude usage limit reached")])
    task = orc.start_task(open_project(orc, project), "Design the architecture for a plugin system")
    t = orc.db.get_task(task["id"])
    assert t["status"] == "completed" and t["owner"] == LOCAL and t["workflow"] == "local_only"
    fallback = next(e for e in orc.db.activity(task["id"]) if e["kind"] == "task.fallback")
    assert "Claude failed" in fallback["reason"] and "usage limit" in fallback["reason"]
    assert len(orc.backends[CLAUDE].requests) == 1  # Not retried, not bounced back.


def test_declining_a_direct_claude_task_hands_it_to_qwen(orc_factory, project):
    orc = orc_factory(qwen=[ADD_MULTIPLY], claude=["never"], claude__confirm_before_send=True)
    task = orc.start_task(open_project(orc, project), "Implement a trading system across the inventory, datastore and UI modules")
    assert orc.db.get_task(task["id"])["status"] == "awaiting_claude"
    orc.decline_claude(task["id"])
    t = orc.db.get_task(task["id"])
    assert t["owner"] == LOCAL and t["status"] == "awaiting_approval" and orc.backends[CLAUDE].requests == []
    assert "keep this task with Qwen" in t["state"]["assign_reason"]


def test_model_switch_limit_stops_alternation():
    sig = AttemptSignals(invalid_outputs=5, handoffs=2)
    ok, reason = should_escalate(sig, routing(max_escalations=3), "escalate", True)
    assert not ok and "switch limit" in reason
    ok, reason = should_escalate(AttemptSignals(invalid_outputs=5), routing(failure_escalation=False), "escalate", True)
    assert not ok and "turned off" in reason
    ok, _ = should_escalate(AttemptSignals(invalid_outputs=5, escalations=1), routing(), "escalate", True)
    assert not ok  # Default: one escalation per task.


# 14-17: monitor, statistics, persistence, concurrency --------------------------------------------

def test_activity_shows_the_routing_sequence(orc_factory, project):
    orc = orc_factory(claude=[ADD_MULTIPLY])
    task = orc.start_task(open_project(orc, project), "Implement a trading system across the inventory, datastore and UI modules")
    k = kinds(orc, task["id"])
    order = ["task.created", "step.started", "task.classified", "task.assigned", "privacy.checked", "backend.request"]
    positions = [k.index(x) for x in order]
    assert positions == sorted(positions)
    classified = next(e for e in orc.db.activity(task["id"]) if e["kind"] == "task.classified")
    assert "complexity" in classified["text"] and classified["reason"]
    assert orc.backends[LOCAL].requests == []


def test_distribution_stats_use_real_records(orc_factory, project):
    orc = orc_factory(qwen=["a", "b"], claude=["c"])
    pid = open_project(orc, project)
    orc.start_task(pid, "Explain divide")
    orc.start_task(pid, "Explain subtract")
    orc.start_task(pid, "Design the architecture for a plugin system")
    running = orc.db.create_task(pid, "running", "running", "ask")
    orc.db.update_task(running["id"], status="running")
    cancelled = orc.db.create_task(pid, "cancelled", "cancelled", "ask")
    orc.db.update_task(cancelled["id"], status="cancelled")
    stats = orc.routing_stats()
    assert stats["counted"] == 3 and stats["qwen"] == 2 and stats["claude"] == 1 and stats["direct"] == 1
    assert stats["qwen_pct"] == 67 and stats["target_qwen_pct"] == 70
    assert "Too few" in stats["note"]  # Fewer than 5 finished tasks.
    assert orc.backends[CLAUDE].requests.__len__() == 1  # Statistics never call a model.


def test_routing_preferences_persist(app_home):
    cfg, _ = config_mod.validate({})
    cfg = config_mod.merge_update(cfg, {"routing": {"aggressiveness": "prefer_qwen", "target_local_share": 80},
                                        "privacy": {"cloud_excluded_globs": ["secret/**"]}})
    config_mod.save(cfg)
    loaded, warnings = config_mod.load()  # A fresh start reads the file again.
    assert not warnings
    assert loaded["routing"]["aggressiveness"] == "prefer_qwen" and loaded["routing"]["target_local_share"] == 80
    assert loaded["privacy"]["cloud_excluded_globs"] == ["secret/**"]
    with pytest.raises(config_mod.ConfigError):
        config_mod.validate_strict(config_mod.merge_update(cfg, {"routing": {"aggressiveness": "yolo"}}))


def test_project_local_only_persists(cfg, project, tmp_path):
    from conftest import FakeBackend
    from enoy.orchestrator import Orchestrator
    from enoy.storage import Storage

    db = tmp_path / "f.db"
    orc = Orchestrator(Storage(db), cfg, {LOCAL: FakeBackend(LOCAL), CLAUDE: FakeBackend(CLAUDE)}, synchronous=True)
    pid = orc.open_project(str(project))["id"]
    orc.set_project_local_only(pid, True)
    orc.db.close()
    assert Storage(db).get_project(pid)["local_only"] == 1


def test_concurrent_proposals_cannot_overwrite_each_other(orc_factory, project):
    other = ADD_MULTIPLY.replace("multiply", "times")
    orc = orc_factory(qwen=[ADD_MULTIPLY, other])
    pid = open_project(orc, project)
    a = orc.start_task(pid, "Add a multiply function to calculator.py")
    b = orc.start_task(pid, "Add a times function to calculator.py")
    cs_a = orc.db.list_changesets(a["id"], "pending")[0]["id"]
    cs_b = orc.db.list_changesets(b["id"], "pending")[0]["id"]
    orc.approve_changeset(cs_a)
    with pytest.raises(TaskError):
        orc.approve_changeset(cs_b)  # Proposed against the old file; refused instead of overwriting.
    text = (project / "calculator.py").read_text(encoding="utf-8")
    assert "def multiply" in text and "def times" not in text
    assert orc.db.get_task(a["id"])["state"]["route"]["task_id"] == a["id"]
    assert orc.db.get_task(b["id"])["state"]["route"]["task_id"] == b["id"]
