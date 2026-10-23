import sys
import threading
import time

import pytest

from fusion_ai import commands
from fusion_ai.config import DEFAULT_ALLOWLIST


@pytest.mark.parametrize("command,level", [
    ("python -m pytest -q", "allowlisted"),
    ("pytest tests", "allowlisted"),
    ("git status", "allowlisted"),
    ("npm test", "allowlisted"),
    ("python script.py", "approval"),
    ("node build.js", "approval"),
    ("pip install requests", "high_risk"),
    ("python -m pip install requests", "high_risk"),
    ("npm install left-pad", "high_risk"),
    ("git push origin main", "high_risk"),
    ("git reset --hard", "high_risk"),
    ("curl https://example.com", "high_risk"),
    ("del calculator.py", "high_risk"),
    ("powershell -Command Get-Process", "high_risk"),
    ("python -m pytest C:\\Windows", "high_risk"),
    ("format C:", "blocked"),
    ("reg add HKCU\\Software\\X", "blocked"),
    ("runas /user:admin cmd", "blocked"),
    ("type .env", "blocked"),
    ("pytest && del x", "blocked"),
    ("pytest | more", "blocked"),
    ("pytest > out.txt", "blocked"),
    ("echo %USERPROFILE%", "blocked"),
    ("powershell -EncodedCommand AAAA", "blocked"),
    ("", "blocked"),
])
def test_policy_levels(command, level):
    assert commands.classify(command, DEFAULT_ALLOWLIST).level == level, command


def test_run_captures_output_and_exit_code(tmp_path):
    ok = commands.run([sys.executable, "-c", "print('hello'); import sys; print('warn', file=sys.stderr)"], tmp_path, 30)
    assert ok.status == "passed" and ok.exit_code == 0
    assert "hello" in ok.stdout and "warn" in ok.stderr and ok.duration_s > 0
    bad = commands.run([sys.executable, "-c", "raise SystemExit(3)"], tmp_path, 30)
    assert bad.status == "failed" and bad.exit_code == 3


def test_run_timeout(tmp_path):
    start = time.monotonic()
    r = commands.run([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 1)
    assert r.status == "timeout" and r.exit_code is None
    assert time.monotonic() - start < 15
    assert "stopped the command" in r.stderr


def test_run_cancel(tmp_path):
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    r = commands.run([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 60, cancel)
    assert r.status == "cancelled"


def test_missing_program(tmp_path):
    r = commands.run(["definitely-not-a-program-xyz"], tmp_path, 10)
    assert r.status == "error" and "Could not start" in r.stderr


def test_secret_env_vars_are_not_passed(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "abc123secret")
    monkeypatch.setenv("HARMLESS_SETTING", "visible")
    r = commands.run([sys.executable, "-c", "import os; print(os.environ.get('MY_API_KEY')); print(os.environ.get('HARMLESS_SETTING'))"], tmp_path, 30)
    assert "abc123secret" not in r.stdout
    assert "None" in r.stdout and "visible" in r.stdout


def test_project_venv_python_is_preferred(tmp_path):
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_bytes(b"")
    assert commands.resolve_program("python", tmp_path) == str(scripts / "python.exe")
    assert commands.resolve_program("git", tmp_path) != str(scripts / "git.exe")
    other = tmp_path / "no_venv"
    other.mkdir()
    assert "no_venv" not in commands.resolve_program("python", other)


def test_output_is_redacted(tmp_path):
    r = commands.run([sys.executable, "-c", "print('token ghp_abcdefghijklmnopqrstuvwxyz0123456789')"], tmp_path, 30)
    assert "ghp_" not in r.stdout and "[REDACTED]" in r.stdout
