"""Shared HTTP client for API providers (T2.3). Uses a local fake server only."""

import socket
import threading
import time

import pytest

from enoy.backends.base import BackendError
from enoy.backends.http import request_json
from fakeapi import FakeAPI

SECRET = "sk-SENTINEL-http-0123456789"


@pytest.fixture
def api():
    a = FakeAPI()
    yield a
    a.close()


def call(api, path="/x", **kw):
    kw.setdefault("backoff", 0.01)
    return request_json("POST", api.url + path, body={"a": 1}, headers={"Authorization": "Bearer " + SECRET},
                        secret=SECRET, **kw)


def test_success_returns_json_and_sends_body(api):
    api.add("/x", (200, {"ok": True}))
    assert call(api) == {"ok": True}
    assert api.requests[0]["body"] == {"a": 1} and api.requests[0]["headers"]["content-type"] == "application/json"


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_are_not_retried(api, status):
    api.add("/x", (status, {"error": {"message": "bad key " + SECRET}}))
    with pytest.raises(BackendError) as exc:
        call(api)
    assert exc.value.kind == "auth" and len(api.requests) == 1
    assert SECRET not in str(exc.value)


def test_rate_limit_honours_retry_after_then_gives_up(api):
    api.add("/x", (429, {"error": "slow down"}, {"Retry-After": "0"}), (429, {}, {"Retry-After": "0"}),
            (429, {}, {"Retry-After": "0"}))
    with pytest.raises(BackendError) as exc:
        call(api, retries=2)
    assert exc.value.kind == "limit" and len(api.requests) == 3


def test_rate_limit_recovers(api):
    api.add("/x", (429, {}, {"Retry-After": "0"}), (200, {"ok": 1}))
    assert call(api) == {"ok": 1} and len(api.requests) == 2


def test_server_errors_are_retried_then_unavailable(api):
    api.add("/x", (500, {}), (503, {}), (502, {}))
    with pytest.raises(BackendError) as exc:
        call(api, retries=2)
    assert exc.value.kind == "unavailable" and len(api.requests) == 3


def test_payment_required_is_a_limit_without_retry(api):
    api.add("/x", (402, {"error": {"message": "Insufficient Balance"}}))
    with pytest.raises(BackendError) as exc:
        call(api)
    assert exc.value.kind == "limit" and "Insufficient Balance" in str(exc.value) and len(api.requests) == 1


def test_bad_request_is_failed_and_shows_reason(api):
    api.add("/x", (400, {"error": {"message": "Model not exist"}}))
    with pytest.raises(BackendError) as exc:
        call(api)
    assert exc.value.kind == "failed" and "Model not exist" in str(exc.value) and "400" in str(exc.value)


def test_invalid_json_is_malformed(api):
    api.add("/x", (200, "not json"))
    with pytest.raises(BackendError) as exc:
        call(api)
    assert exc.value.kind == "malformed"


def test_connection_refused_is_unavailable():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(BackendError) as exc:
        request_json("GET", f"http://127.0.0.1:{port}/x", retries=0, timeout=2)
    assert exc.value.kind == "unavailable"


def test_timeout_is_timeout(api):
    api.add("/x", lambda req: (time.sleep(1.5), (200, {}))[1])
    with pytest.raises(BackendError) as exc:
        call(api, timeout=0.3, retries=0)
    assert exc.value.kind == "timeout"


def test_cancel_stops_a_running_request(api):
    api.add("/x", lambda req: (time.sleep(3), (200, {}))[1])
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    start = time.time()
    with pytest.raises(BackendError) as exc:
        call(api, cancel=cancel, timeout=10)
    assert exc.value.kind == "cancelled" and time.time() - start < 2
