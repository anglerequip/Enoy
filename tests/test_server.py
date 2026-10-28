"""Application startup and the local HTTP API (fake backends, real server)."""

import http.client
import io
import json

import pytest

from conftest import FakeBackend
from enoy.backends.base import CLAUDE, CODEX as LOCAL
from enoy.orchestrator import EventBus, Orchestrator
from enoy.server import App, Server
from enoy.storage import Storage

ADD = """<<<FILE notes.txt
hello
>>>END
"""


@pytest.fixture
def server(cfg, tmp_path):
    cfg["agent"]["native_tools"] = False  # Exercise Enoy's patch approval endpoints.
    backends = {LOCAL: FakeBackend(LOCAL, ["An answer.", ADD, "Done.\nNEXT: done"]), CLAUDE: FakeBackend(CLAUDE, available=False)}
    orc = Orchestrator(Storage(":memory:"), cfg, backends, EventBus(), backups_dir=tmp_path / "bk", synchronous=True)
    app = App(orc, [], save_config=False)
    srv = Server(app).start()
    yield srv
    srv.stop()


def call(srv, method, path, body=None, token=True, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    headers = {"Content-Type": "application/json", "Host": host or f"127.0.0.1:{srv.port}"}
    if token:
        headers["X-Enoy-Token"] = srv.app.token
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(data)
    except ValueError:
        return resp.status, data.decode()


def test_ui_is_served_with_token(server):
    status, html = call(server, "GET", "/", token=False)
    assert status == 200 and server.app.token in html and "Enoy" in html
    status, _ = call(server, "GET", "/static/app.js", token=False)
    assert status == 200


def test_chat_attachment_upload_is_bounded_and_stays_in_project(server, tmp_path):
    folder = tmp_path / "code"
    folder.mkdir()
    opened = server.app.dispatch("POST", "/api/projects/open", {}, {"path": str(folder)})
    pid = opened['project']['id']
    payload = b"example attachment"
    result = server.app.store_attachment(pid, "notes.txt", io.BytesIO(payload), len(payload))
    assert result['path'] == 'enoy-uploads/notes.txt'
    assert (folder / 'enoy-uploads' / 'notes.txt').read_bytes() == b"example attachment"
    from enoy.server import ApiError
    with pytest.raises(ApiError):
        server.app.store_attachment(pid, "../.env", io.BytesIO(payload), len(payload))
    second = server.app.store_attachment(pid, "notes.txt", io.BytesIO(payload), len(payload))
    assert second['path'] == 'enoy-uploads/notes-1.txt'
    with pytest.raises(ApiError):
        server.app.store_attachment(pid, "huge.mp4", io.BytesIO(payload), 2_000_000_001)


def test_provider_launch_endpoint(server, monkeypatch):
    from enoy import provider_launch
    calls = []
    def launch(provider, settings, action):
        calls.append((provider, settings, action))
        return {'opened': True}
    monkeypatch.setattr(provider_launch, 'launch', launch)
    url = '/api/providers/claude/launch'
    assert call(server, 'POST', url, {}, token=False)[0] == 401
    assert not calls
    assert call(server, 'POST', url, {'action': 'login'}) == (200, {'opened': True})
    assert calls == [('claude', server.app.orc.cfg['claude'], 'login')]
    def fail(*args):
        raise ValueError('CLI missing')
    monkeypatch.setattr(provider_launch, 'launch', fail)
    assert call(server, 'POST', url, {}) == (400, {'error': 'CLI missing'})


def test_usage_api(server):
    import time
    assert call(server, "GET", "/api/usage", token=False)[0] == 401
    assert call(server, "POST", "/api/usage/claude", {}, token=False)[0] == 401
    assert call(server, "GET", "/api/usage/savings", token=False)[0] == 401
    status, data = call(server, "GET", "/api/usage")
    assert status == 200
    assert data["codex"]["windows"] == []
    assert data["claude"]["source"] == "Claude subscription"
    assert call(server, "POST", "/api/usage/claude", {"five_hour": {"used_percent": -1, "resets_at": time.time() + 100}})[0] == 400
    status, data = call(server, "POST", "/api/usage/claude", {"seven_day": {"used_percent": 64, "resets_at": time.time() + 100}})
    assert status == 200 and data["windows"][0]["remaining_percent"] == 36
    assert call(server, "GET", "/api/usage")[1]["claude"]["windows"] == data["windows"]
    assert call(server, "GET", "/api/usage/savings")[1]["estimated_tokens_saved"] == 0


def test_explicit_status_refresh_clears_auth_cache(server):
    backend = server.app.orc.backends[CLAUDE]
    backend._auth_cache = (1, {'loggedIn': False})
    call(server, 'GET', '/api/status?refresh=1')
    assert backend._auth_cache is None


def test_update_api_requires_auth_and_handles_active_tasks(server, monkeypatch):
    assert call(server, 'GET', '/api/updates', token=False)[0] == 401
    assert call(server, 'POST', '/api/updates', {}, token=False)[0] == 401
    assert call(server, 'GET', '/api/updates')[1]['status'] == 'idle'
    monkeypatch.setattr(server.app.updates, 'busy', lambda: True)
    assert call(server, 'POST', '/api/updates', {})[0] == 409
    monkeypatch.setattr(server.app.updates, 'start', lambda install: {'status': 'checking'})
    assert call(server, 'POST', '/api/updates', {}) == (200, {'status': 'checking'})


def test_regular_chat_launch_never_calls_a_backend(server, monkeypatch):
    from enoy import provider_launch
    opened = []
    monkeypatch.setattr(provider_launch, 'open_chatgpt', lambda: opened.append(True) or {'opened': True})
    before = {name: len(backend.requests) for name, backend in server.app.orc.backends.items()}
    assert call(server, 'POST', '/api/chatgpt/open', {}, token=False)[0] == 401
    assert not opened
    assert call(server, 'POST', '/api/chatgpt/open', {'prompt': 'must not be forwarded'}) == (200, {'opened': True})
    assert opened == [True]
    assert {name: len(backend.requests) for name, backend in server.app.orc.backends.items()} == before


def test_appearance_api_is_authenticated_and_persistent(server):
    assert call(server,'GET','/api/appearance',token=False)[0]==401
    assert call(server,'POST','/api/appearance',{},token=False)[0]==401
    value={'mode':'code','preset':'coffee','custom':{'accent':'#abcdef'}}
    status,saved=call(server,'POST','/api/appearance',value)
    assert status==200 and saved['colours']['code']['preset']=='coffee'
    assert call(server,'GET','/api/appearance')[1]==saved
    assert call(server,'POST','/api/appearance',{'mode':'code','preset':'coffee','custom':{'accent':'bad'}})[0]==400
    assert call(server,'GET','/api/appearance')[1]==saved


def test_api_requires_token_and_local_host(server):
    assert call(server, "GET", "/api/status", token=False)[0] == 401
    assert call(server, "GET", "/api/status", host="evil.example.com")[0] == 403
    status, data = call(server, "GET", "/api/status")
    assert status == 200 and data["codex"]["available"] is True and data["claude"]["available"] is False


def test_agent_permission_modes_round_trip_through_api(server):
    assert call(server, "GET", "/api/status")[1]["agent"]["access_mode"] == "workspace"
    status, data = call(server, "POST", "/api/config", {"config": {"agent": {"native_tools": True, "access_mode": "full"}}})
    assert status == 200 and data["config"]["agent"] == {"native_tools": True, "access_mode": "full"}
    assert call(server, "GET", "/api/status")[1]["agent"]["access_mode"] == "full"
    assert call(server, "POST", "/api/config", {"config": {"agent": {"access_mode": "invalid"}}})[0] == 400
    assert call(server, "GET", "/api/status")[1]["agent"]["access_mode"] == "full"


def test_static_path_traversal_blocked(server):
    status, _ = call(server, "GET", "/static/../server.py", token=False)
    assert status == 404


def test_full_flow_over_http(server, project):
    status, data = call(server, "POST", "/api/projects/open", {"path": str(project)})
    assert status == 200
    pid = data["project"]["id"]
    status, tree = call(server, "GET", f"/api/projects/{pid}/tree")
    assert status == 200 and tree["count"] >= 4
    assert call(server, "GET", f"/api/projects/{pid}/file?path=.env")[0] == 403
    assert call(server, "GET", f"/api/projects/{pid}/file?path=../x")[0] == 403

    status, data = call(server, "POST", "/api/tasks", {"project_id": pid, "objective": "Explain divide"})
    assert status == 200
    status, detail = call(server, "GET", f"/api/tasks/{data['task']['id']}")
    assert detail["task"]["status"] == "completed" and detail["messages"][-1]["content"] == "An answer."

    status, data = call(server, "POST", "/api/tasks", {"project_id": pid, "objective": "Create notes.txt", "kind": "edit", "backend": LOCAL})
    tid = data["task"]["id"]
    status, sets = call(server, "GET", f"/api/changesets?project_id={pid}&status=pending")
    assert len(sets["changesets"]) == 1
    cs_id = sets["changesets"][0]["id"]
    assert call(server, "POST", f"/api/changesets/{cs_id}/approve")[0] == 200
    assert (project / "notes.txt").read_text(encoding="utf-8") == "hello\n"
    status, brief = call(server, "GET", f"/api/tasks/{tid}/brief")
    assert "Create notes.txt" in brief["brief"] and "notes.txt" in brief["brief"]


def test_command_endpoint_asks_before_unlisted_commands(server, project):
    pid = call(server, "POST", "/api/projects/open", {"path": str(project)})[1]["project"]["id"]
    status, data = call(server, "POST", "/api/commands/run", {"project_id": pid, "command": "node -e 1"})
    assert status == 200 and data["needs_approval"] is True
    status, data = call(server, "POST", "/api/commands/run", {"project_id": pid, "command": "format C:", "approved": True})
    assert status == 400 and "Blocked" in data["error"]


def test_invalid_settings_rejected(server):
    status, data = call(server, "POST", "/api/config", {"config": {"routing": {"mode": "chaos"}}})
    assert status == 400
    status, data = call(server, "POST", "/api/config", {"config": {"routing": {"max_attempts": 3}}})
    assert status == 200 and data["config"]["routing"]["max_attempts"] == 3


def test_unknown_project_is_a_clean_error(server):
    status, data = call(server, "POST", "/api/tasks", {"project_id": 999, "objective": "x"})
    assert status == 404 and "Project not found" in data["error"]


def test_plain_chat_without_project(server):
    status, data = call(server, "POST", "/api/projects/default")
    assert status == 200
    pid = data["project"]["id"]
    projects = call(server, "GET", "/api/projects")[1]["projects"]
    assert [p["is_default"] for p in projects if p["id"] == pid] == [True]
    # Even an edit-sounding request is answered as a question: there are no files to change.
    status, data = call(server, "POST", "/api/tasks", {"project_id": pid, "objective": "Write a function that adds two numbers"})
    task = call(server, "GET", f"/api/tasks/{data['task']['id']}")[1]
    assert task["task"]["kind"] == "ask" and task["task"]["status"] == "completed"
    system = server.app.orc.backends[LOCAL].requests[-1].messages[0].content
    assert "ChatGPT" in system


def test_followup_can_attach_files(server, project):
    pid = call(server, "POST", "/api/projects/open", {"path": str(project)})[1]["project"]["id"]
    tid = call(server, "POST", "/api/tasks", {"project_id": pid, "objective": "Explain this project"})[1]["task"]["id"]
    server.app.orc.backends[LOCAL].replies.append("README says it is a sample.")
    status, _ = call(server, "POST", f"/api/tasks/{tid}/message", {"text": "And the readme?", "files": ["README.md"]})
    assert status == 200
    sent = server.app.orc.backends[LOCAL].requests[-1].messages[-1].content
    assert "=== README.md ===" in sent
    assert call(server, "POST", f"/api/tasks/{tid}/message", {"text": "  "})[0] == 400


def test_build_app_starts(app_home):
    """The real wiring (config, database, both adapters) starts without a window."""
    from enoy.app import build

    app, orc = build(save_config=False)
    srv = Server(app).start()
    try:
        status, html = call(srv, "GET", "/", token=False)
        assert status == 200
        assert (app_home / "enoy.db").exists()
    finally:
        srv.stop()
        orc.db.close()


def test_activity_endpoints(server, project):
    orc = server.app.orc
    status, data = call(server, "POST", "/api/projects/open", {"path": str(project)})
    pid = data["project"]["id"]
    status, data = call(server, "POST", "/api/tasks", {"project_id": pid, "objective": "Explain divide"})
    tid = data["task"]["id"]
    status, snap = call(server, "GET", f"/api/activity?focus={tid}")
    assert status == 200 and snap["focus"]["task"]["id"] == tid
    assert {a["name"] for a in snap["agents"]} == {LOCAL, CLAUDE}
    assert snap["events"] and all("category" in e for e in snap["events"])
    status, only = call(server, "GET", f"/api/activity?task={tid}&limit=5")
    assert status == 200 and len(only["events"]) <= 5 and all(e["task_id"] == tid for e in only["events"])
    status, log = call(server, "GET", f"/api/activity/log?task_id={tid}")
    assert status == 200 and log["activity"][0]["kind"] == "task.created" and log["steps"]
    status, _ = call(server, "GET", "/api/activity/log?task_id=999")
    assert status == 404
    requests = len(orc.backends[LOCAL].requests)
    status, data = call(server, "POST", "/api/dispatch", {"paused": True})
    assert status == 200 and data["paused"] is True
    status, data = call(server, "POST", "/api/tasks", {"project_id": pid, "objective": "Add notes.txt", "backend": LOCAL})
    assert data["task"]["status"] == "queued" and len(orc.backends[LOCAL].requests) == requests
    status, data = call(server, "POST", "/api/dispatch", {"paused": False})
    assert data["paused"] is False and data["queued"] == 1
    assert orc.db.get_task(tid + 1)["status"] == "awaiting_approval"  # The queued task ran after resuming.
    assert orc.backends[CLAUDE].requests == []  # Monitoring and dispatch never reach Claude.
