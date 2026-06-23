"""Latching circuit-breakers for Arbiter (Lane 4a).

Design contract (INTERFACES.md §8, §11):
  - Breakers are **latching**: once tripped they stay tripped until an explicit
    admin-level call to ``reset()``.  Advisor and fusion code MUST NOT call
    ``reset()`` — it is exposed only here at the infrastructure layer and must
    be wired exclusively to an admin endpoint in Wave-C.
  - State is persisted in the ``breaker_state`` table (migrated by
    001a_breakers.sql) so trips survive process restarts.
  - No ``datetime.now()`` anywhere in this module.  All time values come from
    the ``clock`` parameter passed by the caller (INTERFACES.md §11.1 / §3).
  - Trips are mirrored to the append-only audit log via ``arbiter.db.audit.audit``.

Breakers (§3.9):
  daily_loss                  — portfolio daily loss >= 2 %
  per_position_intraday       — per-position intraday loss <= -5 %
  mirofish_3x_consecutive_fail — MiroFish HTTP advisor fails 3× in a row
  a3_volume_anomaly           — A3 detects vol anomaly on a held name
  broker_non_200              — any broker response is non-200
  confidence_distribution_shift — confidence distribution shift > 30 %

Public API
----------
  CircuitBreaker               — registry class (all state in DB)
  BREAKER_NAMES                — frozenset of canonical name strings
  BreakerTrippedError          — raised by check_* helpers on new trip

Infra note
----------
``reset()`` is intentionally NOT re-exported from ``arbiter.safety`` (the
integration __init__); only ``breakers.CircuitBreaker`` exposes it.  Advisor
and fusion layers that import from ``arbiter.safety`` cannot reach ``reset()``
through normal import paths.  A Wave-C admin route must import this module
directly:  ``from arbiter.safety.breakers import CircuitBreaker``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


# ---------------------------------------------------------------------------
# Canonical breaker names (§3.9)
# ---------------------------------------------------------------------------

BREAKER_NAMES: frozenset[str] = frozenset(
    {
        "daily_loss",
        "per_position_intraday",
        "mirofish_3x_consecutive_fail",
        "a3_volume_anomaly",
        "broker_non_200",
        "confidence_distribution_shift",
    }
)

# Thresholds (all expressed as positive magnitudes — caller provides sign-correct value)
_DAILY_LOSS_THRESHOLD: float = -0.02          # trip when pnl_pct <= -2 %
_PER_POSITION_THRESHOLD: float = -0.05        # trip when position_pct <= -5 %
_CONF_SHIFT_THRESHOLD: float = 0.30           # trip when shift_magnitude > 30 %


# ---------------------------------------------------------------------------
# Clock protocol — accepts any object with a ``now() -> datetime`` method,
# or a plain callable returning datetime.  NEVER calls datetime.now() here.
# ---------------------------------------------------------------------------

class _ClockLike(Protocol):
    def now(self) -> datetime: ...


def _clock_ts(clock: _ClockLike | None) -> str:
    """Return an ISO timestamp string from *clock*, or the sentinel."""
    if clock is None:
        return "NO_CLOCK"
    return clock.now().isoformat()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class BreakerTrippedError(RuntimeError):
    """Raised when a check_ helper trips a breaker."""

    def __init__(self, name: str, reason: str) -> None:
        self.breaker_name = name
        self.reason = reason
        super().__init__(f"Breaker tripped [{name}]: {reason}")


# ---------------------------------------------------------------------------
# Internal DB helpers (isolated — do not call helpers.insert_row because the
# breaker_state table has TEXT PK not a ULID PK)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _BreakerRow:
    breaker_name: str
    latched: bool
    latched_at: str | None
    reason: str | None


def _upsert_breaker(
    conn: sqlite3.Connection,
    name: str,
    *,
    latched: bool,
    latched_at: str | None,
    reason: str | None,
) -> None:
    """INSERT OR REPLACE the breaker state row.

    This is the ONE place that writes breaker_state.  The INSERT OR REPLACE
    pattern is the minimal deviation from insert-only necessary to implement
    latching (the row uses TEXT PK so supersede_row doesn't apply cleanly;
    documented as a justified exception in §10 spirit — state NOT history).
    """
    conn.execute(
        """
        INSERT INTO breaker_state (breaker_name, latched, latched_at, reason)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(breaker_name) DO UPDATE SET
            latched    = excluded.latched,
            latched_at = excluded.latched_at,
            reason     = excluded.reason
        """,
        (name, int(latched), latched_at, reason),
    )
    conn.commit()


def _fetch_breaker(
    conn: sqlite3.Connection,
    name: str,
) -> _BreakerRow | None:
    """Return the current row for *name*, or ``None`` if not yet written."""
    row = conn.execute(
        "SELECT breaker_name, latched, latched_at, reason FROM breaker_state WHERE breaker_name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return _BreakerRow(
        breaker_name=row["breaker_name"],
        latched=bool(row["latched"]),
        latched_at=row["latched_at"],
        reason=row["reason"],
    )


# ---------------------------------------------------------------------------
# Main registry
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Registry of latching circuit-breakers backed by the ``breaker_state`` DB table.

    All methods require an explicit ``conn`` so that callers control the
    connection lifetime and tests can use in-memory databases.

    Admin note
    ----------
    ``reset()`` clears a latched breaker.  It is intentionally NOT re-exported
    from the safety package __init__.  Only direct importers of
    ``arbiter.safety.breakers`` can call it — keeping the "cannot be cleared
    by advisor/fusion code" guarantee structural rather than merely documented.
    """

    # ------------------------------------------------------------------
    # Core primitives
    # ------------------------------------------------------------------

    def trip(
        self,
        name: str,
        reason: str,
        conn: sqlite3.Connection,
        clock: _ClockLike | None = None,
        *,
        audit_path: str | None = None,
    ) -> None:
        """Latch breaker *name*.

        If the breaker is already latched this is a no-op (latching is
        idempotent — the first trip wins; use reset() then trip() to update).

        Args:
            name:       Canonical breaker name (must be in BREAKER_NAMES).
            reason:     Human-readable trip reason.
            conn:       Open SQLite connection with breaker_state migrated.
            clock:      Clock object or None (writes ``"NO_CLOCK"`` sentinel).
            audit_path: Override the audit file path (for tests).

        Raises:
            ValueError: If *name* is not a canonical breaker name.
        """
        if name not in BREAKER_NAMES:
            raise ValueError(
                f"Unknown breaker {name!r}. Valid names: {sorted(BREAKER_NAMES)}"
            )

        existing = _fetch_breaker(conn, name)
        if existing is not None and existing.latched:
            # Already latched — idempotent, do not overwrite original trip.
            return

        ts = _clock_ts(clock)
        _upsert_breaker(conn, name, latched=True, latched_at=ts, reason=reason)

        # Mirror to audit log (INTERFACES.md §10)
        from arbiter.db.audit import audit  # local import to avoid circular
        audit(
            "breaker_trip",
            {"breaker_name": name, "reason": reason, "latched_at": ts},
            ts=ts if ts != "NO_CLOCK" else None,
            audit_path=audit_path,
        )

    def is_tripped(
        self,
        name: str,
        conn: sqlite3.Connection,
    ) -> bool:
        """Return True if breaker *name* is currently latched.

        Args:
            name: Canonical breaker name.
            conn: Open SQLite connection.

        Raises:
            ValueError: If *name* is not a canonical breaker name.
        """
        if name not in BREAKER_NAMES:
            raise ValueError(
                f"Unknown breaker {name!r}. Valid names: {sorted(BREAKER_NAMES)}"
            )
        row = _fetch_breaker(conn, name)
        return row is not None and row.latched

    def any_tripped(self, conn: sqlite3.Connection) -> list[str]:
        """Return names of all currently-latched breakers (may be empty).

        Args:
            conn: Open SQLite connection.

        Returns:
            Sorted list of latched breaker names.
        """
        rows = conn.execute(
            "SELECT breaker_name FROM breaker_state WHERE latched = 1"
        ).fetchall()
        return sorted(row["breaker_name"] for row in rows)

    def reset(
        self,
        name: str,
        conn: sqlite3.Connection,
        clock: _ClockLike | None = None,
        *,
        audit_path: str | None = None,
    ) -> None:
        """Clear a latched breaker (ADMIN ONLY).

        This method is intentionally absent from the safety package __init__.
        Only admin-level code that directly imports
        ``arbiter.safety.breakers.CircuitBreaker`` can invoke it.

        Args:
            name:       Canonical breaker name.
            conn:       Open SQLite connection.
            clock:      Clock for audit timestamp (or None -> sentinel).
            audit_path: Override audit path (for tests).

        Raises:
            ValueError: If *name* is not a canonical breaker name.
        """
        if name not in BREAKER_NAMES:
            raise ValueError(
                f"Unknown breaker {name!r}. Valid names: {sorted(BREAKER_NAMES)}"
            )

        _upsert_breaker(conn, name, latched=False, latched_at=None, reason=None)

        ts = _clock_ts(clock)
        from arbiter.db.audit import audit
        audit(
            "breaker_reset",
            {"breaker_name": name, "reset_at": ts},
            ts=ts if ts != "NO_CLOCK" else None,
            audit_path=audit_path,
        )

    # ------------------------------------------------------------------
    # Domain check helpers — each embeds the §3.9 threshold logic
    # ------------------------------------------------------------------

    def check_daily_loss(
        self,
        pnl_pct: float,
        conn: sqlite3.Connection,
        clock: _ClockLike | None = None,
        *,
        audit_path: str | None = None,
    ) -> None:
        """Trip ``daily_loss`` if *pnl_pct* is <= -2 %.

        Args:
            pnl_pct:    Portfolio daily P&L as a decimal fraction
                        (e.g. -0.025 means -2.5 %).
            conn:       Open SQLite connection.
            clock:      Clock for timestamps.
            audit_path: Override audit path (for tests).

        Raises:
            BreakerTrippedError: If the threshold is breached and the breaker
                                 was not already latched.
        """
        if pnl_pct <= _DAILY_LOSS_THRESHOLD:
            reason = (
                f"Daily loss {pnl_pct:.4%} breached threshold "
                f"{_DAILY_LOSS_THRESHOLD:.4%}"
            )
            already = self.is_tripped("daily_loss", conn)
            self.trip("daily_loss", reason, conn, clock, audit_path=audit_path)
            if not already:
                raise BreakerTrippedError("daily_loss", reason)

    def check_per_position(
        self,
        position_pct: float,
        conn: sqlite3.Connection,
        clock: _ClockLike | None = None,
        *,
        audit_path: str | None = None,
    ) -> None:
        """Trip ``per_position_intraday`` if *position_pct* is <= -5 %.

        Args:
            position_pct: Intraday P&L on a single position as a decimal
                          fraction (e.g. -0.06 means -6 %).
            conn:         Open SQLite connection.
            clock:        Clock for timestamps.
            audit_path:   Override audit path (for tests).

        Raises:
            BreakerTrippedError: If the threshold is breached and not already latched.
        """
        if position_pct <= _PER_POSITION_THRESHOLD:
            reason = (
                f"Per-position intraday loss {position_pct:.4%} breached "
                f"threshold {_PER_POSITION_THRESHOLD:.4%}"
            )
            already = self.is_tripped("per_position_intraday", conn)
            self.trip(
                "per_position_intraday", reason, conn, clock, audit_path=audit_path
            )
            if not already:
                raise BreakerTrippedError("per_position_intraday", reason)

    def check_mirofish_consecutive_fail(
        self,
        consecutive_fails: int,
        conn: sqlite3.Connection,
        clock: _ClockLike | None = None,
        *,
        audit_path: str | None = None,
    ) -> None:
        """Trip ``mirofish_3x_consecutive_fail`` at 3 or more consecutive failures.

        Args:
            consecutive_fails: Number of consecutive MiroFish HTTP failures.
            conn:              Open SQLite connection.
            clock:             Clock for timestamps.
            audit_path:        Override audit path (for tests).

        Raises:
            BreakerTrippedError: If threshold reached and not already latched.
        """
        if consecutive_fails >= 3:
            reason = (
                f"MiroFish HTTP advisor failed {consecutive_fails}× consecutively "
                f"(threshold: 3)"
            )
            already = self.is_tripped("mirofish_3x_consecutive_fail", conn)
            self.trip(
                "mirofish_3x_consecutive_fail",
                reason,
                conn,
                clock,
                audit_path=audit_path,
            )
            if not already:
                raise BreakerTrippedError("mirofish_3x_consecutive_fail", reason)

    def check_a3_volume_anomaly(
        self,
        ticker: str,
        conn: sqlite3.Connection,
        clock: _ClockLike | None = None,
        *,
        audit_path: str | None = None,
    ) -> None:
        """Trip ``a3_volume_anomaly`` when A3 detects a vol anomaly on a held name.

        The caller (A3 advisor) determines that an anomaly exists and passes
        the held *ticker*.  This method records and latches the trip.

        Args:
            ticker:     The ticker with the detected volume anomaly.
            conn:       Open SQLite connection.
            clock:      Clock for timestamps.
            audit_path: Override audit path (for tests).

        Raises:
            BreakerTrippedError: If not already latched.
        """
        reason = f"A3 detected volume anomaly on held name {ticker!r}"
        already = self.is_tripped("a3_volume_anomaly", conn)
        self.trip("a3_volume_anomaly", reason, conn, clock, audit_path=audit_path)
        if not already:
            raise BreakerTrippedError("a3_volume_anomaly", reason)

    def check_broker_non_200(
        self,
        status_code: int,
        endpoint: str,
        conn: sqlite3.Connection,
        clock: _ClockLike | None = None,
        *,
        audit_path: str | None = None,
    ) -> None:
        """Trip ``broker_non_200`` if *status_code* is not 200.

        Args:
            status_code: HTTP response status code from the broker.
            endpoint:    Broker endpoint that returned the non-200 (for audit).
            conn:        Open SQLite connection.
            clock:       Clock for timestamps.
            audit_path:  Override audit path (for tests).

        Raises:
            BreakerTrippedError: If the status is non-200 and not already latched.
        """
        if status_code != 200:
            reason = (
                f"Broker returned HTTP {status_code} on {endpoint!r}"
            )
            already = self.is_tripped("broker_non_200", conn)
            self.trip("broker_non_200", reason, conn, clock, audit_path=audit_path)
            if not already:
                raise BreakerTrippedError("broker_non_200", reason)

    def check_confidence_distribution_shift(
        self,
        shift_magnitude: float,
        conn: sqlite3.Connection,
        clock: _ClockLike | None = None,
        *,
        audit_path: str | None = None,
    ) -> None:
        """Trip ``confidence_distribution_shift`` if *shift_magnitude* > 30 %.

        Args:
            shift_magnitude: Fractional shift in the confidence distribution
                             (e.g. 0.35 means 35 % shift).
            conn:            Open SQLite connection.
            clock:           Clock for timestamps.
            audit_path:      Override audit path (for tests).

        Raises:
            BreakerTrippedError: If threshold exceeded and not already latched.
        """
        if shift_magnitude > _CONF_SHIFT_THRESHOLD:
            reason = (
                f"Confidence distribution shift {shift_magnitude:.2%} exceeds "
                f"threshold {_CONF_SHIFT_THRESHOLD:.2%}"
            )
            already = self.is_tripped("confidence_distribution_shift", conn)
            self.trip(
                "confidence_distribution_shift",
                reason,
                conn,
                clock,
                audit_path=audit_path,
            )
            if not already:
                raise BreakerTrippedError("confidence_distribution_shift", reason)
