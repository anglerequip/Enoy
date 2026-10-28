"""Spending limits for API model providers.

Before a request, Enoy reserves the most it could cost (prompt plus the full output allowance
at the configured prices). If that would pass a provider's daily or monthly limit, the request
is not sent. Afterwards the reservation is replaced by the cost of the tokens actually used, or
by the provider's own figure when it reports one. Local models cost nothing.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from typing import Callable

from .backends.base import BackendError
from .storage import Storage


class SpendLimitError(BackendError):
    def __init__(self, message: str):
        super().__init__("budget", message)


def estimate_tokens(text: str) -> int:
    """A deliberately high estimate (about 3 characters per token) so reservations err on the safe side."""
    return len(text) // 3 + 1


class Spend:
    def __init__(self, cfg: Callable[[], dict], db: Storage):
        self._cfg = cfg
        self.db = db

    def _provider(self, pid: str) -> dict:
        return self._cfg()["build"]["providers"].get(pid) or {}

    @staticmethod
    def day_start() -> float:
        return dt.datetime.combine(dt.date.today(), dt.time()).timestamp()

    @staticmethod
    def month_start() -> float:
        return dt.datetime.combine(dt.date.today().replace(day=1), dt.time()).timestamp()

    @staticmethod
    def _prices(model: dict) -> tuple[float, float, float] | None:
        if model.get("price_in") is None or model.get("price_out") is None:
            return None
        cached = model.get("price_cached")
        return model["price_in"], model["price_in"] if cached is None else cached, model["price_out"]

    def _used(self, pid: str, since: float) -> float:
        row = self.db._one("SELECT COALESCE(SUM(CASE WHEN status = 'reserved' THEN reserved_usd "
                           "ELSE COALESCE(cost_usd, 0) END), 0) AS s FROM model_calls WHERE provider = ? AND created >= ?",
                           (pid, since))
        return row["s"]

    def reserve(self, model: dict, prompt_tokens: int, max_output: int, role: str = "", purpose: str = "",
                plan_id: int | None = None, dev_task_id: int | None = None, run_id: int | None = None,
                sent: str = "", chars: int = 0) -> int:
        pid = model["provider"]
        p = self._provider(pid)
        caps = [(p.get("daily_limit_usd", 0), self.day_start(), "daily"),
                (p.get("monthly_limit_usd", 0), self.month_start(), "monthly")]
        source, reserved = "estimate", 0.0
        prices = self._prices(model)
        if p.get("local"):
            source = "local"
        elif prices is None:
            if any(cap > 0 for cap, _, _ in caps):
                raise SpendLimitError(f"No price is set for model '{model['id']}', so Enoy cannot keep the spending "
                                      "limit. The request was not sent. Enter its prices in Settings → Providers.")
            source = "unknown"
        else:
            reserved = (prompt_tokens * prices[0] + max_output * prices[2]) / 1e6
        with self.db._tx():
            for cap, since, label in caps:
                if source == "estimate" and cap > 0:
                    used = self._used(pid, since)
                    if used + reserved > cap:
                        raise SpendLimitError(
                            f"{pid}: this request could cost up to ${reserved:.4f}. With ${used:.4f} already used, "
                            f"that would pass the {label} limit of ${cap:.2f}. The request was not sent; raise the "
                            "limit in Settings → Providers or wait for the next period.")
            return self.db._exec(
                "INSERT INTO model_calls (provider, model, role, purpose, plan_id, dev_task_id, run_id, status, "
                "reserved_usd, prices, cost_source, chars, content, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?, ?)",
                (pid, model.get("model", ""), role, purpose, plan_id, dev_task_id, run_id, reserved,
                 json.dumps(prices) if prices and source == "estimate" else "", source, chars, sent,
                 time.time())).lastrowid

    @staticmethod
    def _cost(call: dict, usage: dict | None) -> float | None:
        """Priced with the rates saved when the request was reserved."""
        if call["cost_source"] == "local":
            return 0.0
        prices = json.loads(call["prices"]) if call["prices"] else None
        if not usage or prices is None:
            return None
        return (usage.get("input_tokens", 0) * prices[0] + usage.get("cached_tokens", 0) * prices[1]
                + usage.get("output_tokens", 0) * prices[2]) / 1e6

    def finish(self, call_id: int, usage: dict | None, provider_cost: float | None = None) -> None:
        call = self.db._one("SELECT * FROM model_calls WHERE id = ?", (call_id,))
        cost, source = provider_cost, "provider"
        if cost is None:
            cost = self._cost(call, usage)
            source = call["cost_source"] if call["cost_source"] in ("local", "unknown") or cost is None else "estimate"
            if cost is None:
                source = "unknown"
        usage = usage or {}
        self.db._exec("UPDATE model_calls SET status = 'done', input_tokens = ?, cached_tokens = ?, output_tokens = ?, "
                      "cost_usd = ?, cost_source = ?, finished = ? WHERE id = ?",
                      (usage.get("input_tokens"), usage.get("cached_tokens"), usage.get("output_tokens"), cost, source,
                       time.time(), call_id))

    def fail(self, call_id: int, error: str, usage: dict | None = None) -> None:
        """A failed request is normally not billed; if the provider reported usage, that is what counts."""
        call = self.db._one("SELECT * FROM model_calls WHERE id = ?", (call_id,))
        cost = self._cost(call, usage) if usage else 0.0
        self.db._exec("UPDATE model_calls SET status = 'failed', cost_usd = ?, error = ?, finished = ? WHERE id = ?",
                      (cost if cost is not None else 0.0, error[:500], time.time(), call_id))

    def totals(self, pid: str) -> dict:
        p = self._provider(pid)

        def spent(since: float) -> float:
            return self.db._one("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM model_calls WHERE provider = ? AND "
                                "status != 'reserved' AND created >= ?", (pid, since))["s"]

        reserved = self.db._one("SELECT COALESCE(SUM(reserved_usd), 0) AS s FROM model_calls WHERE provider = ? AND "
                                "status = 'reserved'", (pid,))["s"]
        return {"today": round(spent(self.day_start()), 6), "month": round(spent(self.month_start()), 6),
                "daily_limit": p.get("daily_limit_usd", 0), "monthly_limit": p.get("monthly_limit_usd", 0),
                "reserved": round(reserved, 6)}

    def recover(self) -> int:
        """At startup: requests cut off by a restart may still be billed, so their reservation counts as spent."""
        return self.db._exec("UPDATE model_calls SET status = 'interrupted', cost_usd = reserved_usd, "
                             "error = 'Enoy stopped during this request.', finished = ? WHERE status = 'reserved'",
                             (time.time(),)).rowcount
