"""Historical outcome-backfill harness — W-BACKFILL (Tier-1 ADD #1).

The learning loop (trust / calibration) is inert until enough CLOSED outcomes
exist.  Live ingest accrues those at ~1 per real elapsed horizon, so a cold
start takes ~1–1.5 years before the ledger activates (see
``docs/audit/I2-statistical-power.md``).  This module short-circuits that wait:
it REPLAYS already-ingested historical disclosures (the ``filings`` table)
through the EXISTING signal → opinion → idea → outcome-labeler pipeline to MINT
``ResolvedOutcome`` rows for ideas whose horizon has ALREADY elapsed relative to
a passed-in ``cutoff_as_of`` ("now").  Those outcomes are durable, PIT-clean,
and immediately consumable by the trust ledger and calibrator.

Pure reuse — nothing here re-implements detection, scoring, emission, idea
construction, labeling, or persistence:

    1. ``signals.detection.detect_signals(conn, cutoff_as_of)`` — reads filings
       PIT-bounded (``filing_ts <= cutoff_as_of``) and returns the SAME signals
       the live cycle would have seen.
    2. ``signals.scoring.score_signal`` + ``signals.emit.emit_opinion`` — the
       SAME opinion the advisor would have emitted at ``signal.window_end``.
    3. ``orchestrator.idea.make_idea`` — reconstructs the Idea (deterministic
       ``idea_id`` derived from the opinion fingerprint → idempotent re-runs).
    4. ``evaluation.outcome_labeler.label`` — mints the ResolvedOutcome.
    5. ``orchestrator.idea_store`` / ``signals.opinion_store`` /
       ``evaluation.outcome_store`` — persist (insert-only, idempotent).

PIT-cleanliness guarantee
-------------------------
Two structural guards make look-ahead impossible:

    * Eligibility gate — an idea is only labeled when its horizon end
      (``opinion.as_of + horizon_days``) is ``<= cutoff_as_of``.  An idea whose
      horizon has not yet elapsed by the cutoff is SKIPPED, never minted.
    * Read window — the labeler is called with ``cutoff_as_of`` set to the
      idea's HORIZON END (not wall-clock, not the replay "now"), so every price
      / beta read is PIT-bounded to the historical decision/label window.  No
      read can ever see data past the horizon the outcome is measuring.

No ``datetime.now()`` is called anywhere (``check_no_lookahead.sh`` stays
clean): the cutoff is always injected by the caller.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from arbiter.evaluation.outcome_labeler import label
from arbiter.evaluation.outcome_store import query_outcomes, store_outcome
from arbiter.data.pit import PITGateway
from arbiter.orchestrator.idea import make_idea
from arbiter.orchestrator.idea_store import persist_new_idea
from arbiter.signals.detection import detect_signals
from arbiter.signals.emit import emit_opinion
from arbiter.signals.opinion_store import persist_opinion
from arbiter.signals.scoring import score_signal
from arbiter.types import IdeaState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackfillReport:
    """Summary of a backfill pass.

    Attributes
    ----------
    n_signals:
        Signals detected from filings ≤ ``cutoff_as_of``.
    n_outcomes_minted:
        New ``ResolvedOutcome`` rows written this pass.
    n_skipped_unelapsed:
        Ideas skipped because their horizon had NOT elapsed by the cutoff
        (PIT gate — the principled "not yet resolvable" case).
    n_skipped_existing:
        Ideas skipped because an outcome already existed (idempotency).
    n_skipped_abstain:
        Signals that emitted no opinion (advisor abstained).
    n_errors:
        Ideas skipped because labeling raised (e.g. missing price bar).
    """

    n_signals: int
    n_outcomes_minted: int
    n_skipped_unelapsed: int
    n_skipped_existing: int
    n_skipped_abstain: int
    n_errors: int

    def render(self) -> str:
        return (
            "Backfill complete.\n"
            f"  signals detected   : {self.n_signals}\n"
            f"  outcomes minted    : {self.n_outcomes_minted}\n"
            f"  skipped (unelapsed): {self.n_skipped_unelapsed}\n"
            f"  skipped (existing) : {self.n_skipped_existing}\n"
            f"  skipped (abstain)  : {self.n_skipped_abstain}\n"
            f"  errors             : {self.n_errors}"
        )


def _deterministic_idea_id(advisor_id: str, source_fingerprint: str) -> str:
    """Stable idea id for a (advisor, fingerprint) pair.

    Deriving the id deterministically from the opinion's source fingerprint
    makes re-runs idempotent WITHOUT touching the live ULID-minting path: the
    same historical signal always reconstructs the same idea, so the outcome
    idempotency guard (idea_id + advisor_id already present) fires on re-run.

    A 26-char uppercase base32-ish slug keeps it shaped like a ULID for the
    schema's TEXT primary key.
    """
    digest = hashlib.sha256(
        f"backfill:{advisor_id}:{source_fingerprint}".encode()
    ).hexdigest()
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
    out = []
    for i in range(26):
        out.append(alphabet[int(digest[i * 2 : i * 2 + 2], 16) % 32])
    return "".join(out)


def backfill_outcomes(
    conn: sqlite3.Connection,
    pit: PITGateway,
    *,
    cutoff_as_of: datetime,
    audit_path: str | Path | None = None,
) -> BackfillReport:
    """Replay historical filings and mint outcomes for elapsed-horizon ideas.

    Parameters
    ----------
    conn:
        Open SQLite connection (filings already ingested).
    pit:
        Point-in-time gateway holding REAL historical price/beta bars.  The
        ONLY source of prices — no look-ahead.
    cutoff_as_of:
        Information timestamp representing "now".  Filings after this are not
        seen (PIT gate in ``detect_signals``); ideas whose horizon has not
        elapsed by this instant are not labeled.  MUST come from the caller's
        clock — never ``datetime.now()``.
    audit_path:
        Override the audit file path (tests).

    Returns
    -------
    BackfillReport
    """
    if cutoff_as_of.tzinfo is None:
        raise ValueError("backfill_outcomes: cutoff_as_of must be tz-aware UTC")

    signals = detect_signals(conn, cutoff_as_of)

    n_minted = 0
    n_unelapsed = 0
    n_existing = 0
    n_abstain = 0
    n_errors = 0

    for signal in signals:
        # Emit the opinion the advisor WOULD have produced at signal time.
        # window_end is the information timestamp at which the signal became
        # actionable (== the decision as_of), PIT-bounded by construction.
        as_of = signal.window_end
        try:
            score_bundle = score_signal(signal, as_of, conn)
        except Exception:  # noqa: BLE001 — scoring is best-effort; fall back to cold start
            score_bundle = None

        opinion = emit_opinion(signal, as_of, score_bundle)
        if opinion is None:
            n_abstain += 1
            continue

        # ---- PIT eligibility gate: horizon must have elapsed by the cutoff ----
        horizon_end = opinion.as_of + timedelta(days=opinion.horizon_days)
        if horizon_end > cutoff_as_of:
            n_unelapsed += 1
            continue

        idea_id = _deterministic_idea_id(opinion.advisor_id, opinion.source_fingerprint)

        # ---- Idempotency: skip if this outcome already exists ----
        existing = query_outcomes(
            conn, idea_id=idea_id, advisor_id=opinion.advisor_id
        )
        if existing:
            n_existing += 1
            continue

        # ---- Reconstruct the closed idea ----
        idea = make_idea(
            ticker=opinion.ticker,
            thesis=opinion.rationale,
            horizon_days=opinion.horizon_days,
            as_of=opinion.as_of,
            state=IdeaState.CLOSED,
            idea_id=idea_id,
        )

        # ---- Label the outcome (PIT-CLEAN: read window capped at horizon_end) ----
        # Passing horizon_end (NOT the replay "now") as the labeler's
        # cutoff_as_of means every price/beta read is bounded to the historical
        # label window — no read can see data past the horizon being measured.
        try:
            outcome = label(
                idea,
                pit=pit,
                cutoff_as_of=horizon_end,
                advisor_id=opinion.advisor_id,
                advisor_confidence=opinion.confidence,
                stance_score=opinion.stance_score,
            )
        except (LookupError, ValueError) as exc:
            logger.warning(
                "backfill: skipping idea %s (%s) — labeling failed: %s",
                idea.idea_id,
                idea.ticker,
                exc,
            )
            n_errors += 1
            continue

        # ---- Persist idea + opinion + outcome (insert-only, idempotent) ----
        persist_new_idea(conn, idea, created_at=opinion.as_of)
        persist_opinion(
            conn, opinion, idea_id=idea.idea_id, as_of=opinion.as_of,
            audit_path=audit_path,
        )
        # Outcome is stamped at the horizon end — the instant it became
        # resolvable — never the replay "now" (PIT-clean created_at).
        store_outcome(outcome, conn, as_of=horizon_end, audit_path=audit_path)
        n_minted += 1

    report = BackfillReport(
        n_signals=len(signals),
        n_outcomes_minted=n_minted,
        n_skipped_unelapsed=n_unelapsed,
        n_skipped_existing=n_existing,
        n_skipped_abstain=n_abstain,
        n_errors=n_errors,
    )
    logger.info(
        "backfill: %d signals → %d outcomes minted (%d unelapsed, %d existing, "
        "%d abstain, %d errors)",
        report.n_signals,
        report.n_outcomes_minted,
        report.n_skipped_unelapsed,
        report.n_skipped_existing,
        report.n_skipped_abstain,
        report.n_errors,
    )
    return report
