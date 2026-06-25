"""Open option position recording and lifecycle queries — P2.

INSERT-ONLY DESIGN
------------------
A position is "OPEN" when it has a row in ``option_positions`` AND no matching
row in ``option_outcomes`` (matched on idea_id + occ_symbol).  Closing means
inserting an ``option_outcomes`` row — ``option_positions`` is NEVER updated.

This module exposes two public functions:

``record_open_position``
    Insert one row into ``option_positions`` when a paper buy-to-open order
    is accepted by the broker.  Returns the new ULID position id.

``list_open_positions``
    LEFT JOIN ``option_positions`` against ``option_outcomes`` on
    (idea_id, occ_symbol) and return only those rows whose outcome is NULL —
    i.e. positions that have not yet been closed.  Returns a list of dicts
    (one per open position) with all ``option_positions`` columns.
"""
from __future__ import annotations

import datetime
import sqlite3
from typing import Any

from arbiter.db.helpers import generate_ulid, insert_row
from arbiter.options.types import OptionContract, OptionOrder


def record_open_position(
    conn: sqlite3.Connection,
    *,
    idea_id: str,
    shadow_id: str | None,
    contract: OptionContract,
    order: OptionOrder,
    broker_order_id: str,
    underlying_open_price: float,
    thesis_horizon_date: datetime.date,
    original_conviction: float,
    open_ts: str,
    created_at: str,
) -> str:
    """Record a newly opened paper option position.

    Inserts one row into ``option_positions`` and returns its ULID id.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open arbiter DB connection.
    idea_id : str
        FK → ``ideas.idea_id``.
    shadow_id : str | None
        FK → ``option_shadow_log.id`` if the position was shadow-evaluated
        first; ``None`` for positions opened without a prior shadow row.
    contract : OptionContract
        The selected contract (from ``select_contract()``).
    order : OptionOrder
        The fully-sized order (from ``size_option()``), used for
        ``contracts_qty``, ``est_premium``, and the derived entry limit price.
    broker_order_id : str
        The Alpaca-assigned order id returned by ``client.place()``.
    underlying_open_price : float
        Underlying equity price at the moment of order submission (USD).
    thesis_horizon_date : datetime.date
        Calendar date on or after which the horizon exit trigger fires.
    original_conviction : float
        Signed conviction score at the time the position was opened.
    open_ts : str
        Tz-aware UTC ISO timestamp when the position was opened (engine clock).
    created_at : str
        Tz-aware UTC ISO insertion timestamp.

    Returns
    -------
    str
        The ULID primary key of the inserted ``option_positions`` row.
    """
    position_id = generate_ulid()

    # Derive the per-share limit price from the total premium and qty.
    # est_premium = contracts_qty × mid_price × 100 → per_share = mid_price.
    # We record the per-share limit price sent to the broker (which the client
    # adds one tick to; here we store the pre-tick base for auditability).
    # If contracts_qty is 0 or premium is 0, store 0.0 (defensive guard).
    qty_shares = order.contracts_qty * 100
    entry_limit_price: float = (
        order.est_premium / qty_shares if qty_shares > 0 else 0.0
    )

    row: dict[str, Any] = {
        "id": position_id,
        "idea_id": idea_id,
        "shadow_id": shadow_id,
        "underlying": contract.underlying,
        "occ_symbol": contract.occ_symbol,
        "side": contract.side.value,
        "strike": contract.strike,
        "expiry": contract.expiry.isoformat(),
        "contracts_qty": order.contracts_qty,
        "entry_premium": order.est_premium,
        "entry_limit_price": entry_limit_price,
        "delta_at_open": contract.delta,
        "iv_at_open": contract.iv,
        "underlying_open_price": underlying_open_price,
        "thesis_horizon_date": thesis_horizon_date.isoformat(),
        "original_conviction": original_conviction,
        "broker_order_id": broker_order_id,
        "open_ts": open_ts,
        "created_at": created_at,
    }

    insert_row(conn, "option_positions", row)
    return position_id


def list_open_positions(conn: sqlite3.Connection) -> list[dict]:
    """Return all currently open option positions.

    Openness is derived by the ABSENCE of a matching ``option_outcomes`` row —
    the table is never updated; a close = inserting an outcome row.

    The join matches on (idea_id, occ_symbol) which is the natural identity of
    a position; ``option_outcomes`` does not carry a position_id FK (and
    ``outcomes.py`` must not be changed), so we match on these two fields
    instead.

    Returns
    -------
    list[dict]
        One dict per open position, containing all columns from
        ``option_positions``.  Empty list when no open positions exist.
    """
    sql = """
        SELECT p.*
        FROM option_positions AS p
        LEFT JOIN option_outcomes AS o
            ON o.idea_id    = p.idea_id
           AND o.occ_symbol = p.occ_symbol
        WHERE o.id IS NULL
        ORDER BY p.open_ts
    """
    cursor = conn.execute(sql)
    # Return plain dicts so callers don't need sqlite3.Row awareness.
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]
