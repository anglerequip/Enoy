"""Claude first: every message starts with Claude, which hands most of the writing to Qwen."""

from fusion_ai import context as ctx
from fusion_ai.backends.base import CLAUDE, LOCAL, ChatResult

from test_lead import ADD_MULTIPLY, answers, delegate, kinds, plan_task
from test_orchestrator import open_project

HANDOFF = ("MODE: handoff\nQwen will write that for you.\n```qwen\nWrite a Python function dedupe(items) that "
           "keeps the first of each item and the order. Show only the code and one sentence.\n```")


def first_orc(orc_factory, qwen=None, claude=None, **kw):
    kw.setdefault("lead__enabled", True)
    kw.setdefault("lead__style", "claude_first")
    kw.setdefault("permissions__auto_apply_patches", True)
    return orc_factory(qwen=qwen, claude=claude, **kw)


def test_casual_messages_start_with_claude_too(orc_factory, project):
    orc = first_orc(orc_factory, claude=["MODE: answer\nHi! What are we working on?"])
    task = orc.start_task(open_project(orc, project), "hey, how are you?")
    assert answers(orc, task["id"]) == [(CLAUDE, "Hi! What are we working on?")]
    assert orc.backends[LOCAL].requests == [] and "lead.skipped" not in kinds(orc, task["id"])
    prompt = orc.backends[CLAUDE].requests[0].messages[-1].content
    assert "MODE: handoff" in prompt and ctx.CLAUDE_FIRST_NOTE.format(share=70) in prompt


def test_balanced_style_keeps_the_old_lead(orc_factory, project):
    orc = first_orc(orc_factory, qwen=["Doing well!"], lead__style="balanced")
    orc.start_task(open_project(orc, project), "hey, how are you?")
    assert orc.backends[CLAUDE].requests == []
    orc2 = first_orc(orc_factory, claude=["MODE: answer\nIt is thread-safe."], lead__style="balanced")
    orc2.start_task(open_project(orc2, project), "Is divide thread-safe? Explain the race conditions.")
    prompt = orc2.backends[CLAUDE].requests[0].messages[-1].content
    assert "MODE: handoff" not in prompt and "Claude first" not in prompt


def test_claude_hands_the_reply_to_qwen_and_the_next_message_starts_with_claude(orc_factory):
    code = "def dedupe(items):\n    return list(dict.fromkeys(items))\n\nKeeps the first of each item."
    orc = first_orc(orc_factory, qwen=[code], claude=[HANDOFF, "MODE: answer\nYes, it keeps the order."])
    from fusion_ai import paths
    pid = orc.open_project(str(paths.general_chat_dir()))["id"]
    task = orc.start_task(pid, "write me a function that removes duplicates from a list")
    assert answers(orc, task["id"]) == [(CLAUDE, "Qwen will write that for you."), (LOCAL, code)]
    sent = orc.backends[LOCAL].requests[0].messages[-1].content
    assert "write me a function that removes duplicates" in sent
    assert "Claude, who leads this chat" in sent and "Write a Python function dedupe(items)" in sent
    t = orc.db.get_task(task["id"])
    assert t["status"] == "completed" and t["workflow"] == "claude_leads"
    assert "lead.handoff" in kinds(orc, task["id"])
    work = t["state"]["work"]
    assert work[LOCAL] > 0 and work[CLAUDE] > 0
    orc.send_message(task["id"], "does it keep the order?")
    assert len(orc.backends[CLAUDE].requests) == 2 and len(orc.backends[LOCAL].requests) == 1


def test_handoff_without_a_block_uses_the_reply_as_instructions():
    assert ctx.parse_lead("MODE: handoff\nExplain closures with one example.") == \
        ("handoff", "", "Explain closures with one example.")
    mode, msg, plan = ctx.parse_lead(HANDOFF)
    assert (mode, msg) == ("handoff", "Qwen will write that for you.") and plan.startswith("Write a Python function")


def test_a_clean_team_run_skips_claudes_extra_check(orc_factory, project):
    orc = first_orc(orc_factory, qwen=[ADD_MULTIPLY], claude=[delegate(plan_task())])
    task = orc.start_task(open_project(orc, project), "Add a multiply function")
    t = orc.db.get_task(task["id"])
    assert t["status"] == "completed", t["summary"]
    assert len(orc.backends[CLAUDE].requests) == 1  # The plan only; no review of a run where everything passed.
    assert "lead.review_skipped" in kinds(orc, task["id"])
    assert answers(orc, task["id"])[-1][0] == "team"


def test_writing_share_is_measured_from_reported_tokens(orc_factory, project):
    qwen = ChatResult(text="Long answer from Qwen.", backend=LOCAL, model="fake", usage={"eval_count": 700})
    claude = ChatResult(text=HANDOFF, backend=CLAUDE, model="fake", usage={"output_tokens": 300})
    orc = first_orc(orc_factory, qwen=[qwen], claude=[claude])
    orc.start_task(open_project(orc, project), "Explain how divide handles zero, in detail.")
    work = orc.routing_stats()["work"]
    assert (work["qwen_tokens"], work["claude_tokens"], work["qwen_pct"]) == (700, 300, 70)
    assert work["estimated"] is False
