"""Native CLI capabilities, actual tool events, and filtered-mode boundaries."""
import json
import sys
import threading
import time

import pytest

from enoy import config, native
from enoy.backends.base import CODEX, CLAUDE, BackendError, ChatMessage, ChatRequest
from enoy.backends.codex import CodexBackend
from enoy.backends.claude_code import ClaudeCodeBackend
from test_orchestrator import open_project


@pytest.fixture(autouse=True)
def native_enabled(cfg):
    cfg["agent"]["native_tools"] = True


@pytest.mark.parametrize("provider", [CODEX, CLAUDE])
def test_selected_provider_receives_native_tools_and_actual_project(orc_factory, project, provider):
    def answer(request):
        assert request.native_tools and request.workspace == str(project.resolve())
        assert "native agent tools" in request.messages[0].content
        assert "Enoy's own source code" in request.messages[0].content
        request.on_event({"tool": "Read", "status": "started", "summary": "calculator.py"})
        request.on_event({"tool": "command_execution", "status": "completed", "summary": "pytest", "exit_code": 1})
        return "I inspected the project; the test command failed."
    orc = orc_factory(qwen=[answer], claude=[answer])
    task = orc.start_task(open_project(orc, project), "Inspect the project", backend=provider)
    assert task["status"] == "completed"
    activity = orc.db.activity(task["id"], 100)
    assert any(e["kind"] == "tool.started" and "calculator.py" in e["text"] for e in activity)
    assert any(e["kind"] == "tool.finished" and "exit 1" in e["text"] for e in activity)


@pytest.mark.parametrize("restriction", ["off", "private_glob", "excluded_glob", "private_doc", "worker"])
def test_explicit_filters_and_patch_workers_keep_context_mode(orc_factory, project, restriction):
    orc = orc_factory(qwen=["MODE: answer\nOk."])
    task = orc.start_task(open_project(orc, project), "Explain the project")
    purpose = "lead"
    if restriction == "off":
        orc.cfg["agent"]["native_tools"] = False
    elif restriction == "private_glob":
        orc.cfg["privacy"]["cloud_excluded_globs"] = ["private/**"]
    elif restriction == "excluded_glob":
        orc.cfg["permissions"]["extra_excluded_globs"] = ["private/**"]
    elif restriction == "private_doc":
        doc, _ = orc.add_project_file(task["project_id"], "private.txt", b"secret")
        orc.db.set_project_file_private(task["project_id"], doc["id"], True)
    else:
        purpose = "edit"
    assert native.options(orc, task["id"], CODEX, purpose) == {}


def test_native_cli_flags_keep_approval_review():
    codex = CodexBackend({"model": "gpt-6.1-sol", "effort": "high"}).build_args(native_tools=True)
    assert "--approve-for-me" in codex and "features.shell_tool=true" in codex
    assert 'model_reasoning_effort="high"' in codex
    assert "--dangerously-bypass-approvals-and-sandbox" not in codex
    claude = ClaudeCodeBackend({"safe_mode": True}).build_args("Enoy context", None, native_tools=True)
    assert claude[claude.index("--tools") + 1] == "default"
    assert claude[claude.index("--permission-mode") + 1] == "auto"
    assert "--append-system-prompt" in claude and "--safe-mode" not in claude
    assert "--dangerously-skip-permissions" not in claude
    assert config.validate({})[0]["agent"]["native_tools"]


@pytest.mark.parametrize("provider", [CODEX, CLAUDE])
def test_native_real_subprocess_uses_project_and_streams_events_before_answer(tmp_path, provider):
    script = tmp_path / "fake_cli.py"
    done = threading.Event()
    script.write_text('''import json, os, sys, time
sys.stdin.read()
assert os.path.isfile('project-marker.txt')
if '--approve-for-me' in sys.argv:
    print(json.dumps({'type':'item.started','item':{'type':'command_execution','command':'inspect'}}), flush=True)
    time.sleep(.4)
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Inspected.'}}), flush=True)
else:
    assert '--permission-mode' in sys.argv and 'auto' in sys.argv
    print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Read','input':{'file_path':'project-marker.txt'}}]}}),flush=True)
    time.sleep(.4)
    print(json.dumps({'type':'result','subtype':'success','result':'Inspected.'}),flush=True)
''', encoding="utf-8")
    (tmp_path / "project-marker.txt").write_text("marker")
    cls = CodexBackend if provider == CODEX else ClaudeCodeBackend
    backend = cls({"enabled": True, "timeout_s": 10}, [sys.executable, str(script)])
    event_seen = threading.Event()
    results = []
    def call():
        results.append(backend.chat(ChatRequest([ChatMessage("user", "Inspect")], native_tools=True,
                       workspace=str(tmp_path), on_event=lambda e: event_seen.set())))
        done.set()
    worker = threading.Thread(target=call)
    worker.start()
    assert event_seen.wait(4)
    assert not done.is_set()
    worker.join(5)
    assert done.is_set() and results[0].text == "Inspected."


def test_cancel_native_cli_stops_waiting(tmp_path):
    script = tmp_path / "slow.py"
    script.write_text("import sys,time\nsys.stdin.read()\ntime.sleep(30)\n")
    cancel = threading.Event()
    backend = CodexBackend({"timeout_s": 10}, [sys.executable, str(script)])
    timer = threading.Timer(.3, cancel.set)
    timer.start()
    started = time.monotonic()
    with pytest.raises(BackendError, match="cancelled"):
        backend.chat(ChatRequest([ChatMessage("user", "wait")], native_tools=True, workspace=str(tmp_path), cancel=cancel))
    assert time.monotonic() - started < 8


def test_general_chat_has_real_folder_discovery_without_native_edits(orc_factory):
    orc = orc_factory(qwen=["MODE: answer\nFound the source folder."])
    from enoy import paths
    task = orc.start_task(orc.open_project(str(paths.general_chat_dir()))["id"], "Find Enoy's folder yourself")
    request = orc.backends[CODEX].requests[-1]
    assert not request.native_tools and "find_folders" in request.enoy_tools
    assert "Enoy's own source code" in request.messages[0].content


@pytest.mark.parametrize("provider", [CODEX, CLAUDE])
def test_full_access_general_chat_runs_native_agent_without_folder_selection(orc_factory, provider):
    from pathlib import Path
    from enoy import paths
    orc = orc_factory(qwen=["Inspected."], claude=["Inspected."], agent__access_mode="full")
    task = orc.start_task(orc.open_project(str(paths.general_chat_dir()))["id"],
                          "Find Enoy and remodel this UI", backend=provider)
    assert task["status"] == "completed"
    request = orc.backends[provider].requests[-1]
    assert request.native_tools and request.access_mode == "full"
    assert request.workspace == str(Path.home())
    assert "Full Access is explicitly enabled" in request.messages[0].content
    if provider == CLAUDE:
        assert "You are Claude" in request.messages[0].content


def test_full_access_codex_uses_supported_policy_without_bypassing_approvals():
    request = ChatRequest([], native_tools=True, access_mode="full")
    args = CodexBackend({}).build_args(native_tools=True, request=request)
    assert args[args.index("--sandbox") + 1] == "danger-full-access"
    assert 'approval_policy="on-request"' in args
    assert "--dangerously-bypass-approvals-and-sandbox" not in args
    assert "--approve-for-me" not in args


@pytest.mark.parametrize("provider", [CODEX, CLAUDE])
def test_native_followup_resumes_only_same_scope_and_refreshes_file_index(orc_factory, project, provider):
    from enoy.backends.base import ChatResult
    def edit(request):
        (project / "newly_created.txt").write_text("Native edit")
        return ChatResult("Done.", provider, "fake", session_id="native-session")
    orc = orc_factory(qwen=[edit], claude=[edit])
    pid = open_project(orc, project)
    task = orc.start_task(pid, "Inspect", backend=provider)
    assert native.options(orc, task["id"], provider, "ask")["resume_session"] == "native-session"
    assert "newly_created.txt" in orc.workspace(pid).index
    orc.cfg["agent"]["access_mode"] = "full"
    assert native.options(orc, task["id"], provider, "ask")["resume_session"] is None
    orc.cfg["agent"]["access_mode"] = "workspace"
    orc.cfg[provider]["model"] = "another-model"
    assert native.options(orc, task["id"], provider, "ask")["resume_session"] is None


def test_codex_native_resume_arguments_preserve_session():
    request = ChatRequest([], native_tools=True, resume_session="session-id")
    args = CodexBackend({}).build_args(native_tools=True, request=request)
    assert args[-3:] == ["resume", "session-id", "-"]
    assert "--ephemeral" not in args


def test_snapshot_marks_omissions_and_preserves_valid_json(orc_factory, project):
    from enoy.coordinator import snapshot
    orc = orc_factory(qwen=["MODE: answer\nOk."])
    task = orc.start_task(open_project(orc, project), "Inspect")
    orc.db.add_message(task["id"], "user", "a" * 20000 + "IMPORTANT END")
    data = json.loads(snapshot(orc, task["id"]).split("\n", 1)[1])
    assert data["conversation"][-1]["content"].endswith("IMPORTANT END")
    assert "omitted" in data["conversation"][-1]["content"]
    assert data["context_notes"]


def test_invalid_access_mode_rejected():
    with pytest.raises(config.ConfigError):
        config.validate_strict({"agent": {"access_mode": "unrecognized"}})


@pytest.mark.parametrize("provider", [CODEX, CLAUDE])
def test_delegated_native_worker_edits_disk_and_reviewer_sees_observed_diff(orc_factory, project, provider):
    from test_lead import delegate, plan_task
    def edit(request):
        assert request.native_tools and request.purpose == "native_edit"
        assert "Use your native file and command tools" in request.messages[0].content
        assert "<<<FILE" not in request.messages[-1].content
        path = project / "calculator.py"
        path.write_text(path.read_text() + "\n\ndef multiply(a, b):\n    return a * b\n")
        return "Added multiply with native tools."
    def review(request):
        assert "+def multiply" in request.messages[-1].content
        return "Changes inspected.\nNEXT: done"
    replies = [delegate(plan_task(model=provider))] + ([edit] if provider == CODEX else []) + [review]
    orc = orc_factory(qwen=replies, claude=[edit])
    task = orc.start_task(open_project(orc, project), "Add multiply")
    assert task["status"] == "completed", task["summary"]
    dev = next(t for t in orc.db.team_tasks(task["id"]) if t["role"] == "developer")
    assert dev["result"]["native"] and "calculator.py" in dev["result"]["changed"]
    assert not orc.db.list_changesets(task["id"])
