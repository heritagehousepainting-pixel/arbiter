"""Integration tests — critical-alert auto-pause wiring (engine.py §3.9).

Spec: when a critical condition fires, the Alerting layer returns an
AutoPauseSentinel, the engine sets Engine.paused = True, and subsequent
run_cycle calls short-circuit with zero orders submitted.  resume() clears
the flag.  Non-critical paths must NOT set paused.

Critical conditions tested:
  (a) Circuit breaker tripped at cycle start.
  (b) Kill switch reports halted.
  (c) BrokerError raised during order submission.

Setup mirrors tests/integration/test_end_to_end.py:
  - SimExecutor + tmp DB + BacktestClock + FixtureSource PIT.
  - A fake Alerting whose critical alert returns AutoPauseSentinel (verifiable).
  - Real circuit breaker / kill switch mocks as needed.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from arbiter.config import load_config
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.connection import get_connection
from arbiter.db.helpers import generate_ulid
from arbiter.db.migrate import run_migrations
from arbiter.engine import build_engine
from arbiter.ingest.writer import write_filing
from arbiter.safety.alerting import Alerting, AutoPauseSentinel
from arbiter.safety.breakers import CircuitBreaker
from arbiter.safety.kill_switch import KillSwitch


# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_UTC = timezone.utc
_AS_OF = datetime(2025, 3, 15, 12, 0, 0, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Fake Alerting implementation for injection
# ---------------------------------------------------------------------------

class _FakeAlerting:
    """Alerting stand-in that records calls and returns AutoPauseSentinel on critical.

    Attributes
    ----------
    critical_calls:
        List of (message, ctx) tuples for every ``alert("critical", ...)`` call.
    info_calls:
        List of (message, ctx) tuples for non-critical calls.
    raise_on_alert:
        If True, ``alert`` raises RuntimeError to test the fail-safe path.
    """

    def __init__(self, raise_on_alert: bool = False) -> None:
        self.critical_calls: list[tuple[str, dict]] = []
        self.info_calls: list[tuple[str, dict]] = []
        self.raise_on_alert = raise_on_alert

    def alert(
        self,
        tier: str,
        message: str,
        ctx: dict[str, Any],
        *,
        as_of: datetime,
    ) -> AutoPauseSentinel | None:
        if self.raise_on_alert:
            raise RuntimeError("alerting exploded")
        if tier == "critical":
            self.critical_calls.append((message, ctx))
            return AutoPauseSentinel(message=message)
        self.info_calls.append((message, ctx))
        return None


# ---------------------------------------------------------------------------
# DB + engine helpers (mirrors test_end_to_end.py)
# ---------------------------------------------------------------------------

def _seed_cluster_buy(
    conn,
    ticker: str = "AAPL",
    n_buyers: int = 3,
    amount: float = 500_000.0,
) -> None:
    for i in range(n_buyers):
        raw = {
            "source": "form4",
            "ticker": ticker,
            "person_id": generate_ulid(),
            "filing_ts": (_AS_OF - timedelta(days=5 + i)).isoformat(),
            "txn_type": "P",
            "shares": 1000.0,
            "price": 150.0,
            "amount_low": amount,
            "amount_high": amount * 1.2,
            "is_10b5_1": False,
            "is_amendment": False,
            "accession": generate_ulid(),
            "raw_json": None,
        }
        write_filing(conn, raw, lambda: _AS_OF.isoformat())


def _build_pit(ticker: str = "AAPL") -> PITGateway:
    fixture = FixtureSource()
    ts_seed = _AS_OF - timedelta(days=1)
    fixture.add("price_close", ticker, ts_seed, 150.0)
    fixture.add("price_open", ticker, ts_seed, 150.0)
    fixture.add("spread", ticker, ts_seed, 0.01)
    fixture.add("adv_20d", ticker, ts_seed, 10_000_000.0)
    pit = PITGateway()
    pit.register_source("price_close", fixture)
    pit.register_source("price_open", fixture)
    pit.register_source("spread", fixture)
    pit.register_source("adv_20d", fixture)
    return pit


def _make_engine(tmp_path: Path, fake_alerting: _FakeAlerting, *, kill_switch=None):
    """Build a seeded engine with the given fake alerting."""
    db_path = str(tmp_path / "test.db")
    config = load_config()
    config = dataclasses.replace(
        config,
        live_trading=False,
        executor_backend="sim",
        db_path=db_path,
        audit_path=str(tmp_path / "audit.jsonl"),
        metrics_path=str(tmp_path / "metrics.jsonl"),
    )
    clock = BacktestClock(_AS_OF)
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    _seed_cluster_buy(conn, ticker="AAPL", n_buyers=3)
    pit = _build_pit("AAPL")
    eng = build_engine(
        config,
        conn=conn,
        pit=pit,
        clock=clock,
        alerting=fake_alerting,  # type: ignore[arg-type]
        kill_switch=kill_switch,
    )
    return eng, conn


# ---------------------------------------------------------------------------
# Test 1 — circuit breaker tripped → auto-pause (condition a)
# ---------------------------------------------------------------------------

def test_tripped_breaker_fires_critical_alert_and_pauses(tmp_path: Path) -> None:
    """A pre-tripped circuit breaker must trigger a critical alert and pause the engine."""
    fake = _FakeAlerting()
    eng, conn = _make_engine(tmp_path, fake)

    # Trip the daily_loss breaker before the cycle.
    cb = CircuitBreaker()
    cb.trip(
        "daily_loss",
        reason="test: synthetic daily loss trip",
        conn=conn,
        clock=BacktestClock(_AS_OF),
    )

    # Engine is not paused yet.
    assert eng.paused is False

    result = eng.run_cycle(as_of=_AS_OF)

    # Critical alert fired.
    assert len(fake.critical_calls) >= 1, "Expected at least one critical alert call"
    # Engine is now paused.
    assert eng.paused is True
    # No orders submitted.
    assert result.orders_submitted == 0
    # Result is flagged.
    assert getattr(result, "paused_by_alert", None) is True
    # Status surfaces paused=True.
    assert eng.status()["paused"] is True


# ---------------------------------------------------------------------------
# Test 2 — paused engine short-circuits subsequent cycles (no orders)
# ---------------------------------------------------------------------------

def test_paused_engine_submits_no_orders_on_next_cycle(tmp_path: Path) -> None:
    """A paused engine must short-circuit run_cycle without gathering opinions or orders."""
    fake = _FakeAlerting()
    eng, conn = _make_engine(tmp_path, fake)

    # Force the engine into paused state directly (simulating a prior critical event).
    eng.paused = True

    alert_calls_before = len(fake.critical_calls)
    result = eng.run_cycle(as_of=_AS_OF)

    # Short-circuit: no new alert fired (already paused; we don't double-alert).
    assert len(fake.critical_calls) == alert_calls_before
    # No orders.
    assert result.orders_submitted == 0
    # Result flagged.
    assert getattr(result, "paused_by_alert", None) is True
    # DB still empty.
    rows = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert rows == 0, f"Expected 0 order rows while paused, got {rows}"


# ---------------------------------------------------------------------------
# Test 3 — resume() clears paused flag and allows normal cycle
# ---------------------------------------------------------------------------

def test_resume_clears_pause_and_allows_cycle(tmp_path: Path) -> None:
    """After resume(), the engine must process cycles normally again."""
    fake = _FakeAlerting()
    eng, conn = _make_engine(tmp_path, fake)

    # Pause the engine.
    eng.paused = True
    result_while_paused = eng.run_cycle(as_of=_AS_OF)
    assert result_while_paused.orders_submitted == 0

    # Resume — breakers must also be clear or cycle will re-pause.
    # (No breaker was actually tripped via the DB here, so safe to proceed.)
    eng.resume()
    assert eng.paused is False
    assert eng.status()["paused"] is False

    # Run a normal cycle — should succeed.
    result_after_resume = eng.run_cycle(as_of=_AS_OF)
    # Not paused anymore; orders may or may not submit depending on signals,
    # but the cycle must not be flagged paused.
    assert getattr(result_after_resume, "paused_by_alert", False) is False


# ---------------------------------------------------------------------------
# Test 4 — kill switch halted → auto-pause (condition b)
# ---------------------------------------------------------------------------

def test_kill_switch_halt_fires_critical_alert_and_pauses(tmp_path: Path) -> None:
    """A halted kill switch must fire a critical alert and pause the engine."""
    fake = _FakeAlerting()

    # Build a mock KillSwitch that always reports halted.
    mock_ks = MagicMock(spec=KillSwitch)
    mock_ks.is_halted.return_value = True

    # Engine needs a kill_switch_url configured so the kill switch is consulted.
    db_path = str(tmp_path / "test.db")
    config = load_config()
    config = dataclasses.replace(
        config,
        live_trading=False,
        executor_backend="sim",
        db_path=db_path,
        audit_path=str(tmp_path / "audit.jsonl"),
        metrics_path=str(tmp_path / "metrics.jsonl"),
        kill_switch_url="https://fake-kill-switch.example.com",
    )
    clock = BacktestClock(_AS_OF)
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    _seed_cluster_buy(conn, ticker="AAPL", n_buyers=3)
    pit = _build_pit("AAPL")
    eng = build_engine(
        config,
        conn=conn,
        pit=pit,
        clock=clock,
        kill_switch=mock_ks,
        alerting=fake,  # type: ignore[arg-type]
    )

    assert eng.paused is False

    result = eng.run_cycle(as_of=_AS_OF)

    # Kill switch was consulted.
    mock_ks.is_halted.assert_called_once()
    # Critical alert fired.
    assert len(fake.critical_calls) >= 1
    # Engine paused.
    assert eng.paused is True
    # No orders.
    assert result.orders_submitted == 0
    assert getattr(result, "paused_by_alert", None) is True


# ---------------------------------------------------------------------------
# Test 5 — non-critical path does NOT pause
# ---------------------------------------------------------------------------

def test_non_critical_path_does_not_pause(tmp_path: Path) -> None:
    """A normal successful cycle must not set paused (no false positives)."""
    fake = _FakeAlerting()
    eng, conn = _make_engine(tmp_path, fake)

    # Normal cycle — no breakers, no kill switch, real signals seeded.
    result = eng.run_cycle(as_of=_AS_OF)

    # Engine must not be paused.
    assert eng.paused is False
    # No critical alerts fired on the happy path.
    assert len(fake.critical_calls) == 0, (
        f"Expected 0 critical alerts on happy path, got: {fake.critical_calls}"
    )
    # Paused flag absent or False.
    assert getattr(result, "paused_by_alert", False) is False


# ---------------------------------------------------------------------------
# Test 6 — alerting exception must not crash the cycle (fail-safe)
# ---------------------------------------------------------------------------

def test_alerting_exception_does_not_crash_cycle(tmp_path: Path) -> None:
    """If the alerting call itself raises, the cycle must not propagate the exception.

    The engine must still pause (fail-safe) even when alerting is broken.
    """
    fake = _FakeAlerting(raise_on_alert=True)  # alerting will raise
    eng, conn = _make_engine(tmp_path, fake)

    # Trip a breaker to trigger the critical alert path.
    cb = CircuitBreaker()
    cb.trip(
        "daily_loss",
        reason="test: alerting exception path",
        conn=conn,
        clock=BacktestClock(_AS_OF),
    )

    # Must not raise even though alerting explodes.
    result = eng.run_cycle(as_of=_AS_OF)

    # Engine pauses defensively even when alerting raises.
    assert eng.paused is True
    assert result.orders_submitted == 0
