"""Tests for arbiter.policy.decision — Lane 12a.

Covered cases:
- Gate HALTED → no orders returned
- Gate DEGRADED (0.25×) → orders present but smaller qty
- Positive conviction → BUY order
- Negative conviction → SELL order
- Near-zero conviction (|c| < threshold) → no order (flat)
- Missing ADV → no order
- Exits are attached on returned PaperOrder
- PaperOrder fields populated: order_id, dedup_hash, ticker, side, qty, horizon_bucket, entry_date, advisor_signature
- High conviction → larger qty than low conviction (all else equal)
- Multiple horizon buckets → multiple orders (one per passing bucket)
- decide_all across multiple tickers
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from arbiter.contract.seams import PaperOrder, TradingDecision
from arbiter.policy.decision import decide, decide_all
from arbiter.types import DegradationLevel, HorizonBucket, OrderSide
from tests.policy.conftest import (
    FakeClock,
    adv_always,
    adv_missing,
    make_fusion,
    _make_gate,
)


PORTFOLIO = 100_000.0
ENTRY_PRICE = 150.0


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _decide(
    ticker="AAPL",
    conviction=0.5,
    cold_start=False,
    bucket=HorizonBucket.SHORT,
    adv_usd=10_000_000.0,
    gate_allowed=True,
    gate_multiplier=1.0,
    gate_level=DegradationLevel.NORMAL,
    live_advisor_count=2,
    current_sector_exposure=0.0,
    current_gross_exposure=0.0,
    current_open_positions=0,
    cfg=None,
):
    """Convenience wrapper for decide() in tests."""
    from arbiter.config import Config

    if cfg is None:
        cfg = Config(
            live_trading=False, db_path=":memory:", audit_path="/dev/null",
            executor_backend="sim",
            metrics_path="/dev/null", max_position_pct=0.05, max_sector_pct=0.20,
            max_gross_pct=0.80, max_open_positions=20, adv_cap_pct=0.02,
            alpaca_api_key="", alpaca_secret_key="", alpaca_paper_base_url="",
            alpaca_data_base_url="", alpaca_timeout=20.0, edgar_user_agent="",
            kill_switch_url="", alert_webhook_url="",
        )

    gate_decision = TradingDecision(
        allowed=gate_allowed, size_multiplier=gate_multiplier,
        level=gate_level, reasons=[],
    )
    gate = lambda account, count: gate_decision

    adv_fn = adv_always(adv_usd) if adv_usd is not None else adv_missing()

    return decide(
        ticker=ticker,
        bucket_outputs={bucket: make_fusion(bucket=bucket, conviction=conviction, cold_start=cold_start)},
        account=object(),
        gate=gate,
        adv_provider=adv_fn,
        clock=FakeClock(),
        config=cfg,
        portfolio_equity=PORTFOLIO,
        live_advisor_count=live_advisor_count,
        current_sector_exposure=current_sector_exposure,
        current_gross_exposure=current_gross_exposure,
        current_open_positions=current_open_positions,
        entry_price=ENTRY_PRICE,
    )


# ---------------------------------------------------------------------------
# Gate: HALTED → no orders
# ---------------------------------------------------------------------------

class TestGateHalted:
    def test_halted_returns_empty(self):
        """HALTED gate produces no orders."""
        orders = _decide(conviction=0.9, gate_allowed=False, gate_multiplier=0.0)
        assert orders == []

    def test_halted_overrides_high_conviction(self):
        """Even conviction=1.0 yields no order when gate is halted."""
        orders = _decide(conviction=1.0, gate_allowed=False, gate_multiplier=0.0)
        assert orders == []


# ---------------------------------------------------------------------------
# Gate: DEGRADED → orders present but smaller qty
# ---------------------------------------------------------------------------

class TestGateDegraded:
    def test_degraded_returns_order(self):
        """DEGRADED gate still produces orders."""
        orders = _decide(conviction=0.5, gate_multiplier=0.25)
        assert len(orders) == 1

    def test_degraded_reduces_qty(self):
        """DEGRADED (0.25×) produces smaller qty than NORMAL (1.0×)."""
        normal_orders = _decide(conviction=0.1, gate_multiplier=1.0)
        degraded_orders = _decide(conviction=0.1, gate_multiplier=0.25)
        assert len(normal_orders) == 1
        assert len(degraded_orders) == 1
        assert degraded_orders[0].qty == pytest.approx(normal_orders[0].qty * 0.25)


# ---------------------------------------------------------------------------
# Conviction → BUY / SELL / flat
# ---------------------------------------------------------------------------

class TestConvictionToSide:
    def test_positive_conviction_produces_buy(self):
        orders = _decide(conviction=0.5)
        assert len(orders) == 1
        assert orders[0].side == OrderSide.BUY

    def test_negative_conviction_produces_sell(self):
        orders = _decide(conviction=-0.5)
        assert len(orders) == 1
        assert orders[0].side == OrderSide.SELL

    def test_near_zero_positive_produces_no_order(self):
        """|conviction| = 0.04 < 0.05 threshold → flat."""
        orders = _decide(conviction=0.04)
        assert orders == []

    def test_near_zero_negative_produces_no_order(self):
        orders = _decide(conviction=-0.04)
        assert orders == []

    def test_exactly_at_threshold_produces_order(self):
        """|conviction| = 0.05 == threshold → order placed."""
        orders = _decide(conviction=0.05)
        assert len(orders) == 1

    def test_zero_conviction_produces_no_order(self):
        orders = _decide(conviction=0.0)
        assert orders == []


# ---------------------------------------------------------------------------
# Missing ADV → no order
# ---------------------------------------------------------------------------

class TestMissingAdv:
    def test_missing_adv_produces_no_order(self):
        orders = _decide(conviction=0.9, adv_usd=None)
        assert orders == []


# ---------------------------------------------------------------------------
# PaperOrder fields
# ---------------------------------------------------------------------------

class TestPaperOrderFields:
    def test_order_fields_populated(self):
        orders = _decide(ticker="TSLA", conviction=0.5, bucket=HorizonBucket.MEDIUM)
        assert len(orders) == 1
        o = orders[0]
        assert o.ticker == "TSLA"
        assert o.side == OrderSide.BUY
        assert o.qty > 0
        assert o.horizon_bucket == HorizonBucket.MEDIUM
        assert isinstance(o.entry_date, date)
        assert isinstance(o.order_id, str) and len(o.order_id) > 0
        assert isinstance(o.dedup_hash, str) and len(o.dedup_hash) == 64  # sha256 hex
        assert isinstance(o.advisor_signature, str) and len(o.advisor_signature) > 0

    def test_exits_attached(self):
        orders = _decide(conviction=0.6, bucket=HorizonBucket.SHORT)
        assert len(orders) == 1
        exits = orders[0].exits
        assert "stop_loss" in exits
        assert "horizon_expiry" in exits
        assert "conviction_reversal" in exits

    def test_exits_stop_loss_below_entry_for_buy(self):
        orders = _decide(conviction=0.6, bucket=HorizonBucket.SHORT)
        o = orders[0]
        assert o.side == OrderSide.BUY
        assert o.exits["stop_loss"] < ENTRY_PRICE

    def test_exits_stop_loss_above_entry_for_sell(self):
        orders = _decide(conviction=-0.6, bucket=HorizonBucket.SHORT)
        o = orders[0]
        assert o.side == OrderSide.SELL
        assert o.exits["stop_loss"] > ENTRY_PRICE

    def test_dedup_hash_is_deterministic(self):
        """Same inputs produce same dedup_hash."""
        orders_a = _decide(ticker="AAPL", conviction=0.5, bucket=HorizonBucket.SHORT)
        orders_b = _decide(ticker="AAPL", conviction=0.5, bucket=HorizonBucket.SHORT)
        assert orders_a[0].dedup_hash == orders_b[0].dedup_hash

    def test_dedup_hash_matches_idempotency_single_source(self):
        """D1 P2: decision's dedup_hash equals idempotency.dedup_hash(order).

        decision.py no longer computes the hash locally; it delegates to the
        single source in idempotency.py.  Recomputing it on the emitted order
        must reproduce the exact stored value (no drift).
        """
        from arbiter.execution.idempotency import dedup_hash

        orders = _decide(ticker="AAPL", conviction=0.5, bucket=HorizonBucket.SHORT)
        o = orders[0]
        assert o.dedup_hash == dedup_hash(o)

    def test_different_tickers_different_hash(self):
        orders_a = _decide(ticker="AAPL", conviction=0.5)
        orders_b = _decide(ticker="MSFT", conviction=0.5)
        assert orders_a[0].dedup_hash != orders_b[0].dedup_hash


# ---------------------------------------------------------------------------
# High conviction → larger size than low conviction
# ---------------------------------------------------------------------------

class TestConvictionScalesSize:
    def test_high_conviction_larger_than_low(self):
        """High conviction yields larger qty (below all caps)."""
        low_orders = _decide(conviction=0.1)
        high_orders = _decide(conviction=0.4)
        assert len(low_orders) == 1
        assert len(high_orders) == 1
        assert high_orders[0].qty > low_orders[0].qty

    def test_conviction_proportional_to_qty(self):
        """Conviction doubling doubles qty (when no cap binds)."""
        low_orders = _decide(conviction=0.1)
        double_orders = _decide(conviction=0.2)
        assert double_orders[0].qty == pytest.approx(low_orders[0].qty * 2.0)


# ---------------------------------------------------------------------------
# Multiple horizon buckets
# ---------------------------------------------------------------------------

class TestMultipleBuckets:
    def test_two_buckets_produce_two_orders(self):
        from arbiter.config import Config
        cfg = Config(
            live_trading=False, db_path=":memory:", audit_path="/dev/null",
            executor_backend="sim",
            metrics_path="/dev/null", max_position_pct=0.05, max_sector_pct=0.20,
            max_gross_pct=0.80, max_open_positions=20, adv_cap_pct=0.02,
            alpaca_api_key="", alpaca_secret_key="", alpaca_paper_base_url="",
            alpaca_data_base_url="", alpaca_timeout=20.0, edgar_user_agent="",
            kill_switch_url="", alert_webhook_url="",
        )
        gate_d = TradingDecision(allowed=True, size_multiplier=1.0, level=DegradationLevel.NORMAL, reasons=[])
        gate = lambda a, n: gate_d

        bucket_outputs = {
            HorizonBucket.SHORT: make_fusion(bucket=HorizonBucket.SHORT, conviction=0.5),
            HorizonBucket.MEDIUM: make_fusion(bucket=HorizonBucket.MEDIUM, conviction=0.4),
        }
        orders = decide(
            ticker="NVDA", bucket_outputs=bucket_outputs, account=object(),
            gate=gate, adv_provider=adv_always(10_000_000.0),
            clock=FakeClock(), config=cfg, portfolio_equity=PORTFOLIO,
        )
        assert len(orders) == 2
        buckets = {o.horizon_bucket for o in orders}
        assert HorizonBucket.SHORT in buckets
        assert HorizonBucket.MEDIUM in buckets

    def test_mixed_conviction_buckets(self):
        """One bucket with flat conviction and one with real conviction → 1 order."""
        from arbiter.config import Config
        cfg = Config(
            live_trading=False, db_path=":memory:", audit_path="/dev/null",
            executor_backend="sim",
            metrics_path="/dev/null", max_position_pct=0.05, max_sector_pct=0.20,
            max_gross_pct=0.80, max_open_positions=20, adv_cap_pct=0.02,
            alpaca_api_key="", alpaca_secret_key="", alpaca_paper_base_url="",
            alpaca_data_base_url="", alpaca_timeout=20.0, edgar_user_agent="",
            kill_switch_url="", alert_webhook_url="",
        )
        gate_d = TradingDecision(allowed=True, size_multiplier=1.0, level=DegradationLevel.NORMAL, reasons=[])
        gate = lambda a, n: gate_d

        bucket_outputs = {
            HorizonBucket.SHORT: make_fusion(bucket=HorizonBucket.SHORT, conviction=0.0),  # flat
            HorizonBucket.LONG: make_fusion(bucket=HorizonBucket.LONG, conviction=0.6),
        }
        orders = decide(
            ticker="META", bucket_outputs=bucket_outputs, account=object(),
            gate=gate, adv_provider=adv_always(10_000_000.0),
            clock=FakeClock(), config=cfg, portfolio_equity=PORTFOLIO,
        )
        assert len(orders) == 1
        assert orders[0].horizon_bucket == HorizonBucket.LONG


# ---------------------------------------------------------------------------
# decide_all multi-ticker wrapper
# ---------------------------------------------------------------------------

class TestDecideAll:
    def _cfg(self):
        from arbiter.config import Config
        return Config(
            live_trading=False, db_path=":memory:", audit_path="/dev/null",
            executor_backend="sim",
            metrics_path="/dev/null", max_position_pct=0.05, max_sector_pct=0.20,
            max_gross_pct=0.80, max_open_positions=20, adv_cap_pct=0.02,
            alpaca_api_key="", alpaca_secret_key="", alpaca_paper_base_url="",
            alpaca_data_base_url="", alpaca_timeout=20.0, edgar_user_agent="",
            kill_switch_url="", alert_webhook_url="",
        )

    def test_multiple_tickers_produce_orders(self):
        cfg = self._cfg()
        gate_d = TradingDecision(allowed=True, size_multiplier=1.0, level=DegradationLevel.NORMAL, reasons=[])
        gate = lambda a, n: gate_d

        outputs = {
            "AAPL": {HorizonBucket.SHORT: make_fusion(conviction=0.5)},
            "MSFT": {HorizonBucket.SHORT: make_fusion(conviction=0.3)},
        }
        orders = decide_all(
            bucket_outputs_by_ticker=outputs, account=object(),
            gate=gate, adv_provider=adv_always(10_000_000.0),
            clock=FakeClock(), config=cfg, portfolio_equity=PORTFOLIO,
        )
        assert len(orders) == 2
        tickers = {o.ticker for o in orders}
        assert "AAPL" in tickers
        assert "MSFT" in tickers

    def test_halted_gate_all_empty(self):
        cfg = self._cfg()
        gate_d = TradingDecision(allowed=False, size_multiplier=0.0, level=DegradationLevel.HALTED, reasons=[])
        gate = lambda a, n: gate_d

        outputs = {
            "AAPL": {HorizonBucket.SHORT: make_fusion(conviction=0.8)},
            "GOOG": {HorizonBucket.MEDIUM: make_fusion(conviction=0.6)},
        }
        orders = decide_all(
            bucket_outputs_by_ticker=outputs, account=object(),
            gate=gate, adv_provider=adv_always(10_000_000.0),
            clock=FakeClock(), config=cfg, portfolio_equity=PORTFOLIO,
        )
        assert orders == []

    def test_sector_cap_rolls_across_batch(self):
        """Sector exposure accumulates within a decide_all() batch.

        Finding 5 fix: sector_by_ticker is wired and running_sector accumulates
        so that N same-sector tickers don't each get the full 20% headroom.

        Config: max_sector_pct=20%, portfolio=100k → sector cap=$20k.
        If sector cap did NOT roll, 4 tickers × $5k = $20k orders all pass.
        With rolling sector cap: after the 4th ticker exhausts $20k, 5th gets $0.
        """
        cfg = self._cfg()
        gate_d = TradingDecision(allowed=True, size_multiplier=1.0, level=DegradationLevel.NORMAL, reasons=[])
        gate = lambda a, n: gate_d

        # 5 tickers all in same sector "TECH".
        # Per-name cap = 5% * 100k = $5k; sector cap = 20% * 100k = $20k.
        # With 5 tickers at $5k each = $25k > $20k sector cap.
        tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
        outputs = {
            t: {HorizonBucket.SHORT: make_fusion(conviction=1.0)}  # conviction=1 → per-name cap binds
            for t in tickers
        }
        sector_map = {t: "TECH" for t in tickers}

        orders = decide_all(
            bucket_outputs_by_ticker=outputs, account=object(),
            gate=gate, adv_provider=adv_always(10_000_000.0),
            clock=FakeClock(), config=cfg, portfolio_equity=PORTFOLIO,
            sector_by_ticker=sector_map,
        )

        total_qty = sum(o.qty for o in orders)
        # Sector cap = $20k; all 5 tickers in same sector → total must not exceed $20k
        assert total_qty <= cfg.max_sector_pct * PORTFOLIO + 1e-6, (
            f"Sector cap violated: total_qty={total_qty} > "
            f"max={cfg.max_sector_pct * PORTFOLIO} "
            f"(Finding 5: sector not rolled within batch)"
        )
        # Should still produce orders (not zero)
        assert len(orders) >= 1, "Expected at least one order in sector batch"

    def test_unknown_sector_default_caps_across_batch(self):
        """Without sector_by_ticker, all tickers default to UNKNOWN sector.

        The conservative default means the 20% sector cap still binds across
        all same-default-sector tickers (not just per-ticker).
        """
        cfg = self._cfg()
        gate_d = TradingDecision(allowed=True, size_multiplier=1.0, level=DegradationLevel.NORMAL, reasons=[])
        gate = lambda a, n: gate_d

        tickers = ["X1", "X2", "X3", "X4", "X5"]
        outputs = {
            t: {HorizonBucket.SHORT: make_fusion(conviction=1.0)}
            for t in tickers
        }

        # No sector_by_ticker — defaults to UNKNOWN for all
        orders = decide_all(
            bucket_outputs_by_ticker=outputs, account=object(),
            gate=gate, adv_provider=adv_always(10_000_000.0),
            clock=FakeClock(), config=cfg, portfolio_equity=PORTFOLIO,
            # sector_by_ticker omitted → all default to "UNKNOWN"
        )

        total_qty = sum(o.qty for o in orders)
        # All in "UNKNOWN" sector → sector cap = 20% * 100k = $20k
        assert total_qty <= cfg.max_sector_pct * PORTFOLIO + 1e-6, (
            f"UNKNOWN sector cap violated: total_qty={total_qty} > "
            f"max={cfg.max_sector_pct * PORTFOLIO}"
        )
