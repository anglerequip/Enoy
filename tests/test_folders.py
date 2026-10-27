"""Working in the right folder: Enoy knows its own source, can find folders, and moves a chat after your OK."""

import threading
import time
from pathlib import Path

import pytest

from enoy import context as ctx
from enoy.backends.base import CLAUDE, LOCAL, ChatResult
from enoy.tools import FolderService, ToolError
from enoy.tools import folders

from test_agent import agent_orc, call
from test_orchestrator import open_project


@pytest.fixture
def code(tmp_path):
    root = tmp_path / "work" / "my-app"
    (root / "ui").mkdir(parents=True)
    (root / "ui" / "status.js").write_text("export const dots = ['Ollama ready'];\n", encoding="utf-8")
    (root / "package.json").write_text("{}", encoding="utf-8")
    return root


@pytest.fixture
def empty(tmp_path):
    p = tmp_path / "Enoy General"
    p.mkdir()
    return p


def test_enoy_knows_its_own_source():
    src = folders.enoy_source()
    assert src and (Path(src) / "enoy" / "ui" / "app.js").is_file()


def test_scan_finds_folders_by_name_and_skips_system_folders(tmp_path, code):
    (tmp_path / "work" / "node_modules" / "my-app-copy").mkdir(parents=True)
    hits = folders.scan("my-app", [(tmp_path, 3)], time.monotonic() + 3)
    assert code in hits and not any("node_modules" in str(h) for h in hits)
    assert folders.markers(code) == ["package.json"]


def test_find_folders_tool_lists_known_folders_and_enoy(code):
    svc = FolderService("", [("my-app", str(code))], folders.enoy_source())
    from enoy.tools.registry import ToolContext
    out = folders._find_folders({}, ToolContext(cfg={}, cancel=threading.Event(), services={"folders": svc}))
    paths = [f["path"] for f in out["folders"]]
    assert str(code) in paths and folders.enoy_source() in paths
    assert any(f.get("note") == "Enoy's own source code (this app)" for f in out["folders"])
    named = folders._find_folders({"name": "enoy"}, ToolContext(cfg={}, cancel=threading.Event(), services={"folders": svc}))
    assert named["folders"][0]["path"] == folders.enoy_source()


def test_switch_targets_must_be_real_project_folders(tmp_path, code):
    assert folders.check_target(str(code)) == code.resolve()
    for bad in (str(tmp_path / "missing"), "relative/path", str(Path.home()), Path(str(code)).anchor):
        with pytest.raises(ToolError):
            folders.check_target(bad)
    with pytest.raises(ToolError, match="already works"):
        folders.check_target(str(code), current=str(code))


def test_parse_lead_folder_mode():
    mode, msg, path = ctx.parse_lead("MODE: folder C:\\Code\\enoy\nThat code is in Enoy's source.")
    assert (mode, msg, path) == ("folder", "That code is in Enoy's source.", "C:\\Code\\enoy")


def test_claude_moves_the_chat_to_enoys_code_after_your_ok(orc_factory, empty, code):
    orc = orc_factory(claude=[f"MODE: folder {code}\nThe status dots are drawn in that folder's code.",
                              "MODE: answer\nThe dots live in ui/status.js."],
                      lead__enabled=True, lead__style="claude_first")
    pid = open_project(orc, empty)
    task = orc.start_task(pid, "restyle the sidebar indicators")
    prompt = orc.backends[CLAUDE].requests[0].messages[-1].content
    assert "MODE: folder" in prompt and "Enoy's own source code" in prompt and folders.enoy_source() in prompt
    t = orc.db.get_task(task["id"])
    req = t["state"]["pending_folder"]
    assert t["status"] == "completed" and req["path"] == str(code.resolve()) and req["by"] == CLAUDE
    assert len(orc.backends[CLAUDE].requests) == 1  # Nothing moves or runs before you answer.
    moved = orc.answer_folder(task["id"], True)
    assert moved["id"] == task["id"]
    t = orc.db.get_task(task["id"])
    assert orc.db.get_project(t["project_id"])["path"] == str(code.resolve())
    assert t["state"].get("pending_folder") is None and t["status"] == "completed"
    second = orc.backends[CLAUDE].requests[1].messages[-1].content
    assert "restyle the sidebar indicators" in second and "Project: my-app" in second  # Claude now works there.
    assert "folder.moved" in [a["kind"] for a in orc.db.activity(task["id"], 100)]


def test_staying_keeps_the_chat_where_it_is(orc_factory, empty, code):
    orc = orc_factory(claude=[f"MODE: folder {code}\nIt is in my-app."], lead__enabled=True, lead__style="claude_first")
    pid = open_project(orc, empty)
    task = orc.start_task(pid, "fix the sidebar")
    orc.answer_folder(task["id"], False)
    t = orc.db.get_task(task["id"])
    assert t["project_id"] == pid and t["state"].get("pending_folder") is None
    assert len(orc.backends[CLAUDE].requests) == 1


def test_qwen_finds_the_folder_and_asks_to_move(cfg, tmp_path, empty, code):
    find = ChatResult(text="", backend=LOCAL, model="fake",
                      tool_calls=[{"name": "find_folders", "arguments": {"name": "my-app"}}])
    replies = [find,
               call("switch_folder", path=str(code), reason="The status dots are in my-app/ui/status.js."),
               "That code is in my-app; Enoy asks you to move this chat there."]
    orc, _ = agent_orc(cfg, tmp_path, replies)
    orc.db.upsert_project(str(code), "my-app")  # Opened in Enoy before.
    task = orc.start_task(open_project(orc, empty), "restyle the sidebar indicators", backend=LOCAL)
    results = [m.content for m in orc.backends[LOCAL].requests[-1].messages if m.role == "tool"]
    assert '"name":"my-app"' in results[0] and '"markers":["package.json"]' in results[0]
    req = orc.db.get_task(task["id"])["state"]["pending_folder"]
    assert req["path"] == str(code.resolve()) and req["by"] == LOCAL
    system = orc.backends[LOCAL].requests[0].messages[0].content
    assert "Enoy's own source code" in system and "switch_folder" in system


def test_a_chat_that_changed_files_continues_in_a_new_chat(orc_factory, project, code):
    orc = orc_factory()
    pid = open_project(orc, project)
    task = orc.start_task(pid, "hello", backend=LOCAL)
    orc.db.create_changeset(task["id"], LOCAL, [], {"ok": True, "errors": [], "warnings": []}, "earlier change")
    assert orc._request_folder(task["id"], str(code), "It is in my-app.", CLAUDE)
    new = orc.answer_folder(task["id"], True)
    assert new["id"] != task["id"] and orc.db.get_task(task["id"])["project_id"] == pid
    assert orc.db.get_project(new["project_id"])["path"] == str(code.resolve())


def test_empty_folder_note_is_shown_once(orc_factory, empty):
    orc = orc_factory(qwen=["One.", "Two."])
    task = orc.start_task(open_project(orc, empty), "hi", backend=LOCAL)
    orc.send_message(task["id"], "again", backend=LOCAL)
    notes = [e for e in orc.db.events(task["id"]) if e["type"] == "note" and "is empty" in e["text"]]
    assert len(notes) == 1


def test_requests_to_change_enoy_itself_are_recognised():
    for yes in ('redesign these "ollama ready" "qwen ready" "claude ready" so they look nicer',
                "add a dark mode to Enoy", "make this app start faster", "fix the status dots"):
        assert folders.about_enoy_change(yes), yes
    for no in ("make the app responsive", "what is Enoy?", "add a function to calculator.py", "hey"):
        assert not folders.about_enoy_change(no), no


def test_enoy_offers_its_own_code_without_asking_a_model(orc_factory, monkeypatch, empty, code):
    monkeypatch.setattr(folders, "enoy_source", lambda: str(code))
    orc = orc_factory(qwen=["Here is how you could do it."], claude=["MODE: answer\nunused"],
                      lead__enabled=True, lead__style="claude_first")
    task = orc.start_task(open_project(orc, empty), 'redesign the "qwen ready" dot so it fits in nicer')
    t = orc.db.get_task(task["id"])
    assert t["state"]["pending_folder"]["path"] == str(code.resolve()) and t["state"]["pending_folder"]["is_enoy"]
    assert orc.backends[CLAUDE].requests == [] and orc.backends[LOCAL].requests == []
    assert orc.db.messages(task["id"])[-1]["backend"] == "enoy"
    # "Stay here": the message is answered after all, in this folder.
    orc.answer_folder(task["id"], False)
    assert len(orc.backends[CLAUDE].requests) + len(orc.backends[LOCAL].requests) == 1
    assert orc.db.get_task(task["id"])["state"].get("pending_folder") is None


def test_enoy_does_not_redirect_when_a_real_project_is_open(orc_factory, monkeypatch, project, code):
    monkeypatch.setattr(folders, "enoy_source", lambda: str(code))
    orc = orc_factory(qwen=["Done."])
    task = orc.start_task(open_project(orc, project), "fix the status dots", backend=LOCAL)
    assert orc.db.get_task(task["id"])["state"].get("pending_folder") is None
