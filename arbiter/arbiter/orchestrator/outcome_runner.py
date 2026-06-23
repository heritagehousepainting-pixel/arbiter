"""Outcome runner — WP-C (Phase-2 persistence).

Ties the in-memory outcome sweep (Lane 13) and the outcome labeler (Lane 14a)
to durable persistence (WP-A ``idea_store`` + Lane 14a ``outcome_store``).

Flow (per PHASE2-PERSISTENCE-PLAN.md §WP-C):
    1. Load MONITORED ideas from the DB.
    2. ``sweep_outcomes`` advances eligible ideas MONITORED -> OUTCOME_READY
       *in memory* (mutating the loaded ``Idea`` objects) and emits an
       ``OutcomeReadyEvent`` per advanced idea.
    3. For each event:
         - attempt to label the idea via PIT FIRST,
           * on ``LookupError`` — the required price is not yet available
             (EXPECTED for fresh ideas still inside their price window) —
             log a warning and continue, persisting NOTHING.  The durable row
             stays MONITORED so the idea is re-attempted on the next sweep.
             This never aborts the sweep; remaining ideas still process.
           * on success — persist the OUTCOME_READY transition, store the
             outcome, transition the idea to CLOSED, and record the stored
             outcome id.
    4. Return the list of stored outcome ids.

The whole step is structurally look-ahead-safe: every timestamp comes from the
injected ``clock`` (never ``datetime.now()``), and prices come only via PIT.

``advisor_id_for`` is a caller-supplied ``callable(idea) -> str``.  For the MVP
the engine passes a horizon-based stub (e.g. returning ``"A1.congress"`` for
long-horizon ideas and ``"A1.insider"`` for short-horizon ones); the empirical
trust feed stays Phase-3-gated.  ``advisor_confidence_for`` is an optional
``callable(idea) -> float``; when omitted, confidence defaults to ``1.0``.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Callable

from arbiter.contract.seams import Idea
from arbiter.data.clock import Clock
from arbiter.data.pit import PITGateway
from arbiter.evaluation import attribution
from arbiter.orchestrator import idea_store
from arbiter.orchestrator.outcome_sweep import sweep_outcomes
from arbiter.types import IdeaState

logger = logging.getLogger(__name__)


def run_outcome_sweep(
    conn: sqlite3.Connection,
    *,
    pit: PITGateway,
    clock: Clock,
    advisor_id_for: Callable[[Idea], str],
    advisor_confidence_for: Callable[[Idea], float] | None = None,
    audit_path: str | Path | None = None,
    metrics=None,
) -> list[str]:
    """Sweep MONITORED ideas, label the ready ones, and persist the results.

    Parameters
    ----------
    conn:
        Open SQLite connection (migrated).
    pit:
        Point-in-time gateway — the ONLY price source for labeling.
    clock:
        Injected clock.  ``clock.now()`` supplies every timestamp (transition
        times, label cutoff, outcome ``as_of``).  Never ``datetime.now()``.
    advisor_id_for:
        ``callable(idea) -> str`` returning the advisor id to attribute the
        outcome to.  The engine passes a horizon-based stub for the MVP.
    advisor_confidence_for:
        Optional ``callable(idea) -> float``.  Defaults to ``1.0`` when omitted.
    audit_path:
        Override audit file path (threaded through to both the idea-state
        transitions and the outcome insert).  Useful in tests.

    Returns
    -------
    list[str]
        Primary keys of the outcome rows stored this sweep (one per idea that
        was successfully labeled and CLOSED).  Empty when nothing was ready or
        every ready idea hit a price ``LookupError``.
    """
    # 1. Load MONITORED ideas.
    ideas = idea_store.load_ideas_by_state(conn, {IdeaState.MONITORED})
    if not ideas:
        return []

    # B2 — guard against double-labeling: skip any MONITORED idea that has a
    # SELL order row for its (ticker, bucket).  Those exits are owned by the
    # exit monitor's reconcile/close-out path (it labels with the REAL exit
    # price + the trigger's label_kind).  Without this guard, a pending-SELL
    # idea whose horizon date has passed would be labeled "normal" by the
    # horizon sweep in the same cycle while the sell is in flight — a
    # double-process with the wrong label.
    ideas = [idea for idea in ideas if not _has_sell_order(conn, idea)]
    if not ideas:
        return []

    # 2. Advance eligible ideas MONITORED -> OUTCOME_READY in memory.
    events = sweep_outcomes(ideas, clock)
    if not events:
        return []

    # Index the (mutated) in-memory ideas by id so we can recover the object
    # the sweep just flipped to OUTCOME_READY.
    ideas_by_id: dict[str, Idea] = {idea.idea_id: idea for idea in ideas}

    stored_ids: list[str] = []

    for event in events:
        now = clock.now()

        idea = ideas_by_id.get(event.idea_id)
        if idea is None:  # pragma: no cover — sweep events always map back.
            logger.warning(
                "outcome_runner: no in-memory idea for event idea_id=%s; skipping",
                event.idea_id,
            )
            continue

        # 3. Attempt to label + fan out per-advisor outcomes FIRST — before
        #    persisting any state transition.  Attribution recovers the
        #    persisted opinions for the idea and writes ONE outcome per
        #    contributing advisor (each with its own stance/confidence); when no
        #    opinion is recoverable it falls back to the horizon-proxy
        #    ``advisor_id_for`` (#5a, D2/D4).
        #    Missing price (LookupError) is EXPECTED for fresh ideas still
        #    inside their price window: log a warning and continue, persisting
        #    NOTHING.  The durable row stays MONITORED so the idea is
        #    re-attempted on the next sweep (the in-memory flip to OUTCOME_READY
        #    by ``sweep_outcomes`` is discarded).  This must not abort the rest
        #    of the sweep.
        try:
            oids = attribution.resolve_advisor_outcomes(
                conn,
                idea,
                pit=pit,
                cutoff_as_of=now,
                label_kind="normal",
                audit_path=audit_path,
                metrics=metrics,
                fallback_advisor_id_for=advisor_id_for,
                fallback_advisor_confidence_for=advisor_confidence_for,
            )
        except LookupError as exc:
            logger.warning(
                "outcome_runner: price not yet available for idea %s (%s) — "
                "leaving MONITORED for retry on next sweep: %s",
                idea.idea_id,
                idea.ticker,
                exc,
            )
            continue

        if not oids:
            # Nothing written (no opinion + no fallback) — leave MONITORED.
            continue

        # Success: persist the legal FSM path MONITORED -> OUTCOME_READY ->
        # CLOSED in the durable store.  The CLOSED flip happens AFTER the
        # per-advisor fan-out loop has stored every linked advisor's outcome.
        idea_store.update_idea_state(
            conn,
            event.idea_id,
            IdeaState.OUTCOME_READY,
            updated_state_at=now,
            audit_path=audit_path,
        )
        idea_store.update_idea_state(
            conn,
            idea.idea_id,
            IdeaState.CLOSED,
            updated_state_at=now,
            audit_path=audit_path,
        )
        stored_ids.extend(oids)

    # 4. Return the stored outcome ids.
    return stored_ids


def _has_sell_order(conn: sqlite3.Connection, idea: Idea) -> bool:
    """Return True if a SELL order row exists for the idea (B2 guard).

    Prefers the exact ``orders.idea_id`` link (B5) when present; falls back to
    the (ticker, horizon_bucket) join for legacy NULL rows.  ``is_superseded``
    is not a column on ``orders`` (B0), so "non-superseded" is implicit.
    """
    ticker, bucket = idea.dedupe_key
    row = conn.execute(
        "SELECT 1 FROM orders WHERE side = 'SELL' "
        "AND (idea_id = ? OR (ticker = ? AND horizon_bucket = ?)) LIMIT 1",
        (idea.idea_id, ticker, bucket),
    ).fetchone()
    return row is not None
