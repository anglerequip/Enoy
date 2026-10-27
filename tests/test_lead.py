"""Claude as the lead: it reads each message first, answers or plans, Qwen and the team execute, Claude checks.

Fake Qwen and fake Claude only: no model or cloud calls.
"""

import json
import subprocess
import sys

import pytest

from enoy import activity
from enoy import context as ctx
from enoy.backends.base import CLAUDE, LOCAL, BackendError
from enoy.project import Workspace

from test_orchestrator import ADD_MULTIPLY, BREAK_SUBTRACT, FIX_SUBTRACT, open_project

PYTEST = f'"{sys.executable}" -m pytest -q'




def lead_orc(orc_factory, qwen=None, claude=None, **kw):
    kw.setdefault("lead__enabled", True)
    kw.setdefault("lead__style", "balanced")  # The Claude-first tests below set claude_first themselves.
    kw.setdefault("permissions__auto_apply_patches", True)
    return orc_factory(qwen=qwen, claude=claude, **kw)


def delegate(*tasks, message="The team will add it.", acceptance=("multiply(a, b) returns a * b", "All tests pass")):
    plan = {"acceptance": list(acceptance), "architecture": "Pure functions in calculator.py.", "tasks": list(tasks)}
    return f"MODE: delegate\n{message}\n```json\n{json.dumps(plan)}\n```"


def plan_task(key="T1", title="Add multiply to calculator.py", files=("calculator.py",), deps=(), model="qwen", **extra):
    return {"id": key, "title": title, "description": title, "files": list(files), "depends_on": list(deps),
            "model": model, "why": "Small, clear function.", "expected_output": "An EDIT block for calculator.py",
            "constraints": ["Keep the existing functions unchanged"], **extra}


def answers(orc, task_id):
    return [(m["backend"], m["content"]) for m in orc.db.messages(task_id) if m["role"] == "assistant"]


def kinds(orc, task_id):
    return [a["kind"] for a in orc.db.activity(task_id, 500)]


# Entry point ------------------------------------------------------------------------











# Plan and delegate ------------------------------------------------------------------















# Claude unavailable, cost and your control ------------------------------------------------





















# Memory, git and the Activity panel ----------------------------------------------------------



def git(project, *args):
    subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True)








# The chat Claude sees, empty folders and "no changes" replies ---------------------------------









# Qwen's team tasks in agent mode -------------------------------------------------------------

def _tool(name, **args):
    from enoy.backends.base import ChatResult
    return ChatResult(text="", backend=LOCAL, model="fake", tool_calls=[{"name": name, "arguments": args}])
