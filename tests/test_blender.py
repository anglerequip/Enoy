"""Blender provider wiring, privacy, and real cross-process MCP coordination."""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from enoy import blender, config, paths
from enoy.backends.base import CODEX, CLAUDE, ChatRequest
from enoy.backends.codex import CodexBackend
from enoy.backends.claude_code import _write_mcp_config
from enoy.blender_bridge import SceneLock


@pytest.mark.parametrize("provider", [CODEX, CLAUDE])
def test_both_providers_get_blender_in_general_chat_without_native_file_access(orc_factory, provider):
    def answer(request):
        assert not request.native_tools
        server = request.mcp_servers["blender"]
        assert server["env"]["ENOY_BLENDER_ACTOR"] == provider
        assert "enoy.blender_bridge" in server["args"]
        assert "same scene" in request.messages[0].content
        return "MODE: answer\nInspected the scene."
    orc = orc_factory(qwen=[answer], claude=[answer], blender__enabled=True,
                      blender__server_executable=sys.executable)
    pid = orc.open_project(str(paths.general_chat_dir()))["id"]
    assert orc.start_task(pid, "Inspect Blender", backend=provider)["status"] == "completed"


@pytest.mark.parametrize("restriction", ["disabled", "tools_off", "private_glob", "private_doc", "cloud_off"])
def test_blender_respects_shared_privacy_and_tool_settings(orc_factory, restriction):
    orc = orc_factory(qwen=["MODE: answer\nReady."], blender__enabled=True,
                      blender__server_executable=sys.executable)
    task = orc.start_task(orc.open_project(str(paths.general_chat_dir()))["id"], "Hello")
    if restriction == "disabled":
        orc.cfg["blender"]["enabled"] = False
    elif restriction == "tools_off":
        orc.cfg["tools"]["enabled"] = False
    elif restriction == "private_glob":
        orc.cfg["privacy"]["cloud_excluded_globs"] = ["private/**"]
    elif restriction == "private_doc":
        doc, _ = orc.add_project_file(task["project_id"], "private.txt", b"private")
        orc.db.set_project_file_private(task["project_id"], doc["id"], True)
    else:
        orc.cfg["privacy"]["cloud_policy"] = "never"
    assert blender.servers(orc, task["id"], CODEX, "ask") == {}
    assert blender.instructions(orc, task["id"], "ask") == ""


def test_provider_specific_mcp_configuration_keeps_images_and_permission_review(tmp_path):
    server = {"command": sys.executable, "args": ["-m", "enoy.blender_bridge"],
              "tool_timeout_sec": 600, "env": {"ENOY_BLENDER_ACTOR": "codex"}}
    request = ChatRequest([], mcp_servers={"blender": server})
    args = CodexBackend({}).build_args(request=request)
    assert "--ignore-user-config" in args
    assert "mcp_servers.blender.enabled=true" in args
    assert "mcp_servers.blender.tool_timeout_sec=600" in args
    assert 'approval_policy="on-request"' in args
    assert 'approvals_reviewer="auto_review"' in args
    assert "features.shell_tool=false" in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    native_args = CodexBackend({}).build_args(native_tools=True, request=ChatRequest(
        [], native_tools=True, access_mode="full", mcp_servers={"blender": server}))
    assert 'approvals_reviewer="auto_review"' in native_args
    assert 'approval_policy="on-request"' in native_args
    assert 'mcp_servers.blender.env.ENOY_BLENDER_ACTOR="codex"' in args
    # Filtered requests already ignore user config. A lone disabled server entry
    # without a transport would make Codex reject the entire configuration.
    assert "mcp_servers.blender.enabled=false" not in CodexBackend({}).build_args(request=ChatRequest([]))
    cfg = json.loads(open(_write_mcp_config(["read_file"], {}, {"blender": server}), encoding="utf-8").read())
    assert set(cfg["mcpServers"]) == {"enoy", "blender"}
    assert "tool_timeout_sec" not in cfg["mcpServers"]["blender"]
    assert cfg["mcpServers"]["blender"]["args"] == server["args"]


def test_blender_settings_validate_loopback_only():
    with pytest.raises(config.ConfigError):
        config.validate_strict({"blender": {"host": "remote.example"}})
    with pytest.raises(config.ConfigError):
        config.validate_strict({"blender": {"port": 0}})


def test_scene_lock_reports_contention_and_releases(tmp_path):
    with SceneLock(tmp_path):
        with pytest.raises(TimeoutError):
            with SceneLock(tmp_path, timeout=.1):
                pass
    with SceneLock(tmp_path, timeout=.1):
        pass


def test_real_relays_serialize_providers_and_preserve_mcp_content(tmp_path):
    fake = tmp_path / "fake_mcp.py"
    trace = tmp_path / "trace.jsonl"
    fake.write_text('''import json,sys,time
trace,actor=sys.argv[1:]
for line in sys.stdin:
    msg=json.loads(line)
    if 'id' not in msg: continue
    if msg['method']=='initialize':
        result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'fake','version':'1'}}
    elif msg['method']=='tools/list':
        result={'tools':[{'name':'inspect','description':'Scene','inputSchema':{'type':'object'}}]}
    else:
        with open(trace,'a') as f: f.write(json.dumps([actor,'start'])+'\\n')
        time.sleep(.2)
        with open(trace,'a') as f: f.write(json.dumps([actor,'end'])+'\\n')
        result={'content':[{'type':'text','text':'Scene verified'}, {'type':'image','mimeType':'image/png','data':'YWJj'}],
                'isError':msg.get('params',{}).get('arguments',{}).get('fail',False)}
    print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}),flush=True)
''', encoding="utf-8")
    clients = []

    def start(actor):
        script = ("import sys; from enoy.blender_bridge import relay; "
                  "relay([sys.executable,sys.argv[1],sys.argv[2],sys.argv[3]],sys.argv[4],sys.argv[3])")
        proc = subprocess.Popen([sys.executable, "-u", "-c", script, str(fake), str(trace), actor, str(tmp_path)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        clients.append(proc)
        return proc

    def call(proc, mid, method, params=None):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    try:
        a, b = start("codex"), start("claude")
        with ThreadPoolExecutor(2) as pool:
            init = [pool.submit(call, p, 1, "initialize") for p in clients]
            assert all(f.result(timeout=10)["result"]["serverInfo"]["name"] == "fake" for f in init)
            assert call(a, 2, "tools/list")["result"]["tools"][0]["name"] == "inspect"
            runs = [pool.submit(call, p, 3, "tools/call", {"name": "inspect", "arguments": {"fail": i == 1}})
                    for i, p in enumerate(clients)]
            responses = [f.result(timeout=10) for f in runs]
        assert responses[0]["result"]["content"][1]["data"] == "YWJj"
        assert responses[1]["result"]["isError"] is True
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        assert [e[1] for e in events] == ["start", "end", "start", "end"]
        journal = json.loads((tmp_path / "activity.json").read_text())
        assert {e["provider"] for e in journal} == {"codex", "claude"}
        assert sorted(e["ok"] for e in journal) == [False, True]
        assert all("YWJj" not in e["result"] for e in journal)
    finally:
        for proc in clients:
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()
