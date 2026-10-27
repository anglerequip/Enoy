"""Quota validation, official CLI protocol and conservative savings accounting."""
import json
import sys
import time

import pytest

from enoy.storage import Storage
from enoy.usage import codex_limits, claude_limits, _claude_windows, UsageMonitor, save_claude, window


@pytest.mark.parametrize("used", [-1, 101, float("nan"), float("inf"), True, "50", None])
def test_invalid_percentage(used):
    with pytest.raises(ValueError):
        window("Weekly", used, time.time() + 500)


def test_manual_snapshot_validation_and_clear():
    db = Storage(":memory:")
    stamp = time.time() + 1000
    saved = save_claude(db, {"five_hour": {"used_percent": 0, "resets_at": stamp}})
    assert saved["windows"][0]["remaining_percent"] == 100
    assert json.loads(db.kv_get("claude_usage_snapshot")) == saved
    with pytest.raises(ValueError):
        save_claude(db, {"seven_day": {"used_percent": 50, "resets_at": 1}})
    assert json.loads(db.kv_get("claude_usage_snapshot")) == saved
    assert save_claude(db, {})["windows"] == []


def test_codex_protocol(tmp_path):
    script = tmp_path / "rpc.py"
    script.write_text('''import sys,json
for line in sys.stdin:
 e=json.loads(line)
 if e['method']=='initialize':
  print(json.dumps({'id':e['id'],'result':{}}),flush=True)
 elif e['method']=='initialized':
  ready=True
 elif e['method']=='account/rateLimits/read':
  assert ready
  print(json.dumps({'id':e['id'],'result':{'rateLimits':{'primary':{'usedPercent':23,'windowDurationMins':300,'resetsAt':2000000000},'secondary':{'usedPercent':100,'windowDurationMins':10080,'resetsAt':2000000001}}}}),flush=True)
''', encoding="utf-8")
    class Backend:
        def _base_cmd(self):
            return [sys.executable, str(script)]
    result = codex_limits(Backend())
    assert [(w["label"], w["remaining_percent"]) for w in result["windows"]] == [("5 hours", 77), ("Weekly", 0)]


def test_savings_only_completed_requests(tmp_path):
    db = Storage(":memory:")
    # Use public creation methods so foreign-key relationships are real.
    project = db.upsert_project(str(tmp_path), "Test")
    task = db.create_task(project["id"], "History", "History", "ask")
    db.add_message(task["id"], "user", "x" * 120)
    content = 'Shared state snapshot (recent records; any omissions are marked):\n' + json.dumps({"conversation": [{"content": "x" * 30}]})
    tx = db.add_transmission(task["id"], "codex", "ask", [], content)
    assert db.context_savings()["estimated_tokens_saved"] == 0
    db.set_transmission_usage(tx, '{}')
    assert db.context_savings()["estimated_tokens_saved"] == 30
    tx2 = db.add_transmission(task["id"], "claude", "ask", [], "Older format")
    db.set_transmission_usage(tx2, '{}')
    assert db.context_savings()["estimated_tokens_saved"] == 30


def test_claude_control_protocol_sends_no_model_request(tmp_path):
    script = tmp_path / "claude_rpc.py"
    script.write_text('''import sys,json
for line in sys.stdin:
 e=json.loads(line)
 assert e['type']=='control_request'
 assert e['request']=={'subtype':'get_usage','skip_behaviors':True}
 print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':e['request_id'],'response':{'rate_limits':{'five_hour':{'utilization':0,'resets_at':None},'seven_day':{'utilization':100,'resets_at':'2099-10-28T12:59:59Z'}}}}}),flush=True)
''', encoding="utf-8")
    class Backend:
        def _base_cmd(self):
            return [sys.executable, str(script)]
    result = claude_limits(Backend())
    assert result["source"] == "Claude subscription"
    assert [(w["label"], w["remaining_percent"]) for w in result["windows"]] == [("5 hours", 100), ("Weekly", 0)]
    assert result["windows"][0]["resets_at"] is None
    assert "limit reached" in result["detail"]


@pytest.mark.parametrize("data", [{}, {"rate_limits": None}, {"rate_limits": {}},
                                      {"rate_limits": {"five_hour": {"utilization": 101, "resets_at": None}}}])
def test_claude_missing_or_invalid_limits_are_not_inferred(data):
    with pytest.raises(ValueError):
        _claude_windows(data)


def test_claude_usage_refresh_cache_and_manual_fallback(orc_factory, monkeypatch):
    import enoy.usage as usage
    orc = orc_factory()
    orc.backends['claude']._base_cmd = lambda: ['fake-claude']
    monitor = UsageMonitor()
    monitor.cached = {'checked_at': time.time(), 'windows': []}
    calls = []
    def live(backend):
        calls.append(backend)
        return _claude_windows({'rate_limits': {'seven_day': {'utilization': 75, 'resets_at': time.time() + 1000}}})
    monkeypatch.setattr(usage, 'claude_limits', live)
    assert monitor.read(orc)['claude']['windows'][0]['remaining_percent'] == 25
    monitor.read(orc)
    assert len(calls) == 1
    monitor.read(orc, refresh=True)
    assert len(calls) == 2
    def unavailable(backend):
        raise ValueError('Unsupported CLI')
    monkeypatch.setattr(usage, 'claude_limits', unavailable)
    result = monitor.read(orc, refresh=True)['claude']
    assert result['windows'] == [] and 'Live usage unavailable' in result['detail']
    save_claude(orc.db, {'five_hour': {'used_percent': 12, 'resets_at': time.time() + 1000}})
    fallback = monitor.read(orc)['claude']
    assert fallback['source'] == 'Manual snapshot'
    assert fallback['windows'][0]['remaining_percent'] == 88
    assert 'manual snapshot' in fallback['detail']
