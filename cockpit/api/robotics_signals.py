"""Recent robotics signals for the cockpit feed (read-only, defensive).

Reads the ``robotics_signals`` table the arbiter scan writes (#3c). Returns an
empty feed if the table is absent (DB not yet migrated, or no scan has run) —
the cockpit never writes and must degrade cleanly.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .contract import RoboticsSignal, RoboticsSignals


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_robotics_signals(conn: sqlite3.Connection, *, limit: int = 30) -> RoboticsSignals:
    """Most-recent signals first (trigger-hits ahead within a timestamp)."""
    try:
        rows = conn.execute(
            "SELECT as_of, headline, summary, category, symbols, trigger_hit, "
            "trigger_name, sources FROM robotics_signals "
            "ORDER BY as_of DESC, trigger_hit DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    except sqlite3.OperationalError:
        return RoboticsSignals(signals=[], as_of=_now())  # table missing → empty

    signals = [
        RoboticsSignal(
            as_of=as_of,
            headline=headline,
            summary=summary or "",
            category=category or "other",
            symbols=[s for s in (symbols or "").split(",") if s],
            trigger_hit=bool(trigger_hit),
            trigger_name=trigger_name,
            sources=[s for s in (sources or "").split(",") if s],
        )
        for as_of, headline, summary, category, symbols, trigger_hit, trigger_name, sources in rows
    ]
    return RoboticsSignals(signals=signals, as_of=_now())
