"""Agent mode: Qwen looks, edits, runs and checks with tools in one turn."""

import pytest

from conftest import FakeBackend
from enoy.backends.base import CLAUDE, LOCAL, ChatMessage, ChatResult
from enoy.orchestrator import EventBus, Orchestrator, _fit_window
from enoy.storage import Storage


def call(name, **args):
    return ChatResult(text="", backend=LOCAL, model="fake", tool_calls=[{"name": name, "arguments": args}])


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
                                                encoding="utf-8")
    (root / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    return root


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


def test_agent_reads_edits_and_runs_the_tests(cfg, tmp_path, proj):
    replies = [
        call("list_files"),
        call("read_file", path="calc.py"),
        call("edit_file", path="calc.py", old_text="    return a + b\n",
             new_text="    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"),
        call("run_command", command="python -m pytest -q"),
        "Added multiply() to calc.py; the tests pass (2 passed).",
    ]
    orc, ran = agent_orc(cfg, tmp_path, replies)
    pid = orc.open_project(str(proj))["id"]
    task = orc.db.get_task(orc.start_task(pid, "add a multiply function", backend=LOCAL)["id"])
    assert task["status"] == "completed" and task["workflow"] == "agent"
    assert "def multiply(a, b):" in (proj / "calc.py").read_text()
    assert ran == ["python -m pytest -q"]
    reqs = orc.backends[LOCAL].requests
    offered = {t["function"]["name"] for t in reqs[0].tools}
    assert {"read_file", "edit_file", "run_command", "write_file"} <= offered
    assert "Enoy's agent" in reqs[0].messages[0].content and "calc.py" in reqs[0].messages[0].content
    results = [m.content for m in reqs[-1].messages if m.role == "tool"]
    assert "calc.py" in results[0] and ".env" not in results[0]
    assert "   1| def add(a, b):" in results[1]
    cs = orc.db.list_changesets(task["id"])
    assert len(cs) == 1 and cs[0]["status"] == "applied" and cs[0]["files"][0]["path"] == "calc.py"
    orc.revert_changeset(cs[0]["id"])  # Undo restores the whole turn.
    assert "multiply" not in (proj / "calc.py").read_text()
    answer = orc.db.messages(task["id"])[-1]["content"]
    assert answer.startswith("Added multiply()")


def test_staged_mode_proposes_instead_of_writing(cfg, tmp_path, proj):
    replies = [call("write_file", path="calc.py", content="def add(a, b):\n    return b + a\n"), "Swapped the operands."]
    orc, _ = agent_orc(cfg, tmp_path, replies, agent_apply=False)
    pid = orc.open_project(str(proj))["id"]
    task = orc.db.get_task(orc.start_task(pid, "swap the operands in add", backend=LOCAL)["id"])
    assert "return a + b" in (proj / "calc.py").read_text()  # Nothing written yet.
    pending = orc.db.list_changesets(task["id"], "pending")
    assert len(pending) == 1 and task["status"] == "awaiting_approval"
    orc.approve_changeset(pending[0]["id"])
    assert "return b + a" in (proj / "calc.py").read_text()


def test_guardrails_secrets_syntax_and_commands(cfg, tmp_path, proj):
    replies = [
        call("read_file", path=".env"),
        call("read_file", path="../outside.txt"),
        call("edit_file", path="calc.py", old_text="    return a + b\n", new_text="    return (a + b\n"),
        call("run_command", command="pip install requests"),
        call("run_command", command="format C:"),
        "I could not do it.",
    ]
    orc, ran = agent_orc(cfg, tmp_path, replies)
    pid = orc.open_project(str(proj))["id"]
    task = orc.db.get_task(orc.start_task(pid, "do risky things", backend=LOCAL)["id"])
    sent = [m.content for m in orc.backends[LOCAL].requests[-1].messages if m.role == "tool"]
    assert "secrets file" in sent[0]
    assert "API_KEY" not in " ".join(sent)
    assert '"error_kind":"denied"' in sent[1]
    assert '"error_kind":"invalid"' in sent[2] and "return a + b" in (proj / "calc.py").read_text()
    assert '"error_kind":"needs_approval"' in sent[3] and ran == []
    assert '"error_kind":"denied"' in sent[4] or '"error_kind":"needs_approval"' in sent[4]
    assert task["state"]["suggested_commands"][0] == "pip install requests"
    assert orc.db.list_changesets(task["id"]) == []


def test_general_chat_agent_has_no_file_tools(cfg, tmp_path):
    from enoy import paths
    orc, _ = agent_orc(cfg, tmp_path, ["Hello!"])
    pid = orc.open_project(str(paths.general_chat_dir()))["id"]
    orc.start_task(pid, "hey", backend=LOCAL)
    req = orc.backends[LOCAL].requests[0]
    offered = {t["function"]["name"] for t in (req.tools or [])}
    assert not offered & {"read_file", "edit_file", "write_file", "run_command"}


def test_tool_conversation_is_kept_inside_the_window():
    msgs = [ChatMessage("system", "S" * 100), ChatMessage("user", "do it")]
    for i in range(6):
        msgs.append(ChatMessage("assistant", "", tool_calls=[{"name": "read_file", "arguments": {}}]))
        msgs.append(ChatMessage("tool", "X" * 3000, tool_name="read_file"))
    _fit_window(msgs, 6000)
    assert sum(len(m.content) for m in msgs) <= 6000 + 3000
    assert msgs[0].content == "S" * 100 and msgs[-1].content == "X" * 3000  # Instructions and newest result kept.


def test_announcing_a_next_step_is_not_an_ending():
    from enoy.orchestrator import _announces_more
    assert _announces_more("Changed todo.py. Now let me write comprehensive tests:")
    assert _announces_more("I read app.js. Let me search for where the dots are drawn.")
    for done in ("Added multiply; 4 tests pass.", "Done. Let me know if you want more.",
                 "If you want, I'll add tests too.", ""):
        assert not _announces_more(done), done


def test_agent_is_nudged_once_when_it_stops_mid_task(cfg, tmp_path, proj):
    replies = [call("read_file", path="calc.py"), "Now let me add the function:",
               call("edit_file", path="calc.py", old_text="    return a + b\n",
                    new_text="    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"),
               "Added multiply()."]
    orc, _ = agent_orc(cfg, tmp_path, replies)
    pid = orc.open_project(str(proj))["id"]
    task = orc.db.get_task(orc.start_task(pid, "add a multiply function", backend=LOCAL)["id"])
    assert "def multiply" in (proj / "calc.py").read_text()
    assert orc.db.messages(task["id"])[-1]["content"] == "Added multiply()."


def test_search_falls_back_to_lines_with_most_words(cfg, tmp_path, proj):
    (proj / "ui.js").write_text('const a = up ? "Ollama ready" : "Ollama offline";\nconst b = "Qwen ready";\n'
                                'const c = "unrelated ready";\n', encoding="utf-8")
    replies = [call("search_code", query="ollama ready qwen ready claude ready"), "Found them."]
    orc, _ = agent_orc(cfg, tmp_path, replies)
    pid = orc.open_project(str(proj))["id"]
    orc.start_task(pid, "where are the status dots", backend=LOCAL)
    result = [m.content for m in orc.backends[LOCAL].requests[-1].messages if m.role == "tool"][0]
    assert "Ollama ready" in result and "Qwen ready" in result and "unrelated" not in result
    assert "most of its words" in result
