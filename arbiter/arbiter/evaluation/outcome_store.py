"""Outcome store — Lane 14a.

Persists ``ResolvedOutcome`` objects into the ``outcomes`` table using the
insert-only helpers from ``arbiter.db.helpers``.  Emits an audit line on
every successful insert (INTERFACES.md §10, §11).

Design constraints (INTERFACES.md §10, §11):
    - Insert-only via ``insert_row`` — never UPDATE or DELETE outcomes rows.
    - Corrections flow through ``supersede_row`` (caller's responsibility to
      call with the old outcome id).
    - Timestamps come from the caller's ``as_of`` / ``clock`` — never
      ``datetime.now()``.

Public surface:
    store_outcome(outcome, conn, *, as_of, audit_path=None) -> str
    supersede_outcome(old_id, outcome, conn, *, as_of, audit_path=None) -> str
    query_outcomes(conn, *, idea_id=None, advisor_id=None, ticker=None) -> list[dict]
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from arbiter.contract.seams import ResolvedOutcome
from arbiter.db.audit import audit
from arbiter.db.helpers import insert_row, supersede_row


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def store_outcome(
    outcome: ResolvedOutcome,
    conn: sqlite3.Connection,
    *,
    as_of: datetime,
    audit_path: str | Path | None = None,
) -> str:
    """Insert *outcome* into the ``outcomes`` table and emit an audit line.

    Parameters
    ----------
    outcome:
        The ``ResolvedOutcome`` to persist.
    conn:
        Open SQLite connection (WAL, row_factory already set).
    as_of:
        Information timestamp to record as ``created_at``.  MUST come from
        the Lane-3 clock — never ``datetime.now()``.
    audit_path:
        Override the audit file path (useful in tests).  Defaults to
        ``Config.audit_path``.

    Returns
    -------
    str
        The ULID primary key of the newly inserted row.
    """
    row = _outcome_to_row(outcome, as_of=as_of)
    pk = insert_row(conn, "outcomes", row)

    audit(
        "insert_outcome",
        {
            "id": pk,
            "idea_id": outcome.idea_id,
            "advisor_id": outcome.advisor_id,
            "ticker": outcome.ticker,
            "alpha_bps": outcome.alpha_bps,
            "binary": outcome.binary,
            "label_kind": outcome.label_kind,
            "abstained": outcome.abstained,
        },
        ts=as_of.isoformat(),
        audit_path=audit_path,
    )

    return pk


def supersede_outcome(
    old_id: str,
    outcome: ResolvedOutcome,
    conn: sqlite3.Connection,
    *,
    as_of: datetime,
    audit_path: str | Path | None = None,
) -> str:
    """Insert a corrected *outcome* and mark the old row superseded.

    Follows the insert-only correction pattern: new row carries
    ``supersedes_id = old_id``; old row gets ``is_superseded = 1``
    (the ONLY in-place UPDATE allowed per INTERFACES.md §11.2).

    Parameters
    ----------
    old_id:
        Primary key of the outcome row being superseded.
    outcome:
        The corrected ``ResolvedOutcome``.
    conn:
        Open SQLite connection.
    as_of:
        Information timestamp for the new row's ``created_at``.
    audit_path:
        Override the audit file path.

    Returns
    -------
    str
        The ULID primary key of the new (correcting) row.
    """
    row = _outcome_to_row(outcome, as_of=as_of)
    new_id = supersede_row(conn, "outcomes", old_id, row)

    audit(
        "supersede_outcome",
        {
            "old_id": old_id,
            "new_id": new_id,
            "idea_id": outcome.idea_id,
            "advisor_id": outcome.advisor_id,
            "ticker": outcome.ticker,
            "alpha_bps": outcome.alpha_bps,
            "binary": outcome.binary,
            "label_kind": outcome.label_kind,
        },
        ts=as_of.isoformat(),
        audit_path=audit_path,
    )

    return new_id


def query_outcomes(
    conn: sqlite3.Connection,
    *,
    idea_id: str | None = None,
    advisor_id: str | None = None,
    ticker: str | None = None,
    include_superseded: bool = False,
    as_of: datetime | None = None,
    strict_lt: bool = False,
) -> list[dict[str, Any]]:
    """Return outcome rows matching the given filters.

    All returned dicts map column name → value (using ``sqlite3.Row`` factory).
    By default superseded rows are excluded (``is_superseded = 0``).

    Parameters
    ----------
    conn:
        Open SQLite connection.
    idea_id:
        Filter by idea_id (exact match).
    advisor_id:
        Filter by advisor_id (exact match).
    ticker:
        Filter by ticker (exact match).
    include_superseded:
        If True, include rows where ``is_superseded = 1``.
    as_of:
        Optional point-in-time cutoff on ``created_at`` (defense in depth for the
        learning loop, #4 / D3).  When provided, appends ``AND created_at <= ?``
        (or ``< ?`` when ``strict_lt`` is True).  Backwards-compatible: defaults
        to no cutoff.
    strict_lt:
        When True (and ``as_of`` is set), use a STRICT ``created_at < as_of``
        cutoff instead of ``<=`` (D0 — excludes same-cycle outcomes stamped at
        the current ``now``).

    Returns
    -------
    list[dict[str, Any]]
        Each element is a dict of column → value for a matching outcome row.
    """
    clauses: list[str] = []
    params: list[Any] = []

    if not include_superseded:
        clauses.append("is_superseded = 0")
    if idea_id is not None:
        clauses.append("idea_id = ?")
        params.append(idea_id)
    if advisor_id is not None:
        clauses.append("advisor_id = ?")
        params.append(advisor_id)
    if ticker is not None:
        clauses.append("ticker = ?")
        params.append(ticker)
    if as_of is not None:
        clauses.append("created_at < ?" if strict_lt else "created_at <= ?")
        params.append(as_of.isoformat())

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM outcomes {where} ORDER BY created_at ASC"  # noqa: S608

    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _outcome_to_row(outcome: ResolvedOutcome, *, as_of: datetime) -> dict[str, Any]:
    """Serialize a ``ResolvedOutcome`` to a DB row dict."""
    return {
        "idea_id": outcome.idea_id,
        "advisor_id": outcome.advisor_id,
        "ticker": outcome.ticker,
        "alpha_bps": outcome.alpha_bps,
        "binary": outcome.binary,
        "advisor_confidence": outcome.advisor_confidence,
        "stance_score": outcome.stance_score,
        "abstained": int(outcome.abstained),
        "horizon_days": outcome.horizon_days,
        "label_kind": outcome.label_kind,
        "created_at": as_of.isoformat(),
    }
