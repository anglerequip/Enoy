"""Build provider routes (T2.8): keys are never returned, logged or stored outside Credential Manager."""

import logging

import pytest

from conftest import FakeBackend
from enoy.backends.base import CLAUDE, CODEX
from enoy.orchestrator import EventBus, Orchestrator
from enoy.server import App, Server
from enoy.storage import Storage
from enoy.tools import keystore
from fakeapi import FakeAPI
from test_server import call

SECRET = "sk-SENTINEL-routes-0123456789"


@pytest.fixture
def vault(monkeypatch):
    store = {}
    monkeypatch.setattr(keystore, "_read", lambda target: store.get(target))
    monkeypatch.setattr(keystore, "_write", lambda target, blob: store.__setitem__(target, blob.decode()))
    monkeypatch.setattr(keystore, "_remove", lambda target: store.pop(target, None))
    return store


@pytest.fixture
def api():
    a = FakeAPI()
    yield a
    a.close()


@pytest.fixture
def server(cfg, tmp_path, vault, api):
    cfg["build"]["providers"]["deepseek"].update(enabled=True, base_url=api.url)
    backends = {CODEX: FakeBackend(CODEX), CLAUDE: FakeBackend(CLAUDE)}
    orc = Orchestrator(Storage(tmp_path / "enoy.db"), cfg, backends, EventBus(), backups_dir=tmp_path / "bk",
                       synchronous=True)
    srv = Server(App(orc, [], save_config=False)).start()
    yield srv
    srv.stop()


def test_provider_list_has_status_but_no_secrets(server):
    status, data = call(server, "GET", "/api/build/providers")
    assert status == 200
    rows = {p["id"]: p for p in data["providers"]}
    assert rows["deepseek"]["state"] == "no_key" and rows["ollama"]["local"]
    assert data["roles"]["planner"] == "claude-cli" and "spend" in data


def test_key_round_trip_never_echoes_the_key(server, vault, api, caplog):
    caplog.set_level(logging.DEBUG)
    responses = []
    responses.append(call(server, "POST", "/api/build/providers/deepseek/key", {"value": SECRET}))
    assert vault == {"Enoy/provider:deepseek": SECRET}
    api.add("/models", (200, {"data": [{"id": "deepseek-flash"}]}))
    responses.append(call(server, "POST", "/api/build/providers/deepseek/test", {}))
    responses.append(call(server, "GET", "/api/build/providers"))
    responses.append(call(server, "GET", "/api/config"))
    responses.append(call(server, "GET", "/api/activity"))
    assert responses[1][1]["ok"] and responses[1][1]["models"] == ["deepseek-flash"]
    assert api.requests[0]["headers"]["authorization"] == "Bearer " + SECRET  # Sent only to the provider.
    for status, body in responses:
        assert status == 200 and SECRET not in str(body)
    db = server.app.orc.db
    for table in ("activity", "kv", "model_calls", "transmissions", "events"):
        assert SECRET not in str(db._all(f"SELECT * FROM {table}"))
    assert SECRET not in caplog.text
    call(server, "POST", "/api/build/providers/deepseek/key", {"value": ""})
    assert vault == {}


def test_unknown_provider_and_bad_key_are_rejected(server):
    assert call(server, "POST", "/api/build/providers/ghost/key", {"value": SECRET})[0] == 404
    status, body = call(server, "POST", "/api/build/providers/deepseek/key", {"value": "x" * 3000})
    assert status == 400 and "x" * 50 not in str(body)


def test_paid_test_route_requires_confirmation(server, vault, api):
    call(server, "POST", "/api/build/providers/deepseek/key", {"value": SECRET})
    server.app.orc.cfg["build"]["models"].append({"id": "ds", "provider": "deepseek", "model": "deepseek-flash",
                                                  "roles": ["implementer"], "price_in": 0.5, "price_out": 2.0})
    assert call(server, "POST", "/api/build/providers/deepseek/paid-test", {"model": "ds"})[0] == 400
    assert api.requests == []


def test_roles_are_validated_and_saved(server):
    status, body = call(server, "POST", "/api/build/roles", {"roles": {"planner": "ghost"}})
    assert status == 400
    status, body = call(server, "POST", "/api/build/roles", {"roles": {"planner": "codex-cli"}})
    assert status == 200 and body["roles"]["planner"] == "codex-cli"
    assert server.app.orc.cfg["build"]["roles"]["planner"] == "codex-cli"
