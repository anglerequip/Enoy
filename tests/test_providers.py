"""Provider service (T2.6) and the spend ledger (T2.7). Only a local fake server is contacted.

Build providers must run on this PC, so they cost nothing; the ledger's limits are a second safety net."""

import time

import pytest

from enoy import config as config_mod
from enoy.backends.base import BackendError, ChatMessage
from enoy.providers import Providers
from enoy.spend import Spend, SpendLimitError
from enoy.storage import Storage
from enoy.tools import keystore
from conftest import local_server
from fakeapi import FakeAPI

SECRET = "sk-SENTINEL-providers-0123456789"


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
def env(api, vault, tmp_path):
    cfg = config_mod.validate({})[0]
    cfg["build"]["providers"]["lmstudio"] = local_server(api.url)
    cfg["build"]["models"].append({"id": "ds", "provider": "lmstudio", "model": "deepseek-flash",
                                   "roles": ["implementer"], "price_in": 0.5, "price_cached": 0.1, "price_out": 2.0})
    db = Storage(tmp_path / "enoy.db")
    return cfg, db, Providers(lambda: cfg, db)


def test_status_needs_no_key_and_runs_on_this_pc(env):
    cfg, db, prov = env
    s = prov.status("lmstudio")
    assert s["state"] == "ready" and s["local"] and not s["key_set"] and not s["verified"]
    keystore.put("provider:lmstudio", SECRET)  # A local server may ask for a key; it stays optional.
    assert prov.status("lmstudio")["key_set"] and SECRET not in repr(prov.status("lmstudio"))
    cfg["build"]["providers"]["lmstudio"]["enabled"] = False
    assert prov.status("lmstudio")["state"] == "disabled"
    assert prov.status("ollama")["local"]


def test_connection_test_lists_models_and_costs_nothing(env, api):
    cfg, db, prov = env
    api.add("/models", (200, {"data": [{"id": "qwen-coder"}, {"id": "qwen-coder-large"}]}))
    result = prov.test("lmstudio")
    assert result["ok"] and result["models"] == ["qwen-coder", "qwen-coder-large"]
    assert [r["path"] for r in api.requests] == ["/models"]
    assert db._all("SELECT * FROM model_calls") == []
    assert not prov.status("lmstudio")["verified"]  # Listing models is not a real model request.
    assert prov.status("lmstudio")["models"] == ["qwen-coder", "qwen-coder-large"]


def test_offline_shows_no_connection(env):
    cfg, db, prov = env
    cfg["build"]["providers"]["lmstudio"]["base_url"] = "http://127.0.0.1:9"
    result = prov.test("lmstudio")
    assert not result["ok"] and result["kind"] == "unavailable"
    assert prov.status("lmstudio")["state"] == "no_connection"


def test_a_real_request_marks_verified_and_costs_nothing(env, api):
    cfg, db, prov = env
    api.add("/chat/completions", (200, {"model": "qwen-coder", "choices": [{"message": {"content": "OK"}}],
                                        "usage": {"prompt_tokens": 12, "completion_tokens": 1}}))
    assert prov.complete("ds", [ChatMessage("user", "Say OK")], max_output=16).text == "OK"
    assert prov.status("lmstudio")["verified"]
    call = db._one("SELECT * FROM model_calls")
    assert call["status"] == "done" and call["input_tokens"] == 12
    assert call["cost_usd"] == 0.0 and call["cost_source"] == "local"


def test_failed_request_does_not_mark_verified(env, api):
    cfg, db, prov = env
    api.add("/chat/completions", (401, {"error": {"message": "invalid key"}}))
    with pytest.raises(BackendError) as exc:
        prov.complete("ds", [ChatMessage("user", "Say OK")], max_output=16)
    assert exc.value.kind == "auth" and not prov.status("lmstudio")["verified"]


@pytest.mark.parametrize("url", ["https://api.deepseek.com", "https://127.0.0.1.example.com/v1",
                                 "http://127.0.0.1@api.example.com/v1", "not an address"])
def test_a_provider_off_this_pc_is_refused_before_anything_is_sent(env, api, url):
    cfg, db, prov = env
    cfg["build"]["providers"]["lmstudio"]["base_url"] = url  # As if a settings check had been bypassed.
    result = prov.test("lmstudio")
    assert not result["ok"] and result["kind"] == "budget" and "this PC" in result["error"]
    with pytest.raises(BackendError) as exc:
        prov.complete("ds", [ChatMessage("user", "Hi")], max_output=16)
    assert exc.value.kind == "budget"
    assert api.requests == [] and db._all("SELECT * FROM model_calls") == []


def test_concurrency_limit_is_per_provider(env):
    cfg, db, prov = env
    cfg["build"]["providers"]["lmstudio"]["max_concurrency"] = 1
    sem = prov.slot("lmstudio")
    assert sem.acquire(timeout=0.1) and not sem.acquire(timeout=0.1)
    sem.release()


# Spend ledger --------------------------------------------------------------------------
# Every Build provider runs on this PC and costs nothing. These tests mark one as remote, which
# settings never allow, to check that the ledger's limits still hold as a second safety net.

@pytest.fixture
def ledger(env):
    cfg, db, prov = env
    cfg["build"]["providers"]["lmstudio"].update(local=False, daily_limit_usd=1.0, monthly_limit_usd=10.0)
    return env


def model(cfg, mid="ds"):
    return next(m for m in cfg["build"]["models"] if m["id"] == mid)


def test_reservation_is_refused_when_it_would_cross_the_daily_cap(ledger):
    cfg, db, prov = ledger
    spend = Spend(lambda: cfg, db)
    cfg["build"]["providers"]["lmstudio"]["daily_limit_usd"] = 0.01
    # 1,000 prompt tokens at $0.5/M + 4,000 output at $2/M = $0.0085: fits.
    first = spend.reserve(model(cfg), prompt_tokens=1000, max_output=4000)
    with pytest.raises(SpendLimitError, match="daily"):
        spend.reserve(model(cfg), prompt_tokens=1000, max_output=4000)
    spend.finish(first, {"input_tokens": 1000, "cached_tokens": 0, "output_tokens": 100})
    row = db._one("SELECT * FROM model_calls WHERE id = ?", (first,))
    assert row["status"] == "done" and abs(row["cost_usd"] - 0.0007) < 1e-9
    spend.reserve(model(cfg), prompt_tokens=1000, max_output=4000)  # Actual cost freed the room.


def test_monthly_cap_counts_earlier_days(ledger):
    cfg, db, prov = ledger
    spend = Spend(lambda: cfg, db)
    cfg["build"]["providers"]["lmstudio"].update(daily_limit_usd=0, monthly_limit_usd=0.02)
    old = spend.reserve(model(cfg), prompt_tokens=1000, max_output=1000)
    spend.finish(old, {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 9000})  # $0.018
    db._exec("UPDATE model_calls SET created = ? WHERE id = ?", (spend.month_start() + 1, old))
    with pytest.raises(SpendLimitError, match="monthly"):
        spend.reserve(model(cfg), prompt_tokens=1000, max_output=1000)


def test_unknown_price_blocks_requests_when_a_cap_is_set(ledger):
    cfg, db, prov = ledger
    spend = Spend(lambda: cfg, db)
    m = dict(model(cfg), price_in=None)
    with pytest.raises(SpendLimitError, match="price"):
        spend.reserve(m, prompt_tokens=10, max_output=10)
    cfg["build"]["providers"]["lmstudio"].update(daily_limit_usd=0, monthly_limit_usd=0)
    call = spend.reserve(m, prompt_tokens=10, max_output=10)
    spend.finish(call, {"input_tokens": 10, "cached_tokens": 0, "output_tokens": 10})
    row = db._one("SELECT * FROM model_calls WHERE id = ?", (call,))
    assert row["cost_usd"] is None and row["cost_source"] == "unknown"


def test_provider_reported_cost_wins_and_local_is_free(ledger):
    cfg, db, prov = ledger
    spend = Spend(lambda: cfg, db)
    call = spend.reserve(model(cfg), prompt_tokens=10, max_output=10)
    spend.finish(call, {"input_tokens": 10, "cached_tokens": 0, "output_tokens": 10}, provider_cost=0.123)
    assert db._one("SELECT cost_usd, cost_source FROM model_calls WHERE id = ?", (call,)) == \
        {"cost_usd": 0.123, "cost_source": "provider"}
    cfg["build"]["models"].append({"id": "local", "provider": "ollama", "model": "qwen3:8b", "roles": ["implementer"]})
    call = spend.reserve(model(cfg, "local"), prompt_tokens=10 ** 6, max_output=10 ** 6)
    spend.finish(call, {"input_tokens": 10 ** 6, "cached_tokens": 0, "output_tokens": 10})
    assert db._one("SELECT cost_usd, cost_source FROM model_calls WHERE id = ?", (call,)) == \
        {"cost_usd": 0.0, "cost_source": "local"}


def test_failed_request_without_usage_costs_nothing_and_totals_add_up(ledger):
    cfg, db, prov = ledger
    spend = Spend(lambda: cfg, db)
    call = spend.reserve(model(cfg), prompt_tokens=1000, max_output=1000)
    spend.fail(call, "timeout")
    assert db._one("SELECT status, cost_usd FROM model_calls WHERE id = ?", (call,)) == {"status": "failed", "cost_usd": 0.0}
    t = spend.totals("lmstudio")
    assert t == {"today": 0.0, "month": 0.0, "daily_limit": 1.0, "monthly_limit": 10.0, "reserved": 0.0}


def test_recover_keeps_interrupted_reservations_counted(ledger):
    cfg, db, prov = ledger
    spend = Spend(lambda: cfg, db)
    call = spend.reserve(model(cfg), prompt_tokens=1000, max_output=1000)
    assert spend.recover() == 1
    row = db._one("SELECT * FROM model_calls WHERE id = ?", (call,))
    assert row["status"] == "interrupted" and row["cost_usd"] == row["reserved_usd"] > 0


def test_app_startup_recovers_spend_reservations(monkeypatch):
    from enoy import app as app_mod
    calls = []
    monkeypatch.setattr(app_mod.Spend, "recover", lambda self: calls.append(True) or 0)
    app_mod.build(synchronous=True, save_config=False)
    assert calls == [True]
