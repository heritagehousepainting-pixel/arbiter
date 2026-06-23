"""STANCE_BASE — cold-start prior table for calibration (Lane 9).

Maps (advisor_type, raw_stance_bin, horizon_bucket) → probability estimate.
Used by :class:`~arbiter.calibration.calibrator.Calibrator` only when no
fitted calibration model exists (i.e. under cold-start conditions).

Design notes
------------
- ``raw_stance`` ∈ [-1.0, 1.0].  We bin it into 5 buckets (strong-short,
  short, neutral, long, strong-long) to keep the prior table manageable.
- Probabilities are P(positive-alpha | stance, horizon).
- Neutral stance (bin 0) → 0.50 (coin-flip) across all advisors.
- Strong directional stance → 0.60–0.70 for trusted advisor types.
- Values are intentionally conservative — the prior is meant to be
  non-informative enough to not wreck live sizing, while still being
  directionally correct per known insider/congress patterns.

INTERFACES.md §11.9:
  "calibration owns raw→prob; STANCE_BASE is cold-start prior"
"""
from __future__ import annotations

from arbiter.types import HorizonBucket

# ---------------------------------------------------------------------------
# Stance bins  (edges are inclusive on the left)
# ---------------------------------------------------------------------------
# Bin index: -2=strong-short, -1=short, 0=neutral, +1=long, +2=strong-long
# We discretise [-1, 1] into 5 equal-width buckets of width 0.4.
# Edge: stance < -0.6  => -2 ; -0.6..< -0.2 => -1 ; -0.2..<0.2 => 0 ; ...

def _stance_bin(raw_stance: float) -> int:
    """Bin raw_stance ∈ [-1, 1] into {-2, -1, 0, +1, +2}."""
    if raw_stance < -0.6:
        return -2
    if raw_stance < -0.2:
        return -1
    if raw_stance < 0.2:
        return 0
    if raw_stance < 0.6:
        return 1
    return 2


# ---------------------------------------------------------------------------
# STANCE_BASE table
#
# Structure: { advisor_type: { HorizonBucket: { stance_bin: prob } } }
#
# advisor_type is the prefix of advisor_id before the first dot, e.g.:
#   "A1.insider" -> "A1"
#   "A1.congress" -> "A1"
#   "A2.mirofish" -> "A2"
#   "A3.quant" -> "A3"
#   "*" is the default fallback.
# ---------------------------------------------------------------------------
STANCE_BASE: dict[str, dict[HorizonBucket, dict[int, float]]] = {
    # A1 — form4/insider + congress disclosures.
    # Directional accuracy edges vs random are modest at SHORT horizon,
    # stronger at MEDIUM (SEC studies; ~55-65% directional).
    "A1": {
        HorizonBucket.INTRADAY: {
            -2: 0.38, -1: 0.44, 0: 0.50, 1: 0.56, 2: 0.62,
        },
        HorizonBucket.SHORT: {
            -2: 0.40, -1: 0.45, 0: 0.50, 1: 0.55, 2: 0.60,
        },
        HorizonBucket.MEDIUM: {
            -2: 0.38, -1: 0.44, 0: 0.50, 1: 0.57, 2: 0.63,
        },
        HorizonBucket.LONG: {
            -2: 0.40, -1: 0.45, 0: 0.50, 1: 0.56, 2: 0.62,
        },
    },
    # A2 — MiroFish (LLM-ensemble).
    # Calibrated to be less aggressive than human insiders; LLM overconfidence
    # means raw stance often overstates.  Prior is conservative.
    "A2": {
        HorizonBucket.INTRADAY: {
            -2: 0.42, -1: 0.46, 0: 0.50, 1: 0.54, 2: 0.58,
        },
        HorizonBucket.SHORT: {
            -2: 0.42, -1: 0.46, 0: 0.50, 1: 0.54, 2: 0.58,
        },
        HorizonBucket.MEDIUM: {
            -2: 0.42, -1: 0.47, 0: 0.50, 1: 0.53, 2: 0.57,
        },
        HorizonBucket.LONG: {
            -2: 0.43, -1: 0.47, 0: 0.50, 1: 0.53, 2: 0.57,
        },
    },
    # A3 — Quantitative/vol-anomaly signals.
    "A3": {
        HorizonBucket.INTRADAY: {
            -2: 0.41, -1: 0.46, 0: 0.50, 1: 0.54, 2: 0.59,
        },
        HorizonBucket.SHORT: {
            -2: 0.41, -1: 0.46, 0: 0.50, 1: 0.54, 2: 0.59,
        },
        HorizonBucket.MEDIUM: {
            -2: 0.42, -1: 0.46, 0: 0.50, 1: 0.54, 2: 0.58,
        },
        HorizonBucket.LONG: {
            -2: 0.42, -1: 0.46, 0: 0.50, 1: 0.54, 2: 0.58,
        },
    },
    # Default / unknown advisor type — maximally conservative prior.
    "*": {
        HorizonBucket.INTRADAY: {
            -2: 0.43, -1: 0.47, 0: 0.50, 1: 0.53, 2: 0.57,
        },
        HorizonBucket.SHORT: {
            -2: 0.43, -1: 0.47, 0: 0.50, 1: 0.53, 2: 0.57,
        },
        HorizonBucket.MEDIUM: {
            -2: 0.43, -1: 0.47, 0: 0.50, 1: 0.53, 2: 0.57,
        },
        HorizonBucket.LONG: {
            -2: 0.43, -1: 0.47, 0: 0.50, 1: 0.53, 2: 0.57,
        },
    },
}


def lookup_prior(
    advisor_id: str,
    raw_stance: float,
    horizon_bucket: HorizonBucket,
) -> float:
    """Return the cold-start prior probability for a given (advisor, stance, horizon).

    Parameters
    ----------
    advisor_id:
        Full dotted advisor ID (e.g. "A1.insider").  The first dotted segment
        is used to key into the table; falls back to "*" if unknown.
    raw_stance:
        Raw stance score ∈ [-1.0, 1.0].
    horizon_bucket:
        The HorizonBucket for this opinion.

    Returns
    -------
    float
        Prior probability P(positive-alpha) ∈ (0, 1).
    """
    advisor_type = advisor_id.split(".")[0] if "." in advisor_id else advisor_id
    table = STANCE_BASE.get(advisor_type, STANCE_BASE["*"])
    bucket_table = table.get(horizon_bucket, table[HorizonBucket.SHORT])
    sbin = _stance_bin(raw_stance)
    return bucket_table[sbin]
