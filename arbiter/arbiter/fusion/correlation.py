"""Correlation-adjusted effective-N and lone-bull tax.

Effective N
-----------
    effective_N = 1 / (Σᵢ Σⱼ wᵢ wⱼ ρᵢⱼ)

where wᵢ are the *normalised* (simplex) weights of advisors present in
the pool, and ρᵢⱼ is the pairwise correlation from the WeightBundle's
``correlation_matrix``.

For i == j the diagonal terms ρᵢᵢ = 1.0 always.

Default ρ when a pair is absent from the correlation_matrix:
- Phase 1 (EqualWeightBundle, empty matrix): we use ρ = 0.0 for
  off-diagonal terms so that effective_N ≈ N (advisors treated as
  independent).  This keeps Phase-1 safe: no spurious deflation.
- Phase 5+ (real matrix from Lane 11): actual correlations flow in and
  reduce effective_N when advisors are correlated.

Lone-Bull Tax
-------------
Applied when:
1. ALL pooled advisors are in agreement (all stance_scores same sign).
2. They are correlated (effective_N / N < LONE_BULL_CORR_THRESHOLD).
3. At least one OTHER opinion (a "dissenter") exists in the original
   unfiltered opinion list with the OPPOSITE sign.

The tax is a flat penalty subtracted from conviction *after*
``signal_strength * diversity_factor``.

Diversity factor
----------------
    diversity_factor = effective_N / N

where N is the count of opinions in the pool.
"""
from __future__ import annotations

import math

from arbiter.contract.opinion import Opinion
from arbiter.contract.seams import WeightBundle

# ρ used for off-diagonal pairs absent from the correlation_matrix.
#
# Phase-1 / empty-matrix default: 0.0 (no deflation).
#   When fusion receives an EqualWeightBundle (Phase 1), its correlation_matrix
#   is empty.  Missing pairs default to ρ=0.0 so effective_N ≈ N — advisors are
#   treated as independent and no spurious conviction deflation occurs.  This is
#   intentional for the MVP; it does NOT mean the system assumes zero correlation.
#
# Phase-5 real matrix (Lane 11 output): actual ρ values flow in and reduce
#   effective_N when advisors share correlated information sources.
#
# IMPORTANT DISTINCTION — the trust ledger's "0.5 sparse prior":
#   INTERFACES.md §5 states "default 0.5 prior when sparse" for the *trust
#   ledger's* correlation_matrix output (WeightBundle.correlation_matrix).
#   That 0.5 is the Lane-11 prior used when computing the ledger's cross-advisor
#   correlation estimates before Phase-5 data is available.  It is NOT this
#   constant.  Fusion's _DEFAULT_OFF_DIAG_RHO=0.0 applies only when a pair is
#   absent from the matrix that fusion receives — which in Phase 1 means all
#   off-diagonal pairs (empty matrix → all pairs default to 0.0 here).
_DEFAULT_OFF_DIAG_RHO: float = 0.0

# Lone-bull threshold: if diversity_factor < this and unanimity + dissenter → tax applies.
_LONE_BULL_CORR_THRESHOLD: float = 0.5

# Magnitude of the lone-bull tax (subtracted from absolute conviction).
_LONE_BULL_TAX_MAGNITUDE: float = 0.10


def effective_n(
    advisor_ids: list[str],
    norm_weights: dict[str, float],
    weights: WeightBundle,
) -> float:
    """Compute effective N via the double-sum formula.

    Parameters
    ----------
    advisor_ids:
        IDs of advisors in the pool (after dedup), in pool order.
    norm_weights:
        Normalised (simplex) weight for each advisor_id.
    weights:
        WeightBundle containing the correlation matrix.

    Returns
    -------
    float
        effective_N = 1 / (Σᵢ Σⱼ wᵢ wⱼ ρᵢⱼ).
        Returns 1.0 if the double sum is zero or negative (safety guard).
    """
    double_sum = 0.0
    for i, ai in enumerate(advisor_ids):
        wi = norm_weights.get(ai, 0.0)
        for j, aj in enumerate(advisor_ids):
            wj = norm_weights.get(aj, 0.0)
            if i == j:
                rho = 1.0
            else:
                # Look up both orderings; fall back to default.
                rho = weights.correlation_matrix.get(
                    (ai, aj),
                    weights.correlation_matrix.get((aj, ai), _DEFAULT_OFF_DIAG_RHO),
                )
            double_sum += wi * wj * rho

    if double_sum <= 0.0:
        return float(len(advisor_ids)) if advisor_ids else 1.0

    return 1.0 / double_sum


def lone_bull_tax(
    pooled_opinions: list[Opinion],
    all_bucket_opinions: list[Opinion],
    diversity_factor: float,
    weights: "WeightBundle | None" = None,
) -> float:
    """Compute the lone-bull penalty (may be 0.0 if conditions not met).

    Parameters
    ----------
    pooled_opinions:
        Opinions that actually entered the pool (post-dedup, post-shadow).
    all_bucket_opinions:
        All opinions for this bucket BEFORE dedup/shadow filtering
        (used to check for dissenters).
    diversity_factor:
        effective_N / N for the current pool.
    weights:
        Optional WeightBundle used to exclude shadow or zero-weight advisors
        from the dissenter search.  An onboarding (shadow=True) or disabled
        (weight<=0) advisor must not silently trigger the −0.10 penalty.
        When None, all non-pooled advisors are considered potential dissenters
        (Phase-1 safe: EqualWeightBundle has no shadow advisors).

    Returns
    -------
    float
        Tax magnitude to subtract from conviction (always ≥ 0).
    """
    if not pooled_opinions:
        return 0.0

    # Check unanimity: all pooled opinions same sign.
    signs = [math.copysign(1.0, op.stance_score) for op in pooled_opinions if op.stance_score != 0.0]
    if not signs:
        return 0.0  # all neutral — no lone-bull scenario

    first_sign = signs[0]
    all_same_sign = all(s == first_sign for s in signs)
    if not all_same_sign:
        return 0.0  # dissent already inside the pool

    # Check high correlation condition.
    if diversity_factor >= _LONE_BULL_CORR_THRESHOLD:
        return 0.0  # diverse enough — no tax

    # Check for external dissenter in the full bucket opinion set.
    # Exclude shadow advisors and zero-weight (disabled) advisors — they must
    # not silently apply the tax when they are onboarding or inactive.
    pooled_ids = {op.advisor_id for op in pooled_opinions}
    for op in all_bucket_opinions:
        if op.advisor_id in pooled_ids:
            continue  # already in pool

        # Skip shadow / zero-weight advisors from the dissenter check.
        if weights is not None:
            aw = weights.weights.get(op.advisor_id)
            if aw is not None and (aw.shadow or aw.weight <= 0.0):
                continue

        if op.stance_score == 0.0:
            continue  # neutral dissenter doesn't count
        dissenter_sign = math.copysign(1.0, op.stance_score)
        if dissenter_sign != first_sign:
            return _LONE_BULL_TAX_MAGNITUDE

    return 0.0


def dispersion(
    pooled_opinions: list[Opinion],
    norm_weights: dict[str, float],
    signal_strength: float,
) -> float:
    """Weighted standard deviation of stance scores.

    Parameters
    ----------
    pooled_opinions:
        Opinions in the pool.
    norm_weights:
        Normalised (simplex) weight per advisor_id.
    signal_strength:
        Weighted mean stance (used as the mean in the variance calculation).

    Returns
    -------
    float
        Weighted standard deviation (≥ 0).
    """
    if len(pooled_opinions) <= 1:
        return 0.0

    variance = 0.0
    for op in pooled_opinions:
        w = norm_weights.get(op.advisor_id, 0.0)
        variance += w * (op.stance_score - signal_strength) ** 2

    return math.sqrt(max(0.0, variance))
