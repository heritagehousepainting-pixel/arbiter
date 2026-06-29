"""Persistence for macro findings consumed by the engine's A4.macro gather."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from arbiter.refresh.types import MacroFinding, Severity

_DDL = """
CREATE TABLE IF NOT EXISTS macro_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    severity TEXT NOT NULL,
    affected_tickers TEXT NOT NULL,
    sources TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_macro_findings_expires ON macro_findings (expires_at);
"""


def create_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()


def persist_findings(conn: sqlite3.Connection, findings: list[MacroFinding],
                     as_of: datetime, *, expiry_days: int = 7) -> int:
    expires = (as_of + timedelta(days=expiry_days)).isoformat()
    n = 0
    for f in findings:
        if not f.affected_tickers:
            continue
        conn.execute(
            "INSERT INTO macro_findings "
            "(as_of, expires_at, summary, severity, affected_tickers, sources) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (as_of.isoformat(), expires, f.summary, f.severity.value,
             ",".join(f.affected_tickers), ",".join(f.sources)),
        )
        n += 1
    conn.commit()
    return n


def read_active_findings(conn: sqlite3.Connection,
                         as_of: datetime) -> list[MacroFinding]:
    rows = conn.execute(
        "SELECT summary, severity, affected_tickers, sources "
        "FROM macro_findings WHERE expires_at > ?", (as_of.isoformat(),)
    ).fetchall()
    out: list[MacroFinding] = []
    for summary, sev, tickers, sources in rows:
        out.append(MacroFinding(
            summary=summary, severity=Severity(sev),
            affected_tickers=[t for t in tickers.split(",") if t],
            sources=[s for s in sources.split(",") if s]))
    return out
