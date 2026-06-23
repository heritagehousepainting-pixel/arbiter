"""Tests for arbiter.trust.brier — recency-weighted Brier skill score."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from arbiter.contract.seams import ResolvedOutcome
from arbiter.trust.brier import (
    brier_skill_score,
    recency_weighted_brier,
    _decay_weight,
    _stance_to_prob,
    _outcome_to_prob,
    HALF_LIFE_DAYS,
    BS_REF,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc(days_ago: float = 0.0) -> datetime:
    """Return a tz-aware UTC datetime days_ago before a fixed reference point."""
    base = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    return base - timedelta(days=days_ago)


def _make_outcome(
    binary: int = 1,
    confidence: float = 0.8,
    abstained: bool = False,
    idea_id: str = "idea-001",
    advisor_id: str = "A1.test",
    stance_score: float | None = None,
) -> ResolvedOutcome:
    # #5a hygiene: the forecast is the advisor's ACTUAL stance, NOT the realized
    # binary.  The factory default is therefore a FIXED constant (+0.5) decoupled
    # from `binary` — a moderately-long stance that does NOT silently track the
    # answer.  A future regression that reintroduces answer-derived forecasts can
    # no longer hide behind a default that happens to align with the outcome.
    # Tests that assert a specific skill sign/magnitude pass `stance_score`
    # explicitly to encode "advisor was right/wrong".
    if stance_score is None:
        stance_score = 0.5
    return ResolvedOutcome(
        idea_id=idea_id,
        advisor_id=advisor_id,
        ticker="AAPL",
        alpha_bps=50.0 if binary == 1 else -50.0,
        binary=binary,
        advisor_confidence=confidence,
        stance_score=stance_score,
        abstained=abstained,
        horizon_days=30,
        label_kind="normal",
    )


AS_OF = _utc(0)


# ---------------------------------------------------------------------------
# Unit tests: helpers
# ---------------------------------------------------------------------------

class TestDecayWeight:
    def test_same_date_returns_one(self):
        w = _decay_weight(AS_OF, AS_OF)
        assert abs(w - 1.0) < 1e-9

    def test_half_life_returns_half(self):
        outcome_date = AS_OF - timedelta(days=HALF_LIFE_DAYS)
        w = _decay_weight(AS_OF, outcome_date)
        assert abs(w - 0.5) < 1e-6

    def test_two_half_lives_returns_quarter(self):
        outcome_date = AS_OF - timedelta(days=2 * HALF_LIFE_DAYS)
        w = _decay_weight(AS_OF, outcome_date)
        assert abs(w - 0.25) < 1e-6

    def test_future_outcome_treated_as_zero_lag(self):
        # outcome_date > as_of: delta_days clamped to 0, weight=1.0
        future = AS_OF + timedelta(days=10)
        w = _decay_weight(AS_OF, future)
        assert abs(w - 1.0) < 1e-9


class TestStanceToProb:
    def test_plus_one_stance_with_full_confidence(self):
        # binary=1, confidence=1.0 → p = (1*1 + 1) / 2 = 1.0
        p = _stance_to_prob(1.0 * 1.0)
        assert abs(p - 1.0) < 1e-9

    def test_minus_one_stance_with_full_confidence(self):
        p = _stance_to_prob(-1.0 * 1.0)
        assert abs(p - 0.0) < 1e-9

    def test_zero_stance(self):
        p = _stance_to_prob(0.0)
        assert abs(p - 0.5) < 1e-9


class TestOutcomeToProb:
    def test_positive(self):
        assert _outcome_to_prob(1) == 1.0

    def test_negative(self):
        assert _outcome_to_prob(-1) == 0.0

    def test_no_call(self):
        assert _outcome_to_prob(0) == 0.5


# ---------------------------------------------------------------------------
# recency_weighted_brier
# ---------------------------------------------------------------------------

class TestRecencyWeightedBrier:
    def test_all_abstain_returns_none(self):
        outcomes = [_make_outcome(abstained=True)]
        dates = [_utc(1)]
        result = recency_weighted_brier(outcomes, dates, AS_OF)
        assert result is None

    def test_length_mismatch_raises(self):
        outcomes = [_make_outcome()]
        dates = []
        with pytest.raises(ValueError, match="equal length"):
            recency_weighted_brier(outcomes, dates, AS_OF)

    def test_single_perfect_outcome(self):
        # Advisor was confidently RIGHT: stance=+1.0, confidence=1.0, binary=+1
        # → p_hat=1.0, p_outcome=1.0, BS=0.0.  Explicit stance encodes "right".
        outcomes = [_make_outcome(binary=1, confidence=1.0, stance_score=1.0)]
        dates = [AS_OF]
        bs = recency_weighted_brier(outcomes, dates, AS_OF)
        assert bs is not None
        assert abs(bs) < 1e-6  # BS ≈ 0

    def test_single_worst_outcome(self):
        # #5a: forecast = advisor's ACTUAL stance, scored vs realized binary.
        # Advisor was confidently LONG (stance=+1, confidence=1.0) but the name
        # went DOWN (binary=-1): p_hat=1.0, p_outcome=0.0 → BS=(1.0-0.0)^2=1.0.
        outcome = ResolvedOutcome(
            idea_id="i1",
            advisor_id="A1.t",
            ticker="X",
            alpha_bps=-50.0,
            binary=-1,               # actual outcome: down
            advisor_confidence=1.0,
            stance_score=1.0,        # advisor predicted LONG with full conviction
            abstained=False,
            horizon_days=30,
            label_kind="normal",
        )
        bs = recency_weighted_brier([outcome], [AS_OF], AS_OF)
        assert bs is not None
        assert abs(bs - 1.0) < 1e-6  # worst possible Brier score

    def test_26_week_decay_downweights_old_outcomes(self):
        """Old outcomes should contribute less to the weighted average."""
        # Two outcomes: recent (good) and old (bad)
        recent_outcome = _make_outcome(binary=1, confidence=1.0, idea_id="i1")
        old_outcome = _make_outcome(binary=-1, confidence=1.0, idea_id="i2")
        # For old_outcome: binary=-1, confidence=1.0 → stance = -1*1 = -1 → p_hat=0.0
        # outcome=-1 → p=0.0 → BS=0.0 (correctly predicted)
        # We need the recent outcome to be wrong and old to be right to see decay effect

        # Let's just test that providing an old date gives lower weight
        recent_date = _utc(0)    # today
        old_date = _utc(364)     # ~2 half-lives ago

        # Same outcome but different dates: old one should be weighted less.
        # `good` is a confidently-RIGHT call (explicit stance=+1.0) → BS=0.
        good = _make_outcome(binary=1, confidence=1.0, stance_score=1.0)
        bad_confidence_outcome = ResolvedOutcome(
            idea_id="i2",
            advisor_id="A1.test",
            ticker="AAPL",
            alpha_bps=-50.0,
            binary=1,
            advisor_confidence=0.0,  # zero confidence → stance*conf=0 → p_hat=0.5 → BS=0.25
            stance_score=1.0,
            abstained=False,
            horizon_days=30,
            label_kind="normal",
        )
        # Recent "bad" (0 confidence) and old "bad" — old should matter less
        bs_only_recent = recency_weighted_brier([bad_confidence_outcome], [recent_date], AS_OF)
        bs_only_old = recency_weighted_brier([bad_confidence_outcome], [old_date], AS_OF)
        # Both should be 0.25 (BS independent of date when normalized)
        # But the weight changes; since we normalize by total weight, the single-outcome
        # BS is the same. The effect shows when mixing outcomes.
        assert bs_only_recent is not None
        assert bs_only_old is not None
        # Both should be equal (single outcome, just weighted differently then normalized)
        assert abs(bs_only_recent - bs_only_old) < 1e-9

        # With two outcomes: one perfect recent + one zero-confidence old
        bs_mix = recency_weighted_brier(
            [good, bad_confidence_outcome],
            [recent_date, old_date],
            AS_OF,
        )
        # The recent perfect outcome (BS=0) should dominate
        assert bs_mix is not None
        # BS should be closer to 0 (good recent outcome) than 0.25 (bad old outcome)
        assert bs_mix < 0.25


# ---------------------------------------------------------------------------
# brier_skill_score
# ---------------------------------------------------------------------------

class TestBrierSkillScore:
    def test_no_outcomes_returns_none(self):
        result = brier_skill_score([], [], AS_OF)
        assert result is None

    def test_all_abstain_returns_none(self):
        result = brier_skill_score(
            [_make_outcome(abstained=True)],
            [AS_OF],
            AS_OF,
        )
        assert result is None

    def test_perfect_prediction_returns_positive_bss(self):
        # Confidently RIGHT: stance=+1.0, confidence=1.0, binary=+1 → BS=0 → BSS=1.0
        outcome = _make_outcome(binary=1, confidence=1.0, stance_score=1.0)
        bss = brier_skill_score([outcome], [AS_OF], AS_OF)
        assert bss is not None
        assert bss > 0

    def test_baseline_prediction_returns_zero(self):
        # confidence=0.0 → stance*conf=0 → p_hat=0.5 → BS=BS_REF → BSS=0
        outcome = ResolvedOutcome(
            idea_id="i1",
            advisor_id="A1.t",
            ticker="X",
            alpha_bps=25.0,
            binary=1,
            advisor_confidence=0.0,
            stance_score=1.0,
            abstained=False,
            horizon_days=30,
            label_kind="normal",
        )
        bss = brier_skill_score([outcome], [AS_OF], AS_OF)
        # p_hat = (1*0 + 1) / 2 = 0.5; p_outcome=1.0; BS=(0.5-1.0)^2=0.25=BS_REF
        # BSS = 1 - 0.25/0.25 = 0.0
        assert bss is not None
        assert abs(bss) < 1e-6

    def test_wrong_direction_returns_negative_bss(self):
        # #5a headline: a confidently-WRONG advisor now earns BSS < 0 (was
        # structurally impossible under the binary-reconstructed forecast).
        # Advisor predicted LONG (stance=+0.9, conf=0.8) but the name went DOWN
        # (binary=-1): p_hat=_stance_to_prob(0.72)=0.86, p_outcome=0.0,
        # BS=0.74 > BS_REF=0.25 → BSS = 1 - 0.74/0.25 ≈ -1.96 < 0.
        wrong = ResolvedOutcome(
            idea_id="i1",
            advisor_id="A1.t",
            ticker="X",
            alpha_bps=-80.0,
            binary=-1,               # actual outcome: down
            advisor_confidence=0.8,
            stance_score=0.9,        # advisor said LONG
            abstained=False,
            horizon_days=30,
            label_kind="normal",
        )
        bss = brier_skill_score([wrong], [AS_OF], AS_OF)
        assert bss is not None
        assert bss < 0.0

        # Control: a confidently-RIGHT advisor earns BSS > 0.
        right = ResolvedOutcome(
            idea_id="i2",
            advisor_id="A1.t",
            ticker="X",
            alpha_bps=80.0,
            binary=1,                # actual outcome: up
            advisor_confidence=0.9,
            stance_score=0.9,        # advisor said LONG, and was right
            abstained=False,
            horizon_days=30,
            label_kind="normal",
        )
        bss_right = brier_skill_score([right], [AS_OF], AS_OF)
        assert bss_right is not None
        assert bss_right > 0.0


# ---------------------------------------------------------------------------
# Audit fix: Finding P1b — binary==0 outcomes excluded from Brier loop
# ---------------------------------------------------------------------------

class TestNoCallOutcomesExcluded:
    """Regression tests for audit finding P1b: binary==0 (no-call) must not
    inflate Brier skill by awarding a free perfect score (BS=0.0)."""

    def test_binary_zero_single_outcome_returns_none(self) -> None:
        """A roster of only binary==0 (no-call) outcomes gives no scorable data → None."""
        outcomes = [_make_outcome(binary=0, confidence=0.5)]
        dates = [AS_OF]
        result = recency_weighted_brier(outcomes, dates, AS_OF)
        # binary==0 is treated as abstention: no scorable outcomes → None
        assert result is None

    def test_binary_zero_excluded_like_abstention(self) -> None:
        """binary==0 rows must be skipped in the Brier loop (same as abstained=True).
        Before fix: p_hat=0.5, p_outcome=0.5 → BS=0.0 → free perfect score.
        After fix: row is skipped entirely."""
        no_call_outcome = _make_outcome(binary=0, confidence=0.5)
        dates = [AS_OF]
        result_no_call = recency_weighted_brier([no_call_outcome], dates, AS_OF)
        # binary==0 excluded → None (no scorable rows)
        assert result_no_call is None

    def test_binary_zero_does_not_inflate_bss(self) -> None:
        """Mixing binary==0 rows with real outcomes must not pull BSS artificially high.
        Before fix: no-call rows added BS=0.0 entries (free skill), inflating average.
        After fix: they're excluded from the sum entirely."""
        # One mediocre real outcome: binary=1, confidence=0.0 → BS=0.25 → BSS=0.0
        mediocre = ResolvedOutcome(
            idea_id="i1",
            advisor_id="A1.t",
            ticker="X",
            alpha_bps=0.0,
            binary=1,
            advisor_confidence=0.0,   # zero confidence → BS=0.25 → BSS=0
            stance_score=1.0,
            abstained=False,
            horizon_days=30,
            label_kind="normal",
        )
        # Several no-call rows: before fix these gave BS=0.0 (inflating skill)
        no_calls = [_make_outcome(binary=0, confidence=0.5) for _ in range(10)]
        all_outcomes = [mediocre] + no_calls
        all_dates = [AS_OF] * len(all_outcomes)

        bs_mixed = recency_weighted_brier(all_outcomes, all_dates, AS_OF)
        bs_mediocre_only = recency_weighted_brier([mediocre], [AS_OF], AS_OF)

        assert bs_mixed is not None
        assert bs_mediocre_only is not None
        # After fix: no-call rows excluded → BS should equal the mediocre-only result
        assert abs(bs_mixed - bs_mediocre_only) < 1e-9, (
            f"Expected {bs_mediocre_only}, got {bs_mixed}: "
            "binary==0 rows are inflating Brier score if they differ"
        )

    def test_bss_binary_zero_all_no_calls_returns_none(self) -> None:
        """brier_skill_score with only binary==0 outcomes → None (no scorable data)."""
        outcomes = [_make_outcome(binary=0, confidence=0.5) for _ in range(5)]
        dates = [AS_OF] * 5
        result = brier_skill_score(outcomes, dates, AS_OF)
        assert result is None


# ---------------------------------------------------------------------------
# E1: out-of-range stance/confidence is clamped (no BSS blowup, no mute)
# ---------------------------------------------------------------------------

class TestClampOutOfRange:
    """An out-of-range stance/confidence must not blow BS past 1.0 (which would
    drive BSS to large negatives and permanently mute the advisor)."""

    def test_resolved_outcome_clamps_at_construction(self) -> None:
        oc = ResolvedOutcome(
            idea_id="i", advisor_id="A1.t", ticker="X",
            alpha_bps=10.0, binary=1,
            advisor_confidence=5.0,   # out of [0,1]
            stance_score=3.0,         # out of [-1,1]
            abstained=False, horizon_days=30, label_kind="normal",
        )
        assert oc.stance_score == 1.0
        assert oc.advisor_confidence == 1.0

        oc2 = ResolvedOutcome(
            idea_id="i", advisor_id="A1.t", ticker="X",
            alpha_bps=-10.0, binary=-1,
            advisor_confidence=-2.0, stance_score=-9.0,
            abstained=False, horizon_days=30, label_kind="normal",
        )
        assert oc2.stance_score == -1.0
        assert oc2.advisor_confidence == 0.0

    def test_out_of_range_bss_stays_bounded(self) -> None:
        """Even a confidently-WRONG out-of-range row stays BSS >= -3 (not -8)."""
        wrong = ResolvedOutcome(
            idea_id="i", advisor_id="A1.t", ticker="X",
            alpha_bps=-80.0, binary=-1,        # name went down
            advisor_confidence=9.0,            # absurd confidence
            stance_score=7.0,                  # absurd LONG stance
            abstained=False, horizon_days=30, label_kind="normal",
        )
        bss = brier_skill_score([wrong], [AS_OF], AS_OF)
        assert bss is not None
        # Worst case after clamp: p_hat=1.0, p_outcome=0.0 → BS=1.0 → BSS=1-1/0.25=-3.
        assert bss >= -3.0 - 1e-9
        # Sanity: still negative (was wrong), just not the -8 blowup.
        assert bss < 0.0

    def test_brier_clamp_independent_of_construction(self) -> None:
        """The Brier helpers clamp defensively too (in case a raw dict bypasses
        ResolvedOutcome construction).  We verify by patching the frozen fields
        AFTER construction via object.__setattr__ to simulate a corrupt row."""
        oc = _make_outcome(binary=1, confidence=1.0, stance_score=1.0)
        object.__setattr__(oc, "stance_score", 9.0)
        object.__setattr__(oc, "advisor_confidence", 9.0)
        bss = brier_skill_score([oc], [AS_OF], AS_OF)
        assert bss is not None
        # Clamped to p_hat=1.0, p_outcome=1.0 → BS=0 → BSS=1.0 (perfect, right).
        assert abs(bss - 1.0) < 1e-9
