"""Security hardening of the Build workspace (T11.1-T11.3): nothing secret leaves the PC through the new
provider paths, child processes get no secrets, every Build action is in Activity, and Stop kills commands."""

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from enoy import winproc
from enoy.devagent import Scope, scrub_worktree
from enoy.gitwork import _env as git_env
from test_build_api import (CLAUDE, IMPLEMENTER, MUL_TEST, PLAN, TEST_FILE, api, call, git, oa, ok, open_repo, repo,  # noqa: F401
                            server, vault)

ENV_SECRET = "SENTINEL-dotenv-7f3a9c"
PRIVATE_SECRET = "SENTINEL-private-41bd"


# T11.1 ----------------------------------------------------------------------------------------

def test_scrub_removes_secrets_and_private_files_from_a_worktree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / ".env").write_text("TOKEN=x")
    (tmp_path / "src" / "id.pem").write_text("key")
    (tmp_path / "src" / "notes.private.md").write_text("mine")
    (tmp_path / "src" / "app.py").write_text("print(1)")
    (tmp_path / ".env.example").write_text("TOKEN=")
    removed = scrub_worktree(tmp_path, ["*.private.md"])
    assert sorted(removed) == [".env", "src/id.pem", "src/notes.private.md"]
    assert (tmp_path / "src" / "app.py").exists() and (tmp_path / ".env.example").exists()


def test_private_globs_hide_files_from_the_implementer(tmp_path):
    (tmp_path / "keep.txt").write_text("ok")
    (tmp_path / "secret.cfg").write_text(PRIVATE_SECRET)
    scope = Scope(tmp_path, writable={"keep.txt"}, excluded_globs=["*.cfg"])
    with pytest.raises(Exception):
        scope.read("secret.cfg")
    assert "secret.cfg" not in scope.ws.index


def test_secrets_never_reach_the_implementer(server, repo, api):
    """A committed .env and a private file exist; the implementer tries to read them and even writes a
    test that prints .env. No request to the provider may contain either secret."""
    server.app.orc.cfg["privacy"]["cloud_excluded_globs"] = ["*.cfg"]
    (repo / ".env").write_text(f"PLAIN={ENV_SECRET}\n")
    (repo / "settings.cfg").write_text(PRIVATE_SECRET + "\n")
    git(repo, "add", "-A", "-f")
    git(repo, "commit", "-q", "-m", "secrets committed by mistake")
    # Prints at import time, before the import that fails in the test phase, so the leak path really runs.
    leaky_test = ("import pathlib\nfor name in ('.env', 'settings.cfg'):\n"
                  "    p = pathlib.Path(name)\n    print('LEAK', p.read_text() if p.exists() else 'missing')\n"
                  "from calc import multiply\n\n\ndef test_multiply():\n    assert multiply(2, 3) == 6\n")
    pid = open_repo(server, repo)
    plan = ok(server, "POST", "/api/build/plans", {"project_id": pid, "goal": "Add multiply"})["plan"]
    ok(server, "POST", f"/api/build/plans/{plan['id']}/approve", {"version": 1})
    api.add("/chat/completions",
            oa(calls=[("read_file", {"path": ".env"}), ("read_file", {"path": "settings.cfg"}),
                      ("search_code", {"query": "SENTINEL"}), ("write_file", {"path": TEST_FILE, "content": leaky_test})]),
            oa(calls=[("run_command", {"command": f"python -m pytest -q -s {TEST_FILE}"})]),
            oa("Tests written."), *IMPLEMENTER[2:])
    ok(server, "POST", f"/api/build/plans/{plan['id']}/run", {"mode": "all"})
    sent = json.dumps([r["body"] for r in api.requests])
    assert len(api.requests) >= 3
    assert ENV_SECRET not in sent and PRIVATE_SECRET not in sent
    # The user's own files are untouched; only the task's copy was scrubbed.
    assert ENV_SECRET in (repo / ".env").read_text() and PRIVATE_SECRET in (repo / "settings.cfg").read_text()


def test_every_api_request_is_recorded_with_what_was_sent(server, repo, api):
    pid = open_repo(server, repo)
    plan = ok(server, "POST", "/api/build/plans", {"project_id": pid, "goal": "Add multiply"})["plan"]
    ok(server, "POST", f"/api/build/plans/{plan['id']}/approve", {"version": 1})
    api.add("/chat/completions", *IMPLEMENTER)
    ok(server, "POST", f"/api/build/plans/{plan['id']}/run", {"mode": "all"})
    rows = server.app.orc.db._all("SELECT * FROM model_calls WHERE provider = 'lmstudio' ORDER BY id")
    assert len(rows) == len(api.requests) == 4
    for row, req in zip(rows, api.requests):
        assert row["chars"] == sum(len(m.get("content") or "") for m in req["body"]["messages"])
        assert row["content"] and "Your task T1" in row["content"]
    status, detail = call(server, "GET", f"/api/build/calls/{rows[0]['id']}")
    assert status == 200 and "Your task T1" in detail["content"]


# T11.2 ----------------------------------------------------------------------------------------

def test_child_processes_get_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-SENTINEL-env-1")
    monkeypatch.setenv("MY_SERVICE_TOKEN", "SENTINEL-env-2")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    (tmp_path / "printenv.py").write_text("import os\nprint(sorted(os.environ.items()))\n")
    scope = Scope(tmp_path, writable=set(), commands=["python printenv.py"])
    out = scope.run("python printenv.py")["output"]
    assert "printenv" not in out and "PATH" in out.upper()
    assert "SENTINEL-env-1" not in out and "SENTINEL-env-2" not in out
    env = git_env()
    assert "DEEPSEEK_API_KEY" not in env and "MY_SERVICE_TOKEN" not in env and "GIT_DIR" not in env


# T11.3 ----------------------------------------------------------------------------------------

def test_build_actions_are_in_activity(server, repo, api):
    pid = open_repo(server, repo)
    plan = ok(server, "POST", "/api/build/plans", {"project_id": pid, "goal": "Add multiply"})["plan"]
    ok(server, "POST", f"/api/build/plans/{plan['id']}/approve", {"version": 1})
    api.add("/chat/completions", *IMPLEMENTER)
    ok(server, "POST", f"/api/build/plans/{plan['id']}/run", {"mode": "all"})
    ok(server, "POST", f"/api/build/plans/{plan['id']}/merge", {"confirm": True})
    texts = [r["text"] for r in server.app.orc.db._all("SELECT text FROM activity WHERE task_id IS NULL ORDER BY id")]
    joined = "\n".join(texts)
    for expected in ("version 1 is ready", "approved plan", "T1 attempt 1 started", "T1 tests frozen",
                     "T1 checks passed", "T1 completed", "Merge of plan"):
        assert expected.lower() in joined.lower(), expected


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process tree check")
def test_stop_kills_the_whole_command_tree(tmp_path):
    pidfile = tmp_path / "child.pid"
    (tmp_path / "parent.py").write_text(
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open(r'{pidfile}', 'w').write(str(p.pid))\n"
        "time.sleep(60)\n")
    cancel = threading.Event()
    scope = Scope(tmp_path, writable=set(), commands=["python parent.py"], cancel=cancel)
    result = {}
    t = threading.Thread(target=lambda: result.update(scope.run("python parent.py")))
    start = time.monotonic()
    t.start()
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        time.sleep(0.1)
    child = int(pidfile.read_text())
    cancel.set()
    t.join(15)
    assert result["status"] == "cancelled" and time.monotonic() - start < 20
    time.sleep(0.5)
    assert child not in {p.pid for p in winproc.list_processes()}
