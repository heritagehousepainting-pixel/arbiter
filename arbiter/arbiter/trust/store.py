"""Trust store — persistence + PIT-safe assembly for the learning loop (#4).

Three concerns:

1. ``load_outcomes_for_learning(conn, as_of)`` — the ONLY sanctioned input
   assembler for ledger ``update``/``should_update`` and ``Calibrator.fit``.
   Uses a STRICT ``created_at < as_of`` cutoff (D0): the exit monitor, the
   reconcile close-out, AND the end-of-cycle sweep all stamp ``outcomes`` rows
   with ``created_at = now`` EARLIER in the same ``run_cycle`` than the learning
   step, so a non-strict ``<= now`` cutoff would train a decision at T on
   outcomes resolved at T (same-cycle look-ahead).  Strict ``<`` closes that.

2. ``persist_weight_bundle(conn, bundle, *, as_of, cap_reasons=...)`` — insert +
   supersede prior live rows per advisor in ``trust_weights``, recording the new
   ``cap_reason`` column (migration 025, D1).

3. ``load_latest_weight_bundle(conn, as_of, *, backtest)`` — warm-start read.
   Live (``backtest=False``) reads the latest ``is_superseded=0`` row per advisor.
   Backtest (``backtest=True``) reads ``WHERE as_of <= ? ORDER BY as_of DESC
   LIMIT 1`` per advisor (D4) — PIT-safe under replay, where ``is_superseded``
   reflects the latest REAL run rather than the replay's point in time.

No ``datetime.now()`` anywhere — all timestamps come from the caller's clock.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from arbiter.contract.seams import AdvisorWeight, ResolvedOutcome, WeightBundle
from arbiter.db.helpers import insert_row, supersede_row
from arbiter.evaluation.outcome_store import query_outcomes
from arbiter.trust.weight_resolver import NEGATIVE_SKILL_REASON  # noqa: F401  (re-export convenience)


# ---------------------------------------------------------------------------
# 1. PIT-safe outcome assembly (D0)
# ---------------------------------------------------------------------------

def _row_to_resolved_outcome(row: dict[str, Any]) -> ResolvedOutcome:
    """Reconstruct a ``ResolvedOutcome`` from an ``outcomes`` row dict."""
    return ResolvedOutcome(
        idea_id=row["idea_id"],
        advisor_id=row["advisor_id"],
        ticker=row["ticker"],
        alpha_bps=float(row["alpha_bps"]),
        binary=int(row["binary"]),
        advisor_confidence=float(row["advisor_confidence"]),
        # Legacy rows (pre-026) lack stance_score; default to 0.0 (neutral).
        stance_score=float(row["stance_score"]) if "stance_score" in row.keys() and row["stance_score"] is not None else 0.0,
        abstained=bool(row["abstained"]),
        horizon_days=int(row["horizon_days"]),
        label_kind=row["label_kind"],
    )


def load_outcomes_for_learning(
    conn: sqlite3.Connection,
    as_of: datetime,
) -> dict[str, list[tuple[ResolvedOutcome, datetime]]]:
    """Assemble per-advisor learning inputs with a STRICT ``created_at < as_of`` cutoff.

    This is the ONLY sanctioned input assembler for trust/calibration fit/update.
    The strict ``<`` (not ``<=``) is the D0 fix: it excludes EVERY outcome stamped
    at the current cycle's ``now`` — including same-cycle rows written by the exit
    monitor, the reconcile close-out, and the end-of-cycle sweep — so this cycle's
    learning never sees an outcome resolved in this cycle.

    Returns ``{advisor_id: [(ResolvedOutcome, resolved_at), ...]}`` ordered by
    ``created_at`` ascending (as ``query_outcomes`` returns).
    """
    # SQL-level cutoff (defense in depth) — strict_lt forces ``< ?``.
    rows = query_outcomes(conn, as_of=as_of, strict_lt=True)

    by_advisor: dict[str, list[tuple[ResolvedOutcome, datetime]]] = {}
    for row in rows:
        resolved_at = datetime.fromisoformat(row["created_at"])
        outcome = _row_to_resolved_outcome(row)
        by_advisor.setdefault(row["advisor_id"], []).append((outcome, resolved_at))

    return by_advisor


# ---------------------------------------------------------------------------
# 2. Persist trust weights (D1 — includes cap_reason)
# ---------------------------------------------------------------------------

def persist_weight_bundle(
    conn: sqlite3.Connection,
    bundle: WeightBundle,
    *,
    as_of: datetime,
    cap_reasons: dict[str, str | None] | None = None,
) -> None:
    """Persist *bundle* to ``trust_weights`` — one row per advisor.

    For each advisor we supersede the prior live (``is_superseded=0``) row, if any,
    and insert a fresh row carrying the learned weight + ``cap_reason``.  We persist
    the LEDGER's bundle (the learned weights), not the floored engine bundle, so the
    table is an honest record of what was learned (the floor is a runtime trading
    policy, not a learned weight).
    """
    cap_reasons = cap_reasons or {}
    created_at = as_of.isoformat()

    for advisor_id, aw in bundle.weights.items():
        row = {
            "advisor_id": advisor_id,
            "weight": aw.weight,
            "ci_low": aw.ci_low,
            "ci_high": aw.ci_high,
            "shadow": 1 if aw.shadow else 0,
            "as_of": created_at,
            "cap_reason": cap_reasons.get(advisor_id),
            "created_at": created_at,
        }

        prior = conn.execute(
            "SELECT id FROM trust_weights "
            "WHERE advisor_id = ? AND is_superseded = 0 "
            "ORDER BY as_of DESC LIMIT 1",
            (advisor_id,),
        ).fetchone()

        if prior is not None:
            supersede_row(conn, "trust_weights", prior["id"], row)
        else:
            insert_row(conn, "trust_weights", row)


# ---------------------------------------------------------------------------
# 3. Warm-start read (D4 — two explicit paths)
# ---------------------------------------------------------------------------

def load_latest_weight_bundle(
    conn: sqlite3.Connection,
    as_of: datetime,
    *,
    backtest: bool,
) -> WeightBundle | None:
    """Read the most recent persisted weights to warm-start the engine.

    ``backtest=False`` (live restart): read the latest live row
    (``is_superseded=0``) per advisor — fine because it is the latest REAL run.

    ``backtest=True`` (replay): read ``WHERE as_of <= ? ORDER BY as_of DESC LIMIT
    1`` per advisor (D4).  ``is_superseded`` is NOT PIT-safe under replay (it
    reflects the latest real run, not the replay's point in time), so the backtest
    path keys strictly on the ``as_of`` window.

    NOTE (P2-b — backtest read path is intentionally not used by the engine).
    The engine's ``_build_learning_inputs`` does NOT call this with
    ``backtest=True``: under ``BacktestClock`` it RECOMPUTES the weights/calibrator
    from the ledger EVERY step (D2), which is itself PIT-safe via the strict
    ``created_at < as_of`` cutoff in :func:`load_outcomes_for_learning`.  Recompute,
    not warm-start, is the backtest contract — a cached/persisted bundle would carry
    recency-decay computed at an OLD ``as_of`` and would not be the weight the live
    system had at this step.  The ``backtest=True`` branch here is therefore NOT
    dead code from the engine's point of view: it is the PIT-correct
    ``as_of``-windowed reader for EXTERNAL / DIAGNOSTIC use (offline inspection,
    leaderboard/audit tooling, or any consumer that wants "what was persisted as of
    T" without recomputing).  The engine's only warm-start read is the live
    (``backtest=False``) path on a daemon restart.

    Returns ``None`` when no rows exist.  The correlation matrix is empty (re-derived
    on the next ``ledger.update``).
    """
    advisor_rows = conn.execute(
        "SELECT DISTINCT advisor_id FROM trust_weights"
    ).fetchall()
    if not advisor_rows:
        return None

    weights: dict[str, AdvisorWeight] = {}
    for ar in advisor_rows:
        advisor_id = ar["advisor_id"]
        if backtest:
            row = conn.execute(
                "SELECT * FROM trust_weights "
                "WHERE advisor_id = ? AND as_of <= ? "
                "ORDER BY as_of DESC LIMIT 1",
                (advisor_id, as_of.isoformat()),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM trust_weights "
                "WHERE advisor_id = ? AND is_superseded = 0 "
                "ORDER BY as_of DESC LIMIT 1",
                (advisor_id,),
            ).fetchone()
        if row is None:
            continue
        weights[advisor_id] = AdvisorWeight(
            advisor_id=advisor_id,
            weight=float(row["weight"]),
            ci_low=float(row["ci_low"]),
            ci_high=float(row["ci_high"]),
            shadow=bool(row["shadow"]),
        )

    if not weights:
        return None
    return WeightBundle(weights=weights, correlation_matrix={})


def load_cap_reasons(
    conn: sqlite3.Connection,
    as_of: datetime,
    *,
    backtest: bool,
) -> dict[str, str | None]:
    """Read the persisted ``cap_reason`` per advisor for the same window as
    :func:`load_latest_weight_bundle` so the resolver can suppress negative-skill
    advisors on a warm-started (cache-less) cycle."""
    advisor_rows = conn.execute(
        "SELECT DISTINCT advisor_id FROM trust_weights"
    ).fetchall()
    reasons: dict[str, str | None] = {}
    for ar in advisor_rows:
        advisor_id = ar["advisor_id"]
        if backtest:
            row = conn.execute(
                "SELECT cap_reason FROM trust_weights "
                "WHERE advisor_id = ? AND as_of <= ? "
                "ORDER BY as_of DESC LIMIT 1",
                (advisor_id, as_of.isoformat()),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT cap_reason FROM trust_weights "
                "WHERE advisor_id = ? AND is_superseded = 0 "
                "ORDER BY as_of DESC LIMIT 1",
                (advisor_id,),
            ).fetchone()
        if row is not None:
            reasons[advisor_id] = row["cap_reason"]
    return reasons
