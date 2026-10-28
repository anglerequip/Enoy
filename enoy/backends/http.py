"""HTTP for API model providers, standard library only.

One place decides timeouts, retries, cancel and error kinds for every provider, and
makes sure an API key can never appear in an error message.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from typing import Any

from .. import __version__
from ..redact import redact
from .base import BackendError

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
MAX_RETRY_AFTER_S = 60.0


def _retry_after(headers, fallback: float) -> float:
    value = (headers or {}).get("Retry-After") if headers else None
    if not value:
        return fallback
    try:
        return min(max(float(value), 0.0), MAX_RETRY_AFTER_S)
    except ValueError:
        try:
            import time
            return min(max(parsedate_to_datetime(value).timestamp() - time.time(), 0.0), MAX_RETRY_AFTER_S)
        except (TypeError, ValueError):
            return fallback


def _clean(text: str, secret: str) -> str:
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return redact(text)


def _reason(body: bytes) -> str:
    """The provider's own error message, if the body has one."""
    text = body.decode("utf-8", "replace")
    try:
        data = json.loads(text)
        err = data.get("error", data) if isinstance(data, dict) else data
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or err)[:300]
        return str(err)[:300]
    except ValueError:
        return text.strip()[:300]


def _once(method: str, url: str, headers: dict, data: bytes | None, timeout: float) -> tuple[int, bytes, Any]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b"", exc.headers


def request_json(method: str, url: str, *, body: Any = None, headers: dict | None = None, timeout: float = 60,
                 cancel: threading.Event | None = None, retries: int = 2, backoff: float = 1.0,
                 secret: str = "", label: str = "The provider") -> Any:
    """Send one JSON request with retries. Raises BackendError with a kind the callers act on."""
    hdrs = {"Accept": "application/json", "User-Agent": f"Enoy/{__version__}", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    cancel = cancel or threading.Event()
    last: BackendError | None = None
    for attempt in range(retries + 1):
        if cancel.is_set():
            raise BackendError("cancelled", "Cancelled.")
        box: dict = {}

        def work():
            try:
                box["result"] = _once(method, url, hdrs, data, timeout)
            except BaseException as exc:  # Reported in the calling thread.
                box["error"] = exc

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        while worker.is_alive():
            worker.join(0.05)
            if cancel.is_set():
                raise BackendError("cancelled", "Cancelled; the request was abandoned.")
        delay = backoff * (2 ** attempt)
        if "error" in box:
            exc = box["error"]
            # urllib wraps connect-stage failures (refused, DNS, connect timeout) in URLError: the
            # provider is unreachable. A bare timeout means it connected but did not answer in time.
            if isinstance(exc, (TimeoutError, socket.timeout)):
                last = BackendError("timeout", f"{label} did not answer within {timeout:g} s.")
            elif isinstance(exc, (urllib.error.URLError, ConnectionError, OSError)):
                last = BackendError("unavailable", f"{label} could not be reached (no connection?): "
                                                   f"{_clean(str(getattr(exc, 'reason', exc)), secret)}")
            else:
                raise BackendError("failed", _clean(f"{label} request failed: {exc}", secret)) from None
        else:
            status, raw, resp_headers = box["result"]
            if 200 <= status < 300:
                try:
                    return json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    raise BackendError("malformed", f"{label} returned something that is not JSON.") from None
            reason = _clean(_reason(raw), secret)
            if status in (401, 403):
                raise BackendError("auth", f"{label} refused the API key (HTTP {status}): {reason}")
            if status == 402:
                raise BackendError("limit", f"{label} says the account is out of credit (HTTP 402): {reason}")
            if status not in RETRY_STATUS:
                raise BackendError("failed", f"{label} returned HTTP {status}: {reason}")
            kind = "limit" if status == 429 else "timeout" if status == 408 else "unavailable"
            last = BackendError(kind, f"{label} returned HTTP {status}: {reason}")
            delay = _retry_after(resp_headers, delay)
        if attempt < retries and cancel.wait(delay):
            raise BackendError("cancelled", "Cancelled.")
    raise last
