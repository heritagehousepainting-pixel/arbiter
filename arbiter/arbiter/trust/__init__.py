"""Trust ledger — Lane 11.

Dormant until ≥60 resolved outcomes exist system-wide (Phase-3 activation).
All public entry points are re-exported here for convenience.

Sub-modules
-----------
brier.py            Recency-weighted inverse Brier skill score.
coverage.py         Coverage term (opined / eligible).
ledger.py           Composite trust = geometric mean(skill, calibration, coverage).
regime.py           Regime-change 21-day freeze + 2× post-regime weight.
correlation_matrix  Rolling pairwise ρ from outcome co-movement.
"""
from __future__ import annotations

from arbiter.trust.brier import brier_skill_score, recency_weighted_brier
from arbiter.trust.coverage import coverage_score
from arbiter.trust.ledger import TrustLedger, compute_composite_trust
from arbiter.trust.regime import RegimeTracker, apply_regime_weights
from arbiter.trust.correlation_matrix import CorrelationMatrix

__all__ = [
    "brier_skill_score",
    "recency_weighted_brier",
    "coverage_score",
    "TrustLedger",
    "compute_composite_trust",
    "RegimeTracker",
    "apply_regime_weights",
    "CorrelationMatrix",
]
