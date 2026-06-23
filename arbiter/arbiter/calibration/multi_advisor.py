"""MultiAdvisorCalibrator — per-advisor calibration adapter for fusion (#4, D5).

``fuse``/``pool.py`` use a SINGLE calibrator object, but the real ``Calibrator`` is
per-advisor.  This thin adapter wraps ``dict[str, Calibrator]`` and routes the
fusion seam ``transform_for(advisor_id, raw_stance, horizon_days)`` to the right
advisor's calibrator.  ``pool.py`` already knows ``op.advisor_id``; the additive
``transform_for`` seam (default on the base ``transform``) keeps the contract
backward-compatible.

``is_cold_start`` is True iff EVERY wrapped calibrator is treated as cold (so
``FusionOutput.cold_start`` flips False only once at least one advisor's fitted
model is ACTUALLY APPLIED).

D5 thin-sample gating (the wiring-level gate)
---------------------------------------------
The base ``Calibrator`` fits a model once ``_MIN_FIT_SAMPLES = 2`` non-zero
outcomes exist per bucket — but a 2-point Platt/isotonic fit is fragile and
over-confident and can swing conviction.  D5 mandates staying
PASSTHROUGH-EQUIVALENT until a MEANINGFUL per-bucket sample exists.

So this adapter applies an ADDITIONAL ``min_apply_samples`` (default
``MIN_APPLY_NONZERO_OUTCOMES = 15``) gate at fusion time: an advisor whose
calibrator has fewer than that many non-zero outcomes is routed through its
COLD-START prior (``transform`` falls back to the STANCE_BASE prior), exactly as
if no model had been fit — even though the base ``Calibrator`` may hold a
2-sample model.  This does NOT mutate ``Calibrator._MIN_FIT_SAMPLES`` (other
callers rely on it); the gate lives entirely in the fusion wiring.

The threshold keys on the LARGEST per-(advisor, bucket) non-zero count
(``max_bucket_nonzero_outcomes``), matching the per-bucket spirit of the spec
(D5 / SHADOW-style onboarding gating).  An advisor only "applies" its fitted
model once at least one of its horizon buckets has a meaningful sample.

The ``predict_proba`` [0,1] clamp in ``Calibrator.transform`` prevents any
NaN/degenerate prob from shipping once a model IS applied.
"""
from __future__ import annotations

from arbiter.calibration.calibrator import Calibrator

# Wiring-level minimum non-zero outcomes (per advisor, per bucket) before a
# fitted calibrator is actually APPLIED in fusion.  Below this the advisor stays
# passthrough-equivalent (cold-start prior).  Reuses the shadow/onboarding-style
# threshold magnitude (well above the base Calibrator._MIN_FIT_SAMPLES=2) so a
# fragile 2-point fit never swings conviction (D5).
MIN_APPLY_NONZERO_OUTCOMES: int = 15


class MultiAdvisorCalibrator:
    """Route the fusion calibration seam to a per-advisor ``Calibrator``.

    Parameters
    ----------
    calibrators:
        ``{advisor_id: Calibrator}``.  An advisor absent from this map falls back
        to the raw stance (passthrough-equivalent), so an unknown advisor never
        crashes fusion.
    min_apply_samples:
        Wiring-level minimum non-zero outcomes (largest per-bucket count) before a
        fitted calibrator is applied.  Below this the advisor is treated as cold
        (passthrough-equivalent) regardless of any 2-sample model the base
        ``Calibrator`` may hold (D5).  Defaults to
        :data:`MIN_APPLY_NONZERO_OUTCOMES`.
    """

    def __init__(
        self,
        calibrators: dict[str, Calibrator],
        *,
        min_apply_samples: int = MIN_APPLY_NONZERO_OUTCOMES,
    ) -> None:
        self._calibrators = dict(calibrators)
        self._min_apply_samples = int(min_apply_samples)

    # FROZEN CONTRACT (E2/E4): ALL calibrator branches (identity, prior, Platt,
    # isotonic, gated, unknown-advisor) emit P(positive-alpha) ∈ [0, 1].  Pool
    # (fusion/pool.py) maps to signed space via 2*p - 1 before weighting, so a
    # bearish-calibrated p < 0.5 contributes a NEGATIVE signal.  This flag tells
    # pool.py the output is in probability space (so the 2*p-1 map applies); a
    # plain passthrough calibrator without this flag stays identity ([-1, 1]).
    outputs_probability: bool = True

    def _is_applied(self, cal: Calibrator) -> bool:
        """True iff *cal* has a fitted model AND a meaningful sample (≥ the
        wiring-level threshold).  Otherwise it stays passthrough-equivalent."""
        if cal.is_cold_start:
            return False
        return cal.max_bucket_nonzero_outcomes() >= self._min_apply_samples

    @property
    def is_cold_start(self) -> bool:
        """True iff NO wrapped calibrator is actually applied (every advisor is
        cold OR gated below the meaningful-sample threshold)."""
        if not self._calibrators:
            return True
        return not any(self._is_applied(c) for c in self._calibrators.values())

    def transform(self, raw_stance: float, horizon_days: int) -> float:
        """Advisor-agnostic fallback.  Without an advisor id we cannot route, so
        we behave as the cold-start prior (passthrough-equivalent).  Real routing
        goes through :meth:`transform_for`.

        Returns P(positive-alpha) ∈ [0, 1] (FROZEN CONTRACT, see
        :data:`outputs_probability`).  With no wrapped calibrator to supply a
        prior we fall back to the linear stance→prob map (raw_stance + 1) / 2 so
        the output stays in probability space, never raw [-1, 1]."""
        # Use any wrapped calibrator's prior, else linear stance→prob map.
        for c in self._calibrators.values():
            return c.transform(raw_stance, horizon_days)
        return (raw_stance + 1.0) / 2.0

    def transform_for(
        self, advisor_id: str, raw_stance: float, horizon_days: int
    ) -> float:
        """Route to the named advisor's calibrator (the D5 seam).

        Below the wiring-level meaningful-sample threshold the advisor stays
        passthrough-equivalent: we route through a FRESH unfitted ``Calibrator``
        so the result is the cold-start STANCE_BASE prior, never the fragile
        thin-sample fitted model.
        """
        cal = self._calibrators.get(advisor_id)
        if cal is None:
            # Unknown advisor — no fitted model.  Return P(positive-alpha) ∈
            # [0, 1] (FROZEN CONTRACT) via the linear stance→prob map so the
            # output stays in the SAME space as every other branch; pool.py
            # applies 2*p-1 to recover the signed signal.  (Previously returned
            # raw_stance ∈ [-1, 1] — an inconsistent space that pool's 2*p-1 map
            # would have mis-scaled.)
            return (raw_stance + 1.0) / 2.0
        if not self._is_applied(cal):
            # Thin sample / cold — passthrough-equivalent: use the cold-start
            # prior for this advisor (a fresh unfitted Calibrator yields exactly
            # the STANCE_BASE prior without touching the fitted model).
            return Calibrator(advisor_id).transform(raw_stance, horizon_days)
        return cal.transform(raw_stance, horizon_days)
