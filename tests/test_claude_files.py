"""Claude reads the project itself through Enoy: read-only, without secrets files or private globs."""

import io
import json
import sys

import pytest

from enoy.backends.base import CLAUDE, LOCAL
from enoy.project import Workspace
from enoy.tools import CLAUDE_FILES_NOTE, ToolError
from enoy.tools.agent import CloudFiles

from test_lead import delegate, plan_task
from test_orchestrator import open_project

KEY = "sk-ant-" + "a" * 40


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "app"
    (root / "ui").mkdir(parents=True)
    (root / "private").mkdir()
    (root / "ui" / "app.js").write_text('function renderStatusLine() { return "Ollama ready"; }\n'
                                        f'const KEY = "{KEY}";\n', encoding="utf-8")
    (root / "private" / "notes.txt").write_text("Ollama ready plans, private\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
    return root


def test_cloud_files_hide_private_globs_and_never_write(repo):
    files = CloudFiles(Workspace(repo), lambda rel: rel.startswith("private/"))
    assert "private/notes.txt" not in files.ws.index and "ui/app.js" in files.ws.index
    assert all(h["path"] != "private/notes.txt" for h in files.ws.search("ollama ready"))
    with pytest.raises(ToolError, match="kept private"):
        files.read("private/notes.txt")
    with pytest.raises(ToolError, match="secrets file"):
        files.read(".env")
    with pytest.raises(ToolError, match="Read-only"):
        files.save("ui/app.js", "x")
    assert "renderStatusLine" in files.read("ui/app.js")


def test_mcp_server_serves_the_project_read_only_and_redacted(repo, monkeypatch, capsys):
    from enoy.tools import mcp_server
    monkeypatch.setenv("ENOY_MCP_TOOLS", "list_files,read_file,search_code")
    monkeypatch.setenv("ENOY_MCP_CFG", json.dumps({"tools": {}}))
    monkeypatch.setenv("ENOY_MCP_WORKSPACE", str(repo))
    monkeypatch.setenv("ENOY_MCP_PRIVATE", json.dumps({"cloud_excluded_globs": ["private/**"]}))
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "search_code", "arguments": {"query": "ollama ready"}}},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": "ui/app.js"}}},
             {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "read_file", "arguments": {"path": "private/notes.txt"}}},
             {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
              "params": {"name": "write_file", "arguments": {"path": "ui/app.js", "content": "x"}}}]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(json.dumps(x) for x in lines) + "\n"))
    mcp_server.main()
    out = {r["id"]: r["result"] for r in map(json.loads, capsys.readouterr().out.splitlines())}
    assert sorted(t["name"] for t in out[1]["tools"]) == ["list_files", "read_file", "search_code"]
    assert all(t["annotations"]["readOnlyHint"] for t in out[1]["tools"])
    found = out[2]["content"][0]["text"]
    assert "ui/app.js" in found and "private/notes.txt" not in found
    body = out[3]["content"][0]["text"]
    assert "renderStatusLine" in body and KEY not in body  # Secrets are redacted before Claude sees them.
    assert out[4]["isError"] and "kept private" in out[4]["content"][0]["text"]
    assert out[5]["isError"] and (repo / "ui" / "app.js").read_text(encoding="utf-8").startswith("function")


def test_claude_lead_and_team_requests_carry_the_file_tools(orc_factory, project):
    orc = orc_factory(qwen=[delegate(plan_task(model="claude")), "Done.\nNEXT: done"], claude=["No change blocks."],
                      )
    orc.start_task(open_project(orc, project), "Add a multiply function")
    dev = orc.backends[CLAUDE].requests[0]
    for req in (dev,):
        assert req.enoy_tools == ["list_files", "read_file", "search_code"]
        assert req.enoy_env["ENOY_MCP_WORKSPACE"] == str(project.resolve())
        assert CLAUDE_FILES_NOTE in req.messages[0].content
    assert dev.enoy_env["ENOY_MCP_PRIVATE"]["cloud_excluded_globs"] == []


def test_no_file_tools_without_files_local_only_or_when_switched_off(orc_factory, project, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    orc = orc_factory(claude=["MODE: answer\nOk."] * 3, )
    orc.start_task(open_project(orc, empty), "Is divide thread-safe? Explain the race conditions.", backend=CLAUDE)
    assert orc.backends[CLAUDE].requests[-1].enoy_tools == []
    t = orc.start_task(open_project(orc, project), "Is divide thread-safe? Explain the race conditions.", backend=CLAUDE)
    orc.set_task_local_only(t["id"], True)
    assert orc._claude_file_tools(t["id"]) == []
    orc.set_task_local_only(t["id"], False)
    orc.cfg["tools"]["claude_files"] = False
    assert orc._claude_file_tools(t["id"]) == []
