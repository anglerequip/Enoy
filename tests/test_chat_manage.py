"""Chat rename and delete, and keeping a stopped answer's text."""

import http.client
import json
import time

import pytest

from conftest import FakeBackend
from fusion_ai import paths
from fusion_ai.backends.base import CLAUDE, LOCAL, BackendError
from fusion_ai.orchestrator import EventBus, Orchestrator, TaskError
from fusion_ai.server import App, Server
from fusion_ai.storage import Storage


def make(cfg, tmp_path, qwen=None, synchronous=True):
    backends = {LOCAL: FakeBackend(LOCAL, qwen), CLAUDE: FakeBackend(CLAUDE, available=False)}
    return Orchestrator(Storage(":memory:"), cfg, backends, EventBus(), backups_dir=tmp_path / "bk", synchronous=synchronous)


def plain_chat(orc, text="Hello"):
    default = orc.open_project(str(paths.general_chat_dir()))
    return orc.start_task(default["id"], text)


def test_rename_trims_and_persists(cfg, tmp_path):
    orc = make(cfg, tmp_path, qwen=["Hi."])
    t = plain_chat(orc)
    orc.rename_task(t["id"], "  My   renamed\nchat  ")
    assert orc.db.get_task(t["id"])["title"] == "My renamed chat"
    with pytest.raises(TaskError, match="name"):
        orc.rename_task(t["id"], "   ")
    orc.rename_task(t["id"], "x" * 500)
    assert len(orc.db.get_task(t["id"])["title"]) == Orchestrator.CHAT_TITLE_MAX
    with pytest.raises(TaskError, match="Unknown"):
        orc.rename_task(9999, "x")


def test_delete_removes_the_chat_and_keeps_artifacts(cfg, tmp_path):
    orc = make(cfg, tmp_path, qwen=["Answer one.", "Other answer."])
    t = plain_chat(orc)
    keep = plain_chat(orc, "Second chat")
    msg = orc.db.messages(t["id"])[-1]
    art = orc.db.create_artifact("Saved", "markdown", "Answer one.", task_id=t["id"], message_id=msg["id"])
    orc.delete_task(t["id"])
    assert orc.db.get_task(t["id"]) is None
    assert orc.db.messages(t["id"]) == []
    assert orc.db.activity(t["id"]) == []
    saved = orc.db.get_artifact(art["id"])
    assert saved["content"] == "Answer one." and saved["task_id"] is None and saved["message_id"] is None
    assert orc.db.get_task(keep["id"]) and orc.db.messages(keep["id"])  # Other chats untouched.


def test_delete_refuses_a_running_chat(cfg, tmp_path):
    orc = make(cfg, tmp_path, qwen=["Hi."])
    t = plain_chat(orc)
    orc.db.update_task(t["id"], status="running")
    with pytest.raises(TaskError, match="Stop it first"):
        orc.delete_task(t["id"])
    assert orc.db.get_task(t["id"])


def test_stopped_answer_keeps_its_text(cfg, tmp_path):
    def stream_then_wait(request):
        request.on_token("Lua was created in 1993 ")
        request.on_token("at PUC-Rio.")
        for _ in range(200):
            if request.cancel.is_set():
                raise BackendError("cancelled", "Cancelled.")
            time.sleep(0.02)
        return "finished anyway"

    orc = make(cfg, tmp_path, qwen=[stream_then_wait], synchronous=False)
    t = plain_chat(orc, "Tell me about Lua")
    for _ in range(200):
        if orc._partial.get(t["id"]):
            break
        time.sleep(0.02)
    orc.cancel(t["id"])
    orc.wait_idle(5)
    assert orc.db.get_task(t["id"])["status"] == "cancelled"
    last = orc.db.messages(t["id"])[-1]
    assert last["role"] == "assistant" and last["content"].startswith("Lua was created in 1993 at PUC-Rio.")
    assert "Stopped before the answer was finished" in last["content"]
    assert t["id"] not in orc._partial


@pytest.fixture
def server(cfg, tmp_path):
    orc = make(cfg, tmp_path, qwen=["Hi."])
    srv = Server(App(orc, [], save_config=False)).start()
    yield srv
    srv.stop()


def call(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{srv.port}", "X-Fusion-Token": srv.app.token})
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def test_api_rename_and_delete(server):
    orc = server.app.orc
    t = plain_chat(orc)
    st, d = call(server, "POST", f"/api/tasks/{t['id']}/rename", {"title": "Renamed"})
    assert st == 200 and d["task"]["title"] == "Renamed"
    st, d = call(server, "POST", f"/api/tasks/{t['id']}/rename", {"title": ""})
    assert st == 400
    st, d = call(server, "POST", f"/api/tasks/{t['id']}/delete", {})
    assert st == 400 and "confirmation" in d["error"]
    assert orc.db.get_task(t["id"])
    st, d = call(server, "POST", f"/api/tasks/{t['id']}/delete", {"confirm": True})
    assert st == 200 and orc.db.get_task(t["id"]) is None
    st, d = call(server, "GET", f"/api/tasks/{t['id']}")
    assert st in (400, 404)
