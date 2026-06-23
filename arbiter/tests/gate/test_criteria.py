"""Tests for arbiter.gate.criteria — paper→live gate criteria evaluation.

Covers:
- All criteria met → GateResult.passed=True, failing=[]
- Each single criterion failing → GateResult.passed=False with the criterion named
- CRITERIA_HASH is stable across calls
- Mid-run criteria-hash change is detected and rejected (CriteriaHashMismatch)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from arbiter.gate.criteria import (
    CRITERIA_HASH,
    CriteriaHashMismatch,
    GateResult,
    TradeStats,
    evaluate,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

UTC = timezone.utc

AS_OF = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)
KILL_SWITCH_TESTED = AS_OF - timedelta(days=5)   # 5 days ago — well within 30 days


def _passing_stats(**overrides) -> TradeStats:
    """Return a TradeStats that passes all criteria, with optional overrides."""
    base = dict(
        trading_days=75,
        closed_trades=40,
        sharpe=1.5,
        max_drawdown=0.05,        # 5 % — below the 8 % limit
        breakers_clear=True,
        kill_switch_last_tested_at=KILL_SWITCH_TESTED,
    )
    base.update(overrides)
    return TradeStats(**base)


def _mem_conn_with_schema() -> sqlite3.Connection:
    """Return an in-memory connection with the gate_hash_lock table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gate_hash_lock (
            run_id        TEXT PRIMARY KEY,
            criteria_hash TEXT NOT NULL,
            locked_at     TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Happy-path: all criteria met
# ---------------------------------------------------------------------------

class TestAllCriteriaMet:
    def test_passed_true(self):
        result = evaluate(_passing_stats(), as_of=AS_OF)
        assert result.passed is True

    def test_failing_empty(self):
        result = evaluate(_passing_stats(), as_of=AS_OF)
        assert result.failing == []

    def test_criteria_hash_present(self):
        result = evaluate(_passing_stats(), as_of=AS_OF)
        assert result.criteria_hash == CRITERIA_HASH
        assert len(CRITERIA_HASH) == 64   # SHA-256 hex


# ---------------------------------------------------------------------------
# Single-criterion failures — each must be named in failing list
# ---------------------------------------------------------------------------

class TestSingleCriterionFail:

    def test_insufficient_trading_days(self):
        stats = _passing_stats(trading_days=59)
        result = evaluate(stats, as_of=AS_OF)
        assert result.passed is False
        assert any("trading_days" in msg for msg in result.failing), result.failing
        # All other criteria should NOT appear in failing
        assert not any("closed_trades" in msg for msg in result.failing)

    def test_insufficient_closed_trades(self):
        stats = _passing_stats(closed_trades=29)
        result = evaluate(stats, as_of=AS_OF)
        assert result.passed is False
        assert any("closed_trades" in msg for msg in result.failing), result.failing
        assert not any("trading_days" in msg for msg in result.failing)

    def test_sharpe_too_low(self):
        stats = _passing_stats(sharpe=0.99)
        result = evaluate(stats, as_of=AS_OF)
        assert result.passed is False
        assert any("sharpe" in msg for msg in result.failing), result.failing

    def test_sharpe_exactly_threshold_passes(self):
        stats = _passing_stats(sharpe=1.0)
        result = evaluate(stats, as_of=AS_OF)
        assert result.passed is True

    def test_drawdown_too_high(self):
        stats = _passing_stats(max_drawdown=0.09)   # 9 % > 8 % limit
        result = evaluate(stats, as_of=AS_OF)
        assert result.passed is False
        assert any("drawdown" in msg for msg in result.failing), result.failing

    def test_drawdown_exactly_threshold_passes(self):
        stats = _passing_stats(max_drawdown=0.08)   # exactly at limit — should pass
        # The criterion is > 0.08 → exactly 0.08 does NOT exceed → passes.
        result = evaluate(stats, as_of=AS_OF)
        assert result.passed is True

    def test_breakers_not_clear(self):
        stats = _passing_stats(breakers_clear=False)
        result = evaluate(stats, as_of=AS_OF)
        assert result.passed is False
        assert any("breaker" in msg for msg in result.failing), result.failing

    def test_kill_switch_never_tested(self):
        stats = _passing_stats(kill_switch_last_tested_at=None)
        result = evaluate(stats, as_of=AS_OF)
        assert result.passed is False
        assert any("kill_switch" in msg for msg in result.failing), result.failing

    def test_kill_switch_too_old(self):
        old_test = AS_OF - timedelta(days=31)   # 31 days ago > 30-day limit
        stats = _passing_stats(kill_switch_last_tested_at=old_test)
        result = evaluate(stats, as_of=AS_OF)
        assert result.passed is False
        assert any("kill_switch" in msg for msg in result.failing), result.failing

    def test_kill_switch_exactly_30_days_passes(self):
        # tested exactly 30 days ago — boundary: age_days == 30.0 → NOT > 30 → passes
        tested_at = AS_OF - timedelta(days=30)
        stats = _passing_stats(kill_switch_last_tested_at=tested_at)
        result = evaluate(stats, as_of=AS_OF)
        assert result.passed is True

    def test_kill_switch_just_over_30_days_fails(self):
        tested_at = AS_OF - timedelta(days=30, seconds=1)
        stats = _passing_stats(kill_switch_last_tested_at=tested_at)
        result = evaluate(stats, as_of=AS_OF)
        assert result.passed is False
        assert any("kill_switch" in msg for msg in result.failing), result.failing

    def test_multiple_failures_all_named(self):
        """Two failing criteria → both appear in failing list."""
        stats = _passing_stats(trading_days=10, closed_trades=5)
        result = evaluate(stats, as_of=AS_OF)
        assert result.passed is False
        assert any("trading_days" in msg for msg in result.failing)
        assert any("closed_trades" in msg for msg in result.failing)


# ---------------------------------------------------------------------------
# Criteria hash stability
# ---------------------------------------------------------------------------

class TestCriteriaHashStable:

    def test_hash_is_deterministic(self):
        """CRITERIA_HASH must be the same every time the module is imported."""
        from arbiter.gate import criteria as _mod
        assert _mod.CRITERIA_HASH == CRITERIA_HASH

    def test_hash_in_gate_result_matches_module_constant(self):
        result = evaluate(_passing_stats(), as_of=AS_OF)
        assert result.criteria_hash == CRITERIA_HASH

    def test_hash_is_64_hex_chars(self):
        assert len(CRITERIA_HASH) == 64
        int(CRITERIA_HASH, 16)   # raises ValueError if not valid hex


# ---------------------------------------------------------------------------
# Mid-run criteria change detection (hash-lock)
# ---------------------------------------------------------------------------

class TestCriteriaHashLock:

    def test_first_call_locks_hash(self):
        conn = _mem_conn_with_schema()
        run_id = "TEST_RUN_001"

        evaluate(_passing_stats(), as_of=AS_OF, conn=conn, run_id=run_id)

        row = conn.execute(
            "SELECT criteria_hash FROM gate_hash_lock WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == CRITERIA_HASH

    def test_second_call_same_hash_succeeds(self):
        conn = _mem_conn_with_schema()
        run_id = "TEST_RUN_002"

        evaluate(_passing_stats(), as_of=AS_OF, conn=conn, run_id=run_id)
        # Second evaluation — same hash — must NOT raise
        result = evaluate(_passing_stats(), as_of=AS_OF, conn=conn, run_id=run_id)
        assert result.criteria_hash == CRITERIA_HASH

    def test_mid_run_criteria_change_is_rejected(self):
        """Simulate a mid-run criteria change by manually inserting a stale hash."""
        conn = _mem_conn_with_schema()
        run_id = "TEST_RUN_003"
        stale_hash = "a" * 64   # definitely not CRITERIA_HASH

        # Lock a different hash for this run_id (as if criteria were different at start)
        conn.execute(
            "INSERT INTO gate_hash_lock (run_id, criteria_hash, locked_at) VALUES (?, ?, ?)",
            (run_id, stale_hash, AS_OF.isoformat()),
        )
        conn.commit()

        with pytest.raises(CriteriaHashMismatch) as exc_info:
            evaluate(_passing_stats(), as_of=AS_OF, conn=conn, run_id=run_id)

        assert run_id in str(exc_info.value)
        assert stale_hash in str(exc_info.value)
        assert CRITERIA_HASH in str(exc_info.value)

    def test_conn_without_run_id_raises_value_error(self):
        conn = _mem_conn_with_schema()
        with pytest.raises(ValueError, match="both"):
            evaluate(_passing_stats(), as_of=AS_OF, conn=conn, run_id=None)

    def test_run_id_without_conn_raises_value_error(self):
        with pytest.raises(ValueError, match="both"):
            evaluate(_passing_stats(), as_of=AS_OF, conn=None, run_id="X")
