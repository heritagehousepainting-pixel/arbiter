"""Recency-weighted inverse Brier skill score — Lane 11 / trust sub-module.

The Brier score measures probabilistic forecast accuracy:
    BS = (forecast - outcome)²   ∈ [0, 1]

We convert the advisor's directional stance into a probability forecast:
    p_hat = (stance_score * confidence + 1) / 2   maps [-1, 1] → [0, 1]
    outcome_p = 1 if binary == +1, 0 if binary == -1, 0.5 if binary == 0

The forecast (``p_hat``) comes from the advisor's ACTUAL persisted directional
stance (``outcome.stance_score``, #5a), NOT from the realized ``binary``.  Using
``binary`` to reconstruct the forecast made BS structurally ≤ 0.25 ⇒ BSS ≥ 0
always (negative-skill suppression was unreachable).  Scoring the real stance
against the realized ``binary`` lets a confidently-wrong advisor earn BSS < 0.

Brier *skill* relative to a climatological baseline (0.5 = max uncertainty):
    BSS = 1 - BS / BS_ref    where BS_ref = (0.5)^2 = 0.25
    BSS ∈ (-inf, 1]   negative = worse than chance, 1 = perfect

Recency weighting: exponential decay with a 26-week (182-day) half-life.
    w(t) = 2^(-(as_of_ref - outcome_date).days / 182)

Non-abstain outcomes only: abstained rows are excluded before computation.

All inputs are passed in — no datetime.now(); callers supply as_of.
"""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

import numpy as np

from arbiter.contract.seams import ResolvedOutcome

HALF_LIFE_DAYS: float = 182.0  # 26 weeks
BS_REF: float = 0.25           # climatological Brier score at p=0.5


def _outcome_to_prob(binary: int) -> float:
    """Map binary outcome label to a probability for Brier scoring.

    +1 → 1.0, -1 → 0.0, 0 (no-call) → 0.5
    """
    if binary == 1:
        return 1.0
    if binary == -1:
        return 0.0
    return 0.5  # no-call / uncertain


def _clamp_stance(stance_score: float) -> float:
    """Clamp stance_score into the contract range [-1, 1] (E1).

    An out-of-range stance (a data/ingest bug) would push p_hat outside [0, 1]
    and blow the Brier score far past 1.0 — driving BSS to large negatives (e.g.
    −8) and PERMANENTLY muting an advisor.  Clamp defensively before scoring.
    """
    return max(-1.0, min(1.0, stance_score))


def _clamp_confidence(confidence: float) -> float:
    """Clamp advisor_confidence into the contract range [0, 1] (E1)."""
    return max(0.0, min(1.0, confidence))


def _stance_to_prob(stance_score: float) -> float:
    """Map stance score [-1, 1] to probability [0, 1] for Brier scoring."""
    return (stance_score + 1.0) / 2.0


def _decay_weight(as_of: datetime, outcome_created_at: datetime) -> float:
    """Exponential decay weight relative to as_of with 26-week half-life.

    w = 2^(-(days_ago / HALF_LIFE_DAYS))

    Returns 1.0 if outcome_created_at == as_of (most recent possible),
    approaches 0 as the gap grows.
    """
    delta_days = max(0.0, (as_of - outcome_created_at).total_seconds() / 86400.0)
    return float(np.exp2(-delta_days / HALF_LIFE_DAYS))


def recency_weighted_brier(
    outcomes: Sequence[ResolvedOutcome],
    outcome_dates: Sequence[datetime],
    as_of: datetime,
) -> float | None:
    """Compute recency-weighted average Brier score for non-abstain outcomes.

    Parameters
    ----------
    outcomes:
        Sequence of ResolvedOutcome (all for one advisor).
    outcome_dates:
        Parallel sequence of datetime objects indicating when each outcome
        was resolved (tz-aware UTC).  Must match length of outcomes.
    as_of:
        Reference timestamp for decay weighting (tz-aware UTC).

    Returns
    -------
    float or None
        Weighted Brier score, or None if no non-abstain outcomes exist.
    """
    if len(outcomes) != len(outcome_dates):
        raise ValueError(
            f"outcomes and outcome_dates must have equal length, "
            f"got {len(outcomes)} vs {len(outcome_dates)}"
        )

    weighted_bs: float = 0.0
    total_weight: float = 0.0

    for outcome, date in zip(outcomes, outcome_dates):
        if outcome.abstained:
            continue

        if outcome.binary == 0:
            # No-call (±25 bps market-ambiguous band): neither rewarded nor penalised.
            # Treating it as p_hat=0.5 and p_outcome=0.5 would give BS=0.0 — a free
            # perfect score that inflates skill for advisors who emit many near-zero
            # stances.  Skip these rows instead (same treatment as abstentions).
            continue

        # Forecast = the advisor's ACTUAL directional stance scaled by its
        # self-reported confidence (#5a).  A low-confidence call pulls p_hat
        # toward 0.5 (less penalized when wrong, less rewarded when right); a
        # confidently-WRONG call (stance opposite the realized binary) earns a
        # large BS → BSS < 0 → genuinely suppressible.  We score against the
        # realized binary (ground truth), NOT a binary-reconstructed forecast.
        # Clamp both inputs to their contract ranges before scoring (E1) so a
        # corrupt out-of-range stance/confidence can't blow BS past 1.0 and mute
        # the advisor.  (ResolvedOutcome.__post_init__ also clamps, but Brier
        # stays defensive in case a raw outcome is constructed elsewhere.)
        p_hat = _stance_to_prob(
            _clamp_stance(outcome.stance_score)
            * _clamp_confidence(outcome.advisor_confidence)
        )
        p_outcome = _outcome_to_prob(outcome.binary)
        bs = (p_hat - p_outcome) ** 2.0
        w = _decay_weight(as_of, date)

        weighted_bs += w * bs
        total_weight += w

    if total_weight == 0.0:
        return None

    return weighted_bs / total_weight


def brier_skill_score(
    outcomes: Sequence[ResolvedOutcome],
    outcome_dates: Sequence[datetime],
    as_of: datetime,
) -> float | None:
    """Recency-weighted Brier Skill Score (BSS) relative to climatological baseline.

    BSS = 1 - BS_weighted / BS_ref

    A BSS > 0 means better than chance; BSS < 0 means worse than chance.
    BSS = 1.0 is perfect.

    Returns None if there are no non-abstain outcomes to score.
    """
    bs = recency_weighted_brier(outcomes, outcome_dates, as_of)
    if bs is None:
        return None
    return 1.0 - (bs / BS_REF)
