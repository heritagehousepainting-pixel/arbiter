"""Position reconciler for Lane 12b execution.

reconcile(conn, executor) -> ReconcileResult

Compares the local orders ledger (filled orders) against the broker's
live positions and flags any divergences.

Divergence types:
    - LOCAL_ONLY: ticker has a filled order in the local ledger but no
      matching position at the broker.
    - BROKER_ONLY: ticker has an open position at the broker but no
      corresponding filled order in the local ledger.
    - QTY_MISMATCH: both sides agree the position exists but the share
      count differs by more than a small epsilon.

Design notes:
    - No datetime.now() — caller supplies as_of (INTERFACES.md §11.1).
    - No writes to orders table; divergences go to audit only.
    - This is a diagnostic tool; the engine decides what to do with
      the result (Wave-C wiring point).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

import structlog

from arbiter.db.audit import audit as _audit
from arbiter.shared.executor import Executor, PositionSnapshot

log = structlog.get_logger(__name__)

_QTY_EPSILON = 0.01  # shares; ignore rounding noise below this threshold


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Divergence:
    """A single position divergence between local ledger and broker."""

    kind: str           # "LOCAL_ONLY" | "BROKER_ONLY" | "QTY_MISMATCH"
    ticker: str
    local_qty: float | None
    broker_qty: float | None
    detail: str = ""


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of a reconciliation pass."""

    as_of: datetime
    local_tickers: set[str]
    broker_tickers: set[str]
    divergences: list[Divergence]

    @property
    def clean(self) -> bool:
        """True when no divergences were found."""
        return len(self.divergences) == 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _local_positions(conn: sqlite3.Connection) -> dict[str, float]:
    """Return {ticker: net_qty} from the local orders ledger.

    Sums BUY qty and subtracts SELL qty for filled orders only.
    """
    rows = conn.execute(
        """
        SELECT ticker, side, SUM(qty) as total_qty
        FROM orders
        WHERE status = 'filled'
        GROUP BY ticker, side
        """
    ).fetchall()

    totals: dict[str, float] = {}
    for row in rows:
        ticker = row["ticker"]
        side = row["side"]
        qty = float(row["total_qty"] or 0.0)
        if side == "BUY":
            totals[ticker] = totals.get(ticker, 0.0) + qty
        elif side == "SELL":
            totals[ticker] = totals.get(ticker, 0.0) - qty

    # Return tickers with a non-flat net position — KEEP shorts (negative net):
    # ``abs(v)`` retains a short so it reconciles against the broker's negative
    # qty instead of being dropped (which would falsely flag BROKER_ONLY).
    return {k: v for k, v in totals.items() if abs(v) > _QTY_EPSILON}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def reconcile(
    conn: sqlite3.Connection,
    executor: Executor,
    *,
    as_of: datetime,
    audit_path: str | None = None,
) -> ReconcileResult:
    """Reconcile the local orders ledger against broker positions.

    Parameters
    ----------
    conn:
        Open SQLite connection (orders table must be migrated and readable).
    executor:
        Active executor; ``get_positions()`` is called once.
    as_of:
        Logical timestamp for audit records (tz-aware UTC).
        Must not be datetime.now() at the call site.
    audit_path:
        Override the audit log path (for tests).

    Returns
    -------
    ReconcileResult
        The full reconciliation result including any divergences.
    """
    local: dict[str, float] = _local_positions(conn)
    broker_snapshots: dict[str, PositionSnapshot] = executor.get_positions()
    broker: dict[str, float] = {
        ticker: snap.shares for ticker, snap in broker_snapshots.items()
    }

    local_tickers = set(local.keys())
    broker_tickers = set(broker.keys())

    divergences: list[Divergence] = []

    # Tickers in local ledger but not at broker
    for ticker in local_tickers - broker_tickers:
        d = Divergence(
            kind="LOCAL_ONLY",
            ticker=ticker,
            local_qty=local[ticker],
            broker_qty=None,
            detail=f"Local ledger has {local[ticker]:.4f} shares; broker shows 0",
        )
        divergences.append(d)
        log.warning("reconciler.divergence", **_div_dict(d))

    # Tickers at broker but not in local ledger
    for ticker in broker_tickers - local_tickers:
        d = Divergence(
            kind="BROKER_ONLY",
            ticker=ticker,
            local_qty=None,
            broker_qty=broker[ticker],
            detail=f"Broker has {broker[ticker]:.4f} shares; local ledger shows 0",
        )
        divergences.append(d)
        log.warning("reconciler.divergence", **_div_dict(d))

    # Tickers in both — check for qty mismatch
    for ticker in local_tickers & broker_tickers:
        diff = abs(local[ticker] - broker[ticker])
        if diff > _QTY_EPSILON:
            d = Divergence(
                kind="QTY_MISMATCH",
                ticker=ticker,
                local_qty=local[ticker],
                broker_qty=broker[ticker],
                detail=(
                    f"Local={local[ticker]:.4f} Broker={broker[ticker]:.4f} "
                    f"diff={diff:.4f}"
                ),
            )
            divergences.append(d)
            log.warning("reconciler.divergence", **_div_dict(d))

    result = ReconcileResult(
        as_of=as_of,
        local_tickers=local_tickers,
        broker_tickers=broker_tickers,
        divergences=divergences,
    )

    # Audit the reconciliation pass
    _audit(
        "reconciler.pass",
        {
            "local_count": len(local_tickers),
            "broker_count": len(broker_tickers),
            "divergence_count": len(divergences),
            "clean": result.clean,
            "divergences": [_div_dict(d) for d in divergences],
        },
        ts=as_of.isoformat(),
        audit_path=audit_path,
    )

    if result.clean:
        log.info("reconciler.clean", local_count=len(local_tickers))
    else:
        log.error(
            "reconciler.divergences_found",
            count=len(divergences),
            divergences=[_div_dict(d) for d in divergences],
        )

    return result


def _div_dict(d: Divergence) -> dict:
    return {
        "kind": d.kind,
        "ticker": d.ticker,
        "local_qty": d.local_qty,
        "broker_qty": d.broker_qty,
        "detail": d.detail,
    }
