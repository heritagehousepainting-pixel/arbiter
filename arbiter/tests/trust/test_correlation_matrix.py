"""Tests for arbiter.trust.correlation_matrix — pairwise ρ matrix."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest
import numpy as np

from arbiter.contract.seams import ResolvedOutcome
from arbiter.trust.correlation_matrix import (
    CorrelationMatrix,
    DEFAULT_PRIOR,
    MIN_PAIRS,
    FINGERPRINT_BOOST,
)


def _utc(days_ago: float = 0.0) -> datetime:
    base = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    return base - timedelta(days=days_ago)


AS_OF = _utc(0)


def _make_outcome(
    idea_id: str,
    advisor_id: str,
    alpha_bps: float,
    abstained: bool = False,
) -> ResolvedOutcome:
    return ResolvedOutcome(
        idea_id=idea_id,
        advisor_id=advisor_id,
        ticker="TEST",
        alpha_bps=alpha_bps,
        binary=1 if alpha_bps > 0 else -1,
        advisor_confidence=0.8,
        stance_score=1.0 if alpha_bps > 0 else -1.0,
        abstained=abstained,
        horizon_days=30,
        label_kind="normal",
    )


class TestCorrelationMatrixDefaultPrior:
    def test_unknown_pair_returns_default_prior(self):
        cm = CorrelationMatrix.build({})
        rho = cm.get("A1.insider", "A2.mirofish")
        assert rho == DEFAULT_PRIOR

    def test_default_prior_is_0_5(self):
        assert DEFAULT_PRIOR == 0.5

    def test_sparse_sample_returns_prior(self):
        """Fewer than MIN_PAIRS co-observations → fall back to 0.5 prior."""
        # Only 3 shared ideas (< MIN_PAIRS=10)
        outcomes_a = [
            (_make_outcome(f"idea-{i}", "A1.t", float(i)), _utc(i))
            for i in range(3)
        ]
        outcomes_b = [
            (_make_outcome(f"idea-{i}", "A2.t", float(i)), _utc(i))
            for i in range(3)
        ]
        cm = CorrelationMatrix.build({"A1.t": outcomes_a, "A2.t": outcomes_b})
        rho = cm.get("A1.t", "A2.t")
        assert rho == DEFAULT_PRIOR

    def test_self_correlation_is_one(self):
        cm = CorrelationMatrix.build({"A1.t": []})
        assert cm.get("A1.t", "A1.t") == 1.0


class TestCorrelationMatrixComovement:
    def _make_shared_outcomes(
        self, n: int, adv_a: str, adv_b: str, correlated: bool = True
    ) -> dict[str, list[tuple[ResolvedOutcome, datetime]]]:
        """Build n shared-idea outcomes. If correlated, both advisors agree on direction."""
        outcomes_a = []
        outcomes_b = []
        for i in range(n):
            alpha_a = float(10 + i)  # always positive
            alpha_b = float(10 + i) if correlated else float(-(10 + i))
            outcomes_a.append((_make_outcome(f"idea-{i}", adv_a, alpha_a), _utc(i)))
            outcomes_b.append((_make_outcome(f"idea-{i}", adv_b, alpha_b), _utc(i)))
        return {adv_a: outcomes_a, adv_b: outcomes_b}

    def test_perfectly_correlated_advisors(self):
        """Two advisors with identical alpha_bps on shared ideas → ρ ≈ 1.0."""
        data = self._make_shared_outcomes(15, "A1.t", "A2.t", correlated=True)
        cm = CorrelationMatrix.build(data)
        rho = cm.get("A1.t", "A2.t")
        assert rho > 0.99

    def test_anti_correlated_advisors(self):
        """Perfectly anti-correlated alpha_bps → ρ ≈ -1.0."""
        data = self._make_shared_outcomes(15, "A1.t", "A2.t", correlated=False)
        cm = CorrelationMatrix.build(data)
        rho = cm.get("A1.t", "A2.t")
        assert rho < -0.99

    def test_symmetry(self):
        """ρ(A, B) == ρ(B, A)."""
        data = self._make_shared_outcomes(15, "A1.t", "A2.t", correlated=True)
        cm = CorrelationMatrix.build(data)
        assert cm.get("A1.t", "A2.t") == cm.get("A2.t", "A1.t")

    def test_abstained_outcomes_excluded(self):
        """Abstained outcomes don't contribute to correlation."""
        # 15 shared outcomes for both, but all of A2's are abstained
        outcomes_a = [
            (_make_outcome(f"idea-{i}", "A1.t", float(i + 1)), _utc(i))
            for i in range(15)
        ]
        outcomes_b = [
            (_make_outcome(f"idea-{i}", "A2.t", float(i + 1), abstained=True), _utc(i))
            for i in range(15)
        ]
        data = {"A1.t": outcomes_a, "A2.t": outcomes_b}
        cm = CorrelationMatrix.build(data)
        # No co-observations because A2 abstained on all → fall back to prior
        rho = cm.get("A1.t", "A2.t")
        assert rho == DEFAULT_PRIOR


class TestFingerprintCollision:
    def test_fingerprint_collision_boosts_rho(self):
        """Shared fingerprints → ρ set to FINGERPRINT_BOOST (0.9) if estimate is lower."""
        outcomes_a = [(_make_outcome("i1", "A1.t", 5.0), AS_OF)]
        outcomes_b = [(_make_outcome("i2", "A2.t", -5.0), AS_OF)]
        # No shared ideas → prior would be 0.5
        fps = {"A1.t": {"fp-abc", "fp-xyz"}, "A2.t": {"fp-abc"}}  # shared fp
        data = {"A1.t": outcomes_a, "A2.t": outcomes_b}
        cm = CorrelationMatrix.build(data, fingerprints_by_advisor=fps)
        rho = cm.get("A1.t", "A2.t")
        assert rho == FINGERPRINT_BOOST

    def test_no_fingerprint_overlap_no_boost(self):
        fps = {"A1.t": {"fp-aaa"}, "A2.t": {"fp-bbb"}}
        cm = CorrelationMatrix.build({"A1.t": [], "A2.t": []}, fingerprints_by_advisor=fps)
        rho = cm.get("A1.t", "A2.t")
        assert rho == DEFAULT_PRIOR  # still prior (sparse)


class TestToBundleDict:
    def test_bundle_dict_roundtrip(self):
        outcomes_a = [
            (_make_outcome(f"idea-{i}", "A1.t", float(i + 1)), _utc(i))
            for i in range(15)
        ]
        outcomes_b = [
            (_make_outcome(f"idea-{i}", "A2.t", float(i + 1)), _utc(i))
            for i in range(15)
        ]
        cm = CorrelationMatrix.build({"A1.t": outcomes_a, "A2.t": outcomes_b})
        bundle = cm.to_bundle_dict()
        assert isinstance(bundle, dict)
        assert ("A1.t", "A2.t") in bundle or ("A2.t", "A1.t") in bundle
        # Self-correlations
        assert bundle.get(("A1.t", "A1.t")) == 1.0
        assert bundle.get(("A2.t", "A2.t")) == 1.0
