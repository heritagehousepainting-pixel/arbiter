"""Opinion store — sub-project #5a (real attribution).

Persists gathered :class:`~arbiter.contract.opinion.Opinion` rows into the
``opinions`` table at decision time, linked to the idea they informed
(``idea_id``).  At resolution the attribution resolver recovers these rows so
each advisor is scored against ITS OWN emitted stance/confidence.

Design constraints (INTERFACES.md §10, §11; spec D1/D5/D6):
    - Insert-only via ``insert_row`` — never UPDATE or DELETE opinion rows.
    - Timestamps (``as_of`` / ``created_at``) come from the caller's clock —
      never ``datetime.now()`` (backtests stamp the replay date; PIT-clean).
    - Idempotency SELECT-guard: a re-run at the same ``as_of`` must NOT
      double-insert.  Guard on (advisor_id, idea_id, source_fingerprint, as_of).

Public surface:
    persist_opinion(conn, opinion, *, idea_id, as_of, audit_path=None) -> str | None
    query_opinions_for_idea(conn, idea_id, *, include_superseded=False) -> list[dict]
    query_opinions(conn, *, ticker=None, advisor_id=None, idea_id=None,
                   as_of=None, strict_lt=False, include_superseded=False) -> list[dict]
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from arbiter.contract.opinion import Opinion
from arbiter.db.audit import audit
from arbiter.db.helpers import insert_row


def persist_opinion(
    conn: sqlite3.Connection,
    opinion: Opinion,
    *,
    idea_id: str | None,
    as_of: datetime,
    audit_path: str | Path | None = None,
) -> str | None:
    """Insert *opinion* into ``opinions``, linked to *idea_id*, at *as_of*.

    Insert-only.  ``created_at`` = ``as_of.isoformat()`` (the decision time, from
    the injected clock; a backtest stamps the replay date — PIT-clean).
    ``idea_id`` may be ``None`` (no matching idea this cycle — normal on
    source-overlapping tickers; persisted-but-never-attributed for audit
    completeness, E3).

    Idempotency: skips the insert (returns the existing id) when a non-superseded
    row already exists for ``(advisor_id, idea_id, source_fingerprint, as_of)`` so
    a re-run / retry at the same ``as_of`` does not double-insert.

    Returns the row's primary-key ULID (newly inserted OR the pre-existing one),
    or ``None`` only when the guard SELECT itself cannot run.  Errors are NOT
    swallowed silently (E1) — the caller surfaces / counts them.
    """
    as_of_iso = as_of.isoformat()

    # Idempotency SELECT-guard (D1).  idea_id may be NULL → use IS NULL.
    if idea_id is None:
        existing = conn.execute(
            "SELECT id FROM opinions WHERE advisor_id = ? AND idea_id IS NULL "
            "AND source_fingerprint = ? AND as_of = ? AND is_superseded = 0 LIMIT 1",
            (opinion.advisor_id, opinion.source_fingerprint, as_of_iso),
        ).fetchone()
    else:
        existing = conn.execute(
            "SELECT id FROM opinions WHERE advisor_id = ? AND idea_id = ? "
            "AND source_fingerprint = ? AND as_of = ? AND is_superseded = 0 LIMIT 1",
            (opinion.advisor_id, idea_id, opinion.source_fingerprint, as_of_iso),
        ).fetchone()
    if existing is not None:
        return existing["id"]

    row: dict[str, Any] = {
        "advisor_id": opinion.advisor_id,
        "ticker": opinion.ticker,
        "stance_score": opinion.stance_score,
        "confidence": opinion.confidence,
        "confidence_source": opinion.confidence_source.value,
        "horizon_days": opinion.horizon_days,
        "as_of": as_of_iso,
        "rationale": opinion.rationale,
        "source_fingerprint": opinion.source_fingerprint,
        "run_group_id": opinion.run_group_id,
        "idea_id": idea_id,
        "is_superseded": 0,
        "created_at": as_of_iso,
    }
    pk = insert_row(conn, "opinions", row)

    audit(
        "persist_opinion",
        {
            "id": pk,
            "advisor_id": opinion.advisor_id,
            "ticker": opinion.ticker,
            "idea_id": idea_id,
            "stance_score": opinion.stance_score,
            "confidence": opinion.confidence,
        },
        ts=as_of_iso,
        audit_path=audit_path,
    )
    return pk


def query_opinions_for_idea(
    conn: sqlite3.Connection,
    idea_id: str,
    *,
    include_superseded: bool = False,
) -> list[dict[str, Any]]:
    """Return the persisted opinion rows linked to *idea_id* — the resolution-time
    recovery query: "the opinions that drove idea X"."""
    return query_opinions(
        conn, idea_id=idea_id, include_superseded=include_superseded
    )


def query_opinions(
    conn: sqlite3.Connection,
    *,
    ticker: str | None = None,
    advisor_id: str | None = None,
    idea_id: str | None = None,
    as_of: datetime | None = None,
    strict_lt: bool = False,
    include_superseded: bool = False,
) -> list[dict[str, Any]]:
    """Return opinion rows matching the given filters (mirrors query_outcomes)."""
    clauses: list[str] = []
    params: list[Any] = []

    if not include_superseded:
        clauses.append("is_superseded = 0")
    if ticker is not None:
        clauses.append("ticker = ?")
        params.append(ticker)
    if advisor_id is not None:
        clauses.append("advisor_id = ?")
        params.append(advisor_id)
    if idea_id is not None:
        clauses.append("idea_id = ?")
        params.append(idea_id)
    if as_of is not None:
        clauses.append("created_at < ?" if strict_lt else "created_at <= ?")
        params.append(as_of.isoformat())

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM opinions {where} ORDER BY created_at ASC"  # noqa: S608

    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]
