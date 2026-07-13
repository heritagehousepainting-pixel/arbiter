"""Persistence for robotics-signal developments (mirrors refresh/findings_store).

Written by the arbiter-side scan; read read-only by the cockpit feed (#3c) and,
later, the probationary A5.robotics advisor (#3d). Schema also lives in
``db/migrations/035_robotics_signals.sql``; ``create_table`` here is the
belt-and-suspenders programmatic form used in tests (same pattern as
``findings_store.create_table`` for ``macro_findings``).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from arbiter.robotics_signal.types import RoboticsDevelopment

_DDL = """
CREATE TABLE IF NOT EXISTS robotics_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    headline TEXT NOT NULL,
    summary TEXT NOT NULL,
    category TEXT NOT NULL,
    symbols TEXT NOT NULL,
    trigger_hit INTEGER NOT NULL,
    trigger_name TEXT,
    sources TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_robotics_signals_as_of ON robotics_signals (as_of);
CREATE INDEX IF NOT EXISTS idx_robotics_signals_trigger ON robotics_signals (trigger_hit);
"""


def create_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()


def persist_signals(conn: sqlite3.Connection, developments: list[RoboticsDevelopment],
                    as_of: datetime) -> int:
    """Insert one row per development. Returns the number written."""
    create_table(conn)  # idempotent — safe even pre-migration
    n = 0
    for d in developments:
        conn.execute(
            "INSERT INTO robotics_signals "
            "(as_of, headline, summary, category, symbols, trigger_hit, trigger_name, sources) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (as_of.isoformat(), d.headline, d.summary, d.category,
             ",".join(d.symbols), 1 if d.trigger_hit else 0, d.trigger_name,
             ",".join(d.sources)),
        )
        n += 1
    conn.commit()
    return n


def read_recent_signals(conn: sqlite3.Connection, *, limit: int = 30) -> list[dict]:
    """Most-recent signals first (trigger-hits sort ahead within a timestamp)."""
    rows = conn.execute(
        "SELECT as_of, headline, summary, category, symbols, trigger_hit, "
        "trigger_name, sources FROM robotics_signals "
        "ORDER BY as_of DESC, trigger_hit DESC, id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def read_active_trigger_hits(conn: sqlite3.Connection, as_of: datetime,
                             *, window_days: int = 7) -> list[dict]:
    """Trigger-hits inside a recency window, most-recent first.

    Feeds the probationary A5.robotics advisor (#3d).  Mirrors
    ``findings_store.read_active_findings``: SQL-level filtering so a stale
    signal can never keep nudging the engine.  Only rows with ``trigger_hit=1``
    whose ``as_of`` is within ``window_days`` of ``as_of`` are returned.
    Timestamps are UTC ISO strings, so the ``>=`` string comparison sorts
    chronologically.
    """
    cutoff = (as_of - timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        "SELECT as_of, headline, summary, category, symbols, trigger_hit, "
        "trigger_name, sources FROM robotics_signals "
        "WHERE trigger_hit = 1 AND as_of >= ? "
        "ORDER BY as_of DESC, id DESC",
        (cutoff,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row: tuple) -> dict:
    as_of, headline, summary, category, symbols, trigger_hit, trigger_name, sources = row
    return {
        "as_of": as_of,
        "headline": headline,
        "summary": summary,
        "category": category,
        "symbols": [s for s in (symbols or "").split(",") if s],
        "trigger_hit": bool(trigger_hit),
        "trigger_name": trigger_name,
        "sources": [s for s in (sources or "").split(",") if s],
    }
