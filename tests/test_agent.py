"""Agent mode: Qwen looks, edits, runs and checks with tools in one turn."""

import pytest

from conftest import FakeBackend
from enoy.backends.base import CLAUDE, LOCAL, ChatMessage, ChatResult
from enoy.orchestrator import EventBus, Orchestrator, _fit_window
from enoy.storage import Storage


def call(name, **args):
    return ChatResult(text="", backend=LOCAL, model="fake", tool_calls=[{"name": name, "arguments": args}])




def agent_orc(cfg, tmp_path, replies, **tools):
    cfg["tools"]["agent"] = True
    cfg["tools"].update(tools)
    orc = Orchestrator(Storage(":memory:"), cfg, {LOCAL: FakeBackend(LOCAL, replies), CLAUDE: FakeBackend(CLAUDE)},
                       EventBus(), backups_dir=tmp_path / "bk", synchronous=True)
    ran = []

    def fake_run(project_id, command, approved=False, task_id=None, wait=False):
        ran.append(command)
        rid = orc.db.add_command_run(project_id, command, task_id)
        orc.db.finish_command_run(rid, "passed", 0, "2 passed in 0.01s", "", 0.1)
        return {"needs_approval": False, "run_id": rid}
    orc.run_command = fake_run
    return orc, ran
