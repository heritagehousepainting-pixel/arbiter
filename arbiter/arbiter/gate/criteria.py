"""Paper→live gate criteria — Lane 12c (criteria.py).

The criteria set is IMMUTABLE once computed: its SHA-256 hash is written to
``gate_hash_lock`` at first evaluation for a given ``run_id``.  Any subsequent
call that computes a different hash raises ``CriteriaHashMismatch``.

Criteria (spec §5 + Phase 7):
    1. Trading history ≥ 60 days
    2. Closed trades ≥ 30
    3. Sharpe ratio ≥ 1.0
    4. Max drawdown ≤ 8 %    (passed as a positive fraction, e.g. 0.08)
    5. All circuit breakers clear (no latched breakers)
    6. Kill-switch tested within the last 30 days

Design constraints:
    - No ``datetime.now()`` — ``as_of`` is always injected.
    - ``from __future__ import annotations`` (py3.11+).
    - Stat values are passed in / queried externally; this module does NOT
      import other in-progress lanes.  Stats arrive as a plain ``TradeStats``
      dataclass so the caller can populate it from DB or fixtures.

Public API
----------
CRITERIA_HASH : str
    Stable SHA-256 hex of the canonical criteria definition.  Changing any
    threshold here changes the hash and will be caught mid-run.

evaluate(stats, *, conn=None, run_id=None, as_of) -> GateResult
    Evaluate all criteria and return a ``GateResult``.  When ``conn`` and
    ``run_id`` are provided the hash is persisted/verified in ``gate_hash_lock``.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# Canonical criteria definition — changing anything here changes CRITERIA_HASH.
# The dict is sorted so key order cannot affect the hash.
# ---------------------------------------------------------------------------
_CRITERIA_SPEC: dict = {
    "min_trading_days": 60,
    "min_closed_trades": 30,
    "min_sharpe": 1.0,
    "max_drawdown": 0.08,         # positive fraction; gate rejects if drawdown > 0.08
    "max_kill_switch_age_days": 30,
    # Circuit breakers must all be clear (latched == False).
    # This sentinel is here so that relaxing the breaker check changes the hash.
    "breakers_must_be_clear": True,
}

CRITERIA_HASH: str = hashlib.sha256(
    json.dumps(_CRITERIA_SPEC, sort_keys=True).encode()
).hexdigest()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class CriteriaHashMismatch(Exception):
    """Raised when the live criteria hash differs from the hash locked for this run.

    This indicates the criteria set was changed mid-run, which is forbidden.
    """


# ---------------------------------------------------------------------------
# Input stats dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TradeStats:
    """Closed-trade statistics passed into ``evaluate``.

    All fields are computed externally (from ``orders`` + ``outcomes`` tables
    or from test fixtures) so this module has no DB dependency.

    Attributes
    ----------
    trading_days:
        Number of calendar / trading days since the first closed trade.
    closed_trades:
        Count of closed (non-superseded) paper trades.
    sharpe:
        Annualised Sharpe ratio over all closed trades.
    max_drawdown:
        Maximum drawdown as a positive fraction (e.g. 0.05 = 5 %).
    breakers_clear:
        True if ALL circuit breakers are unlatched; False if any is tripped.
    kill_switch_last_tested_at:
        Datetime (tz-aware UTC) of the most recent successful kill-switch test.
        Pass ``None`` if the kill switch has never been tested.
    """
    trading_days: int
    closed_trades: int
    sharpe: float
    max_drawdown: float              # positive fraction, e.g. 0.08
    breakers_clear: bool
    kill_switch_last_tested_at: Optional[datetime]


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateResult:
    """Result of evaluating the paper→live criteria set.

    Attributes
    ----------
    passed:
        True only when EVERY criterion is met.
    failing:
        List of human-readable strings describing each failing criterion.
        Empty when ``passed=True``.
    criteria_hash:
        SHA-256 hex of the criteria spec used for this evaluation.
    """
    passed: bool
    failing: list[str] = field(default_factory=list)
    criteria_hash: str = CRITERIA_HASH


# ---------------------------------------------------------------------------
# Hash-lock helpers
# ---------------------------------------------------------------------------

def _lock_or_verify(conn: sqlite3.Connection, run_id: str, as_of: datetime) -> None:
    """Persist the criteria hash for ``run_id`` or verify it matches.

    First call: inserts a row into ``gate_hash_lock``.
    Subsequent calls: compares stored hash against ``CRITERIA_HASH``.

    Raises
    ------
    CriteriaHashMismatch
        If the stored hash differs from ``CRITERIA_HASH``.
    """
    row = conn.execute(
        "SELECT criteria_hash FROM gate_hash_lock WHERE run_id = ?",
        (run_id,),
    ).fetchone()

    if row is None:
        # First evaluation for this run — lock the hash.
        conn.execute(
            "INSERT INTO gate_hash_lock (run_id, criteria_hash, locked_at) VALUES (?, ?, ?)",
            (run_id, CRITERIA_HASH, as_of.isoformat()),
        )
        conn.commit()
    else:
        locked = row[0]
        if locked != CRITERIA_HASH:
            raise CriteriaHashMismatch(
                f"Criteria hash changed mid-run for run_id={run_id!r}. "
                f"Locked={locked!r}, current={CRITERIA_HASH!r}. "
                "Deploy requires a new run_id, not a mid-run criteria swap."
            )


# ---------------------------------------------------------------------------
# Public evaluator
# ---------------------------------------------------------------------------

def evaluate(
    stats: TradeStats,
    *,
    as_of: datetime,
    conn: Optional[sqlite3.Connection] = None,
    run_id: Optional[str] = None,
) -> GateResult:
    """Evaluate all paper→live gate criteria.

    Parameters
    ----------
    stats:
        Closed-trade statistics (computed externally from DB or fixtures).
    as_of:
        Information timestamp (tz-aware UTC).  Never uses wall-clock.
    conn:
        Optional SQLite connection.  When provided (with ``run_id``), the
        criteria hash is persisted / verified in ``gate_hash_lock``.
    run_id:
        Identifier for the current run/session (ULID).  Required when
        ``conn`` is provided.

    Returns
    -------
    GateResult
        ``passed=True`` only when every criterion is satisfied.

    Raises
    ------
    CriteriaHashMismatch
        When ``conn`` + ``run_id`` are provided and the stored hash for this
        run differs from ``CRITERIA_HASH`` (mid-run criteria change detected).
    ValueError
        If ``conn`` is provided without ``run_id`` or vice-versa.
    """
    if (conn is None) != (run_id is None):
        raise ValueError("Provide both 'conn' and 'run_id', or neither.")

    # Persist / verify hash-lock before evaluation so a mid-run change is
    # caught before any partial result is returned.
    if conn is not None and run_id is not None:
        _lock_or_verify(conn, run_id, as_of)

    failing: list[str] = []
    spec = _CRITERIA_SPEC

    # 1. Minimum trading days
    if stats.trading_days < spec["min_trading_days"]:
        failing.append(
            f"trading_days={stats.trading_days} < {spec['min_trading_days']} required"
        )

    # 2. Minimum closed trades
    if stats.closed_trades < spec["min_closed_trades"]:
        failing.append(
            f"closed_trades={stats.closed_trades} < {spec['min_closed_trades']} required"
        )

    # 3. Sharpe ratio
    if stats.sharpe < spec["min_sharpe"]:
        failing.append(
            f"sharpe={stats.sharpe:.4f} < {spec['min_sharpe']:.1f} required"
        )

    # 4. Maximum drawdown (positive fraction)
    if stats.max_drawdown > spec["max_drawdown"]:
        failing.append(
            f"max_drawdown={stats.max_drawdown:.4f} > {spec['max_drawdown']:.2f} allowed"
        )

    # 5. Circuit breakers all clear
    if spec["breakers_must_be_clear"] and not stats.breakers_clear:
        failing.append("circuit breakers not clear (latched breaker detected)")

    # 6. Kill-switch tested within the last 30 days
    max_age_days = spec["max_kill_switch_age_days"]
    if stats.kill_switch_last_tested_at is None:
        failing.append(
            f"kill_switch has never been tested (required within {max_age_days} days)"
        )
    else:
        age_days = (as_of - stats.kill_switch_last_tested_at).total_seconds() / 86400.0
        if age_days > max_age_days:
            failing.append(
                f"kill_switch last tested {age_days:.1f} days ago "
                f"(must be within {max_age_days} days)"
            )

    return GateResult(
        passed=len(failing) == 0,
        failing=failing,
        criteria_hash=CRITERIA_HASH,
    )
