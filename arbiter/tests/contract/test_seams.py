"""Tests for seam dataclasses — Lane 9 core.

Covers INTERFACES.md §4–§9.
"""
from __future__ import annotations

import pytest
from datetime import date, datetime, timezone

from arbiter.contract.seams import (
    AdvisorWeight,
    EqualWeightBundle,
    FusionOutput,
    Idea,
    PaperOrder,
    ResolvedOutcome,
    TradingDecision,
    WeightBundle,
)
from arbiter.types import (
    DegradationLevel,
    HorizonBucket,
    IdeaState,
    OrderSide,
)


# ---------------------------------------------------------------------------
# §4  FusionOutput
# ---------------------------------------------------------------------------

class TestFusionOutput:
    def test_construct(self):
        fo = FusionOutput(
            bucket=HorizonBucket.SHORT,
            conviction=0.6,
            dispersion=0.1,
            effective_n=1.8,
            n_opinions=3,
            advisor_contributions={"A1.insider": 0.4, "A1.congress": 0.2},
            vetoes=[],
            cold_start=True,
        )
        assert fo.bucket == HorizonBucket.SHORT
        assert fo.conviction == 0.6
        assert fo.n_opinions == 3
        assert fo.cold_start is True

    def test_frozen(self):
        fo = FusionOutput(
            bucket=HorizonBucket.MEDIUM,
            conviction=0.3,
            dispersion=0.05,
            effective_n=2.0,
            n_opinions=2,
            advisor_contributions={},
            vetoes=["A2.mirofish"],
            cold_start=False,
        )
        with pytest.raises((AttributeError, TypeError)):
            fo.conviction = 0.9  # type: ignore[misc]

    def test_vetoes_list(self):
        fo = FusionOutput(
            bucket=HorizonBucket.LONG,
            conviction=-0.2,
            dispersion=0.3,
            effective_n=1.0,
            n_opinions=1,
            advisor_contributions={"A1.insider": -0.2},
            vetoes=["A2.mirofish", "A3.news"],
            cold_start=False,
        )
        assert "A2.mirofish" in fo.vetoes
        assert "A3.news" in fo.vetoes


# ---------------------------------------------------------------------------
# §5  AdvisorWeight + WeightBundle + EqualWeightBundle
# ---------------------------------------------------------------------------

class TestAdvisorWeight:
    def test_construct(self):
        aw = AdvisorWeight(
            advisor_id="A1.insider",
            weight=0.4,
            ci_low=0.3,
            ci_high=0.5,
            shadow=False,
        )
        assert aw.advisor_id == "A1.insider"
        assert aw.weight == 0.4
        assert aw.shadow is False

    def test_frozen(self):
        aw = AdvisorWeight(
            advisor_id="A2.mirofish",
            weight=0.35,
            ci_low=0.2,
            ci_high=0.35,
            shadow=True,
        )
        with pytest.raises((AttributeError, TypeError)):
            aw.weight = 0.5  # type: ignore[misc]

    def test_shadow_onboarding(self):
        """shadow=True marks an advisor in onboarding with zero live weight."""
        aw = AdvisorWeight(
            advisor_id="A3.news",
            weight=0.0,
            ci_low=0.0,
            ci_high=0.0,
            shadow=True,
        )
        assert aw.shadow is True
        assert aw.weight == 0.0


class TestWeightBundle:
    def test_construct(self):
        weights = {
            "A1.insider": AdvisorWeight("A1.insider", 0.5, 0.4, 0.6, False),
            "A2.mirofish": AdvisorWeight("A2.mirofish", 0.35, 0.2, 0.35, False),
        }
        corr = {("A1.insider", "A2.mirofish"): 0.3}
        wb = WeightBundle(weights=weights, correlation_matrix=corr)
        assert "A1.insider" in wb.weights
        assert wb.correlation_matrix[("A1.insider", "A2.mirofish")] == 0.3

    def test_frozen(self):
        wb = WeightBundle(weights={}, correlation_matrix={})
        with pytest.raises((AttributeError, TypeError)):
            wb.weights = {}  # type: ignore[misc]


class TestEqualWeightBundle:
    def test_single_advisor(self):
        # EqualWeightBundle emits raw LOG-POOL weights of 1.0 each (not 1/N simplex).
        # pool.py performs the single authoritative normalisation step.
        wb = EqualWeightBundle(["A1.insider"])
        assert "A1.insider" in wb.weights
        assert wb.weights["A1.insider"].weight == pytest.approx(1.0)
        assert wb.correlation_matrix == {}

    def test_two_advisors_equal_raw_weight(self):
        # Both advisors get raw log-pool weight 1.0 each; pool.py will normalise to 0.5 each.
        wb = EqualWeightBundle(["A1.insider", "A2.mirofish"])
        w1 = wb.weights["A1.insider"].weight
        w2 = wb.weights["A2.mirofish"].weight
        assert w1 == pytest.approx(1.0)
        assert w2 == pytest.approx(1.0)
        # Weights are equal (same raw value)
        assert w1 == pytest.approx(w2)

    def test_three_advisors_all_raw_weight_one(self):
        # Raw log-pool weights are 1.0 each; pool.py normalises to simplex 1/N.
        ids = ["A1.insider", "A1.congress", "A2.mirofish"]
        wb = EqualWeightBundle(ids)
        for aw in wb.weights.values():
            assert aw.weight == pytest.approx(1.0)

    def test_empty_list(self):
        wb = EqualWeightBundle([])
        assert wb.weights == {}
        assert wb.correlation_matrix == {}

    def test_correlation_matrix_empty(self):
        """Phase 1: EqualWeightBundle always has empty correlation matrix."""
        wb = EqualWeightBundle(["A1.insider", "A2.mirofish"])
        assert wb.correlation_matrix == {}

    def test_all_weights_not_shadow(self):
        wb = EqualWeightBundle(["A1.insider", "A1.congress"])
        for aw in wb.weights.values():
            assert aw.shadow is False

    def test_ci_equals_weight_for_equal_bundle(self):
        """Phase-1: ci_low == ci_high == weight (1.0 raw log-pool)."""
        wb = EqualWeightBundle(["A1.insider"])
        aw = wb.weights["A1.insider"]
        assert aw.ci_low == pytest.approx(aw.weight)
        assert aw.ci_high == pytest.approx(aw.weight)
        assert aw.weight == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# §6  ResolvedOutcome
# ---------------------------------------------------------------------------

class TestResolvedOutcome:
    def test_construct(self):
        ro = ResolvedOutcome(
            idea_id="01HJKL0000000000000000AA",
            advisor_id="A1.insider",
            ticker="NVDA",
            alpha_bps=42.5,
            binary=1,
            advisor_confidence=0.75,
            stance_score=1.0,
            abstained=False,
            horizon_days=30,
            label_kind="normal",
        )
        assert ro.alpha_bps == 42.5
        assert ro.binary == 1
        assert ro.label_kind == "normal"

    def test_frozen(self):
        ro = ResolvedOutcome(
            idea_id="01HJKL0000000000000000BB",
            advisor_id="A1.insider",
            ticker="AAPL",
            alpha_bps=-10.0,
            binary=-1,
            advisor_confidence=0.4,
            stance_score=-1.0,
            abstained=False,
            horizon_days=60,
            label_kind="early_exit",
        )
        with pytest.raises((AttributeError, TypeError)):
            ro.alpha_bps = 0.0  # type: ignore[misc]

    def test_label_kinds(self):
        """All label_kind values from the spec are storable."""
        for kind in ("normal", "early_exit", "reversal", "corporate_event", "partial"):
            ro = ResolvedOutcome(
                idea_id="x",
                advisor_id="A1.insider",
                ticker="TSLA",
                alpha_bps=0.0,
                binary=0,
                advisor_confidence=0.5,
                stance_score=0.0,
                abstained=False,
                horizon_days=30,
                label_kind=kind,
            )
            assert ro.label_kind == kind

    def test_abstained(self):
        ro = ResolvedOutcome(
            idea_id="y",
            advisor_id="A2.mirofish",
            ticker="MSFT",
            alpha_bps=0.0,
            binary=0,
            advisor_confidence=0.0,
            stance_score=0.0,
            abstained=True,
            horizon_days=30,
            label_kind="normal",
        )
        assert ro.abstained is True


# ---------------------------------------------------------------------------
# §7  Idea (mutable state field)
# ---------------------------------------------------------------------------

class TestIdea:
    def test_construct(self):
        idea = Idea(
            idea_id="01HJKL0000000000000000CC",
            ticker="AAPL",
            thesis="Cluster insider buy ahead of earnings",
            horizon_days=30,
            state=IdeaState.NASCENT,
            as_of=datetime(2026, 1, 15, tzinfo=timezone.utc),
            dedupe_key=("AAPL", HorizonBucket.SHORT.value),
        )
        assert idea.state == IdeaState.NASCENT
        assert idea.dedupe_key == ("AAPL", "SHORT")

    def test_state_is_mutable(self):
        """Idea.state is intentionally mutable for FSM transitions."""
        idea = Idea(
            idea_id="01HJKL0000000000000000DD",
            ticker="GOOG",
            thesis="Thesis",
            horizon_days=60,
            state=IdeaState.NASCENT,
            as_of=datetime(2026, 1, 15, tzinfo=timezone.utc),
            dedupe_key=("GOOG", HorizonBucket.MEDIUM.value),
        )
        idea.state = IdeaState.GATHERING
        assert idea.state == IdeaState.GATHERING

    def test_all_idea_states_assignable(self):
        """All IdeaState enum values can be assigned."""
        idea = Idea(
            idea_id="01HJKL0000000000000000EE",
            ticker="TSLA",
            thesis="Thesis",
            horizon_days=200,
            state=IdeaState.NASCENT,
            as_of=datetime(2026, 1, 15, tzinfo=timezone.utc),
            dedupe_key=("TSLA", HorizonBucket.LONG.value),
        )
        for s in IdeaState:
            idea.state = s
            assert idea.state == s


# ---------------------------------------------------------------------------
# §8  TradingDecision
# ---------------------------------------------------------------------------

class TestTradingDecision:
    def test_construct_allowed(self):
        td = TradingDecision(
            allowed=True,
            size_multiplier=1.0,
            level=DegradationLevel.NORMAL,
            reasons=[],
        )
        assert td.allowed is True
        assert td.size_multiplier == 1.0
        assert td.level == DegradationLevel.NORMAL

    def test_construct_degraded(self):
        td = TradingDecision(
            allowed=True,
            size_multiplier=0.25,
            level=DegradationLevel.DEGRADED,
            reasons=["Only 1 live advisor"],
        )
        assert td.size_multiplier == 0.25
        assert td.level == DegradationLevel.DEGRADED

    def test_construct_halted(self):
        td = TradingDecision(
            allowed=False,
            size_multiplier=0.0,
            level=DegradationLevel.HALTED,
            reasons=["0 live advisors — HALTED"],
        )
        assert td.allowed is False
        assert td.size_multiplier == 0.0

    def test_frozen(self):
        td = TradingDecision(
            allowed=True,
            size_multiplier=1.0,
            level=DegradationLevel.NORMAL,
            reasons=[],
        )
        with pytest.raises((AttributeError, TypeError)):
            td.allowed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# §9  PaperOrder
# ---------------------------------------------------------------------------

class TestPaperOrder:
    def test_construct(self):
        po = PaperOrder(
            order_id="01HJKL0000000000000000FF",
            dedup_hash="sha256:abcdef1234567890",
            ticker="AAPL",
            side=OrderSide.BUY,
            qty=100.0,
            horizon_bucket=HorizonBucket.SHORT,
            entry_date=date(2026, 1, 16),
            advisor_signature="A1.insider:2026-01-15",
            exits={
                "stop_loss": 145.0,
                "horizon_expiry": date(2026, 2, 15),
                "conviction_reversal": -0.25,
            },
        )
        assert po.ticker == "AAPL"
        assert po.side == OrderSide.BUY
        assert po.qty == 100.0
        assert po.exits["stop_loss"] == 145.0

    def test_frozen(self):
        po = PaperOrder(
            order_id="01HJKL0000000000000000GG",
            dedup_hash="sha256:xyz",
            ticker="GOOG",
            side=OrderSide.SELL,
            qty=50.0,
            horizon_bucket=HorizonBucket.LONG,
            entry_date=date(2026, 1, 16),
            advisor_signature="A1.insider:2026-01-15",
            exits={},
        )
        with pytest.raises((AttributeError, TypeError)):
            po.qty = 0.0  # type: ignore[misc]

    def test_sell_side(self):
        po = PaperOrder(
            order_id="01HJKL0000000000000000HH",
            dedup_hash="sha256:sell123",
            ticker="NVDA",
            side=OrderSide.SELL,
            qty=25.0,
            horizon_bucket=HorizonBucket.MEDIUM,
            entry_date=date(2026, 3, 1),
            advisor_signature="A1.congress:2026-03-01",
            exits={
                "stop_loss": 900.0,
                "horizon_expiry": date(2026, 6, 1),
                "conviction_reversal": 0.25,
            },
        )
        assert po.side == OrderSide.SELL
