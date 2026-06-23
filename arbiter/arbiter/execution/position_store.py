"""Position store — WP-B (Phase-2 persistence: position continuity).

Persists the in-memory ``SimExecutor`` state to the durable ``sim_positions``
and ``sim_account`` tables (migration 022) so paper positions survive across
runs and ``arbiter status`` reflects real ``open_positions``.

Per PHASE2-PERSISTENCE-PLAN FROZEN decision #2, this snapshot is the source of
truth for status — we do NOT reconstruct from ``orders`` (no fill price / cost
basis recoverable there).

Unlike the fact tables governed by the §11.2 insert-only rule, this is mutable
runtime state: ``snapshot_executor`` wipes-and-rewrites ``sim_positions`` and
upserts the single ``sim_account`` row each cycle.

Public API (FROZEN — WP-D depends on this exactly):
    snapshot_executor(conn, executor, *, as_of) -> None
    load_account_state(conn) -> dict | None
    open_position_count(conn) -> int
    seed_executor(conn, executor) -> None
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from arbiter.shared.sim_executor import SimExecutor


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def snapshot_executor(
    conn: sqlite3.Connection,
    executor: SimExecutor,
    *,
    as_of: datetime,
) -> None:
    """Persist *executor*'s current state to the durable tables.

    Wipes ``sim_positions`` and re-inserts one row per current position, then
    upserts the ``sim_account`` singleton (id=1).  The whole operation runs in
    a single transaction so a crash cannot leave a half-written snapshot.

    Parameters
    ----------
    conn:
        Open SQLite connection (WAL, row_factory already set).
    executor:
        The ``SimExecutor`` whose state to snapshot.
    as_of:
        Information timestamp recorded as ``updated_at``.  MUST come from the
        Lane-3 clock — never ``datetime.now()``.
    """
    state = executor.export_state()
    updated_at = as_of.isoformat()

    # Wrap the wipe+rewrite+upsert in a SAVEPOINT (not a bare ``BEGIN``) so it
    # is safe whether or not a parent transaction is already open — a plain
    # ``BEGIN`` would raise "cannot start a transaction within a transaction"
    # when the engine already has an implicit/open transaction, and the
    # engine's broad except would swallow it, silently skipping the snapshot.
    # The SAVEPOINT + RELEASE still commits atomically (all-or-nothing).
    conn.execute("SAVEPOINT snapshot_op")
    try:
        conn.execute("DELETE FROM sim_positions")
        for pos in state["positions"]:
            conn.execute(
                "INSERT INTO sim_positions (ticker, shares, avg_price, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (pos["ticker"], pos["shares"], pos["avg_price"], updated_at),
            )
        conn.execute(
            "INSERT INTO sim_account (id, cash, realized_pl, updated_at) "
            "VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "cash = excluded.cash, "
            "realized_pl = excluded.realized_pl, "
            "updated_at = excluded.updated_at",
            (state["cash"], state["realized_pl"], updated_at),
        )
        conn.execute("RELEASE snapshot_op")
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO snapshot_op")
        conn.execute("RELEASE snapshot_op")
        raise


def load_account_state(conn: sqlite3.Connection) -> dict | None:
    """Return the durable executor state, or ``None`` if never snapshotted.

    The returned dict is shaped exactly like ``SimExecutor.export_state()`` so
    it can be fed straight into ``SimExecutor.restore_state``.
    """
    account = conn.execute(
        "SELECT cash, realized_pl FROM sim_account WHERE id = 1"
    ).fetchone()
    if account is None:
        return None

    position_rows = conn.execute(
        "SELECT ticker, shares, avg_price FROM sim_positions"
    ).fetchall()

    return {
        "cash": account["cash"],
        "realized_pl": account["realized_pl"],
        "positions": [
            {
                "ticker": row["ticker"],
                "shares": row["shares"],
                "avg_price": row["avg_price"],
            }
            for row in position_rows
        ],
    }


def open_position_count(conn: sqlite3.Connection) -> int:
    """Return the number of durable open positions (``shares > 0``)."""
    row = conn.execute(
        "SELECT count(*) FROM sim_positions WHERE shares > 0"
    ).fetchone()
    return int(row[0])


def seed_executor(conn: sqlite3.Connection, executor: SimExecutor) -> None:
    """Restore *executor* from the durable tables.  No-op if never snapshotted.

    Called at ``build_engine`` so the in-memory broker resumes from the last
    persisted snapshot instead of a fresh starting balance.
    """
    state = load_account_state(conn)
    if state is None:
        return
    executor.restore_state(state)
