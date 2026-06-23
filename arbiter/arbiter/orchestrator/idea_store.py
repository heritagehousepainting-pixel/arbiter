"""Idea store — WP-A (Phase-2 persistence).

Persists the mutable ``Idea`` lifecycle record into the ``ideas`` table.

Insert-only carve-out (FROZEN design decision, PHASE2-PERSISTENCE-PLAN §1;
documented §11.2 in-place-UPDATE carve-out):
    Unlike the immutable FACT tables (filings, opinions, outcomes,
    trust_weights) which are append-only per INTERFACES.md §11.2, the
    ``ideas`` row is a *mutable lifecycle record*.  ``update_idea_state``
    performs a deliberate, documented **in-place UPDATE** of ``state`` +
    ``updated_state_at`` keyed by the STABLE ``idea_id``.  This is the one
    place — alongside supersede — where an in-place UPDATE is permitted.
    ``idea_id`` never changes for the life of the idea (orders & outcomes
    reference it), and every state change emits an audit line.

FSM legality (observability, NOT enforcement):
    Callers OWN FSM enforcement — they FSM-check the in-memory ``Idea``
    (via ``lifecycle.transition``) before persisting.  As the durable
    source of truth, ``update_idea_state`` additionally performs a
    lightweight, defensive legality check against the *persisted* state and
    logs a WARNING when the transition is illegal (or the row is missing).
    It does NOT block: the UPDATE still applies.  Raising here would risk
    being swallowed by the engine's broad ``except`` and could silently
    drop a state change; instead the warning surfaces any divergence
    between the in-memory object and the DB row in logs/audit for
    investigation.  This is log-only by design for the MVP.

Public API (FROZEN — other WPs code against this exactly):
    persist_new_idea(conn, idea, *, created_at) -> None
    update_idea_state(conn, idea_id, new_state, *, updated_state_at) -> None
    load_ideas_by_state(conn, states) -> list[Idea]
    load_active_ideas(conn) -> list[Idea]
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from arbiter.contract.seams import Idea
from arbiter.db.audit import audit
from arbiter.orchestrator import lifecycle
from arbiter.types import IdeaState

logger = logging.getLogger(__name__)


# Terminal states excluded from "active" ideas.
_TERMINAL_STATES: frozenset[IdeaState] = frozenset({
    IdeaState.CLOSED,
    IdeaState.ABANDONED,
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def persist_new_idea(
    conn: sqlite3.Connection,
    idea: Idea,
    *,
    created_at: datetime,
) -> None:
    """Insert *idea* into the ``ideas`` table (idempotent on ``idea_id``).

    Uses ``INSERT OR IGNORE`` keyed on the ``idea_id`` primary key, so
    re-persisting an idea that already exists is a no-op (the existing row,
    including its current ``state``, is left untouched).

    The ``dedupe_key`` tuple is split into the ``dedupe_key_ticker`` and
    ``dedupe_key_bucket`` columns.  ``updated_state_at`` is initialised to
    ``created_at``.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    idea:
        The ``Idea`` to persist.
    created_at:
        Information timestamp recorded as ``created_at`` and the initial
        ``updated_state_at``.  MUST come from the Lane-3 clock.
    """
    dedupe_ticker, dedupe_bucket = idea.dedupe_key
    conn.execute(
        """
        INSERT OR IGNORE INTO ideas (
            idea_id, ticker, thesis, horizon_days, state, as_of,
            dedupe_key_ticker, dedupe_key_bucket, created_at, updated_state_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            idea.idea_id,
            idea.ticker,
            idea.thesis,
            idea.horizon_days,
            idea.state.value,
            idea.as_of.isoformat(),
            dedupe_ticker,
            dedupe_bucket,
            created_at.isoformat(),
            created_at.isoformat(),
        ),
    )
    conn.commit()


def update_idea_state(
    conn: sqlite3.Connection,
    idea_id: str,
    new_state: IdeaState,
    *,
    updated_state_at: datetime,
    audit_path: str | Path | None = None,
) -> None:
    """In-place UPDATE the lifecycle state of an existing idea.

    This is the deliberate insert-only carve-out (see module docstring):
    ``UPDATE ideas SET state=?, updated_state_at=? WHERE idea_id=?``.
    Emits an ``"idea_state_transition"`` audit line.

    Defensive FSM legality check (log-only — see module docstring): the
    *persisted* state is read first and the transition validated via
    ``lifecycle.can_transition``.  Callers OWN FSM enforcement; if the row
    is missing or the persisted-state → ``new_state`` transition is illegal,
    a WARNING is logged but the UPDATE STILL proceeds (this store does not
    block, it surfaces divergence for investigation).

    Parameters
    ----------
    conn:
        Open SQLite connection.
    idea_id:
        Stable primary key of the idea to transition.
    new_state:
        The new ``IdeaState``.
    updated_state_at:
        Timestamp of the transition (from the Lane-3 clock).
    audit_path:
        Override the audit file path (useful in tests).
    """
    # Defensive legality check against the durable (persisted) state.
    # Log-only: never blocks the UPDATE below.
    row = conn.execute(
        "SELECT state FROM ideas WHERE idea_id = ?", (idea_id,)
    ).fetchone()
    if row is None:
        logger.warning(
            "update_idea_state: idea %r not found in store; "
            "persisting transition to %s anyway (caller owns FSM enforcement)",
            idea_id,
            new_state.value,
        )
    else:
        current = IdeaState(row["state"])
        if not lifecycle.can_transition(current, new_state):
            logger.warning(
                "update_idea_state: illegal FSM transition for idea %r: "
                "%s -> %s; persisting anyway (log-only, caller owns FSM "
                "enforcement)",
                idea_id,
                current.value,
                new_state.value,
            )

    conn.execute(
        "UPDATE ideas SET state = ?, updated_state_at = ? WHERE idea_id = ?",
        (new_state.value, updated_state_at.isoformat(), idea_id),
    )
    conn.commit()

    audit(
        "idea_state_transition",
        {"idea_id": idea_id, "new_state": new_state.value},
        ts=updated_state_at.isoformat(),
        audit_path=audit_path,
    )


def load_ideas_by_state(
    conn: sqlite3.Connection,
    states: set[IdeaState],
) -> list[Idea]:
    """Return active (non-superseded) ideas whose state is in *states*.

    Reconstructs each ``Idea`` DIRECTLY (NOT via ``make_idea``, which would
    force NASCENT and recompute the dedupe key).  ``as_of`` is parsed with
    ``datetime.fromisoformat``; if the parsed value is naive it is assumed
    UTC and ``timezone.utc`` is attached.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    states:
        Set of ``IdeaState`` values to include.  An empty set returns ``[]``.

    Returns
    -------
    list[Idea]
        Reconstructed ideas (``is_superseded = 0`` only).
    """
    if not states:
        return []

    placeholders = ", ".join("?" for _ in states)
    sql = (
        "SELECT * FROM ideas "
        f"WHERE is_superseded = 0 AND state IN ({placeholders}) "  # noqa: S608
        "ORDER BY created_at ASC"
    )
    params = [s.value for s in states]
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_idea(row) for row in rows]


def load_active_ideas(conn: sqlite3.Connection) -> list[Idea]:
    """Return all ideas in non-terminal states (everything but CLOSED/ABANDONED).

    Used by the cycle for cross-run dedupe and by the outcome sweep.
    """
    active_states = set(IdeaState) - _TERMINAL_STATES
    return load_ideas_by_state(conn, active_states)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _row_to_idea(row: sqlite3.Row) -> Idea:
    """Reconstruct an ``Idea`` directly from a DB row."""
    as_of = datetime.fromisoformat(row["as_of"])
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    return Idea(
        idea_id=row["idea_id"],
        ticker=row["ticker"],
        thesis=row["thesis"],
        horizon_days=row["horizon_days"],
        state=IdeaState(row["state"]),
        as_of=as_of,
        dedupe_key=(row["dedupe_key_ticker"], row["dedupe_key_bucket"]),
    )
