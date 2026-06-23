"""Log-opinion pool for a single HorizonBucket.

The log-pool aggregates advisor opinions using their LOG-POOL weights
(from a WeightBundle).  With EqualWeightBundle the weights are 1/N each.

The log-pool for a set of N advisors with weights {w_i} and stance
scores {s_i} (mapped through calibration to probabilities p_i ∈ [0,1])
is the geometric mixture of the individual predictive distributions.

Formula
-------
For stance-based pooling the signed aggregate signal is:

    raw_signal = Σ w_i * stance_i                (weighted mean stance)

where w_i are the *normalised* simplex weights derived from the
log-pool weights (so they sum to 1 over the advisors present).

A calibrator maps each raw stance to a calibrated probability, but in
Phase 1 the calibrator is the identity passthrough so the raw mean
stance IS the pool output (mapped from [-1,1] to effectively a signal
in [-1,1]).

Weights that are zero (disabled advisors) or belong to advisors not
present in the opinion list are excluded from the normalisation step.

Returns
-------
Tuple of
    signal_strength: float         — signed aggregate in [-1, 1]
    advisor_contributions: dict    — advisor_id → contribution (w_i * stance_i before normalisation)
    normalised_weights: dict       — advisor_id → simplex weight used
"""
from __future__ import annotations

from arbiter.contract.opinion import Opinion
from arbiter.contract.seams import WeightBundle


def pool_opinions(
    opinions: list[Opinion],
    weights: WeightBundle,
    calibrator,
) -> tuple[float, dict[str, float], dict[str, float]]:
    """Compute log-pool aggregate for a list of opinions (single bucket).

    Parameters
    ----------
    opinions:
        Non-empty list of Opinion objects, all in the same HorizonBucket.
        Abstaining opinions (None) must be excluded *before* calling this.
    weights:
        WeightBundle with per-advisor LOG-POOL weights.
    calibrator:
        Object with ``transform(raw_stance: float, horizon_days: int) -> float``
        mapping raw stance ∈ [-1, 1] to calibrated probability ∈ [0, 1].
        Phase-1 default: identity passthrough (returns (stance + 1) / 2, or just
        the stance treated as a signal; engine decides interpretation).

    Returns
    -------
    signal_strength:
        Weighted mean of calibrated signals, mapped back to [-1, 1] space.
        For Phase-1 identity calibrator this is just the weighted mean stance.
    advisor_contributions:
        advisor_id → weighted contribution (w_i_norm * calibrated_signal_i).
    normalised_weights:
        advisor_id → simplex weight w_i_norm used in the pool.
    """
    if not opinions:
        return 0.0, {}, {}

    # Sign-space contract (E2/E4, FROZEN): the calibrator emits P(positive-alpha)
    # ∈ [0, 1] for EVERY branch (identity, prior, Platt, isotonic, gated,
    # unknown-advisor).  Pool is the ONE place that maps probability → signed
    # signal via ``2*p - 1`` before weighting, so a bearish-calibrated p < 0.5
    # contributes a NEGATIVE signal and a neutral p == 0.5 contributes 0.
    # A plain passthrough calibrator (no ``outputs_probability`` flag) already
    # returns a signed stance ∈ [-1, 1], so we DON'T re-map it.
    outputs_probability = bool(getattr(calibrator, "outputs_probability", False))

    # Determine which advisors participate (skip zero-weight / shadow) and their
    # raw LOG-POOL weight.  Keyed by advisor_id — one weight per advisor even if
    # the advisor contributes MULTIPLE opinions (different run_groups) in this
    # bucket.
    raw_weights: dict[str, float] = {}
    for op in opinions:
        if op.advisor_id in raw_weights:
            continue  # weight already resolved for this advisor
        aw = weights.weights.get(op.advisor_id)
        if aw is None:
            # Advisor not in bundle — treat as equal participant (Phase-1 fallback).
            raw_weights[op.advisor_id] = 1.0
        elif aw.shadow or aw.weight <= 0.0:
            # Shadow or disabled — excluded from pool.
            pass
        else:
            raw_weights[op.advisor_id] = aw.weight

    if not raw_weights:
        # All opinions are from shadow/disabled advisors — no signal.
        return 0.0, {}, {}

    # An advisor with N opinions in this bucket holds ONE simplex weight; that
    # weight is split EVENLY across the advisor's N opinions so the advisor's
    # total contribution equals (simplex_weight * mean signed signal) and the
    # signal is neither double-counted nor a contribution dropped (E4).  Count
    # only opinions from participating (non-shadow/disabled) advisors.
    op_count: dict[str, int] = {}
    for op in opinions:
        if op.advisor_id in raw_weights:
            op_count[op.advisor_id] = op_count.get(op.advisor_id, 0) + 1

    # Single authoritative normalisation step: convert raw LOG-POOL weights to
    # simplex weights (sum-to-1) here and only here.  Weights in WeightBundle
    # (including EqualWeightBundle) are raw log-pool scores (e.g. 1.0 each for
    # equal-weight; real trust scores in Phase 3).  Phase-3 implementers: do NOT
    # pre-normalise weights before storing them in WeightBundle; pool.py normalises.
    total_weight = sum(raw_weights.values())
    norm_weights: dict[str, float] = {
        aid: w / total_weight for aid, w in raw_weights.items()
    }

    # Apply calibrator to each opinion and compute weighted sum.  Contributions
    # are keyed per (advisor_id, run_group_id) so two opinions from the SAME
    # advisor in this bucket each get their own entry — previously both wrote to
    # advisor_contributions[advisor_id], so one silently overwrote the other and
    # sum(contributions) != signal_strength (E4).
    # NOTE (DEFERRED): the cross-ticker bucket-pooling structure is intentionally
    # left as-is; this fix only corrects the per-contribution keying.
    signal_strength = 0.0
    advisor_contributions: dict[str, float] = {}

    for op in opinions:
        if op.advisor_id not in norm_weights:
            continue  # excluded (shadow / disabled)
        # Split the advisor's simplex weight evenly across its opinions so the
        # advisor's contributions sum to (simplex_weight * mean signal).
        w_op = norm_weights[op.advisor_id] / op_count[op.advisor_id]

        # calibrator.transform_for routes per advisor (D5 seam); Phase-1
        # passthrough/base default delegates to ``transform`` (identity).
        # Fall back to ``transform`` for any minimal calibrator stub that
        # predates the additive seam (keeps the contract backward-compatible).
        _transform_for = getattr(calibrator, "transform_for", None)
        if _transform_for is not None:
            calibrated = _transform_for(op.advisor_id, op.stance_score, op.horizon_days)
        else:
            calibrated = calibrator.transform(op.stance_score, op.horizon_days)

        # Map probability → signed signal (FROZEN: 2*p - 1).  Passthrough/signed
        # calibrators are already in [-1, 1] and are used unchanged.
        signed = (2.0 * calibrated - 1.0) if outputs_probability else calibrated

        contrib = w_op * signed
        # Per-(advisor, run_group) key so same-advisor opinions don't collide.
        rg = getattr(op, "run_group_id", None)
        key = f"{op.advisor_id}::{rg}" if op_count[op.advisor_id] > 1 and rg is not None else op.advisor_id
        advisor_contributions[key] = advisor_contributions.get(key, 0.0) + contrib
        signal_strength += contrib

    return signal_strength, advisor_contributions, norm_weights
