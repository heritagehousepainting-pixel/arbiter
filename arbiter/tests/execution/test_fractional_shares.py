"""Tier-2 #4 — fractional-share fallback (2026-07-02).

When the whole-share floor is 0 (stock price above the position notional) and
``allow_fractional=True``, submit_order sizes a FRACTIONAL qty (floored to
4 dp) instead of zero-share-skipping.  Alpaca accepts fractional market/limit
DAY orders in paper + live (docs "fractional-trading", verified 2026-07-02).

Also locks the exit-path companion fix: ``presized_shares`` is preserved as a
FLOAT — the old ``int()`` coercion would truncate a fractional position to 0
and strand it unexitable.
"""
from __future__ import annotations

import math

import pytest

from arbiter.config import Config
from arbiter.data.slippage import model_slippage
from arbiter.execution.alpaca_adapter import AlpacaAdapter
from arbiter.execution.submit import _ZERO_SHARE_SKIP, submit_order
from arbiter.types import OrderSide

from tests.execution.conftest import make_paper_order


def _make_config() -> Config:
    return Config(
        live_trading=False,
        executor_backend="alpaca_paper",
        db_path=":memory:",
        audit_path="/tmp/audit.jsonl",
        metrics_path="/tmp/metrics.jsonl",
        max_position_pct=0.05,
        max_sector_pct=0.20,
        max_gross_pct=0.80,
        max_open_positions=20,
        adv_cap_pct=0.02,
        alpaca_api_key="test_key",
        alpaca_secret_key="test_secret",
        alpaca_paper_base_url="https://paper-api.alpaca.markets",
        alpaca_data_base_url="https://data.alpaca.markets",
        alpaca_timeout=20.0,
        edgar_user_agent="test@test.com",
        kill_switch_url="",
        alert_webhook_url="",
    )


def _expected_fractional(notional: float, raw_price: float, spread: float) -> float:
    limit = model_slippage(raw_price, spread)
    return math.floor((notional / limit) * 10_000) / 10_000


class _NonFractionableShim:
    """Wrap an executor and declare every asset non-fractionable."""

    def __init__(self, inner):
        self._inner = inner
        self.name = inner.name

    def is_fractionable(self, ticker: str) -> bool:  # noqa: ARG002
        return False

    def __getattr__(self, item):
        return getattr(self._inner, item)


class TestFractionalFallback:
    def test_fractional_fallback_on_zero_whole_shares(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """$250 notional at a ~$398 stock → fractional qty, not a skip."""
        spread = 0.05
        raw_price = 398.0
        order = make_paper_order(ticker="TSLA", qty=250.0)
        result = submit_order(
            order,
            sim_executor,
            fixed_clock,
            conn=mem_conn,
            spread=spread,
            raw_price=raw_price,
            audit_path=str(tmp_audit),
            allow_fractional=True,
        )
        expected = _expected_fractional(250.0, raw_price, spread)
        assert result.filled is True
        assert 0.0 < expected < 1.0
        row = mem_conn.execute(
            "SELECT qty FROM orders WHERE order_id = ?", (order.order_id,)
        ).fetchone()
        assert row["qty"] == pytest.approx(expected)

    def test_default_remains_zero_share_skip(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """Without the flag the legacy whole-share-only behavior is intact."""
        order = make_paper_order(ticker="TSLA", qty=250.0)
        result = submit_order(
            order,
            sim_executor,
            fixed_clock,
            conn=mem_conn,
            spread=0.05,
            raw_price=398.0,
            audit_path=str(tmp_audit),
        )
        assert result.zero_share is True
        assert result.status == _ZERO_SHARE_SKIP

    def test_whole_share_path_unchanged_when_flag_on(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """Fractional fallback only fires when the floor is 0."""
        order = make_paper_order(qty=5_000.0)
        result = submit_order(
            order,
            sim_executor,
            fixed_clock,
            conn=mem_conn,
            spread=0.05,
            raw_price=100.0,
            audit_path=str(tmp_audit),
            allow_fractional=True,
        )
        assert result.filled is True
        row = mem_conn.execute(
            "SELECT qty FROM orders WHERE order_id = ?", (order.order_id,)
        ).fetchone()
        assert row["qty"] == 49.0  # whole shares, no fractional tail

    def test_non_fractionable_asset_falls_back_to_skip(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """Executor says the asset is not fractionable → legacy skip."""
        order = make_paper_order(ticker="TSLA", qty=250.0)
        result = submit_order(
            order,
            _NonFractionableShim(sim_executor),
            fixed_clock,
            conn=mem_conn,
            spread=0.05,
            raw_price=398.0,
            audit_path=str(tmp_audit),
            allow_fractional=True,
        )
        assert result.zero_share is True
        assert result.status == _ZERO_SHARE_SKIP

    def test_sub_dollar_notional_still_skips(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """Below Alpaca's practical $1 floor we never place a fractional order."""
        order = make_paper_order(ticker="TSLA", qty=0.50)
        result = submit_order(
            order,
            sim_executor,
            fixed_clock,
            conn=mem_conn,
            spread=0.05,
            raw_price=398.0,
            audit_path=str(tmp_audit),
            allow_fractional=True,
        )
        assert result.zero_share is True

    def test_exit_presized_fractional_not_truncated(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """A fractional position must be exitable: presized_shares stays float.

        The old ``int(presized_shares)`` would floor 0.62 → 0 → zero-share skip
        → the position could never close.
        """
        spread = 0.05
        raw_price = 398.0
        buy = make_paper_order(ticker="TSLA", qty=250.0)
        submit_order(
            buy,
            sim_executor,
            fixed_clock,
            conn=mem_conn,
            spread=spread,
            raw_price=raw_price,
            audit_path=str(tmp_audit),
            allow_fractional=True,
        )
        held = sim_executor.get_positions()["TSLA"].shares
        assert 0.0 < held < 1.0

        sell = make_paper_order(ticker="TSLA", side=OrderSide.SELL, qty=held)
        result = submit_order(
            sell,
            sim_executor,
            fixed_clock,
            conn=mem_conn,
            spread=spread,
            raw_price=raw_price,
            audit_path=str(tmp_audit),
            presized_shares=held,
            is_exit=True,
        )
        assert result.filled is True
        row = mem_conn.execute(
            "SELECT qty FROM orders WHERE order_id = ?", (sell.order_id,)
        ).fetchone()
        assert row["qty"] == pytest.approx(held)


class TestIsFractionable:
    """AlpacaAdapter.is_fractionable — cached, fail-closed."""

    def _adapter(self, responses):
        calls = []

        def fake_get(url, headers):  # noqa: ARG001
            calls.append(url)
            resp = responses.pop(0)
            if isinstance(resp, Exception):
                raise resp
            return resp

        return AlpacaAdapter(config=_make_config(), http_get=fake_get), calls

    def test_true_and_cached(self):
        adapter, calls = self._adapter([{"fractionable": True}])
        assert adapter.is_fractionable("TSLA") is True
        assert adapter.is_fractionable("TSLA") is True  # served from cache
        assert len(calls) == 1
        assert calls[0].endswith("/v2/assets/TSLA")

    def test_false_is_respected(self):
        adapter, _ = self._adapter([{"fractionable": False}])
        assert adapter.is_fractionable("BRK.A") is False

    def test_error_fails_closed_and_is_not_cached(self):
        adapter, calls = self._adapter(
            [RuntimeError("boom"), {"fractionable": True}]
        )
        assert adapter.is_fractionable("TSLA") is False  # fail-closed
        assert adapter.is_fractionable("TSLA") is True  # retried, not cached
        assert len(calls) == 2
