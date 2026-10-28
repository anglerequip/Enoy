"""Build workspace API (Phase 9 back end): projects, plans, approval, runs, merge and monitoring.

The planner and reviewer are fake CLI backends. The implementer is a fake OpenAI-compatible server reached
through the real adapter, spending ledger and tool loop. Real git and pytest run in a temporary repository.
"""

import json
import shutil
import subprocess

import pytest

from conftest import FakeBackend
from enoy.backends.base import CLAUDE, CODEX
from enoy.orchestrator import EventBus, Orchestrator
from enoy.server import App, Server
from enoy.storage import Storage
from enoy.tools import keystore
from fakeapi import FakeAPI
from test_server import call

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
SECRET = "sk-SENTINEL-build-api-0123456789"
TEST_FILE = "tests/test_multiply.py"
MUL_TEST = "from calc import multiply\n\n\ndef test_multiply():\n    assert multiply(2, 3) == 6\n"
PLAN = {"summary": "Add multiply.", "architecture": "One function in calc.py.", "risks": [], "questions": [],
        "tasks": [{"id": "T1", "title": "Add multiply", "objective": "multiply(a, b) returns a * b.", "depends_on": [],
                   "allowed_files": ["calc.py"], "test_files": [TEST_FILE], "spec": "Add multiply(a, b) to calc.py.",
                   "inputs_outputs": "numbers", "edge_cases": [], "tests": [
                       {"name": "test_multiply", "file": TEST_FILE, "expect_failure": "ImportError"}],
                   "verify_commands": [f"python -m pytest -q {TEST_FILE}"], "regression_commands": [],
                   "acceptance": ["multiply(2, 3) == 6"], "completion": "tests pass", "blockers": []}]}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def oa(content=None, calls=()):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"id": f"call_{i}", "type": "function",
                              "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]
    return (200, {"model": "deepseek-flash", "choices": [{"message": msg, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 1000, "completion_tokens": 50}})


IMPLEMENTER = [
    oa(calls=[("write_file", {"path": TEST_FILE, "content": MUL_TEST})]), oa("Tests written."),
    oa(calls=[("edit_file", {"path": "calc.py", "old_text": "    return a + b\n",
                             "new_text": "    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"})]),
    oa("Implemented multiply."),
]


@pytest.fixture
def vault(monkeypatch):
    store = {}
    monkeypatch.setattr(keystore, "_read", lambda target: store.get(target))
    monkeypatch.setattr(keystore, "_write", lambda target, blob: store.__setitem__(target, blob.decode()))
    monkeypatch.setattr(keystore, "_remove", lambda target: store.pop(target, None))
    return store


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "calc"
    (r / "tests").mkdir(parents=True)
    git(tmp_path, "init", "-q", "-b", "main", str(r))
    git(r, "config", "user.name", "Test")
    git(r, "config", "user.email", "t@example.com")
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (r / "tests" / "test_add.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    (r / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    (r / "CLAUDE.md").write_text("Keep functions small.\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "initial")
    return r


@pytest.fixture
def api():
    a = FakeAPI()
    yield a
    a.close()


@pytest.fixture
def server(cfg, tmp_path, vault, api):
    cfg["build"]["providers"]["deepseek"].update(enabled=True, base_url=api.url)
    cfg["build"]["models"].append({"id": "ds", "provider": "deepseek", "model": "deepseek-flash",
                                   "roles": ["implementer"], "price_in": 0.5, "price_out": 2.0})
    cfg["build"]["roles"]["implementer"] = "ds"
    keystore.put("provider:deepseek", SECRET)
    plan_reply = "```json\n" + json.dumps(PLAN) + "\n```"
    backends = {CLAUDE: FakeBackend(CLAUDE, [plan_reply]),
                CODEX: FakeBackend(CODEX, ['```json\n{"verdict": "approve", "findings": []}\n```'])}
    orc = Orchestrator(Storage(tmp_path / "enoy.db"), cfg, backends, EventBus(), backups_dir=tmp_path / "bk",
                       synchronous=True)
    srv = Server(App(orc, [], save_config=False)).start()
    yield srv
    srv.stop()


def open_repo(server, repo):
    status, data = call(server, "POST", "/api/projects/open", {"path": str(repo)})
    assert status == 200
    return data["project"]["id"]


def ok(server, method, path, body=None):
    status, data = call(server, method, path, body)
    assert status == 200, data
    return data


def test_projects_show_git_and_instructions(server, repo):
    pid = open_repo(server, repo)
    rows = {p["id"]: p for p in ok(server, "GET", "/api/build/projects")["projects"]}
    p = rows[pid]
    assert p["git"] and p["branch"] == "main" and p["uncommitted"] == 0
    assert p["instructions"] == ["CLAUDE.md"] and "python -m pytest -q" in p["test_commands"]


def test_full_flow_plan_approve_run_monitor_merge(server, repo, api):
    pid = open_repo(server, repo)
    plan = ok(server, "POST", "/api/build/plans", {"project_id": pid, "goal": "Add multiply"})["plan"]
    detail = ok(server, "GET", f"/api/build/plans/{plan['id']}")
    assert detail["versions"][0]["body"] == PLAN and detail["job"]["error"] == ""
    assert detail["roles"]["planner"]["id"] == "claude-cli" and detail["roles"]["implementer"]["local"] is False

    ok(server, "POST", f"/api/build/plans/{plan['id']}/approve", {"version": 1})
    assert "T1 Add multiply" in (repo / "planning.md").read_text(encoding="utf-8")

    api.add("/chat/completions", *IMPLEMENTER)
    run = ok(server, "POST", f"/api/build/plans/{plan['id']}/run", {"mode": "all"})
    assert run["job"]["result"]["status"] == "completed", run
    detail = ok(server, "GET", f"/api/build/plans/{plan['id']}")
    task = detail["tasks"][0]
    assert task["status"] == "completed" and task["evidence"]["review"] == "approved by codex-cli"
    assert task["runs"][-1]["impl_commit"] and detail["plan"]["status"] == "completed"
    assert detail["changed_files"] == ["calc.py", TEST_FILE]
    assert "[x] **T1 Add multiply**" in (repo / "planning.md").read_text(encoding="utf-8")
    assert "multiply" not in (repo / "calc.py").read_text()  # Not merged into the user's branch yet.

    mon = ok(server, "GET", "/api/build/monitor")
    calls = [c for c in mon["calls"] if c["provider"] == "deepseek"]
    assert len(calls) == 4 and all(c["status"] == "done" for c in calls)
    assert mon["spend"]["deepseek"]["today"] == pytest.approx(4 * (1000 * 0.5 + 50 * 2.0) / 1e6)
    assert any(r["status"] == "completed" for r in mon["runs"])

    assert call(server, "POST", f"/api/build/plans/{plan['id']}/merge", {})[0] == 400
    merged = ok(server, "POST", f"/api/build/plans/{plan['id']}/merge", {"confirm": True})
    assert merged["ok"] and "def multiply" in (repo / "calc.py").read_text()

    for status, body in [call(server, "GET", f"/api/build/plans/{plan['id']}"), call(server, "GET", "/api/build/monitor")]:
        assert SECRET not in json.dumps(body)


def test_planner_failure_is_shown_and_nothing_is_stored(server, repo):
    server.app.orc.backends[CLAUDE].replies = ["no plan here", "still no plan"]
    pid = open_repo(server, repo)
    plan = ok(server, "POST", "/api/build/plans", {"project_id": pid, "goal": "Add multiply"})["plan"]
    detail = ok(server, "GET", f"/api/build/plans/{plan['id']}")
    assert detail["versions"] == [] and "JSON" in detail["job"]["error"]


def test_runs_need_a_suitable_implementer(server, repo):
    pid = open_repo(server, repo)
    plan = ok(server, "POST", "/api/build/plans", {"project_id": pid, "goal": "Add multiply"})["plan"]
    ok(server, "POST", f"/api/build/plans/{plan['id']}/approve", {"version": 1})
    server.app.orc.cfg["build"]["roles"]["implementer"] = ""
    status, body = call(server, "POST", f"/api/build/plans/{plan['id']}/run", {"mode": "all"})
    assert status == 400 and "implementer" in body["error"].lower()
    server.app.orc.cfg["build"]["models"][0]["roles"] = ["planner", "reviewer", "implementer"]
    server.app.orc.cfg["build"]["roles"]["implementer"] = "claude-cli"
    status, body = call(server, "POST", f"/api/build/plans/{plan['id']}/run", {"mode": "all"})
    assert status == 400 and "API or local" in body["error"]


def test_local_only_project_refuses_a_cloud_implementer(server, repo):
    pid = open_repo(server, repo)
    plan = ok(server, "POST", "/api/build/plans", {"project_id": pid, "goal": "Add multiply"})["plan"]
    ok(server, "POST", f"/api/build/plans/{plan['id']}/approve", {"version": 1})
    server.app.orc.set_project_local_only(pid, True)
    status, body = call(server, "POST", f"/api/build/plans/{plan['id']}/run", {"mode": "all"})
    assert status == 400 and "local" in body["error"]


def test_retry_requeues_a_failed_task(server, repo, api):
    pid = open_repo(server, repo)
    plan = ok(server, "POST", "/api/build/plans", {"project_id": pid, "goal": "Add multiply"})["plan"]
    ok(server, "POST", f"/api/build/plans/{plan['id']}/approve", {"version": 1})
    api.add("/chat/completions", (401, {"error": {"message": "bad key"}}))
    ok(server, "POST", f"/api/build/plans/{plan['id']}/run", {"mode": "all"})
    task = ok(server, "GET", f"/api/build/plans/{plan['id']}")["tasks"][0]
    assert task["status"] in ("failed", "blocked")
    ok(server, "POST", f"/api/build/tasks/{task['id']}/retry", {})
    assert ok(server, "GET", f"/api/build/plans/{plan['id']}")["tasks"][0]["status"] == "ready"


def test_git_init_needs_confirmation(server, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.py").write_text("print(1)\n")
    pid = open_repo(server, plain)
    assert call(server, "POST", f"/api/build/projects/{pid}/git-init", {})[0] == 400
    ok(server, "POST", f"/api/build/projects/{pid}/git-init", {"confirm": True})
    assert (plain / ".git").exists()


def test_parallel_mode_is_refused_while_off(server, repo):
    pid = open_repo(server, repo)
    plan = ok(server, "POST", "/api/build/plans", {"project_id": pid, "goal": "Add multiply"})["plan"]
    ok(server, "POST", f"/api/build/plans/{plan['id']}/approve", {"version": 1})
    status, body = call(server, "POST", f"/api/build/plans/{plan['id']}/run", {"mode": "parallel"})
    assert status == 400 and "off" in body["error"]


def test_claude_md_draft_and_save_with_confirmation(server, repo):
    server.app.orc.backends[CLAUDE].replies = ["```markdown\n# Rules\n\n- Run pytest.\n```"]
    pid = open_repo(server, repo)
    draft = ok(server, "POST", f"/api/build/projects/{pid}/instructions/draft", {})
    assert draft == {"text": "# Rules\n\n- Run pytest.\n", "exists": True}
    status, body = call(server, "POST", f"/api/build/projects/{pid}/instructions", {"text": draft["text"]})
    assert status == 400 and "Confirm" in body["error"]
    assert (repo / "CLAUDE.md").read_text() == "Keep functions small.\n"
    saved = ok(server, "POST", f"/api/build/projects/{pid}/instructions", {"text": draft["text"], "confirm": True})
    assert saved["backup"] and (repo / "CLAUDE.md").read_text() == draft["text"]
