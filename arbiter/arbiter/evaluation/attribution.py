"""Real outcome attribution — sub-project #5a (spec D2/D4).

At resolution time, recover EVERY persisted opinion that drove an idea and write
ONE :class:`~arbiter.contract.seams.ResolvedOutcome` per contributing advisor,
each carrying THAT advisor's own ``stance_score`` + ``confidence`` as the Brier
forecast.  The realized alpha/binary/label_kind are identical across advisors
for the same idea (same entry/exit/beta); only the per-advisor forecast differs.

This replaces the horizon-proxy ``_advisor_id_for`` attribution on the primary
path.  The proxy survives ONLY as a last-resort fallback for an idea with no
recoverable opinion (legacy / orphan close) — it writes a single neutral
(``stance_score=0.0``) outcome and increments ``attribution.fallback_proxy`` so a
silent opinion-persist regression is visible (E1).

Idempotency (D2, E0): a per-``(idea, advisor)`` existence guard means the
resolver only writes outcomes for advisors that do NOT yet have one for the idea.
This makes resolution idempotent across the sweep, the sync close-out, the
reconcile close-out, AND the stranded-closeout retry — and lets a PARTIAL
fan-out (some advisors written, then a crash) be completed safely on a later
re-run that re-selects the idea by strict-subset (E0).

No ``datetime.now()`` — all timestamps come from the caller's clock / as_of.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Callable

from arbiter.contract.seams import Idea
from arbiter.data.pit import PITGateway
from arbiter.evaluation import outcome_labeler, outcome_store
from arbiter.signals import opinion_store

_logger = logging.getLogger(__name__)

# Reserved namespace for the neutral last-resort fallback outcome (E3).  The
# fallback fires when an idea closes with NO recoverable opinion; it must NOT
# emit a real advisor id (e.g. "A1.congress").  If it did, the per-(idea,
# advisor) idempotency guard would treat the neutral stance-0 proxy as the
# advisor's outcome and PERMANENTLY MASK the true per-advisor stance once the
# real opinion is recovered on a later re-run.  Prefixing with "PROXY." gives
# the fallback its own id space that can never collide with a real advisor.
_PROXY_NAMESPACE = "PROXY"


def proxy_advisor_id(real_advisor_id: str) -> str:
    """Return the reserved PROXY.* id for a fallback outcome (E3).

    ``A1.congress`` → ``PROXY.A1.congress``.  Idempotent: an id already in the
    reserved namespace is returned unchanged.
    """
    if real_advisor_id.startswith(f"{_PROXY_NAMESPACE}."):
        return real_advisor_id
    return f"{_PROXY_NAMESPACE}.{real_advisor_id}"


def resolve_advisor_outcomes(
    conn: sqlite3.Connection,
    idea: Idea,
    *,
    pit: PITGateway,
    cutoff_as_of,
    exit_price: float | None = None,
    exit_as_of=None,
    label_kind: str = "normal",
    audit_path: str | Path | None = None,
    metrics=None,
    fallback_advisor_id_for: Callable[[Idea], str] | None = None,
    fallback_advisor_confidence_for: Callable[[Idea], float] | None = None,
) -> list[str]:
    """Fan out per-advisor ResolvedOutcomes for *idea*; return stored outcome ids.

    Steps (D2):
      1. recover ``opinions_for_idea = query_opinions_for_idea(conn, idea.idea_id)``;
      2. dedup to one row per advisor (latest ``created_at`` — the live stance);
      3. skip advisors that already have a non-superseded outcome for the idea
         (per-(idea,advisor) idempotency guard);
      4. for each remaining advisor, label (same realized alpha/binary) carrying
         that advisor's ``stance_score`` + ``confidence``, then ``store_outcome``;
      5. return the stored ids.

    No recoverable opinion → last-resort fallback (D4): one neutral
    (``stance_score=0.0``, ``confidence=1.0``) outcome via
    ``fallback_advisor_id_for`` + increment ``attribution.fallback_proxy`` + WARN.

    A ``LookupError`` from the labeler (a required PIT bar not yet available)
    propagates to the caller unchanged so the existing leave-MONITORED-and-retry
    behavior is preserved.  Because the existence guard is checked BEFORE the
    write loop and a raise aborts the loop, a retry re-labels only the missing
    advisors (idempotent).
    """
    rows = opinion_store.query_opinions_for_idea(conn, idea.idea_id)

    # Dedup to one opinion per advisor (latest created_at wins).  query returns
    # rows ordered by created_at ASC, so the last seen per advisor is the latest.
    latest_by_advisor: dict[str, dict] = {}
    for row in rows:
        latest_by_advisor[row["advisor_id"]] = row

    # --- Last-resort fallback: no recoverable opinion (D4 / E1). ---
    if not latest_by_advisor:
        if fallback_advisor_id_for is None:
            _logger.warning(
                "attribution.no_opinion_no_fallback: idea %s (%s) has no recoverable "
                "opinion and no fallback advisor — writing nothing",
                idea.idea_id, idea.ticker,
            )
            return []
        # Wrap the horizon-proxy advisor in the RESERVED PROXY.* namespace (E3)
        # so the neutral stance-0 fallback can never collide with — and thus
        # permanently mask — the real per-advisor outcome once the opinion is
        # recovered.  The metrics/log still surface the underlying advisor.
        real_advisor_id = fallback_advisor_id_for(idea)
        advisor_id = proxy_advisor_id(real_advisor_id)
        # Guard the fallback advisor too (idempotent across retries).
        existing = {
            o["advisor_id"]
            for o in outcome_store.query_outcomes(conn, idea_id=idea.idea_id)
        }
        if advisor_id in existing:
            return []
        confidence = (
            fallback_advisor_confidence_for(idea)
            if fallback_advisor_confidence_for is not None
            else 1.0
        )
        outcome = outcome_labeler.label(
            idea,
            pit=pit,
            cutoff_as_of=cutoff_as_of,
            advisor_id=advisor_id,
            advisor_confidence=confidence,
            stance_score=0.0,  # neutral → p_hat=0.5 → does not move skill (D4)
            exit_price=exit_price,
            exit_as_of=exit_as_of,
            label_kind=label_kind,
        )
        oid = outcome_store.store_outcome(
            outcome, conn, as_of=cutoff_as_of, audit_path=audit_path
        )
        _logger.warning(
            "attribution.fallback_proxy: idea %s (%s) resolved via horizon proxy "
            "advisor=%s (proxy id=%s) stance=0.0 (no persisted opinion)",
            idea.idea_id, idea.ticker, real_advisor_id, advisor_id,
        )
        if metrics is not None:
            try:
                metrics.record(
                    "attribution.fallback_proxy",
                    {
                        "idea_id": idea.idea_id,
                        "ticker": idea.ticker,
                        "advisor_id": advisor_id,
                        "real_advisor_id": real_advisor_id,
                    },
                    recorded_at=cutoff_as_of.isoformat(),
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning("attribution.metrics_record_failed: %s", exc)
        return [oid]

    # --- Per-advisor fan-out (D2). ---
    existing = {
        o["advisor_id"]
        for o in outcome_store.query_outcomes(conn, idea_id=idea.idea_id)
    }

    stored_ids: list[str] = []
    for advisor_id in sorted(latest_by_advisor.keys()):
        if advisor_id in existing:
            continue  # already written — idempotency guard
        op_row = latest_by_advisor[advisor_id]
        outcome = outcome_labeler.label(
            idea,
            pit=pit,
            cutoff_as_of=cutoff_as_of,
            advisor_id=advisor_id,
            advisor_confidence=float(op_row["confidence"]),
            stance_score=float(op_row["stance_score"]),
            exit_price=exit_price,
            exit_as_of=exit_as_of,
            label_kind=label_kind,
        )
        oid = outcome_store.store_outcome(
            outcome, conn, as_of=cutoff_as_of, audit_path=audit_path
        )
        stored_ids.append(oid)

    return stored_ids


def linked_opinion_advisors(conn: sqlite3.Connection, idea_id: str) -> set[str]:
    """Return the set of advisor_ids with a persisted opinion linked to *idea_id*."""
    return {
        row["advisor_id"]
        for row in opinion_store.query_opinions_for_idea(conn, idea_id)
    }


def stored_outcome_advisors(conn: sqlite3.Connection, idea_id: str) -> set[str]:
    """Return the set of advisor_ids with a non-superseded outcome for *idea_id*."""
    return {
        row["advisor_id"]
        for row in outcome_store.query_outcomes(conn, idea_id=idea_id)
    }
