"""Provider service (T2.6) and spend limits (T2.7). Only a local fake server is contacted."""

import time

import pytest

from enoy import config as config_mod
from enoy.backends.base import BackendError
from enoy.providers import Providers
from enoy.spend import Spend, SpendLimitError
from enoy.storage import Storage
from enoy.tools import keystore
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
    cfg["build"]["providers"]["deepseek"].update(enabled=True, base_url=api.url)
    cfg["build"]["models"].append({"id": "ds", "provider": "deepseek", "model": "deepseek-flash",
                                   "roles": ["implementer"], "price_in": 0.5, "price_cached": 0.1, "price_out": 2.0})
    db = Storage(tmp_path / "enoy.db")
    return cfg, db, Providers(lambda: cfg, db)


def test_status_reports_missing_key_then_ready(env):
    cfg, db, prov = env
    assert prov.status("deepseek")["state"] == "no_key"
    keystore.put("provider:deepseek", SECRET)
    s = prov.status("deepseek")
    assert s["state"] == "ready" and s["key_set"] and not s["local"] and not s["verified"]
    assert SECRET not in repr(s)
    cfg["build"]["providers"]["fireworks"]["enabled"] = False
    assert prov.status("fireworks")["state"] == "disabled"
    assert prov.status("ollama")["local"]


def test_connection_test_lists_models_and_costs_nothing(env, api):
    cfg, db, prov = env
    keystore.put("provider:deepseek", SECRET)
    api.add("/models", (200, {"data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}]}))
    result = prov.test("deepseek")
    assert result["ok"] and result["models"] == ["deepseek-flash", "deepseek-v4-pro"]
    assert [r["path"] for r in api.requests] == ["/models"]
    assert db._all("SELECT * FROM model_calls") == []
    assert not prov.status("deepseek")["verified"]  # Listing models is not a real model request.
    assert prov.status("deepseek")["models"] == ["deepseek-flash", "deepseek-v4-pro"]


def test_offline_shows_no_connection(env):
    cfg, db, prov = env
    keystore.put("provider:deepseek", SECRET)
    cfg["build"]["providers"]["deepseek"]["base_url"] = "http://127.0.0.1:9"
    result = prov.test("deepseek")
    assert not result["ok"] and result["kind"] == "unavailable"
    assert prov.status("deepseek")["state"] == "no_connection"


def test_paid_test_needs_confirmation_and_marks_verified(env, api):
    cfg, db, prov = env
    keystore.put("provider:deepseek", SECRET)
    with pytest.raises(PermissionError):
        prov.paid_test("ds")
    assert api.requests == []
    api.add("/chat/completions", (200, {"model": "deepseek-flash", "choices": [{"message": {"content": "OK"}}],
                                        "usage": {"prompt_tokens": 12, "completion_tokens": 1}}))
    result = prov.paid_test("ds", confirm=True)
    assert result["ok"] and result["reply"] == "OK"
    assert prov.status("deepseek")["verified"]
    call = db._one("SELECT * FROM model_calls")
    assert call["status"] == "done" and call["input_tokens"] == 12 and call["cost_source"] == "estimate"


def test_failed_paid_test_does_not_mark_verified(env, api):
    cfg, db, prov = env
    keystore.put("provider:deepseek", SECRET)
    api.add("/chat/completions", (401, {"error": {"message": "invalid key"}}))
    result = prov.paid_test("ds", confirm=True)
    assert not result["ok"] and result["kind"] == "auth"
    assert not prov.status("deepseek")["verified"]


def test_concurrency_limit_is_per_provider(env):
    cfg, db, prov = env
    cfg["build"]["providers"]["deepseek"]["max_concurrency"] = 1
    sem = prov.slot("deepseek")
    assert sem.acquire(timeout=0.1) and not sem.acquire(timeout=0.1)
    sem.release()


# Spend ledger --------------------------------------------------------------------------

def model(cfg, mid="ds"):
    return next(m for m in cfg["build"]["models"] if m["id"] == mid)


def test_reservation_is_refused_when_it_would_cross_the_daily_cap(env):
    cfg, db, prov = env
    spend = Spend(lambda: cfg, db)
    cfg["build"]["providers"]["deepseek"]["daily_limit_usd"] = 0.01
    # 1,000 prompt tokens at $0.5/M + 4,000 output at $2/M = $0.0085: fits.
    first = spend.reserve(model(cfg), prompt_tokens=1000, max_output=4000)
    with pytest.raises(SpendLimitError, match="daily"):
        spend.reserve(model(cfg), prompt_tokens=1000, max_output=4000)
    spend.finish(first, {"input_tokens": 1000, "cached_tokens": 0, "output_tokens": 100})
    row = db._one("SELECT * FROM model_calls WHERE id = ?", (first,))
    assert row["status"] == "done" and abs(row["cost_usd"] - 0.0007) < 1e-9
    spend.reserve(model(cfg), prompt_tokens=1000, max_output=4000)  # Actual cost freed the room.


def test_monthly_cap_counts_earlier_days(env):
    cfg, db, prov = env
    spend = Spend(lambda: cfg, db)
    cfg["build"]["providers"]["deepseek"].update(daily_limit_usd=0, monthly_limit_usd=0.02)
    old = spend.reserve(model(cfg), prompt_tokens=1000, max_output=1000)
    spend.finish(old, {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 9000})  # $0.018
    db._exec("UPDATE model_calls SET created = ? WHERE id = ?", (spend.month_start() + 1, old))
    with pytest.raises(SpendLimitError, match="monthly"):
        spend.reserve(model(cfg), prompt_tokens=1000, max_output=1000)


def test_unknown_price_blocks_requests_when_a_cap_is_set(env):
    cfg, db, prov = env
    spend = Spend(lambda: cfg, db)
    m = dict(model(cfg), price_in=None)
    with pytest.raises(SpendLimitError, match="price"):
        spend.reserve(m, prompt_tokens=10, max_output=10)
    cfg["build"]["providers"]["deepseek"].update(daily_limit_usd=0, monthly_limit_usd=0)
    call = spend.reserve(m, prompt_tokens=10, max_output=10)
    spend.finish(call, {"input_tokens": 10, "cached_tokens": 0, "output_tokens": 10})
    row = db._one("SELECT * FROM model_calls WHERE id = ?", (call,))
    assert row["cost_usd"] is None and row["cost_source"] == "unknown"


def test_provider_reported_cost_wins_and_local_is_free(env):
    cfg, db, prov = env
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


def test_failed_request_without_usage_costs_nothing_and_totals_add_up(env):
    cfg, db, prov = env
    spend = Spend(lambda: cfg, db)
    call = spend.reserve(model(cfg), prompt_tokens=1000, max_output=1000)
    spend.fail(call, "timeout")
    assert db._one("SELECT status, cost_usd FROM model_calls WHERE id = ?", (call,)) == {"status": "failed", "cost_usd": 0.0}
    t = spend.totals("deepseek")
    assert t == {"today": 0.0, "month": 0.0, "daily_limit": 1.0, "monthly_limit": 10.0, "reserved": 0.0}


def test_recover_keeps_interrupted_reservations_counted(env):
    cfg, db, prov = env
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
