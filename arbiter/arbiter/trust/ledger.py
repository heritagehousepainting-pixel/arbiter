"""Trust ledger — Lane 11 core.

Composite trust = geometric mean(skill, calibration, coverage),
recency-weighted with a 26-week (182-day) exponential half-life.

Caps and floors (INTERFACES.md §5):
    CEILING         0.50  (sample-gated)
    MIROFISH_CAP    0.35  (forever, advisor_id prefix "A2.")
    NEGATIVE_SKILL  → 0.0 + diagnostic hold (shadow=True)
    THIN_SAMPLE_FLOOR 0.02 (when sample < THIN_SAMPLE_THRESHOLD)

Shadow onboarding:
    New advisors start with weight=0, shadow=True.
    Once ≥ SHADOW_THRESHOLD (30) resolved non-abstain outcomes exist,
    weight enters a probationary ramp from 0 → composite_trust
    over the next RAMP_OUTCOMES outcomes.

Weekly update gate:
    A new WeightBundle is only emitted when ≥ MIN_NEW_OUTCOMES (5) new
    outcomes have arrived since the last update.

Dormancy gate:
    The ledger returns None (dormant) until the system has ≥ 60 total
    resolved outcomes (PHASE3_ACTIVATION_THRESHOLD).

All timestamps must be passed in; no datetime.now() anywhere.

Wave-C wiring points
--------------------
- eligible_idea_ids (per advisor, per coverage window): comes from Lane 13
  (Orchestrator) or Lane 14 (Outcome Labeler).  Passed as parameter to
  ``compute_composite_trust`` and ``TrustLedger.update``.
- Calibration score: currently passed in directly (stub=1.0 until Lane
  calibration module is wired).  Wave-C: replace stub with real calibration.
- Regime tracker: passed in from outside; Wave-C wires to regime-detection.
"""
from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

import numpy as np

from arbiter.contract.seams import (
    AdvisorWeight,
    ResolvedOutcome,
    WeightBundle,
)
from arbiter.trust.brier import brier_skill_score, _decay_weight, HALF_LIFE_DAYS
from arbiter.trust.coverage import coverage_score
from arbiter.trust.correlation_matrix import CorrelationMatrix
from arbiter.trust.regime import RegimeTracker, apply_regime_weights

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CEILING: float = 0.50               # Hard weight ceiling for all advisors
MIROFISH_CAP: float = 0.35          # MiroFish (A2.*) forever hard cap
THIN_SAMPLE_FLOOR: float = 0.02     # Floor applied when sample is thin
THIN_SAMPLE_THRESHOLD: int = 35     # At/below this effective-n: apply thin-sample floor
#
# E1 fix — the thin-sample floor used to be DEAD: it was keyed on a raw count <15,
# but an advisor only leaves shadow (where the floor is even consulted) at
# SHADOW_THRESHOLD=30 non-abstain outcomes, so n<15 was unreachable for any live
# advisor.  The floor is now keyed on the *effective* (decay-weighted) sample size
# and the threshold (35) sits ABOVE SHADOW_THRESHOLD (30), so a freshly-graduated
# advisor whose effective-n has been eroded by recency decay (old outcomes count
# for little) genuinely lands in [30, 35] effective and receives the floor.  This
# keeps a just-graduated-but-thin advisor minimally in the pool instead of dropping
# it to ~0 on a noisy composite.

SHADOW_THRESHOLD: int = 30          # Non-abstain outcomes before shadow CAN lift (necessary, not sufficient)
RAMP_OUTCOMES: int = 10             # Outcomes over which weight ramps 0 → composite

# --- Significance-gated graduation (I2 / E1) -------------------------------
# Graduating an advisor to live weight on a bare COUNT (n>=30) let ~half of NULL
# advisors (true skill ~0) graduate, because a count says nothing about whether
# the measured skill is distinguishable from chance.  Graduation now ALSO requires
# a significance / effective-n criterion: the bootstrap CI lower bound on the
# advisor's Brier *skill* must clear zero, AND the effective (decay-weighted)
# sample size must exceed MIN_EFFECTIVE_N.  Both are necessary; the bare count is
# kept only as the floor of the ramp.
MIN_EFFECTIVE_N: float = 20.0       # Effective (Kish) n required to graduate
BOOTSTRAP_DRAWS: int = 1000         # Resamples for the skill CI
BOOTSTRAP_ALPHA: float = 0.10       # 90% CI → ci_low is the 5th percentile
BOOTSTRAP_SEED: int = 1_234_567     # Deterministic CI (no datetime.now / no clock entropy)

PHASE3_ACTIVATION_THRESHOLD: int = 60  # System-wide outcomes before ledger activates
MIN_NEW_OUTCOMES: int = 5           # Minimum new outcomes to trigger a WeightBundle update

MIROFISH_PREFIX: str = "A2."        # All MiroFish advisor IDs start with this

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_mirofish(advisor_id: str) -> bool:
    """Return True for MiroFish advisors (A2.* prefix)."""
    return advisor_id.startswith(MIROFISH_PREFIX)


def _apply_caps(
    advisor_id: str,
    raw_weight: float,
    n_outcomes: int,
    is_negative_skill: bool,
    *,
    is_shadow: bool = False,
    effective_n: float | None = None,
) -> tuple[float, bool]:
    """Apply all caps and floors.  Returns (final_weight, shadow).

    Rules applied in order:
    1. negative skill → 0.0 + shadow
    2. thin-sample floor 0.02 (skipped while in shadow mode — don't override shadow zero)
    3. general ceiling 0.50
    4. MiroFish hard cap 0.35 (forever)

    The thin-sample floor keys on EFFECTIVE n (decay-weighted, Kish) when supplied,
    falling back to the raw count otherwise.  Keying on effective-n is what makes
    the floor reachable (E1): a graduated advisor (raw n ≥ 30) whose effective-n
    has decayed into [THIN_SAMPLE_THRESHOLD or below] gets floored rather than
    dropping to ~0 on a noisy thin composite.
    """
    shadow = False

    if is_negative_skill:
        return 0.0, True  # shadow=True = diagnostic hold

    weight = raw_weight

    # Thin-sample floor is NOT applied while in shadow onboarding — shadow zeroes
    # must stay zero until the ramp completes.  Reachable via effective-n.
    sample_for_floor = effective_n if effective_n is not None else float(n_outcomes)
    if sample_for_floor <= THIN_SAMPLE_THRESHOLD and not is_shadow:
        weight = max(weight, THIN_SAMPLE_FLOOR)

    # General ceiling
    weight = min(weight, CEILING)

    # MiroFish cap
    if _is_mirofish(advisor_id):
        weight = min(weight, MIROFISH_CAP)

    return weight, shadow


def compute_composite_trust(
    outcomes: Sequence[ResolvedOutcome],
    outcome_dates: Sequence[datetime],
    eligible_idea_ids: Sequence[str],
    as_of: datetime,
    *,
    calibration_score: float = 1.0,
    regime_tracker: RegimeTracker | None = None,
) -> float | None:
    """Compute composite trust for one advisor.

    composite = (skill × calibration × coverage) ^ (1/3)
        (geometric mean of the three terms)

    Parameters
    ----------
    outcomes:
        All ResolvedOutcome rows for this advisor.
    outcome_dates:
        Parallel datetime sequence for when each outcome resolved (tz-aware UTC).
    eligible_idea_ids:
        Roster of idea_ids the advisor was eligible to opine on (Wave-C wiring;
        comes from Lane 13/14).
    as_of:
        Reference timestamp for decay weighting (tz-aware UTC).
    calibration_score:
        Float in [0, 1].  Stub = 1.0 until calibration lane is wired (Wave-C).
    regime_tracker:
        Optional RegimeTracker to apply post-regime 2× weights.

    Returns
    -------
    float or None
        Composite trust score in [0, 1], or None if BSS cannot be computed
        (no non-abstain outcomes).  Negative BSS is clamped to 0.0.
    """
    # --- Brier skill ---
    bss = brier_skill_score(outcomes, outcome_dates, as_of)
    if bss is None:
        return None  # No non-abstain outcomes yet

    skill = max(0.0, bss)  # Clamp negative skill to 0 for composite formula

    # --- Calibration (Wave-C stub) ---
    cal = float(np.clip(calibration_score, 0.0, 1.0))

    # --- Coverage ---
    cov = coverage_score(outcomes, eligible_idea_ids)

    # --- Geometric mean ---
    if skill == 0.0 or cal == 0.0 or cov == 0.0:
        # Geometric mean of anything with a zero is zero
        return 0.0

    composite = (skill * cal * cov) ** (1.0 / 3.0)
    return float(np.clip(composite, 0.0, 1.0))


# ---------------------------------------------------------------------------
# Statistical power: effective-n, bootstrap skill CI, MDE
# ---------------------------------------------------------------------------

def _scorable_brier_terms(
    outcomes: Sequence[ResolvedOutcome],
    outcome_dates: Sequence[datetime],
    as_of: datetime,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (per-outcome Brier scores, decay weights) for SCORABLE rows.

    Mirrors the row-selection in brier.recency_weighted_brier (abstained and
    no-call binary==0 rows are dropped) so the bootstrap resamples exactly the
    rows the skill score is computed over.  Does NOT change the brier math —
    it re-derives the same per-row BS from the public stance/confidence/binary
    fields so we can resample them.
    """
    from arbiter.trust.brier import (
        _stance_to_prob,
        _clamp_stance,
        _clamp_confidence,
        _outcome_to_prob,
        _decay_weight,
    )

    bs_list: list[float] = []
    w_list: list[float] = []
    for outcome, date in zip(outcomes, outcome_dates):
        if outcome.abstained or outcome.binary == 0:
            continue
        p_hat = _stance_to_prob(
            _clamp_stance(outcome.stance_score)
            * _clamp_confidence(outcome.advisor_confidence)
        )
        p_outcome = _outcome_to_prob(outcome.binary)
        bs_list.append((p_hat - p_outcome) ** 2.0)
        w_list.append(_decay_weight(as_of, date))
    return np.asarray(bs_list, dtype=float), np.asarray(w_list, dtype=float)


def effective_sample_size(
    outcomes: Sequence[ResolvedOutcome],
    outcome_dates: Sequence[datetime],
    as_of: datetime,
) -> float:
    """Kish effective sample size of the decay-weighted scorable outcomes.

    n_eff = (Σ w)² / Σ w²  ∈ [0, n_raw].  Recency decay erodes effective-n:
    a pile of stale outcomes contributes far less than its raw count.  Used by
    the graduation gate and as a power input.  Returns 0.0 when nothing scorable.
    """
    _, w = _scorable_brier_terms(outcomes, outcome_dates, as_of)
    if w.size == 0:
        return 0.0
    s1 = float(w.sum())
    s2 = float((w * w).sum())
    if s2 == 0.0:
        return 0.0
    return (s1 * s1) / s2


def bootstrap_skill_ci(
    outcomes: Sequence[ResolvedOutcome],
    outcome_dates: Sequence[datetime],
    as_of: datetime,
    *,
    n_boot: int = BOOTSTRAP_DRAWS,
    alpha: float = BOOTSTRAP_ALPHA,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float] | None:
    """Real bootstrap CI on the advisor's Brier SKILL score (BSS).

    Replaces the old fixed ±20% (`composite*0.8`/`*1.2`) placeholder.  Resamples
    the scorable per-outcome Brier terms (with replacement, carrying their decay
    weights) ``n_boot`` times, recomputes the recency-weighted BSS each draw, and
    returns the (alpha/2, 1-alpha/2) percentile interval on the *skill* scale.

    The interval is sample-size-aware: thin samples produce a WIDER band (the
    resample variance is larger), so ``ci_low`` is small/negative for genuinely
    NULL advisors and only clears zero once skill is real AND well-sampled.

    Returns None when there are no scorable outcomes (CI undefined).  Negative
    ci_low is preserved (NOT clamped) — the graduation gate needs the sign.
    """
    bs, w = _scorable_brier_terms(outcomes, outcome_dates, as_of)
    n = bs.size
    if n == 0:
        return None

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_boot, n))
    bs_draws = bs[idx]              # (n_boot, n)
    w_draws = w[idx]
    wsum = w_draws.sum(axis=1)
    # Guard against an all-zero-weight resample (decayed to ~0): fall back to
    # the unweighted mean for that draw.
    safe = wsum > 0.0
    weighted_bs = np.empty(n_boot, dtype=float)
    weighted_bs[safe] = (w_draws[safe] * bs_draws[safe]).sum(axis=1) / wsum[safe]
    weighted_bs[~safe] = bs_draws[~safe].mean(axis=1)

    from arbiter.trust.brier import BS_REF
    bss_draws = 1.0 - (weighted_bs / BS_REF)

    lo = float(np.percentile(bss_draws, 100.0 * (alpha / 2.0)))
    hi = float(np.percentile(bss_draws, 100.0 * (1.0 - alpha / 2.0)))
    return lo, hi


def minimum_detectable_effect(n_eff: float) -> float:
    """Minimum detectable effect (on the BSS scale) at the current effective-n.

    Power proxy: the half-width of a ~90% interval for a skill estimate scales
    like z * sigma / sqrt(n_eff).  Using the climatological Brier dispersion
    (sigma on the BS scale ≈ 0.5, lifted to the BSS scale by /BS_REF) gives a
    monotone, sample-size-aware MDE that SHRINKS as n_eff grows — so the report
    can show "you could only detect a skill of ≥ X at this n".
    """
    if n_eff <= 0.0:
        return float("inf")
    from arbiter.trust.brier import BS_REF
    Z90 = 1.6448536269514722  # one-sided 95% / two-sided 90%
    sigma_bs = 0.5            # max dispersion of a [0,1] Brier term
    return float(Z90 * (sigma_bs / BS_REF) / math.sqrt(n_eff))


def is_significant_skill(
    ci_low: float | None,
    n_eff: float,
    *,
    min_effective_n: float = MIN_EFFECTIVE_N,
) -> bool:
    """Graduation significance gate: skill is real AND well-sampled.

    True only when BOTH hold:
      (a) the bootstrap CI lower bound on skill clears zero (ci_low > 0), i.e.
          the advisor is distinguishable from chance, and
      (b) the effective (decay-weighted) sample size exceeds ``min_effective_n``.

    A NULL advisor (true skill ~0) fails (a): its CI straddles 0 no matter how
    many outcomes it accumulates, so it never graduates on count alone.
    """
    if ci_low is None:
        return False
    return ci_low > 0.0 and n_eff >= min_effective_n


def is_significant_negative_skill(
    ci_high: float | None,
    n_eff: float,
    *,
    min_effective_n: float = MIN_EFFECTIVE_N,
) -> bool:
    """Demotion significance gate: skill is real AND significantly NEGATIVE.

    Symmetric mirror of ``is_significant_skill``.  True only when BOTH hold:
      (a) the bootstrap CI UPPER bound on skill is below zero (ci_high < 0),
          i.e. the advisor is distinguishably WORSE than chance, and
      (b) effective (decay-weighted) sample size exceeds ``min_effective_n``.

    A thin/NULL advisor (CI straddles 0, or n_eff too low) fails and is floored
    rather than muted — it keeps trading and keeps accruing outcomes to learn
    from.  Prevents benching an advisor on a statistically-insignificant blip.
    """
    if ci_high is None:
        return False
    return ci_high < 0.0 and n_eff >= min_effective_n


# ---------------------------------------------------------------------------
# Shadow onboarding ramp
# ---------------------------------------------------------------------------

def _shadow_ramp_weight(
    n_non_abstain: int,
    composite: float,
    *,
    graduated: bool = True,
) -> tuple[float, bool]:
    """Apply shadow onboarding ramp, gated on the significance test.

    Below SHADOW_THRESHOLD: weight=0, shadow=True (count floor, necessary).
    Between SHADOW_THRESHOLD and SHADOW_THRESHOLD+RAMP_OUTCOMES:
        weight = composite * (n_non_abstain - SHADOW_THRESHOLD) / RAMP_OUTCOMES
        shadow=True (still in probationary ramp)
    Above SHADOW_THRESHOLD+RAMP_OUTCOMES:
        weight = composite, shadow = not graduated.

    ``graduated`` is the significance/effective-n gate (see is_significant_skill).
    Even past the count ramp, an advisor that has NOT cleared the significance
    test stays shadow=True with weight=0 — a bare count of 30 no longer lifts it.
    The default True preserves the legacy count-only contract for direct callers
    (unit tests of the ramp mechanics); TrustLedger.update passes the real gate.
    """
    if n_non_abstain < SHADOW_THRESHOLD:
        return 0.0, True

    ramp_progress = n_non_abstain - SHADOW_THRESHOLD
    if ramp_progress < RAMP_OUTCOMES:
        if not graduated:
            # Count says "could ramp" but significance not yet established → hold.
            return 0.0, True
        # Probationary ramp: shadow=True (recorded but discounted)
        fraction = ramp_progress / RAMP_OUTCOMES
        return composite * fraction, True

    if not graduated:
        # Enough COUNT to fully graduate, but the skill is not significant
        # (e.g. a NULL advisor whose CI straddles 0).  Do NOT lift to live weight.
        return 0.0, True

    return composite, False


# ---------------------------------------------------------------------------
# TrustLedger
# ---------------------------------------------------------------------------

@dataclass
class TrustLedger:
    """Stateful trust ledger for all advisors.

    Maintains a record of the last update timestamp so it can enforce
    the ≥5-new-outcomes gate.

    Parameters
    ----------
    last_update_at:
        Timestamp of the last WeightBundle emission (or None if never updated).
    outcomes_at_last_update:
        Per-advisor count of outcomes processed during the last update.
    """

    last_update_at: datetime | None = None
    outcomes_at_last_update: dict[str, int] = field(default_factory=dict)
    # Per-advisor cap reason from the most recent ``update`` (sub-project #4, D1/D6).
    # "negative_skill" when the advisor was suppressed for sub-chance skill; None
    # otherwise (cold/onboarding/graduated).  Read by trust_store.persist_weight_bundle
    # so the persisted row records WHY a weight is 0, letting the resolver tell a
    # genuinely-suppressed advisor apart from a still-cold one.  No datetime here.
    last_cap_reasons: dict[str, str | None] = field(default_factory=dict)

    def should_update(
        self,
        outcomes_by_advisor: dict[str, list[tuple[ResolvedOutcome, datetime]]],
        as_of: datetime,
        regime_tracker: RegimeTracker | None = None,
    ) -> bool:
        """Return True if a new WeightBundle should be emitted.

        Conditions (ALL must be true):
        1. ≥ PHASE3_ACTIVATION_THRESHOLD total resolved outcomes exist.
        2. ≥ MIN_NEW_OUTCOMES new outcomes since last update.
        3. NOT in a regime-freeze period.
        """
        # Phase-3 activation gate
        total_outcomes = sum(len(v) for v in outcomes_by_advisor.values())
        if total_outcomes < PHASE3_ACTIVATION_THRESHOLD:
            return False

        # Count new outcomes since last update
        new_count = 0
        for advisor_id, records in outcomes_by_advisor.items():
            prev = self.outcomes_at_last_update.get(advisor_id, 0)
            new_count += max(0, len(records) - prev)

        if new_count < MIN_NEW_OUTCOMES:
            return False

        # Regime freeze
        if regime_tracker is not None and regime_tracker.is_frozen(as_of):
            return False

        return True

    def update(
        self,
        outcomes_by_advisor: dict[str, list[tuple[ResolvedOutcome, datetime]]],
        eligible_by_advisor: dict[str, list[str]],
        as_of: datetime,
        *,
        calibration_by_advisor: dict[str, float] | None = None,
        regime_tracker: RegimeTracker | None = None,
        fingerprints_by_advisor: dict[str, set[str]] | None = None,
        force: bool = False,
    ) -> WeightBundle | None:
        """Compute and return a new WeightBundle.

        Parameters
        ----------
        outcomes_by_advisor:
            {advisor_id: [(ResolvedOutcome, resolved_at datetime), ...]}
        eligible_by_advisor:
            {advisor_id: [idea_id, ...]}  (Wave-C wiring: comes from L13/L14)
        as_of:
            Reference timestamp (tz-aware UTC).  Never datetime.now().
        calibration_by_advisor:
            Optional {advisor_id: calibration_score}.  Defaults to 1.0 per advisor.
        regime_tracker:
            Optional RegimeTracker for freeze/weight-multiplier logic.
        fingerprints_by_advisor:
            {advisor_id: {source_fingerprint, ...}} for correlation detection.
        force:
            Skip the should_update gate (useful in tests).

        Returns
        -------
        WeightBundle or None
            None if dormant (< PHASE3_ACTIVATION_THRESHOLD outcomes) or if the
            update gate is not satisfied and force=False.
        """
        if not force and not self.should_update(outcomes_by_advisor, as_of, regime_tracker):
            return None

        calibration_by_advisor = calibration_by_advisor or {}

        # Guard: if outcomes exist but the eligible roster is entirely empty for all
        # advisors, coverage collapses to 0.0 → composite 0.0 → all weights 0 → no
        # trades.  This is a silent deadlock that indicates the roster wiring (Lane
        # L13/L14 → L11) has not been completed for Phase 3.  Log a loud warning.
        total_outcomes_count = sum(len(r) for r in outcomes_by_advisor.values())
        total_eligible_count = sum(len(v) for v in eligible_by_advisor.values())
        if total_outcomes_count > 0 and total_eligible_count == 0:
            _log.warning(
                "trust.coverage.empty_roster — eligible-idea roster not wired; "
                "coverage will collapse to 0.0 for all advisors, zeroing all weights "
                "and blocking all trades. Wire the eligible_idea_ids from Lane L13/L14 "
                "into TrustLedger.update(eligible_by_advisor=...) before Phase 3 "
                "activation. This guard fires when outcomes exist but no eligible IDs "
                "are provided."
            )

        weights: dict[str, AdvisorWeight] = {}
        cap_reasons: dict[str, str | None] = {}

        for advisor_id, records in outcomes_by_advisor.items():
            outcomes = [r for r, _ in records]
            dates = [d for _, d in records]
            eligible = eligible_by_advisor.get(advisor_id, [])
            cal = calibration_by_advisor.get(advisor_id, 1.0)

            # Statistical power: bootstrap CI on SKILL + effective-n, computed
            # once and shared by BOTH the demotion gate (below) and the
            # graduation gate (further down).
            skill_ci = bootstrap_skill_ci(outcomes, dates, as_of)
            n_eff = effective_sample_size(outcomes, dates, as_of)
            skill_ci_low = skill_ci[0] if skill_ci is not None else None
            skill_ci_high = skill_ci[1] if skill_ci is not None else None

            # D1/D6: record WHY a weight ends up suppressed.  Demotion is now
            # significance-gated (symmetric with graduation): mute ONLY when the
            # skill CI upper bound is below zero AND effective-n is sufficient,
            # never on a thin/insignificant negative point estimate.
            is_negative_skill = is_significant_negative_skill(skill_ci_high, n_eff)
            cap_reasons[advisor_id] = "negative_skill" if is_negative_skill else None

            composite = compute_composite_trust(
                outcomes,
                dates,
                eligible,
                as_of,
                calibration_score=cal,
                regime_tracker=regime_tracker,
            )

            if composite is None:
                # No scorable outcomes yet — full shadow
                aw = AdvisorWeight(
                    advisor_id=advisor_id,
                    weight=0.0,
                    ci_low=0.0,
                    ci_high=0.0,
                    shadow=True,
                )
                weights[advisor_id] = aw
                continue

            n_non_abstain = sum(1 for o in outcomes if not o.abstained)

            # Significance/effective-n gate: an advisor graduates out of shadow
            # ONLY when the skill CI clears zero AND effective-n is sufficient —
            # NOT on a bare count of 30.  NULL advisors (CI straddles 0) never
            # graduate no matter how many outcomes accumulate.
            graduated = is_significant_skill(skill_ci_low, n_eff)

            # Shadow ramp (gated on the significance test)
            ramped_weight, shadow_from_ramp = _shadow_ramp_weight(
                n_non_abstain, composite, graduated=graduated
            )

            # Caps/floors — pass is_shadow so thin-sample floor doesn't override
            # shadow zero; pass effective-n so the floor is reachable (E1).
            final_weight, shadow_from_neg = _apply_caps(
                advisor_id,
                ramped_weight,
                len(outcomes),
                is_negative_skill,
                is_shadow=shadow_from_ramp,
                effective_n=n_eff,
            )

            shadow = shadow_from_ramp or shadow_from_neg

            # CI on the AdvisorWeight reports the real bootstrap SKILL interval
            # (sample-size-aware; ci_low feeds the graduation gate above).  The
            # legacy ±20% composite band is gone.  ci_low/ci_high are clipped to
            # [0,1] for the AdvisorWeight contract (weights are non-negative); the
            # un-clipped skill CI used for the sign test lives in skill_ci_low.
            if skill_ci is not None:
                ci_low = float(np.clip(skill_ci[0], 0.0, 1.0))
                ci_high = float(np.clip(skill_ci[1], 0.0, 1.0))
            else:
                ci_low = 0.0
                ci_high = 0.0

            aw = AdvisorWeight(
                advisor_id=advisor_id,
                weight=final_weight,
                ci_low=ci_low,
                ci_high=ci_high,
                shadow=shadow,
            )
            weights[advisor_id] = aw

        # Build correlation matrix
        corr_matrix = CorrelationMatrix.build(
            outcomes_by_advisor,
            fingerprints_by_advisor=fingerprints_by_advisor,
            as_of=as_of,
        )

        # Record update state
        self.last_update_at = as_of
        self.last_cap_reasons = cap_reasons
        for advisor_id, records in outcomes_by_advisor.items():
            self.outcomes_at_last_update[advisor_id] = len(records)

        return WeightBundle(
            weights=weights,
            correlation_matrix=corr_matrix.to_bundle_dict(),
        )
