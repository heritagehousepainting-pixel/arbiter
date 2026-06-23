"""Tests for arbiter.tips.account_scorer — account credibility scoring.

Verifies:
  - Block-listed account → score=0.0, flagged=True.
  - Account with >= PUMP_STRIKE_THRESHOLD strikes → score=0.0, flagged=True.
  - Low follower count → down-scored (deduction applied).
  - Young account (< MIN_ACCOUNT_AGE_DAYS) → down-scored.
  - Unknown/default account → score=0.5.
  - Fully credible account (high followers, old, no strikes) → score=0.5 base
    (MVP default — no positive booster in Phase-6; scored up from base is a
    Wave-C enhancement).
  - Combined deductions do not go below 0.0.
  - score_account() convenience function mirrors AccountScorer.score().
  - No datetime.now() anywhere — all timestamps supplied externally.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from arbiter.tips.account_scorer import (
    MIN_ACCOUNT_AGE_DAYS,
    MIN_FOLLOWERS,
    PUMP_STRIKE_THRESHOLD,
    AccountScore,
    AccountScorer,
    score_account,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)


def _old_account() -> datetime:
    """Account created more than MIN_ACCOUNT_AGE_DAYS ago."""
    return _AS_OF - timedelta(days=MIN_ACCOUNT_AGE_DAYS + 30)


def _young_account() -> datetime:
    """Account created less than MIN_ACCOUNT_AGE_DAYS ago."""
    return _AS_OF - timedelta(days=MIN_ACCOUNT_AGE_DAYS - 10)


# ---------------------------------------------------------------------------
# AccountScorer
# ---------------------------------------------------------------------------

class TestAccountScorer:
    def setup_method(self) -> None:
        self.scorer = AccountScorer()

    # ----- Default / unknown account -----

    def test_default_account_score_is_half(self) -> None:
        """Default (no deductions) gives 0.5 — requires sufficient followers."""
        result = self.scorer.score(
            "@decent_unknown",
            as_of=_AS_OF,
            follower_count=MIN_FOLLOWERS,  # at threshold — no follower deduction
        )
        assert result.score == pytest.approx(0.5)
        assert result.flagged is False
        assert result.account == "@decent_unknown"

    def test_zero_follower_account_is_deducted(self) -> None:
        """An account with 0 followers (the literal default) receives a deduction."""
        result = self.scorer.score("@ghost", as_of=_AS_OF)
        # 0 followers < MIN_FOLLOWERS → 0.5 - 0.2 = 0.3
        assert result.score == pytest.approx(0.3, abs=1e-9)
        assert result.flagged is False

    # ----- Block list -----

    def test_block_listed_account_score_zero(self) -> None:
        scorer = AccountScorer(block_list=frozenset({"@manipulator"}))
        result = scorer.score("@manipulator", as_of=_AS_OF)
        assert result.score == 0.0
        assert result.flagged is True
        assert any("block list" in r for r in result.reasons)

    def test_block_listed_account_with_good_stats_still_zero(self) -> None:
        """Block list overrides all other signals."""
        scorer = AccountScorer(block_list=frozenset({"@bigname"}))
        result = scorer.score(
            "@bigname",
            as_of=_AS_OF,
            follower_count=1_000_000,
            account_created_at=_old_account(),
            pump_strike_count=0,
        )
        assert result.score == 0.0
        assert result.flagged is True

    def test_non_block_listed_account_not_flagged(self) -> None:
        scorer = AccountScorer(block_list=frozenset({"@manipulator"}))
        result = scorer.score("@legit", as_of=_AS_OF)
        assert result.flagged is False

    # ----- Pump strikes -----

    def test_pump_strike_at_threshold_is_zero(self) -> None:
        result = self.scorer.score(
            "@pumper",
            as_of=_AS_OF,
            pump_strike_count=PUMP_STRIKE_THRESHOLD,
        )
        assert result.score == 0.0
        assert result.flagged is True
        assert any("pump strike" in r for r in result.reasons)

    def test_pump_strike_above_threshold_is_zero(self) -> None:
        result = self.scorer.score(
            "@serial_pumper",
            as_of=_AS_OF,
            pump_strike_count=PUMP_STRIKE_THRESHOLD + 5,
        )
        assert result.score == 0.0
        assert result.flagged is True

    def test_one_pump_strike_below_threshold_not_zero(self) -> None:
        result = self.scorer.score(
            "@maybe_pumper",
            as_of=_AS_OF,
            pump_strike_count=PUMP_STRIKE_THRESHOLD - 1,
        )
        # Should not be floored to zero by strike rule alone
        assert result.score > 0.0
        assert result.flagged is False

    # ----- Low followers -----

    def test_low_follower_count_deduction(self) -> None:
        result = self.scorer.score(
            "@newbie",
            as_of=_AS_OF,
            follower_count=MIN_FOLLOWERS - 1,
        )
        # Base 0.5 - 0.2 deduction = 0.3
        assert result.score == pytest.approx(0.3, abs=1e-9)
        assert any("follower" in r for r in result.reasons)

    def test_follower_count_zero_explicit_deduction(self) -> None:
        result = self.scorer.score("@ghost2", as_of=_AS_OF, follower_count=0)
        assert result.score == pytest.approx(0.3, abs=1e-9)

    def test_follower_count_at_min_no_deduction(self) -> None:
        result = self.scorer.score(
            "@ok",
            as_of=_AS_OF,
            follower_count=MIN_FOLLOWERS,
        )
        # At exactly the threshold, no deduction applied
        assert result.score == pytest.approx(0.5, abs=1e-9)

    # ----- Young account -----

    def test_young_account_deduction(self) -> None:
        result = self.scorer.score(
            "@youngling",
            as_of=_AS_OF,
            follower_count=MIN_FOLLOWERS,
            account_created_at=_young_account(),
        )
        # Base 0.5 - 0.2 (young) = 0.3
        assert result.score == pytest.approx(0.3, abs=1e-9)
        assert any("Young account" in r or "young" in r.lower() for r in result.reasons)

    def test_old_account_no_age_deduction(self) -> None:
        result = self.scorer.score(
            "@veteran",
            as_of=_AS_OF,
            follower_count=MIN_FOLLOWERS,
            account_created_at=_old_account(),
        )
        assert result.score == pytest.approx(0.5, abs=1e-9)

    # ----- Combined deductions clamped to 0.0 -----

    def test_combined_deductions_floor_at_zero(self) -> None:
        """Low followers + young account → 0.5 - 0.2 - 0.2 = 0.1 (not negative)."""
        result = self.scorer.score(
            "@brand_new_nobody",
            as_of=_AS_OF,
            follower_count=0,
            account_created_at=_young_account(),
        )
        assert result.score == pytest.approx(0.1, abs=1e-9)
        assert result.score >= 0.0

    def test_score_never_negative(self) -> None:
        """Score is always >= 0.0 regardless of deductions."""
        result = self.scorer.score(
            "@worst_case",
            as_of=_AS_OF,
            follower_count=0,
            account_created_at=_young_account(),
            pump_strike_count=PUMP_STRIKE_THRESHOLD - 1,
        )
        assert result.score >= 0.0

    # ----- AccountScore dataclass -----

    def test_result_is_account_score(self) -> None:
        result = self.scorer.score("@user", as_of=_AS_OF)
        assert isinstance(result, AccountScore)
        assert result.account == "@user"

    def test_reasons_is_list(self) -> None:
        result = self.scorer.score("@user", as_of=_AS_OF, follower_count=0)
        assert isinstance(result.reasons, list)
        assert len(result.reasons) >= 1

    def test_no_deductions_empty_reasons(self) -> None:
        result = self.scorer.score(
            "@decent",
            as_of=_AS_OF,
            follower_count=MIN_FOLLOWERS + 100,
            account_created_at=_old_account(),
            pump_strike_count=0,
        )
        assert result.reasons == []

    # ----- Manipulative account end-to-end -----

    def test_manipulative_account_down_scored(self) -> None:
        """A manipulative/low-rep account gets a lower score than default."""
        legit = self.scorer.score(
            "@institutional_pm",
            as_of=_AS_OF,
            follower_count=50_000,
            account_created_at=_old_account(),
            pump_strike_count=0,
        )
        manipulative = self.scorer.score(
            "@pump_lord",
            as_of=_AS_OF,
            follower_count=5,
            account_created_at=_young_account(),
            pump_strike_count=PUMP_STRIKE_THRESHOLD - 1,
        )
        assert manipulative.score < legit.score


# ---------------------------------------------------------------------------
# score_account() convenience function
# ---------------------------------------------------------------------------

class TestScoreAccountConvenience:
    def test_returns_account_score(self) -> None:
        result = score_account("@user", as_of=_AS_OF)
        assert isinstance(result, AccountScore)

    def test_block_list_forwarded(self) -> None:
        result = score_account(
            "@badguy",
            as_of=_AS_OF,
            block_list=frozenset({"@badguy"}),
        )
        assert result.score == 0.0
        assert result.flagged is True

    def test_default_block_list_is_none(self) -> None:
        result = score_account("@nobody", as_of=_AS_OF)
        assert result.flagged is False

    def test_mirrors_scorer_output(self) -> None:
        scorer = AccountScorer()
        direct = scorer.score("@same", as_of=_AS_OF, follower_count=0)
        via_fn = score_account("@same", as_of=_AS_OF, follower_count=0)
        assert direct.score == via_fn.score
        assert direct.flagged == via_fn.flagged
        assert direct.reasons == via_fn.reasons
