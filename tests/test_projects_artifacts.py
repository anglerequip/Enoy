"""Projects (instructions, documents, chats) and Artifacts (versions, export, safety)."""

import base64
import http.client
import json

import pytest

from conftest import FakeBackend
from enoy import artifacts as art_mod
from enoy import knowledge, paths
from enoy.backends.base import CLAUDE, LOCAL
from enoy.orchestrator import EventBus, Orchestrator, TaskError
from enoy.server import App, Server
from enoy.storage import Storage


def make(cfg, tmp_path, qwen=None, claude=None):
    backends = {LOCAL: FakeBackend(LOCAL, qwen), CLAUDE: FakeBackend(CLAUDE, claude)}
    return Orchestrator(Storage(":memory:"), cfg, backends, EventBus(), backups_dir=tmp_path / "bk", synchronous=True)


def sent_to(orc, which, task_id):
    return "\n".join(x["content"] for x in orc.db.transmissions(task_id, which))


# ---------------------------------------------------------------- projects

def test_create_rename_describe_and_reload_instructions(cfg, tmp_path):
    orc = make(cfg, tmp_path)
    p = orc.create_space("Thesis", "My master thesis", "Answer in Swedish.")
    assert p["kind"] == "space" and p["instructions"] == "Answer in Swedish."
    orc.update_project(p["id"], name="Thesis 2026", description="Updated")
    orc.update_project(p["id"], instructions="Use APA citations.")
    again = orc.db.get_project(p["id"])
    assert (again["name"], again["description"], again["instructions"]) == ("Thesis 2026", "Updated", "Use APA citations.")
    assert any(x["id"] == p["id"] for x in orc.db.list_projects())


def test_project_validation(cfg, tmp_path):
    orc = make(cfg, tmp_path)
    with pytest.raises(TaskError):
        orc.create_space("   ")
    orc.create_space("Same")
    with pytest.raises(TaskError, match="already exists"):
        orc.create_space("same")  # Duplicate submit or duplicate name.
    with pytest.raises(TaskError, match="Unknown project"):
        orc.update_project(9999, name="x")
    p = orc.create_space("Other")
    with pytest.raises(TaskError, match="8,000"):
        orc.update_project(p["id"], instructions="x" * 9000)


def test_instructions_apply_only_inside_their_project(cfg, tmp_path):
    orc = make(cfg, tmp_path, qwen=["A.", "B.", "C."])
    a = orc.create_space("A", instructions="SECRET-RULE-A: always mention llamas.")
    b = orc.create_space("B", instructions="RULE-B: be brief.")
    default = orc.open_project(str(paths.general_chat_dir()))
    ta = orc.start_task(a["id"], "Hello there")
    tb = orc.start_task(b["id"], "Hello there")
    tp = orc.start_task(default["id"], "Hello there")
    assert "SECRET-RULE-A" in sent_to(orc, LOCAL, ta["id"])
    assert "SECRET-RULE-A" not in sent_to(orc, LOCAL, tb["id"]) and "RULE-B" in sent_to(orc, LOCAL, tb["id"])
    assert "RULE" not in sent_to(orc, LOCAL, tp["id"])  # Plain chats are unchanged.


def test_folder_project_instructions_reach_qwen(cfg, tmp_path, project):
    orc = make(cfg, tmp_path, qwen=["Ok."])
    pid = orc.open_project(str(project))["id"]
    orc.update_project(pid, instructions="Write tests for every change.")
    t = orc.start_task(pid, "Explain what divide does")
    assert "Write tests for every change." in sent_to(orc, LOCAL, t["id"])


def test_conversations_move_reopen_and_keep_history(cfg, tmp_path, project):
    orc = make(cfg, tmp_path, qwen=["One.", "Two."])
    default = orc.open_project(str(paths.general_chat_dir()))
    space = orc.create_space("Work")
    t = orc.start_task(default["id"], "First question")
    orc.move_task(t["id"], space["id"])
    assert orc.db.get_task(t["id"])["project_id"] == space["id"]
    assert [m["content"] for m in orc.db.messages(t["id"])][:2] == ["First question", "One."]
    orc.send_message(t["id"], "Follow-up")  # Continues in the new project.
    assert len(orc.db.messages(t["id"])) == 4
    orc.move_task(t["id"], default["id"])  # "Remove from project" keeps the chat.
    assert orc.db.get_task(t["id"])["project_id"] == default["id"]
    folder = orc.open_project(str(project))
    with pytest.raises(TaskError, match="code folder"):
        orc.move_task(t["id"], folder["id"])


def test_delete_space_moves_chats_deletes_docs_keeps_artifacts(cfg, tmp_path):
    orc = make(cfg, tmp_path, qwen=["Ok."])
    space = orc.create_space("Temp")
    t = orc.start_task(space["id"], "Hi")
    orc.add_project_file(space["id"], "notes.md", b"some notes")
    art = orc.db.create_artifact("Plan", "markdown", "# Plan", project_id=space["id"], task_id=t["id"])
    result = orc.delete_project(space["id"])
    assert result == {"artifacts_kept": 1, "chats_moved": 1, "files_deleted": 1}
    default = orc.open_project(str(paths.general_chat_dir()))
    assert orc.db.get_task(t["id"])["project_id"] == default["id"]
    assert orc.db.get_artifact(art["id"])["project_id"] is None  # Same record, unlinked.
    assert all(p["id"] != space["id"] for p in orc.db.list_projects())
    with pytest.raises(TaskError):
        orc.delete_project(default["id"])


def test_remove_folder_project_touches_nothing_on_disk(cfg, tmp_path, project):
    orc = make(cfg, tmp_path, qwen=["Ok."])
    pid = orc.open_project(str(project))["id"]
    before = sorted(p.name for p in project.iterdir())
    t = orc.start_task(pid, "Explain divide")
    orc.delete_project(pid)
    assert sorted(p.name for p in project.iterdir()) == before
    assert all(p["id"] != pid for p in orc.db.list_projects())
    again = orc.open_project(str(project))  # Reopening brings it back with its chats.
    assert again["id"] == pid and orc.db.get_task(t["id"])["project_id"] == pid


# ---------------------------------------------------------------- documents

def test_document_validation():
    assert knowledge.validate("notes.md", "héllo".encode())[1] == "héllo"
    assert knowledge.validate("../../etc/notes.txt", b"x")[0] == "notes.txt"  # Directories are dropped.
    for name, data, why in [("a.pdf", b"%PDF-1.4", "not supported"), ("a.exe", b"MZ", "not supported"),
                            (".env", b"KEY=1", "secrets"), ("id_rsa", b"-----BEGIN", "secrets"),
                            ("big.txt", b"x" * (knowledge.MAX_FILE_BYTES + 1), "limit"),
                            ("bin.txt", b"a\x00b", "not a text"), ("empty.md", b"   ", "empty")]:
        with pytest.raises(knowledge.KnowledgeError, match=why):
            knowledge.validate(name, data)


def test_documents_are_scoped_to_their_project(cfg, tmp_path):
    orc = make(cfg, tmp_path)
    a, b = orc.create_space("A"), orc.create_space("B")
    f, created = orc.add_project_file(a["id"], "a.md", b"alpha")
    assert created and not orc.add_project_file(a["id"], "copy.md", b"alpha")[1]  # Same content stored once.
    assert orc.db.project_file(b["id"], f["id"]) is None  # Cross-project access is refused.
    assert orc.db.delete_project_file(b["id"], f["id"]) is False
    assert [x["name"] for x in orc.db.project_files(a["id"])] == ["a.md"]
    assert orc.db.delete_project_file(a["id"], f["id"]) and orc.db.project_files(a["id"]) == []


def test_retrieval_picks_only_relevant_passages():
    files = [{"name": "dough.md", "content": "Dough\n\nUse 12 grams of smoked paprika per kilo of flour.", "private": 0},
             {"name": "hours.md", "content": "Hours\n\nOpen Tuesday to Sunday.", "private": 0}]
    got = knowledge.retrieve(files, "How much paprika goes in the dough?")
    assert [s.name for s in got] == ["dough.md"]
    assert knowledge.retrieve(files, "the and of") == []
    secret = [{"name": "k.md", "content": "token: sk-ant-api03-" + "a" * 40, "private": 0}]
    assert "sk-ant-api03-aaaa" not in knowledge.retrieve(secret, "token")[0].text  # Redacted.


def test_project_knowledge_reaches_qwen_but_private_docs_never_reach_claude(cfg, tmp_path):
    orc = make(cfg, tmp_path, claude=["Claude answer."])
    space = orc.create_space("Shop")
    orc.add_project_file(space["id"], "dough.md", b"Dough\n\nUse 12 grams of smoked paprika per kilo of flour.")
    f, _ = orc.add_project_file(space["id"], "margins.md", b"Margins\n\nOur paprika supplier margin is 37 percent.")
    orc.db.set_project_file_private(space["id"], f["id"], True)
    t = orc.start_task(space["id"], "How much paprika do we use, and what is the paprika margin?", backend=CLAUDE)
    sent = sent_to(orc, CLAUDE, t["id"])
    assert "12 grams of smoked paprika" in sent
    assert "37 percent" not in sent and "margins.md" not in sent
    pending_names = orc.db.transmissions(t["id"], CLAUDE)
    assert pending_names


def test_local_only_project_sends_no_documents_to_claude(cfg, tmp_path):
    orc = make(cfg, tmp_path, qwen=["Local answer."], claude=["should not happen"])
    space = orc.create_space("Private")
    orc.add_project_file(space["id"], "dough.md", b"Use 12 grams of smoked paprika.")
    orc.set_project_local_only(space["id"], True)
    t = orc.start_task(space["id"], "How much paprika?", backend=CLAUDE)
    assert orc.backends[CLAUDE].requests == []
    assert orc.db.get_task(t["id"])["status"] in ("failed", "completed")


def test_excluded_globs_apply_to_documents(cfg, tmp_path):
    cfg["privacy"]["cloud_excluded_globs"] = ["*.private.md"]
    orc = make(cfg, tmp_path, claude=["ok"])
    space = orc.create_space("Shop")
    orc.add_project_file(space["id"], "plan.private.md", b"Paprika expansion plan: buy the farm.")
    t = orc.start_task(space["id"], "What is the paprika expansion plan?", backend=CLAUDE)
    assert "buy the farm" not in sent_to(orc, CLAUDE, t["id"])


# ---------------------------------------------------------------- artifacts (storage and rules)

def test_artifact_versions_conflicts_and_restore():
    db = Storage(":memory:")
    a = db.create_artifact("Spec", "markdown", "v1")
    b = db.update_artifact(a["id"], 1, content="v2")
    assert b["version"] == 2
    assert db.update_artifact(a["id"], 2, content="v2")["version"] == 2  # No change, no new version.
    with pytest.raises(art_mod.Conflict):
        db.update_artifact(a["id"], 1, content="stale tab")  # An older editor cannot overwrite newer work.
    old = db.artifact_version(a["id"], 1)
    c = db.update_artifact(a["id"], 2, note="Restored version 1", content=old["content"])
    assert c["version"] == 3 and c["content"] == "v1"
    assert [v["version"] for v in db.artifact_versions(a["id"])] == [3, 2, 1]
    assert db.artifact_version(a["id"], 2)["content"] == "v2"  # Later work stays recoverable.


def test_failed_save_leaves_artifact_unchanged(monkeypatch):
    db = Storage(":memory:")
    a = db.create_artifact("Spec", "markdown", "v1")
    real = db._exec

    def boom(sql, params=()):
        if sql.startswith("INSERT INTO artifact_versions"):
            raise RuntimeError("disk full")
        return real(sql, params)
    monkeypatch.setattr(db, "_exec", boom)
    with pytest.raises(RuntimeError):
        db.update_artifact(a["id"], 1, content="v2")
    monkeypatch.setattr(db, "_exec", real)
    after = db.get_artifact(a["id"])
    assert after["content"] == "v1" and after["version"] == 1  # Rolled back together.


def test_message_code_blocks_and_titles():
    text = "Here:\n\n```python\nprint('hi')\n```\n\n<<<FILE a.py\n```\nnot a block\n```\n>>>END\n\n```svg\n<svg/>\n```"
    assert art_mod.code_blocks(text) == [("python", "print('hi')"), ("svg", "<svg/>")]
    assert art_mod.from_message(text, 0)["type"] == "code" and art_mod.from_message(text, 0)["language"] == "python"
    assert art_mod.from_message(text, 1)["type"] == "svg"
    assert art_mod.from_message("# Plan\nbody")["title"] == "Plan"
    with pytest.raises(art_mod.ArtifactError):
        art_mod.from_message(text, 5)


@pytest.mark.parametrize("art,name,mime", [
    ({"title": "Plan", "type": "markdown", "content": "# x"}, "Plan.md", "text/markdown"),
    ({"title": "a/b:c", "type": "text", "content": "x"}, "abc.txt", "text/plain"),
    ({"title": "Tool", "type": "code", "language": "python", "content": "x=1"}, "Tool.py", "text/plain"),
    ({"title": "Tool", "type": "code", "language": "", "content": "x=1"}, "Tool.txt", "text/plain"),
    ({"title": "Page", "type": "html", "content": "<p>"}, "Page.html", "text/html"),
    ({"title": "Logo", "type": "svg", "content": "<svg/>"}, "Logo.svg", "image/svg+xml"),
    ({"title": "Data", "type": "json", "content": "{\"a\": 1}"}, "Data.json", "application/json"),
    ({"title": "con", "type": "csv", "content": "a,b"}, "_con.csv", "text/csv"),
])
def test_export_names_and_types(art, name, mime):
    got_name, got_mime, data = art_mod.export(art)
    assert (got_name, got_mime, data) == (name, mime, art["content"].encode())


def test_export_refuses_invalid_json():
    with pytest.raises(art_mod.ArtifactError, match="not valid JSON"):
        art_mod.export({"title": "x", "type": "json", "content": "{oops"})


def test_artifact_survives_its_chat_being_deleted():
    db = Storage(":memory:")
    p = db.upsert_project("C:/x", "x")
    t = db.create_task(p["id"], "t", "t", "ask")
    a = db.create_artifact("Saved", "markdown", "text", task_id=t["id"])
    db._exec("DELETE FROM messages WHERE task_id = ?", (t["id"],))
    db._exec("DELETE FROM tasks WHERE id = ?", (t["id"],))
    assert db.get_artifact(a["id"])["content"] == "text"


# ---------------------------------------------------------------- API

@pytest.fixture
def server(cfg, tmp_path):
    backends = {LOCAL: FakeBackend(LOCAL, ["# Plan\n\nStep one.\n\n```python\nprint(1)\n```"]),
                CLAUDE: FakeBackend(CLAUDE, available=False)}
    orc = Orchestrator(Storage(":memory:"), cfg, backends, EventBus(), backups_dir=tmp_path / "bk", synchronous=True)
    srv = Server(App(orc, [], save_config=False)).start()
    yield srv
    srv.stop()


def call(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{srv.port}", "X-Enoy-Token": srv.app.token})
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def test_api_project_and_artifact_flow(server):
    st, d = call(server, "POST", "/api/projects", {"name": "Shop", "description": "d", "instructions": "be nice"})
    assert st == 200
    pid = d["project"]["id"]
    assert call(server, "POST", "/api/projects", {"name": "Shop"})[0] == 400
    assert call(server, "GET", "/api/projects/99999/files")[0] == 404
    doc = base64.b64encode(b"Opening hours: 11 to 22").decode()
    st, d = call(server, "POST", f"/api/projects/{pid}/files", {"name": "hours.md", "data": doc})
    assert st == 200 and d["created"]
    fid = d["file"]["id"]
    assert call(server, "POST", f"/api/projects/{pid}/files", {"name": "x.pdf", "data": doc})[0] == 400
    other = call(server, "POST", "/api/projects", {"name": "Other"})[1]["project"]["id"]
    assert call(server, "GET", f"/api/projects/{other}/files/{fid}")[0] == 404  # No cross-project reads.
    assert call(server, "GET", f"/api/projects/{pid}/files/{fid}")[1]["file"]["content"] == "Opening hours: 11 to 22"

    st, d = call(server, "POST", "/api/tasks", {"project_id": pid, "objective": "Make a plan"})
    tid = d["task"]["id"]
    msg = [m for m in call(server, "GET", f"/api/tasks/{tid}")[1]["messages"] if m["role"] == "assistant"][0]
    calls_before = len(server.app.orc.backends[LOCAL].requests)
    st, d = call(server, "POST", "/api/artifacts/from-message", {"message_id": msg["id"]})
    assert st == 200 and d["created"] and d["artifact"]["content"] == msg["content"] and d["artifact"]["project_id"] == pid
    assert len(server.app.orc.backends[LOCAL].requests) == calls_before  # Saving made no model call.
    aid = d["artifact"]["id"]
    assert call(server, "POST", "/api/artifacts/from-message", {"message_id": msg["id"]})[1]["created"] is False
    code = call(server, "POST", "/api/artifacts/from-message", {"message_id": msg["id"], "block": 0})[1]["artifact"]
    assert code["type"] == "code" and code["content"] == "print(1)"

    st, d = call(server, "POST", f"/api/artifacts/{aid}/update", {"base_version": 1, "content": "edited", "title": "Plan v2"})
    assert st == 200 and d["artifact"]["version"] == 2
    assert call(server, "POST", f"/api/artifacts/{aid}/update", {"base_version": 1, "content": "stale"})[0] == 409
    assert call(server, "POST", f"/api/artifacts/{aid}/restore", {"version": 1, "base_version": 2})[1]["artifact"]["version"] == 3
    lib = call(server, "GET", "/api/artifacts")[1]["artifacts"]
    in_project = call(server, "GET", f"/api/artifacts?project_id={pid}")[1]["artifacts"]
    assert aid in [a["id"] for a in lib] and aid in [a["id"] for a in in_project]
    assert "content" not in lib[0]  # Lists carry metadata only.
    assert call(server, "POST", f"/api/artifacts/{aid}/project", {"project_id": None})[1]["artifact"]["project_id"] is None
    assert call(server, "GET", "/api/artifacts/424242")[0] == 404
    assert call(server, "POST", f"/api/artifacts/{aid}/delete", {})[0] == 400  # Needs confirmation.
    assert call(server, "POST", f"/api/artifacts/{aid}/delete", {"confirm": True})[0] == 200
    assert call(server, "GET", f"/api/artifacts/{aid}")[0] == 404

    assert call(server, "POST", f"/api/projects/{pid}/delete", {})[0] == 400
    st, d = call(server, "POST", f"/api/projects/{pid}/delete", {"confirm": True})
    assert st == 200 and d["result"]["chats_moved"] == 1
    assert call(server, "GET", f"/api/projects/{pid}")[0] == 404
