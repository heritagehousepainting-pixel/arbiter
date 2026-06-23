"""Tests for arbiter.orchestrator.cycle — run_cycle integration (Lane 13).

Covers:
- Basic happy path: opinions gathered → fuse → decide → submit → MONITORED
- Crashing advisor yields null opinion; cycle continues for other advisors
- Dedupe: same (ticker, bucket) skipped; different bucket allowed
- No valid opinions → no fusion/decision (but no crash)
- Fuse raises → error logged, idea not advanced
- Decide raises → error logged, idea not advanced
- Submit returns False → idea stays FINAL_DECIDED
- CycleResult statistics are accurate
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from arbiter.contract.opinion import Opinion
from arbiter.contract.seams import FusionOutput, Idea, PaperOrder
from arbiter.data.clock import BacktestClock
from arbiter.orchestrator.cycle import CycleResult, run_cycle
from arbiter.orchestrator.idea import make_idea
from arbiter.orchestrator.lifecycle import transition
from arbiter.types import (
    ConfidenceSource,
    HorizonBucket,
    IdeaState,
    OrderSide,
)

_UTC = timezone.utc


# ---------------------------------------------------------------------------
# Stub factories
# ---------------------------------------------------------------------------

def _clock():
    return BacktestClock(datetime(2024, 6, 1, tzinfo=_UTC))


def _opinion(advisor_id: str = "A1.test", horizon_days: int = 10) -> Opinion:
    return Opinion(
        advisor_id=advisor_id,
        ticker="AAPL",
        stance_score=0.6,
        confidence=0.8,
        confidence_source=ConfidenceSource.SELF_REPORTED,
        horizon_days=horizon_days,
        as_of=datetime(2024, 1, 1, tzinfo=_UTC),
        rationale="test",
        source_fingerprint="fp1",
        run_group_id="rg1",
    )


def _fusion_output(bucket: HorizonBucket = HorizonBucket.SHORT) -> FusionOutput:
    return FusionOutput(
        bucket=bucket,
        conviction=0.7,
        dispersion=0.1,
        effective_n=2.0,
        n_opinions=2,
        advisor_contributions={"A1.test": 0.7},
        vetoes=[],
        cold_start=False,
    )


def _paper_order() -> PaperOrder:
    from datetime import date
    return PaperOrder(
        order_id="01ARYZ6S41TSV4RRFFQ69G5FAV",
        dedup_hash="abc123",
        ticker="AAPL",
        side=OrderSide.BUY,
        qty=10.0,
        horizon_bucket=HorizonBucket.SHORT,
        entry_date=date(2024, 6, 1),
        advisor_signature="sig_test",
        exits={"stop_loss": 90.0, "horizon_expiry": date(2024, 6, 11), "conviction_reversal": 0.0},
    )


def _make_idea(ticker: str = "AAPL", horizon_days: int = 10) -> Idea:
    return make_idea(ticker, "thesis", horizon_days=horizon_days, as_of=datetime(2024, 1, 1, tzinfo=_UTC))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestCycleHappyPath:
    def test_full_cycle_advances_to_monitored(self):
        idea = _make_idea()
        op = _opinion()

        advisor_map = {"A1.test": lambda: op}
        fuse = lambda opinions, bucket: _fusion_output(bucket)
        decide = lambda fo, idea: _paper_order()
        submit = lambda order: True

        result = run_cycle([idea], advisor_map, fuse, decide, submit, _clock())

        assert idea.state is IdeaState.MONITORED
        assert result.orders_submitted == 1
        assert result.ideas_processed == 1

    def test_cycle_result_tracks_null_opinions(self):
        idea = _make_idea()
        op = _opinion()

        advisor_map = {
            "A1.good": lambda: op,
            "A1.bad": lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        }
        fuse = lambda opinions, bucket: _fusion_output(bucket)
        decide = lambda fo, idea: _paper_order()
        submit = lambda order: True

        result = run_cycle([idea], advisor_map, fuse, decide, submit, _clock())

        assert result.opinions_null == 1
        assert result.opinions_gathered == 2

    def test_decide_returns_none_no_order_submitted(self):
        """Policy returning None → no order, idea stays FINAL_DECIDED."""
        idea = _make_idea()
        op = _opinion()

        advisor_map = {"A1.test": lambda: op}
        fuse = lambda opinions, bucket: _fusion_output(bucket)
        decide = lambda fo, idea: None  # no trade
        submit = lambda order: True

        result = run_cycle([idea], advisor_map, fuse, decide, submit, _clock())

        assert result.orders_submitted == 0
        assert idea.state is IdeaState.FINAL_DECIDED


# ---------------------------------------------------------------------------
# Fault isolation — crashing advisor doesn't abort cycle
# ---------------------------------------------------------------------------

class TestCycleFaultIsolation:
    def test_crashing_advisor_cycle_continues(self):
        """A crashing advisor yields null opinion; valid advisor's opinion is still fused."""
        idea = _make_idea()
        good_op = _opinion("A1.good")

        fused_opinions = []

        def capturing_fuse(opinions, bucket):
            fused_opinions.extend(opinions)
            return _fusion_output(bucket)

        advisor_map = {
            "A1.good": lambda: good_op,
            "A2.crash": lambda: (_ for _ in ()).throw(RuntimeError("advisor dead")),
        }

        result = run_cycle(
            [idea], advisor_map, capturing_fuse,
            lambda fo, i: _paper_order(), lambda o: True, _clock()
        )

        assert result.opinions_null == 1
        assert result.orders_submitted == 1
        assert good_op in fused_opinions

    def test_all_advisors_crash_no_fusion_no_crash(self):
        """All advisors crash → no valid opinions → no fusion → no order."""
        idea = _make_idea()

        advisor_map = {
            "A1.crash": lambda: (_ for _ in ()).throw(RuntimeError("dead")),
        }
        fuse_calls = []
        fuse = lambda opinions, bucket: (fuse_calls.append(1), _fusion_output(bucket))[1]
        decide = lambda fo, idea: _paper_order()
        submit = lambda order: True

        result = run_cycle([idea], advisor_map, fuse, decide, submit, _clock())

        # No valid opinions for the bucket → fusion not called
        assert result.orders_submitted == 0
        assert len(fuse_calls) == 0


# ---------------------------------------------------------------------------
# Dedupe rules
# ---------------------------------------------------------------------------

class TestCycleDedupe:
    def test_duplicate_ticker_bucket_skipped(self):
        """A duplicate (ticker, bucket) idea is skipped; result counts it."""
        idea1 = _make_idea("AAPL", 10)  # SHORT
        idea2 = _make_idea("AAPL", 15)  # also SHORT — duplicate

        # idea1 is the "active" idea
        transition(idea1, IdeaState.GATHERING)

        op = _opinion()
        advisor_map = {"A1.test": lambda: op}
        fuse = lambda opinions, bucket: _fusion_output(bucket)
        decide = lambda fo, idea: _paper_order()
        submit = lambda order: True

        result = run_cycle(
            [idea2], advisor_map, fuse, decide, submit, _clock(),
            active_ideas=[idea1],
        )

        assert result.ideas_skipped_dedupe == 1
        assert result.ideas_processed == 0
        assert idea2.state is IdeaState.NASCENT  # unchanged

    def test_different_bucket_on_same_ticker_allowed(self):
        """Different horizon buckets on same ticker are NOT duplicates."""
        idea_short = _make_idea("AAPL", 10)   # SHORT
        idea_long = _make_idea("AAPL", 200)   # LONG

        # idea_short is active GATHERING
        transition(idea_short, IdeaState.GATHERING)

        op_long = Opinion(
            advisor_id="A1.test",
            ticker="AAPL",
            stance_score=0.5,
            confidence=0.8,
            confidence_source=ConfidenceSource.SELF_REPORTED,
            horizon_days=200,
            as_of=datetime(2024, 1, 1, tzinfo=_UTC),
            rationale="test",
            source_fingerprint="fp2",
            run_group_id="rg2",
        )

        advisor_map = {"A1.test": lambda: op_long}
        fuse = lambda opinions, bucket: FusionOutput(
            bucket=bucket, conviction=0.6, dispersion=0.1,
            effective_n=1.0, n_opinions=1,
            advisor_contributions={"A1.test": 0.6}, vetoes=[], cold_start=False,
        )
        decide = lambda fo, idea: _paper_order()
        submit = lambda order: True

        result = run_cycle(
            [idea_long], advisor_map, fuse, decide, submit, _clock(),
            active_ideas=[idea_short],
        )

        assert result.ideas_skipped_dedupe == 0
        assert result.ideas_processed == 1
        assert idea_long.state is IdeaState.MONITORED

    def test_intra_batch_dedupe(self):
        """Two ideas in same batch with same (ticker, bucket) — second is skipped."""
        idea1 = _make_idea("AAPL", 10)
        idea2 = _make_idea("AAPL", 12)  # same SHORT bucket

        op = _opinion()
        advisor_map = {"A1.test": lambda: op}
        fuse = lambda opinions, bucket: _fusion_output(bucket)
        decide = lambda fo, idea: _paper_order()
        submit = lambda order: True

        result = run_cycle([idea1, idea2], advisor_map, fuse, decide, submit, _clock())

        assert result.ideas_skipped_dedupe == 1
        assert result.ideas_processed == 1


# ---------------------------------------------------------------------------
# Error handling in fuse / decide / submit
# ---------------------------------------------------------------------------

class TestCycleErrorHandling:
    def test_fuse_raises_is_recorded_not_fatal(self):
        idea = _make_idea()
        op = _opinion()

        def bad_fuse(opinions, bucket):
            raise ValueError("fusion exploded")

        advisor_map = {"A1.test": lambda: op}
        result = run_cycle(
            [idea], advisor_map, bad_fuse,
            lambda fo, i: _paper_order(), lambda o: True, _clock()
        )

        assert result.orders_submitted == 0
        assert len(result.errors) == 1
        assert "fusion" in result.errors[0].lower() or "fuse" in result.errors[0].lower() or "fusion exploded" in result.errors[0].lower()

    def test_decide_raises_is_recorded_not_fatal(self):
        idea = _make_idea()
        op = _opinion()

        def bad_decide(fo, idea):
            raise RuntimeError("policy crashed")

        advisor_map = {"A1.test": lambda: op}
        result = run_cycle(
            [idea], advisor_map, lambda ops, bucket: _fusion_output(bucket),
            bad_decide, lambda o: True, _clock()
        )

        assert result.orders_submitted == 0
        assert len(result.errors) >= 1

    def test_submit_returns_false_not_advanced_to_monitored(self):
        idea = _make_idea()
        op = _opinion()

        advisor_map = {"A1.test": lambda: op}
        fuse = lambda opinions, bucket: _fusion_output(bucket)
        decide = lambda fo, idea: _paper_order()
        submit = lambda order: False  # submission fails

        result = run_cycle([idea], advisor_map, fuse, decide, submit, _clock())

        assert result.orders_submitted == 0
        assert idea.state is IdeaState.FINAL_DECIDED


# ---------------------------------------------------------------------------
# CycleResult statistics
# ---------------------------------------------------------------------------

class TestCycleResultStats:
    def test_empty_input(self):
        result = run_cycle(
            [], {}, lambda ops, b: _fusion_output(b),
            lambda fo, i: None, lambda o: True, _clock()
        )
        assert result.ideas_processed == 0
        assert result.orders_submitted == 0
        assert result.opinions_gathered == 0
