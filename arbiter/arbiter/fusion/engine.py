"""Fusion engine — public entry point.

``fuse(opinions, weights, calibrator) -> dict[HorizonBucket, FusionOutput]``

Pipeline per bucket
-------------------
1. Filter abstains (None opinions are excluded by the caller; this
   function also skips any that slip through as None).
2. Bucket the opinions by ``opinion.horizon_bucket``.
3. For each non-empty bucket:
   a. Detect hard vetoes (veto.py).  If any → emit zeroed FusionOutput,
      skip remaining steps.
   b. Dedup same-run_group_id opinions within the bucket (dedup.py).
   c. Pool the opinions with log-pool weights (pool.py).
   d. Compute effective_N, diversity_factor, lone_bull_tax, dispersion
      (correlation.py).
   e. conviction = signal_strength * diversity_factor - lone_bull_tax
   f. Populate FusionOutput.
4. Buckets with no opinions are ABSENT from the returned dict
   (not 0.5, not NO_SIGNAL — simply missing).

Phase-1 calibrator contract
----------------------------
The ``calibrator`` parameter must expose:
    ``transform(raw_stance: float, horizon_days: int) -> float``

Phase-1 default is an identity passthrough that returns ``raw_stance``
unchanged.  The engine treats the transformed value as the signal in
[-1, 1] space; no 0–1 remapping is applied here (that is calibration's
job when a real calibrator arrives in Phase 3).

Cold-start
----------
``cold_start=True`` whenever no real calibration prior exists.  We
detect this by reading ``calibrator.is_cold_start``, which must be a
no-arg boolean property (or plain bool attribute) on any calibrator
object passed in.  ``PassthroughCalibrator`` exposes it as a property
returning True; the real ``Calibrator`` (Lane 9) exposes it as a property
returning True until the first bucket is fitted.

No wall-clock calls; no ``datetime.now()``.
"""
from __future__ import annotations

from collections import defaultdict

from arbiter.contract.opinion import Opinion
from arbiter.contract.seams import FusionOutput, WeightBundle
from arbiter.fusion.correlation import dispersion, effective_n, lone_bull_tax
from arbiter.fusion.dedup import dedup_bucket
from arbiter.fusion.pool import pool_opinions
from arbiter.fusion.veto import detect_vetoes
from arbiter.types import HorizonBucket


def fuse(
    opinions: list[Opinion | None],
    weights: WeightBundle,
    calibrator,
) -> dict[HorizonBucket, FusionOutput]:
    """Fuse advisor opinions into per-bucket conviction signals.

    Parameters
    ----------
    opinions:
        Raw list of opinions (may contain None for abstaining advisors;
        these are excluded before processing).
    weights:
        WeightBundle with per-advisor log-pool weights and correlation matrix.
    calibrator:
        Calibration object with ``transform(raw_stance, horizon_days) -> float``.
        Phase-1: use ``PassthroughCalibrator()``.

    Returns
    -------
    dict[HorizonBucket, FusionOutput]
        One entry per bucket that has at least one non-abstaining opinion.
        Buckets with no opinions are absent from the dict.
    """
    # 1. Strip abstains (None) and invalid entries.
    valid_opinions: list[Opinion] = [op for op in opinions if op is not None]

    if not valid_opinions:
        return {}

    # 2. Bucket the opinions.
    bucketed: dict[HorizonBucket, list[Opinion]] = defaultdict(list)
    for op in valid_opinions:
        bucketed[op.horizon_bucket].append(op)

    # Detect cold_start: calibrator.is_cold_start must be a bool property (or attr).
    # Using bool() ensures we get a real bool even if the attr is a non-property value.
    # Fallback to True when the attribute is absent (defensive; Phase-1 safe).
    cold_start: bool = bool(getattr(calibrator, "is_cold_start", True))

    result: dict[HorizonBucket, FusionOutput] = {}

    for bucket, bucket_ops in bucketed.items():
        # 3a. Hard-veto check.
        veto_ids = detect_vetoes(bucket_ops)
        if veto_ids:
            result[bucket] = FusionOutput(
                bucket=bucket,
                conviction=0.0,
                dispersion=0.0,
                effective_n=0.0,
                n_opinions=0,
                advisor_contributions={},
                vetoes=veto_ids,
                cold_start=cold_start,
            )
            continue

        # 3b. Dedup same-run_group_id within this bucket.
        deduped = dedup_bucket(bucket_ops)

        # 3c. Pool opinions.
        signal_strength, advisor_contributions, norm_weights = pool_opinions(
            deduped, weights, calibrator
        )

        if not norm_weights:
            # All opinions were shadow/disabled — skip bucket.
            continue

        # 3d. Correlation-adjusted effective N and diversity metrics.
        advisor_ids_in_pool = list(norm_weights.keys())
        n_pool = len(advisor_ids_in_pool)

        eff_n = effective_n(advisor_ids_in_pool, norm_weights, weights)
        diversity_factor = eff_n / n_pool if n_pool > 0 else 1.0

        # Lone-bull tax: pass weights so shadow/zero-weight advisors are excluded
        # from the dissenter search (they must not silently trigger the −0.10 penalty).
        tax = lone_bull_tax(deduped, bucket_ops, diversity_factor, weights)

        # Dispersion (weighted std of stances).
        disp = dispersion(deduped, norm_weights, signal_strength)

        # 3e. Conviction.
        conviction = signal_strength * diversity_factor - tax

        # 3f. Populate FusionOutput.
        result[bucket] = FusionOutput(
            bucket=bucket,
            conviction=conviction,
            dispersion=disp,
            effective_n=eff_n,
            n_opinions=len(deduped),
            advisor_contributions=advisor_contributions,
            vetoes=[],
            cold_start=cold_start,
        )

    return result


class PassthroughCalibrator:
    """Phase-1 identity calibrator.

    Maps raw stance ∈ [-1, 1] to itself unchanged.
    Exposes ``is_cold_start`` as a property returning True so that
    FusionOutput.cold_start is correctly set, and so that fusion's
    ``bool(calibrator.is_cold_start)`` call resolves to a real bool
    (not a bound method) for both PassthroughCalibrator and the real
    Calibrator (which also exposes ``is_cold_start`` as a property).

    This is the default calibrator until Lane 9 (calibration) is wired in
    (Wave-C).
    """

    @property
    def is_cold_start(self) -> bool:
        """Always True — no real calibration prior exists in Phase 1."""
        return True

    def transform(self, raw_stance: float, horizon_days: int) -> float:  # noqa: ARG002
        """Return raw_stance unchanged (identity passthrough)."""
        return raw_stance

    def transform_for(
        self, advisor_id: str, raw_stance: float, horizon_days: int  # noqa: ARG002
    ) -> float:
        """Per-advisor seam (D5).  Additive, advisor-agnostic default → ``transform``.

        ``pool.py`` calls ``calibrator.transform_for(op.advisor_id, ...)``.  The
        passthrough ignores ``advisor_id`` and stays identity, so the seam is
        backward-compatible; ``MultiAdvisorCalibrator`` overrides to route per
        advisor.
        """
        return self.transform(raw_stance, horizon_days)
