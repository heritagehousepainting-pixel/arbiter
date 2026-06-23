"""Read-only DB queries for the Arbiter dashboard (Lane 5 / web).

All functions are pure reads — no INSERT, UPDATE, or DELETE.
Defensive: every function returns sensible defaults when the table is missing
or empty, so the dashboard never 500s on a fresh / partial DB.

Design rules (INTERFACES.md §11):
  - No datetime.now() — timestamps come from DB rows, not wall-clock.
  - Read-only: no mutating statements.
  - Defensive: catches OperationalError (table not yet migrated) and returns [].
"""
from __future__ import annotations

import sqlite3
from typing import Any


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _safe_fetchall(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[sqlite3.Row]:
    """Execute *sql* and return all rows; return [] on OperationalError.

    OperationalError is raised by SQLite when the table doesn't exist (e.g.
    migrations not yet applied).  We swallow it so the dashboard renders
    gracefully on a fresh or partial DB.
    """
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


# ---------------------------------------------------------------------------
# Orders / positions
# ---------------------------------------------------------------------------

def get_recent_orders(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
) -> list[sqlite3.Row]:
    """Return up to *limit* most-recent orders, newest first.

    Returns [] if the orders table doesn't exist or is empty.

    Columns returned: order_id, ticker, side, qty, horizon_bucket,
    entry_date, advisor_signature, status, created_at.
    """
    return _safe_fetchall(
        conn,
        """
        SELECT order_id, ticker, side, qty, horizon_bucket,
               entry_date, advisor_signature, status, created_at
        FROM orders
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    )


# ---------------------------------------------------------------------------
# Breaker / safety state
# ---------------------------------------------------------------------------

def get_tripped_breakers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all latched circuit-breaker rows.

    Returns [] if the breaker_state table doesn't exist or nothing is latched.

    Columns returned: breaker_name, latched_at, reason.
    """
    return _safe_fetchall(
        conn,
        """
        SELECT breaker_name, latched_at, reason
        FROM breaker_state
        WHERE latched = 1
        ORDER BY breaker_name
        """,
    )


def get_all_breakers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all breaker rows (latched or not) for the full status panel.

    Columns returned: breaker_name, latched, latched_at, reason.
    """
    return _safe_fetchall(
        conn,
        """
        SELECT breaker_name, latched, latched_at, reason
        FROM breaker_state
        ORDER BY breaker_name
        """,
    )


def get_advisor_count(conn: sqlite3.Connection) -> int:
    """Return the number of registered advisors.

    Returns 0 if the advisor_registry table doesn't exist.
    """
    rows = _safe_fetchall(conn, "SELECT COUNT(*) AS cnt FROM advisor_registry")
    if not rows:
        return 0
    return int(rows[0]["cnt"])


# ---------------------------------------------------------------------------
# Leaderboard data (signal-type scores / person scores)
# ---------------------------------------------------------------------------

def get_signal_type_scores(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return signal-type accuracy rows from the signals scoring table.

    Returns [] if the table doesn't exist (cold-start / Wave-A).

    Columns returned: signal_type, accuracy, samples, gate_pass.
    """
    return _safe_fetchall(
        conn,
        """
        SELECT signal_type, accuracy, samples, gate_pass
        FROM signal_type_scores
        ORDER BY signal_type
        """,
    )


def get_person_scores(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Return top-*limit* person accuracy rows.

    Returns [] if the table doesn't exist.

    Columns returned: person_id, accuracy, samples, gate_pass.
    """
    return _safe_fetchall(
        conn,
        """
        SELECT person_id, accuracy, samples, gate_pass
        FROM person_scores
        ORDER BY samples DESC, accuracy DESC
        LIMIT ?
        """,
        (limit,),
    )
