"""Tests for arbiter/options/exit.py — premium_stop_exit().

This module tests the DECISION logic only; no real network calls, no DB writes.
The function returns a close-reason string or None — the integrator is responsible
for placing closing orders and recording outcomes.

Coverage
--------
- Non-paper mode guard (returns None immediately)
- Premium stop trigger
- Horizon trigger
- Conviction reversal trigger
- No-trigger case (position stays open)
- Snapshot failure (fail-closed: skip stop, continue to remaining triggers)
- No mid from snapshot (similar fail-closed behaviour)
"""
from __future__ import annotations

import datetime
import sqlite3
from unittest.mock import MagicMock

import pytest

from arbiter.config import Config
from arbiter.options.exit import premium_stop_exit


# ---------------------------------------------------------------------------
# Config factory
# ---------------------------------------------------------------------------

def _make_config(**overrides: object) -> Config:
    """Build a minimal Config for exit tests.

    Defaults to options_mode="paper" with stop_pct=0.50.
    """
    base: dict[str, object] = dict(
        live_trading=False,
        executor_backend="sim",
        db_path="data/arbiter.db",
        audit_path="data/audit.jsonl",
        metrics_path="data/metrics.jsonl",
        max_position_pct=0.05,
        max_sector_pct=0.20,
        max_gross_pct=0.80,
        max_open_positions=20,
        adv_cap_pct=0.02,
        alpaca_api_key="",
        alpaca_secret_key="",
        alpaca_paper_base_url="https://paper-api.alpaca.markets",
        alpaca_data_base_url="https://data.alpaca.markets",
        alpaca_timeout=20.0,
        edgar_user_agent="",
        kill_switch_url="",
        alert_webhook_url="",
        options_mode="paper",
        option_premium_stop_pct=0.50,
    )
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Fixture: fake DB connection (not written to by exit.py)
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_conn() -> sqlite3.Connection:
    """In-memory SQLite connection — not used by exit.py but satisfies the signature."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Helpers: fake client builder
# ---------------------------------------------------------------------------

def _make_client_with_mid(mid: float) -> MagicMock:
    """Return a fake client whose snapshot returns the given mid (via bid=ask=mid)."""
    client = MagicMock()
    client.snapshot.return_value = {
        "AAPL260101C00150000": {
            "bid": mid,
            "ask": mid,
            "iv": 0.30,
            "delta": 0.75,
        }
    }
    return client


def _make_client_no_mid() -> MagicMock:
    """Return a fake client whose snapshot returns a dict with no bid/ask."""
    client = MagicMock()
    client.snapshot.return_value = {
        "AAPL260101C00150000": {
            "bid": None,
            "ask": None,
            "iv": 0.30,
        }
    }
    return client


def _make_client_snapshot_fails() -> MagicMock:
    """Return a fake client whose snapshot raises an exception."""
    client = MagicMock()
    client.snapshot.side_effect = RuntimeError("network timeout")
    return client


# ---------------------------------------------------------------------------
# Fixed test parameters
# ---------------------------------------------------------------------------

_OCC = "AAPL260101C00150000"
_IDEA_ID = "01HZ0000000000000000000001"
_OPEN_TS = "2026-06-01T10:00:00+00:00"
_UNDERLYING = "AAPL"

# entry_premium = $500 total (e.g. 2 contracts × $2.50 mid × 100)
_ENTRY_PREMIUM = 500.0
_CONTRACTS_QTY = 2

# Horizon well in the future — won't fire unless overridden.
_HORIZON_FAR = datetime.date(2026, 12, 31)
# Horizon in the past — will fire.
_HORIZON_PAST = datetime.date(2026, 1, 1)

# "now" as_of string.
_AS_OF = "2026-06-15T10:00:00+00:00"

# Convictions: +0.80 bullish (original), -0.80 bearish (reversal partner).
_BULLISH = 0.80
_BEARISH = -0.80


# ---------------------------------------------------------------------------
# Guard: non-paper mode returns None immediately (no network call)
# ---------------------------------------------------------------------------

class TestModeGuard:
    def test_off_mode_returns_none(self, fake_conn):
        cfg = _make_config(options_mode="off")
        client = MagicMock()
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=_HORIZON_FAR,
            original_conviction=_BULLISH,
            current_conviction=_BULLISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result is None
        client.snapshot.assert_not_called()

    def test_shadow_mode_returns_none(self, fake_conn):
        cfg = _make_config(options_mode="shadow")
        client = MagicMock()
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=_HORIZON_FAR,
            original_conviction=_BULLISH,
            current_conviction=_BULLISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result is None
        client.snapshot.assert_not_called()


# ---------------------------------------------------------------------------
# Premium stop trigger
# ---------------------------------------------------------------------------

class TestPremiumStop:
    def test_fires_when_premium_at_exactly_threshold(self, fake_conn):
        # stop_pct=0.50: threshold = 500 * 0.50 = 250
        # mid that produces total_premium exactly at threshold:
        #   mid × 2 contracts × 100 = 250 → mid = 1.25
        cfg = _make_config(option_premium_stop_pct=0.50)
        client = _make_client_with_mid(1.25)
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=_HORIZON_FAR,
            original_conviction=_BULLISH,
            current_conviction=_BULLISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result == "premium_stop"

    def test_fires_when_premium_below_threshold(self, fake_conn):
        # mid=1.00 → total 200 < threshold 250
        cfg = _make_config(option_premium_stop_pct=0.50)
        client = _make_client_with_mid(1.00)
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=_HORIZON_FAR,
            original_conviction=_BULLISH,
            current_conviction=_BULLISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result == "premium_stop"

    def test_does_not_fire_when_premium_above_threshold(self, fake_conn):
        # mid=2.00 → total 400 > threshold 250 — no stop
        cfg = _make_config(option_premium_stop_pct=0.50)
        client = _make_client_with_mid(2.00)
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=_HORIZON_FAR,
            original_conviction=_BULLISH,
            current_conviction=_BULLISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result is None

    def test_stop_pct_one_always_fires_unless_at_entry(self, fake_conn):
        # stop_pct=1.0 → threshold = 0.0; fires only when current_premium <= 0,
        # which can't happen.  Use stop_pct=0.99 → threshold ≈ 5; a mid of 0.04
        # (total = 8 > 5) should NOT fire the stop.
        cfg = _make_config(option_premium_stop_pct=0.99)
        # mid=0.04 → total = 0.04 × 2 × 100 = 8 > threshold (500 × 0.01 = 5)
        client = _make_client_with_mid(0.04)
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=_HORIZON_FAR,
            original_conviction=_BULLISH,
            current_conviction=_BULLISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Horizon trigger
# ---------------------------------------------------------------------------

class TestHorizonTrigger:
    def test_fires_when_as_of_equals_horizon(self, fake_conn):
        # as_of date == horizon_date → fires
        cfg = _make_config()
        # Use a mid well above stop so stop doesn't fire.
        client = _make_client_with_mid(10.0)
        horizon = datetime.date(2026, 6, 15)  # matches _AS_OF[:10]
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=horizon,
            original_conviction=_BULLISH,
            current_conviction=_BULLISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result == "horizon"

    def test_fires_when_as_of_past_horizon(self, fake_conn):
        cfg = _make_config()
        client = _make_client_with_mid(10.0)
        horizon = datetime.date(2026, 6, 1)  # past _AS_OF date (2026-06-15)
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=horizon,
            original_conviction=_BULLISH,
            current_conviction=_BULLISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result == "horizon"

    def test_does_not_fire_before_horizon(self, fake_conn):
        cfg = _make_config()
        client = _make_client_with_mid(10.0)
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=_HORIZON_FAR,
            original_conviction=_BULLISH,
            current_conviction=_BULLISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Conviction reversal trigger
# ---------------------------------------------------------------------------

class TestReversalTrigger:
    def test_fires_on_bullish_to_bearish_flip(self, fake_conn):
        cfg = _make_config()
        client = _make_client_with_mid(10.0)  # premium well above stop
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=_HORIZON_FAR,
            original_conviction=_BULLISH,
            current_conviction=_BEARISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result == "reversal"

    def test_fires_on_bearish_to_bullish_flip(self, fake_conn):
        cfg = _make_config()
        client = _make_client_with_mid(10.0)
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=_HORIZON_FAR,
            original_conviction=_BEARISH,
            current_conviction=_BULLISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result == "reversal"

    def test_does_not_fire_when_conviction_unchanged(self, fake_conn):
        cfg = _make_config()
        client = _make_client_with_mid(10.0)
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=_HORIZON_FAR,
            original_conviction=_BULLISH,
            current_conviction=0.60,  # same sign, different magnitude
            as_of=_AS_OF,
            config=cfg,
        )
        assert result is None

    def test_does_not_fire_on_zero_current_conviction(self, fake_conn):
        cfg = _make_config()
        client = _make_client_with_mid(10.0)
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=_HORIZON_FAR,
            original_conviction=_BULLISH,
            current_conviction=0.0,  # ambiguous zero
            as_of=_AS_OF,
            config=cfg,
        )
        assert result is None

    def test_does_not_fire_on_zero_original_conviction(self, fake_conn):
        cfg = _make_config()
        client = _make_client_with_mid(10.0)
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=_HORIZON_FAR,
            original_conviction=0.0,
            current_conviction=_BEARISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result is None


# ---------------------------------------------------------------------------
# No-trigger: position stays open
# ---------------------------------------------------------------------------

class TestNoTrigger:
    def test_returns_none_when_no_trigger_fires(self, fake_conn):
        cfg = _make_config()
        # Premium well above stop, horizon far in future, conviction unchanged.
        client = _make_client_with_mid(10.0)
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=_HORIZON_FAR,
            original_conviction=_BULLISH,
            current_conviction=_BULLISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Fail-closed: snapshot failures / missing mid
# ---------------------------------------------------------------------------

class TestSnapshotFailures:
    def test_snapshot_exception_skips_stop_checks_others(self, fake_conn):
        # When snapshot raises, we skip the stop check but still evaluate
        # horizon and reversal.  Here horizon should fire (as_of >= horizon).
        cfg = _make_config()
        client = _make_client_snapshot_fails()
        horizon = datetime.date(2026, 6, 1)  # past
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=horizon,
            original_conviction=_BULLISH,
            current_conviction=_BULLISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result == "horizon"

    def test_snapshot_exception_returns_none_when_no_other_trigger(self, fake_conn):
        # Snapshot fails, no horizon fire, no reversal → None (fail-closed).
        cfg = _make_config()
        client = _make_client_snapshot_fails()
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=_HORIZON_FAR,
            original_conviction=_BULLISH,
            current_conviction=_BULLISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result is None

    def test_no_mid_in_snapshot_skips_stop_check(self, fake_conn):
        # No bid/ask → cannot evaluate stop → still checks horizon+reversal.
        cfg = _make_config()
        client = _make_client_no_mid()
        # Reversal should fire.
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=_HORIZON_FAR,
            original_conviction=_BULLISH,
            current_conviction=_BEARISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result == "reversal"

    def test_no_mid_returns_none_when_no_other_trigger(self, fake_conn):
        cfg = _make_config()
        client = _make_client_no_mid()
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=_HORIZON_FAR,
            original_conviction=_BULLISH,
            current_conviction=_BULLISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Priority: stop fires before horizon and reversal when all three would fire
# ---------------------------------------------------------------------------

class TestTriggerPriority:
    def test_premium_stop_beats_horizon_and_reversal(self, fake_conn):
        # All three triggers fire: stop wins.
        cfg = _make_config(option_premium_stop_pct=0.50)
        # mid=1.00 → total 200 < 250 threshold → stop fires.
        client = _make_client_with_mid(1.00)
        horizon = datetime.date(2026, 1, 1)  # in the past
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=horizon,
            original_conviction=_BULLISH,
            current_conviction=_BEARISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result == "premium_stop"

    def test_horizon_beats_reversal_when_stop_clear(self, fake_conn):
        # Stop does NOT fire (premium above threshold); horizon fires; reversal also would.
        cfg = _make_config()
        # mid=10.0 → total 2000 >> 250 → stop does not fire.
        client = _make_client_with_mid(10.0)
        horizon = datetime.date(2026, 1, 1)  # in the past
        result = premium_stop_exit(
            fake_conn, client,
            occ_symbol=_OCC,
            entry_premium=_ENTRY_PREMIUM,
            contracts_qty=_CONTRACTS_QTY,
            idea_id=_IDEA_ID,
            open_ts=_OPEN_TS,
            underlying=_UNDERLYING,
            thesis_horizon_date=horizon,
            original_conviction=_BULLISH,
            current_conviction=_BEARISH,
            as_of=_AS_OF,
            config=cfg,
        )
        assert result == "horizon"
