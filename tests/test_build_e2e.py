"""Simulated end-to-end run of the Build workspace (T12.1). SIMULATED: the planner and reviewer are fake CLI
backends and the implementer is a fake OpenAI-compatible server reached through the real adapter. Real git,
real pytest, real SQLite and the real HTTP API are used.

Covers: register a project, plan, approve, planning.md, a test-first task, gate, merge, the next task in a fresh
context, a restart (new app on the same data) with settings and task state kept, an offline period and resume.
"""

import json

from conftest import FakeBackend, local_server
from enoy import config as config_mod
from enoy.backends.base import CLAUDE, CODEX
from enoy.devstore import DevStore
from enoy.orchestrator import EventBus, Orchestrator
from enoy.server import App, Server
from enoy.spend import Spend
from enoy.storage import Storage
from enoy.tools import keystore
from fakeapi import FakeAPI
from test_build_api import IMPLEMENTER, PLAN, TEST_FILE, call, git, oa, ok, repo, vault  # noqa: F401

SECRET = "sk-SENTINEL-e2e-0123456789"
DIV_FILE = "tests/test_divide.py"
DIV_TEST = "from calc import divide\n\n\ndef test_divide():\n    assert divide(6, 3) == 2\n"
T2 = dict(PLAN["tasks"][0], id="T2", title="Add divide", depends_on=["T1"], test_files=[DIV_FILE],
          spec="Add divide(a, b) to calc.py.", tests=[{"name": "test_divide", "file": DIV_FILE, "expect_failure": "ImportError"}],
          verify_commands=[f"python -m pytest -q {DIV_FILE}"], acceptance=["divide(6, 3) == 2"])
TWO_TASKS = dict(PLAN, tasks=[PLAN["tasks"][0], T2])
DIVIDE = [oa(calls=[("write_file", {"path": DIV_FILE, "content": DIV_TEST})]), oa("Tests written."),
          oa(calls=[("edit_file", {"path": "calc.py", "old_text": "    return a * b\n",
                                   "new_text": "    return a * b\n\n\ndef divide(a, b):\n    return a / b\n"})]),
          oa("Implemented divide.")]


def start(cfg, db_path, tmp_path):
    """One app launch on a data folder, as app.build() does it (startup recovery included)."""
    storage = Storage(db_path)
    Spend(lambda: cfg, storage).recover()
    DevStore(storage).recover()
    backends = {CLAUDE: FakeBackend(CLAUDE, ["```json\n" + json.dumps(TWO_TASKS) + "\n```"]),
                CODEX: FakeBackend(CODEX, ['```json\n{"verdict": "approve", "findings": []}\n```'] * 4)}
    orc = Orchestrator(storage, cfg, backends, EventBus(), backups_dir=tmp_path / "bk", synchronous=True)
    return Server(App(orc, [], save_config=True)).start()


def test_simulated_end_to_end_with_restart_and_offline(repo, vault, tmp_path):
    api = FakeAPI()
    db_path = tmp_path / "enoy.db"
    cfg = config_mod.validate({})[0]
    srv = start(cfg, db_path, tmp_path)
    try:
        # Settings through the UI's own API: provider on, key, model with prices, implementer role.
        providers = json.loads(json.dumps(cfg["build"]["providers"]))
        providers["lmstudio"] = local_server(api.url)
        models = cfg["build"]["models"] + [{"id": "ds", "provider": "lmstudio", "model": "qwen-coder",
                                             "roles": ["implementer"]}]
        ok(srv, "POST", "/api/config", {"config": {"build": {"providers": providers, "models": models}}})
        ok(srv, "POST", "/api/build/roles", {"roles": {"implementer": "ds"}})
        ok(srv, "POST", "/api/build/providers/lmstudio/key", {"value": SECRET})

        pid = ok(srv, "POST", "/api/projects/open", {"path": str(repo)})["project"]["id"]
        plan = ok(srv, "POST", "/api/build/plans", {"project_id": pid, "goal": "Add multiply and divide"})["plan"]
        ok(srv, "POST", f"/api/build/plans/{plan['id']}/approve", {"version": 1})
        assert "- [ ] **T1 Add multiply**" in (repo / "planning.md").read_text(encoding="utf-8")

        api.add("/chat/completions", *IMPLEMENTER)
        assert ok(srv, "POST", f"/api/build/plans/{plan['id']}/run", {"mode": "next"})["job"]["error"] == ""
        tasks = {t["task_key"]: t for t in ok(srv, "GET", f"/api/build/plans/{plan['id']}")["tasks"]}
        assert tasks["T1"]["status"] == "completed" and tasks["T2"]["status"] == "ready"
        assert tasks["T1"]["frozen"] == {TEST_FILE: tasks["T1"]["frozen"][TEST_FILE]}
        assert "[x] **T1 Add multiply**" in (repo / "planning.md").read_text(encoding="utf-8")
    finally:
        srv.stop()

    # Restart: a new app on the same data folder.
    saved_cfg = config_mod.load()[0]
    assert saved_cfg["build"]["roles"]["implementer"] == "ds" and saved_cfg["build"]["providers"]["lmstudio"]["enabled"]
    saved_cfg["build"]["providers"]["lmstudio"]["base_url"] = "http://127.0.0.1:9"  # The provider is unreachable.
    srv = start(saved_cfg, db_path, tmp_path)
    try:
        detail = ok(srv, "GET", f"/api/build/plans/{plan['id']}")
        assert {t["task_key"]: t["status"] for t in detail["tasks"]} == {"T1": "completed", "T2": "ready"}
        assert detail["plan"]["approved_version"] == 1

        job = ok(srv, "POST", f"/api/build/plans/{plan['id']}/run", {"mode": "all"})["job"]
        assert job["result"]["status"] == "offline"
        assert ok(srv, "GET", f"/api/build/plans/{plan['id']}")["tasks"][1]["status"] == "waiting_network"

        # The connection comes back.
        cfg2 = srv.app.orc.cfg
        providers = json.loads(json.dumps(cfg2["build"]["providers"]))
        providers["lmstudio"]["base_url"] = api.url
        ok(srv, "POST", "/api/config", {"config": {"build": {"providers": providers}}})
        api.add("/models", (200, {"data": [{"id": "deepseek-flash"}]}))
        api.add("/chat/completions", *DIVIDE)
        before = len(api.requests)
        job = ok(srv, "POST", f"/api/build/plans/{plan['id']}/run", {"mode": "all"})["job"]
        assert job["result"]["status"] == "completed", job
        first_t2 = next(r for r in api.requests[before:] if r["path"] == "/chat/completions")["body"]["messages"]
        assert len(first_t2) == 2 and "Your task T2" in first_t2[1]["content"]       # Fresh context.
        assert "Add multiply" in first_t2[1]["content"]                               # Dependency summary only.

        detail = ok(srv, "GET", f"/api/build/plans/{plan['id']}")
        assert detail["plan"]["status"] == "completed" and detail["changed_files"] == ["calc.py", DIV_FILE, TEST_FILE]
        branch = detail["plan"]["integration_branch"]
        assert "def divide" in git(repo, "show", f"{branch}:calc.py")
        assert "multiply" not in (repo / "calc.py").read_text()                       # User's branch untouched.
        assert SECRET not in json.dumps(detail) and SECRET not in json.dumps(ok(srv, "GET", "/api/build/monitor"))
    finally:
        srv.stop()
        api.close()
