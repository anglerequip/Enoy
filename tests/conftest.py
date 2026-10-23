"""Shared fixtures. Every test gets its own app-data folder and project copy."""

from __future__ import annotations

import shutil
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fusion_ai import config as config_mod  # noqa: E402
from fusion_ai.backends.base import CLAUDE, LOCAL, BackendError, BackendStatus, ChatResult  # noqa: E402
from fusion_ai.orchestrator import EventBus, Orchestrator  # noqa: E402
from fusion_ai.storage import Storage  # noqa: E402

FIXTURE = ROOT / "fixtures" / "sample_project"


@pytest.fixture(autouse=True)
def app_home(tmp_path, monkeypatch):
    home = tmp_path / "apphome"
    monkeypatch.setenv("FUSION_AI_HOME", str(home))
    return home


@pytest.fixture
def project(tmp_path) -> Path:
    dest = tmp_path / "project"
    shutil.copytree(FIXTURE, dest)
    return dest


class FakeBackend:
    """Scripted backend. Each reply is a string, a BackendError, or a callable(request)."""

    def __init__(self, name: str, replies=None, available: bool = True):
        self.name = name
        self.replies = list(replies or [])
        self.available = available
        self.requests = []
        self.verified = False
        self.settings = {"model": "fake", "enabled": True}

    def status(self):
        return BackendStatus(self.name, self.available, "fake backend" if self.available else "fake backend offline")

    def chat(self, request):
        self.requests.append(request)
        if not self.replies:
            raise BackendError("failed", "FakeBackend ran out of scripted replies")
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply(request)
        if isinstance(reply, BackendError):
            raise reply
        if isinstance(reply, ChatResult):  # A scripted tool call or a full result.
            if reply.text and request.on_token:
                request.on_token(reply.text)
            return reply
        if request.on_token:
            request.on_token(reply)
        return ChatResult(text=reply, backend=self.name, model="fake", session_id="sess-1" if self.name == CLAUDE else None)


@pytest.fixture
def cfg():
    c, _ = config_mod.validate({})
    c["claude"]["confirm_before_send"] = False
    c["lead"]["enabled"] = False  # Tests of Claude as the lead turn it on themselves (test_lead.py).
    return c


def make_orchestrator(cfg, qwen_replies=None, claude_replies=None, claude_available=True, tmp=None):
    storage = Storage(":memory:")
    backends = {
        LOCAL: FakeBackend(LOCAL, qwen_replies),
        CLAUDE: FakeBackend(CLAUDE, claude_replies, available=claude_available),
    }
    orc = Orchestrator(storage, cfg, backends, EventBus(), backups_dir=tmp, synchronous=True)
    return orc


@pytest.fixture
def orc_factory(cfg, tmp_path):
    def factory(qwen=None, claude=None, claude_available=True, **overrides):
        for dotted, value in overrides.items():
            section, key = dotted.split("__")
            cfg[section][key] = value
        return make_orchestrator(cfg, qwen, claude, claude_available, tmp_path / "backups")
    return factory


def run_in_thread(fn, *args):
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()
    return t
