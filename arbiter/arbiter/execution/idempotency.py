"""Idempotency helpers for Lane 12b execution.

dedup_hash(order) = sha256(ticker + side + horizon + entry_date + advisor_signature)

Idempotency contract (INTERFACES.md §9):
    - ULID primary key per order.
    - dedup_hash UNIQUE in the ``orders`` ledger.
    - Pre-submit check vs local ledger AND broker (get_positions / open orders).
    - Max 1 retry then halt+alert.

No datetime.now() — callers supply clock/as_of.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import date, datetime

import structlog

from arbiter.contract.seams import PaperOrder
from arbiter.shared.executor import Executor

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Deterministic hash
# ---------------------------------------------------------------------------

def _canonical_entry_date(entry_date: object) -> str:
    """Normalize an entry_date to a canonical ISO-date string (YYYY-MM-DD).

    The hash must be stable regardless of whether the caller supplies a
    ``date``, a ``datetime``, or an already-formatted string (D1 P3): a
    ``datetime`` carries a time component that ``str()`` would leak into the
    digest, and a date vs str round-trip could otherwise drift.  We collapse
    every accepted form to the date-only ISO string.
    """
    # ``datetime`` is a subclass of ``date``; check it first to drop the time.
    if isinstance(entry_date, datetime):
        return entry_date.date().isoformat()
    if isinstance(entry_date, date):
        return entry_date.isoformat()
    # Already a string (or other): take the date portion if an ISO datetime
    # leaked through (e.g. "2024-01-15T12:00:00"); otherwise pass through.
    return str(entry_date).split("T", 1)[0]


def dedup_hash(order: PaperOrder) -> str:
    """Return sha256(ticker+side+horizon+entry_date+advisor_signature).

    All fields are coerced to strings and concatenated with ``|`` as a
    separator (safe because none of the fields can legitimately contain
    ``|``).  ``entry_date`` is normalized to a canonical ISO-date string so
    the digest is stable across date / datetime / str inputs (D1 P3).

    This is the single source of truth for the dedup hash; ``policy.decision``
    delegates here so the two can never drift (D1 P2).

    Returns a 64-character lowercase hex digest.
    """
    raw = "|".join([
        order.ticker,
        str(order.side.value),
        str(order.horizon_bucket.value),
        _canonical_entry_date(order.entry_date),
        order.advisor_signature,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Pre-submit duplicate check
# ---------------------------------------------------------------------------

class DuplicateOrderError(Exception):
    """Raised when a duplicate order is detected before submission."""


class HaltSignal(Exception):
    """Raised when the idempotency layer decides trading must halt.

    This is a hard stop: the engine must log, alert, and cease new submissions.
    """


def _check_local_ledger(conn: sqlite3.Connection, dh: str) -> bool:
    """Return True if dh already exists in the local orders table."""
    row = conn.execute(
        "SELECT 1 FROM orders WHERE dedup_hash = ? LIMIT 1",
        (dh,),
    ).fetchone()
    return row is not None


def _check_broker(executor: Executor, ticker: str) -> bool:
    """Return True if the broker already has an open position for ticker.

    This is a lightweight check: we call get_positions() and look for the
    ticker.  Open-order polling is broker-specific; the base executor ABC
    does not expose a ``get_open_orders()`` method, so we check positions
    as a proxy.  AlpacaAdapter overrides this in a subclass if needed.

    Fail-closed on exception: an unreachable broker is treated as a
    potential duplicate (block the submit / raise DuplicateOrderError).
    This prevents double-fills when the broker is temporarily unavailable.
    """
    try:
        positions = executor.get_positions()
        return ticker in positions
    except Exception as exc:
        log.error(
            "idempotency.broker_check_failed",
            error=str(exc),
            action="fail_closed_treat_as_duplicate",
        )
        # Fail-closed: treat as potential duplicate so the order is blocked.
        # A subsequent manual review can clear the ledger if needed.
        raise DuplicateOrderError(
            f"broker check raised exception for {ticker!r} — treating as potential duplicate (fail-closed): {exc}"
        ) from exc


def ensure_not_duplicate(
    order: PaperOrder,
    conn: sqlite3.Connection,
    executor: Executor,
    *,
    dh: str | None = None,
    is_exit: bool = False,
    is_addon: bool = False,
) -> None:
    """Check local ledger and broker for duplicates.

    Parameters
    ----------
    order:
        The PaperOrder about to be submitted.
    conn:
        Open SQLite connection with the orders table migrated.
    executor:
        Active executor (SimExecutor or AlpacaAdapter).
    dh:
        Pre-computed dedup_hash; computed fresh if not supplied.
    is_exit:
        When True this is an EXIT SELL (exit-monitor B3): the broker
        position-presence check is SKIPPED — holding the position is the
        *precondition* for selling, not a duplicate signal.  Idempotency is
        enforced by the local-ledger check only (a live SELL row with the
        same dedup_hash blocks a repeated identical SELL across cycles).
    is_addon:
        When True this is an ADD-ON to a held name (Tier-2 #5, 2026-07-02):
        the broker position-presence check is SKIPPED — accumulation is
        intended, so the held position is not a duplicate signal.  The
        LOCAL-LEDGER check still applies (a same-day identical order — same
        ticker/side/horizon/entry_date/advisor set — stays blocked).  Sizing
        bounds the combined position via the per-name headroom cap; the
        engine additionally enforces a one-add-per-ticker-per-day cooldown.

    Raises
    ------
    DuplicateOrderError
        If the order already exists (idempotent skip).
    """
    hash_val = dh if dh is not None else dedup_hash(order)

    if _check_local_ledger(conn, hash_val):
        log.info(
            "idempotency.duplicate_local",
            order_id=order.order_id,
            dedup_hash=hash_val,
            ticker=order.ticker,
            is_exit=is_exit,
        )
        raise DuplicateOrderError(
            f"Order {order.order_id} (hash={hash_val}) already in local ledger"
        )

    # EXIT SELLs skip the broker position-presence check: a held position is
    # the precondition for selling, so it must NOT be read as a duplicate.
    # ADD-ONs skip it too: accumulating a held name is intended (Tier-2 #5);
    # the local-ledger check above still blocks same-day identical re-entry.
    if is_exit or is_addon:
        return

    if _check_broker(executor, order.ticker):
        log.info(
            "idempotency.duplicate_broker",
            order_id=order.order_id,
            dedup_hash=hash_val,
            ticker=order.ticker,
        )
        raise DuplicateOrderError(
            f"Order {order.order_id}: broker already has open position for {order.ticker}"
        )
