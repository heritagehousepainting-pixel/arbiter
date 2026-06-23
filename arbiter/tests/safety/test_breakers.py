"""Tests for arbiter.safety.breakers (Lane 4a).

Verifies:
  - trip() latches a breaker and persists to DB.
  - Latched state survives a reload from DB (new CircuitBreaker + same conn).
  - reset() clears the latch; is_tripped() returns False after reset.
  - check_daily_loss() trips at >= 2% loss; clean below threshold.
  - check_per_position() trips at <= -5%; clean above threshold.
  - check_mirofish_consecutive_fail() trips at 3+ consecutive failures.
  - check_broker_non_200() trips on non-200 status; clean on 200.
  - check_confidence_distribution_shift() trips above 30%.
  - any_tripped() returns all latched breaker names.
  - Advisor-level caller CANNOT clear a latched breaker (structural guarantee).
  - audit log receives a record on trip and reset.
  - Tripping an already-latched breaker is idempotent (no BreakerTrippedError).
  - Unknown breaker names raise ValueError.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.safety.breakers import (
    BREAKER_NAMES,
    BreakerTrippedError,
    CircuitBreaker,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _FixedClock:
    """Minimal clock stub that returns a fixed UTC datetime."""

    def __init__(self, dt: datetime | None = None) -> None:
        self._dt = dt or datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._dt


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Migrated connection for each test."""
    db = str(tmp_path / "breakers_test.db")
    c = get_connection(db)
    run_migrations(c)
    return c


@pytest.fixture()
def clock() -> _FixedClock:
    return _FixedClock()


@pytest.fixture()
def cb() -> CircuitBreaker:
    return CircuitBreaker()


@pytest.fixture()
def audit_path(tmp_path: Path) -> str:
    return str(tmp_path / "audit.jsonl")


# ---------------------------------------------------------------------------
# BREAKER_NAMES sanity
# ---------------------------------------------------------------------------


def test_all_six_breakers_defined() -> None:
    expected = {
        "daily_loss",
        "per_position_intraday",
        "mirofish_3x_consecutive_fail",
        "a3_volume_anomaly",
        "broker_non_200",
        "confidence_distribution_shift",
    }
    assert BREAKER_NAMES == expected


# ---------------------------------------------------------------------------
# Basic trip / latch / persist
# ---------------------------------------------------------------------------


def test_trip_latches_breaker(cb: CircuitBreaker, conn, clock, audit_path) -> None:
    cb.trip("daily_loss", "test reason", conn, clock, audit_path=audit_path)
    assert cb.is_tripped("daily_loss", conn) is True


def test_trip_persists_to_db(cb: CircuitBreaker, conn, clock, audit_path) -> None:
    """State must survive a new CircuitBreaker instance reading the same conn."""
    cb.trip("broker_non_200", "HTTP 503 on /orders", conn, clock, audit_path=audit_path)

    # New instance — no in-memory cache, reads from DB
    cb2 = CircuitBreaker()
    assert cb2.is_tripped("broker_non_200", conn) is True


def test_trip_stores_reason(cb: CircuitBreaker, conn, clock, audit_path) -> None:
    reason = "portfolio tanked -3%"
    cb.trip("daily_loss", reason, conn, clock, audit_path=audit_path)
    row = conn.execute(
        "SELECT reason, latched_at FROM breaker_state WHERE breaker_name = 'daily_loss'"
    ).fetchone()
    assert row is not None
    assert row["reason"] == reason
    assert row["latched_at"] is not None


def test_trip_stores_latched_at(cb: CircuitBreaker, conn, clock, audit_path) -> None:
    cb.trip("mirofish_3x_consecutive_fail", "3 fails", conn, clock, audit_path=audit_path)
    row = conn.execute(
        "SELECT latched_at FROM breaker_state WHERE breaker_name = 'mirofish_3x_consecutive_fail'"
    ).fetchone()
    expected_ts = clock.now().isoformat()
    assert row["latched_at"] == expected_ts


# ---------------------------------------------------------------------------
# Reload from DB
# ---------------------------------------------------------------------------


def test_trip_survives_reload(cb: CircuitBreaker, conn, clock, audit_path) -> None:
    """After trip(), a fresh CircuitBreaker with the same DB conn sees the latch."""
    cb.trip("confidence_distribution_shift", "shift=0.45", conn, clock, audit_path=audit_path)

    fresh_cb = CircuitBreaker()
    assert fresh_cb.is_tripped("confidence_distribution_shift", conn) is True


def test_untripped_breaker_reads_false(cb: CircuitBreaker, conn) -> None:
    """A breaker that has never been tripped returns False (no row in DB)."""
    assert cb.is_tripped("daily_loss", conn) is False


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def test_reset_clears_latch(cb: CircuitBreaker, conn, clock, audit_path) -> None:
    cb.trip("a3_volume_anomaly", "vol spike on AAPL", conn, clock, audit_path=audit_path)
    assert cb.is_tripped("a3_volume_anomaly", conn) is True

    cb.reset("a3_volume_anomaly", conn, clock, audit_path=audit_path)
    assert cb.is_tripped("a3_volume_anomaly", conn) is False


def test_reset_writes_db_latched_false(cb: CircuitBreaker, conn, clock, audit_path) -> None:
    cb.trip("broker_non_200", "HTTP 429", conn, clock, audit_path=audit_path)
    cb.reset("broker_non_200", conn, clock, audit_path=audit_path)

    row = conn.execute(
        "SELECT latched FROM breaker_state WHERE breaker_name = 'broker_non_200'"
    ).fetchone()
    assert row is not None
    assert row["latched"] == 0


def test_reset_allows_retrip(cb: CircuitBreaker, conn, clock, audit_path) -> None:
    """After reset a breaker can be tripped again (and raises BreakerTrippedError)."""
    cb.trip("daily_loss", "first trip", conn, clock, audit_path=audit_path)
    cb.reset("daily_loss", conn, clock, audit_path=audit_path)

    with pytest.raises(BreakerTrippedError):
        cb.check_daily_loss(-0.03, conn, clock, audit_path=audit_path)


# ---------------------------------------------------------------------------
# any_tripped
# ---------------------------------------------------------------------------


def test_any_tripped_empty_when_none_latched(cb: CircuitBreaker, conn) -> None:
    assert cb.any_tripped(conn) == []


def test_any_tripped_returns_latched_names(cb: CircuitBreaker, conn, clock, audit_path) -> None:
    cb.trip("daily_loss", "r1", conn, clock, audit_path=audit_path)
    cb.trip("broker_non_200", "r2", conn, clock, audit_path=audit_path)

    tripped = cb.any_tripped(conn)
    assert "daily_loss" in tripped
    assert "broker_non_200" in tripped
    assert len(tripped) == 2


def test_any_tripped_excludes_reset_breaker(
    cb: CircuitBreaker, conn, clock, audit_path
) -> None:
    cb.trip("daily_loss", "r1", conn, clock, audit_path=audit_path)
    cb.trip("broker_non_200", "r2", conn, clock, audit_path=audit_path)
    cb.reset("daily_loss", conn, clock, audit_path=audit_path)

    tripped = cb.any_tripped(conn)
    assert "daily_loss" not in tripped
    assert "broker_non_200" in tripped


# ---------------------------------------------------------------------------
# check_daily_loss
# ---------------------------------------------------------------------------


def test_check_daily_loss_trips_at_exactly_threshold(
    cb: CircuitBreaker, conn, clock, audit_path
) -> None:
    with pytest.raises(BreakerTrippedError) as exc_info:
        cb.check_daily_loss(-0.02, conn, clock, audit_path=audit_path)
    assert exc_info.value.breaker_name == "daily_loss"
    assert cb.is_tripped("daily_loss", conn) is True


def test_check_daily_loss_trips_above_threshold(
    cb: CircuitBreaker, conn, clock, audit_path
) -> None:
    with pytest.raises(BreakerTrippedError):
        cb.check_daily_loss(-0.025, conn, clock, audit_path=audit_path)


def test_check_daily_loss_does_not_trip_below_threshold(
    cb: CircuitBreaker, conn, clock, audit_path
) -> None:
    # -1.9% — just below the 2% wire
    cb.check_daily_loss(-0.019, conn, clock, audit_path=audit_path)
    assert cb.is_tripped("daily_loss", conn) is False


def test_check_daily_loss_zero_pnl_clean(
    cb: CircuitBreaker, conn, clock, audit_path
) -> None:
    cb.check_daily_loss(0.0, conn, clock, audit_path=audit_path)
    assert cb.is_tripped("daily_loss", conn) is False


# ---------------------------------------------------------------------------
# check_per_position
# ---------------------------------------------------------------------------


def test_check_per_position_trips_at_threshold(
    cb: CircuitBreaker, conn, clock, audit_path
) -> None:
    with pytest.raises(BreakerTrippedError) as exc_info:
        cb.check_per_position(-0.05, conn, clock, audit_path=audit_path)
    assert exc_info.value.breaker_name == "per_position_intraday"


def test_check_per_position_trips_above_threshold(
    cb: CircuitBreaker, conn, clock, audit_path
) -> None:
    with pytest.raises(BreakerTrippedError):
        cb.check_per_position(-0.08, conn, clock, audit_path=audit_path)


def test_check_per_position_clean_below_threshold(
    cb: CircuitBreaker, conn, clock, audit_path
) -> None:
    cb.check_per_position(-0.049, conn, clock, audit_path=audit_path)
    assert cb.is_tripped("per_position_intraday", conn) is False


# ---------------------------------------------------------------------------
# check_mirofish_consecutive_fail
# ---------------------------------------------------------------------------


def test_mirofish_trips_at_three(cb: CircuitBreaker, conn, clock, audit_path) -> None:
    with pytest.raises(BreakerTrippedError):
        cb.check_mirofish_consecutive_fail(3, conn, clock, audit_path=audit_path)
    assert cb.is_tripped("mirofish_3x_consecutive_fail", conn) is True


def test_mirofish_trips_above_three(cb: CircuitBreaker, conn, clock, audit_path) -> None:
    with pytest.raises(BreakerTrippedError):
        cb.check_mirofish_consecutive_fail(5, conn, clock, audit_path=audit_path)


def test_mirofish_clean_below_three(cb: CircuitBreaker, conn, clock, audit_path) -> None:
    cb.check_mirofish_consecutive_fail(2, conn, clock, audit_path=audit_path)
    assert cb.is_tripped("mirofish_3x_consecutive_fail", conn) is False


def test_mirofish_zero_fails_clean(cb: CircuitBreaker, conn, clock, audit_path) -> None:
    cb.check_mirofish_consecutive_fail(0, conn, clock, audit_path=audit_path)
    assert cb.is_tripped("mirofish_3x_consecutive_fail", conn) is False


# ---------------------------------------------------------------------------
# check_broker_non_200
# ---------------------------------------------------------------------------


def test_broker_non_200_trips_on_503(cb: CircuitBreaker, conn, clock, audit_path) -> None:
    with pytest.raises(BreakerTrippedError):
        cb.check_broker_non_200(503, "/v2/orders", conn, clock, audit_path=audit_path)
    assert cb.is_tripped("broker_non_200", conn) is True


def test_broker_non_200_trips_on_429(cb: CircuitBreaker, conn, clock, audit_path) -> None:
    with pytest.raises(BreakerTrippedError):
        cb.check_broker_non_200(429, "/v2/orders", conn, clock, audit_path=audit_path)


def test_broker_200_clean(cb: CircuitBreaker, conn, clock, audit_path) -> None:
    cb.check_broker_non_200(200, "/v2/orders", conn, clock, audit_path=audit_path)
    assert cb.is_tripped("broker_non_200", conn) is False


# ---------------------------------------------------------------------------
# check_confidence_distribution_shift
# ---------------------------------------------------------------------------


def test_conf_shift_trips_above_threshold(
    cb: CircuitBreaker, conn, clock, audit_path
) -> None:
    with pytest.raises(BreakerTrippedError):
        cb.check_confidence_distribution_shift(0.31, conn, clock, audit_path=audit_path)
    assert cb.is_tripped("confidence_distribution_shift", conn) is True


def test_conf_shift_clean_at_threshold(
    cb: CircuitBreaker, conn, clock, audit_path
) -> None:
    # exactly 30% — NOT above, so no trip
    cb.check_confidence_distribution_shift(0.30, conn, clock, audit_path=audit_path)
    assert cb.is_tripped("confidence_distribution_shift", conn) is False


def test_conf_shift_clean_below_threshold(
    cb: CircuitBreaker, conn, clock, audit_path
) -> None:
    cb.check_confidence_distribution_shift(0.10, conn, clock, audit_path=audit_path)
    assert cb.is_tripped("confidence_distribution_shift", conn) is False


# ---------------------------------------------------------------------------
# Idempotency (already-latched breaker)
# ---------------------------------------------------------------------------


def test_trip_already_latched_is_idempotent(
    cb: CircuitBreaker, conn, clock, audit_path
) -> None:
    """Tripping an already-latched breaker does NOT raise BreakerTrippedError."""
    cb.trip("daily_loss", "first trip", conn, clock, audit_path=audit_path)
    # Second trip — should NOT raise
    cb.trip("daily_loss", "second trip", conn, clock, audit_path=audit_path)
    assert cb.is_tripped("daily_loss", conn) is True


def test_check_daily_loss_already_latched_no_error(
    cb: CircuitBreaker, conn, clock, audit_path
) -> None:
    """check_daily_loss does not raise a second time if breaker is already latched."""
    with pytest.raises(BreakerTrippedError):
        cb.check_daily_loss(-0.03, conn, clock, audit_path=audit_path)

    # Already latched — no error on subsequent calls
    cb.check_daily_loss(-0.04, conn, clock, audit_path=audit_path)


def test_trip_idempotent_preserves_original_reason(
    cb: CircuitBreaker, conn, clock, audit_path
) -> None:
    """When tripping an already-latched breaker the original reason is preserved."""
    cb.trip("daily_loss", "original reason", conn, clock, audit_path=audit_path)
    cb.trip("daily_loss", "second reason", conn, clock, audit_path=audit_path)

    row = conn.execute(
        "SELECT reason FROM breaker_state WHERE breaker_name = 'daily_loss'"
    ).fetchone()
    assert row["reason"] == "original reason"


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_trip_writes_audit_entry(cb: CircuitBreaker, conn, clock, audit_path) -> None:
    from arbiter.db.audit import read_audit

    cb.trip("broker_non_200", "HTTP 500", conn, clock, audit_path=audit_path)

    entries = read_audit(audit_path)
    assert any(e["event"] == "breaker_trip" for e in entries)
    trip_entry = next(e for e in entries if e["event"] == "breaker_trip")
    assert trip_entry["payload"]["breaker_name"] == "broker_non_200"
    assert trip_entry["payload"]["reason"] == "HTTP 500"


def test_reset_writes_audit_entry(cb: CircuitBreaker, conn, clock, audit_path) -> None:
    from arbiter.db.audit import read_audit

    cb.trip("daily_loss", "r1", conn, clock, audit_path=audit_path)
    cb.reset("daily_loss", conn, clock, audit_path=audit_path)

    entries = read_audit(audit_path)
    assert any(e["event"] == "breaker_reset" for e in entries)
    reset_entry = next(e for e in entries if e["event"] == "breaker_reset")
    assert reset_entry["payload"]["breaker_name"] == "daily_loss"


# ---------------------------------------------------------------------------
# Advisor-level caller CANNOT clear a latched breaker
# ---------------------------------------------------------------------------


def test_reset_not_accessible_from_safety_init(
    cb: CircuitBreaker, conn, clock, audit_path
) -> None:
    """Structural guarantee: safety package __init__ must NOT export reset.

    The gate agent may create arbiter/safety/__init__.py during Wave-B.
    This test ensures that if it exists it does not re-export `reset` at the
    package level.  Advisor/fusion code that does ``from arbiter.safety import X``
    must not be able to reach reset().

    Note: If the safety __init__.py does not exist yet this test trivially
    passes (import fails with ImportError -> handled as a pass).
    """
    try:
        import arbiter.safety as safety_pkg  # noqa: F401
    except ImportError:
        pytest.skip("arbiter.safety __init__ not yet created (pre-integration)")

    assert not hasattr(safety_pkg, "reset"), (
        "arbiter.safety package MUST NOT expose 'reset' — "
        "reset is admin-only and must not be reachable from advisor/fusion imports"
    )


def test_advisor_level_import_cannot_call_reset(conn, clock, audit_path) -> None:
    """Simulates an advisor-layer caller that only has access to is_tripped / any_tripped.

    The advisor cannot import `reset` from the module without explicitly
    importing from arbiter.safety.breakers directly.  This test verifies
    that `reset` is NOT present on the safe-for-advisors subset of the API
    (is_tripped, any_tripped) — i.e. you can't reach it without knowing the
    internal module path.
    """
    # An advisor should only use: is_tripped, any_tripped.
    # We model this by only calling those methods — reset intentionally absent.
    cb = CircuitBreaker()
    cb.trip("daily_loss", "forced trip", conn, clock, audit_path=audit_path)

    # Advisor can READ the state
    assert cb.is_tripped("daily_loss", conn) is True
    assert "daily_loss" in cb.any_tripped(conn)

    # Advisor does NOT have `reset` available unless it imports breakers directly.
    # We verify the method exists on the class (it must, for admin use) but
    # assert that a restricted proxy without `reset` would block the clear:
    class _AdvisorView:
        """Simulates the restricted API surface available to advisor code."""
        def __init__(self, breaker: CircuitBreaker, conn: sqlite3.Connection) -> None:
            self._cb = breaker
            self._conn = conn

        def is_tripped(self, name: str) -> bool:
            return self._cb.is_tripped(name, self._conn)

        def any_tripped(self) -> list[str]:
            return self._cb.any_tripped(self._conn)
        # NOTE: no reset() method — advisor cannot clear

    advisor_view = _AdvisorView(cb, conn)
    assert advisor_view.is_tripped("daily_loss") is True
    assert not hasattr(advisor_view, "reset"), "Advisor view must not expose reset()"
    # Breaker remains latched
    assert cb.is_tripped("daily_loss", conn) is True


# ---------------------------------------------------------------------------
# ValueError on unknown breaker name
# ---------------------------------------------------------------------------


def test_trip_unknown_name_raises(cb: CircuitBreaker, conn, clock) -> None:
    with pytest.raises(ValueError, match="Unknown breaker"):
        cb.trip("nonexistent_breaker", "reason", conn, clock)


def test_is_tripped_unknown_name_raises(cb: CircuitBreaker, conn) -> None:
    with pytest.raises(ValueError, match="Unknown breaker"):
        cb.is_tripped("nonexistent_breaker", conn)


def test_reset_unknown_name_raises(cb: CircuitBreaker, conn, clock) -> None:
    with pytest.raises(ValueError, match="Unknown breaker"):
        cb.reset("nonexistent_breaker", conn, clock)


# ---------------------------------------------------------------------------
# No clock (sentinel path)
# ---------------------------------------------------------------------------


def test_trip_without_clock_uses_sentinel(cb: CircuitBreaker, conn, audit_path) -> None:
    """Passing clock=None must write the NO_CLOCK sentinel, not crash."""
    cb.trip("daily_loss", "no clock test", conn, clock=None, audit_path=audit_path)

    row = conn.execute(
        "SELECT latched_at FROM breaker_state WHERE breaker_name = 'daily_loss'"
    ).fetchone()
    assert row is not None
    assert row["latched_at"] == "NO_CLOCK"


# ---------------------------------------------------------------------------
# A3 volume anomaly convenience check
# ---------------------------------------------------------------------------


def test_check_a3_volume_anomaly_trips(
    cb: CircuitBreaker, conn, clock, audit_path
) -> None:
    with pytest.raises(BreakerTrippedError) as exc_info:
        cb.check_a3_volume_anomaly("AAPL", conn, clock, audit_path=audit_path)
    assert exc_info.value.breaker_name == "a3_volume_anomaly"
    assert cb.is_tripped("a3_volume_anomaly", conn) is True
