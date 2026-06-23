"""Tests for arbiter.orchestrator.outcome_sweep — OUTCOME_READY sweep (Lane 13).

Covers:
- Ideas in MONITORED state at or past horizon are marked OUTCOME_READY
- Ideas not yet at horizon stay MONITORED
- Only MONITORED ideas are inspected (other states ignored)
- OutcomeReadyEvent carries original as_of (not current clock)
- OutcomeReadyEvent carries original horizon_days
- on_ready callback is invoked for each ready event
- on_ready callback exceptions are caught (do not abort sweep)
- Ideas past horizon are correctly transitioned
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from arbiter.data.clock import BacktestClock
from arbiter.orchestrator.idea import make_idea
from arbiter.orchestrator.lifecycle import transition
from arbiter.orchestrator.outcome_sweep import OutcomeReadyEvent, sweep_outcomes
from arbiter.types import IdeaState

_UTC = timezone.utc


def _make_monitored_idea(ticker: str, original_as_of: datetime, horizon_days: int):
    """Create an idea already in MONITORED state."""
    idea = make_idea(
        ticker=ticker,
        thesis="test",
        horizon_days=horizon_days,
        as_of=original_as_of,
    )
    # Advance through required states to reach MONITORED
    transition(idea, IdeaState.GATHERING)
    transition(idea, IdeaState.PROVISIONAL_DECIDED)
    transition(idea, IdeaState.FINAL_DECIDED)
    transition(idea, IdeaState.EXECUTED)
    transition(idea, IdeaState.MONITORED)
    return idea


# ---------------------------------------------------------------------------
# Core sweep logic
# ---------------------------------------------------------------------------

class TestSweepOutcomes:
    def test_marks_elapsed_idea_outcome_ready(self):
        original_as_of = datetime(2024, 1, 1, tzinfo=_UTC)
        horizon_days = 10
        idea = _make_monitored_idea("AAPL", original_as_of, horizon_days)

        # Clock is at as_of + 11 days (past horizon)
        now = original_as_of + timedelta(days=11)
        clock = BacktestClock(now)

        events = sweep_outcomes([idea], clock)

        assert len(events) == 1
        assert idea.state is IdeaState.OUTCOME_READY

    def test_does_not_mark_idea_before_horizon(self):
        original_as_of = datetime(2024, 1, 1, tzinfo=_UTC)
        horizon_days = 10
        idea = _make_monitored_idea("AAPL", original_as_of, horizon_days)

        # Clock is only 5 days past as_of — not yet at horizon
        now = original_as_of + timedelta(days=5)
        clock = BacktestClock(now)

        events = sweep_outcomes([idea], clock)

        assert len(events) == 0
        assert idea.state is IdeaState.MONITORED  # unchanged

    def test_marks_at_exact_horizon(self):
        """At exactly as_of + horizon_days, the idea should be ready."""
        original_as_of = datetime(2024, 1, 1, tzinfo=_UTC)
        horizon_days = 7
        idea = _make_monitored_idea("AAPL", original_as_of, horizon_days)

        now = original_as_of + timedelta(days=7)  # exactly at horizon
        clock = BacktestClock(now)

        events = sweep_outcomes([idea], clock)
        assert len(events) == 1
        assert idea.state is IdeaState.OUTCOME_READY

    def test_only_monitors_monitored_ideas(self):
        """Ideas in other states should not be touched."""
        as_of = datetime(2024, 1, 1, tzinfo=_UTC)
        far_future = as_of + timedelta(days=365)
        clock = BacktestClock(far_future)

        nascent = make_idea("AAPL", "t", horizon_days=1, as_of=as_of)
        gathering = make_idea("MSFT", "t", horizon_days=1, as_of=as_of)
        transition(gathering, IdeaState.GATHERING)

        events = sweep_outcomes([nascent, gathering], clock)
        assert len(events) == 0
        assert nascent.state is IdeaState.NASCENT
        assert gathering.state is IdeaState.GATHERING

    def test_empty_ideas_list(self):
        clock = BacktestClock(datetime(2024, 6, 1, tzinfo=_UTC))
        events = sweep_outcomes([], clock)
        assert events == []


# ---------------------------------------------------------------------------
# OutcomeReadyEvent carries original as_of
# ---------------------------------------------------------------------------

class TestOutcomeReadyEvent:
    def test_event_carries_original_as_of(self):
        """The event must have the idea's ORIGINAL as_of, not the clock's now."""
        original_as_of = datetime(2024, 1, 1, tzinfo=_UTC)
        idea = _make_monitored_idea("AAPL", original_as_of, horizon_days=5)

        current_time = datetime(2024, 6, 1, tzinfo=_UTC)  # much later
        clock = BacktestClock(current_time)

        events = sweep_outcomes([idea], clock)

        assert len(events) == 1
        event = events[0]
        assert event.original_as_of == original_as_of
        assert event.original_as_of != current_time

    def test_event_carries_original_horizon_days(self):
        original_as_of = datetime(2024, 1, 1, tzinfo=_UTC)
        idea = _make_monitored_idea("TSLA", original_as_of, horizon_days=30)

        clock = BacktestClock(original_as_of + timedelta(days=31))
        events = sweep_outcomes([idea], clock)

        assert events[0].horizon_days == 30

    def test_event_carries_idea_id_and_ticker(self):
        as_of = datetime(2024, 1, 1, tzinfo=_UTC)
        idea = _make_monitored_idea("NVDA", as_of, horizon_days=10)
        clock = BacktestClock(as_of + timedelta(days=11))

        events = sweep_outcomes([idea], clock)
        event = events[0]

        assert event.idea_id == idea.idea_id
        assert event.ticker == "NVDA"

    def test_event_type_is_outcome_ready_event(self):
        as_of = datetime(2024, 1, 1, tzinfo=_UTC)
        idea = _make_monitored_idea("SPY", as_of, horizon_days=5)
        clock = BacktestClock(as_of + timedelta(days=6))

        events = sweep_outcomes([idea], clock)
        assert isinstance(events[0], OutcomeReadyEvent)


# ---------------------------------------------------------------------------
# on_ready callback
# ---------------------------------------------------------------------------

class TestOnReadyCallback:
    def test_on_ready_called_for_each_event(self):
        as_of = datetime(2024, 1, 1, tzinfo=_UTC)
        idea1 = _make_monitored_idea("AAPL", as_of, horizon_days=5)
        idea2 = _make_monitored_idea("MSFT", as_of, horizon_days=5)

        clock = BacktestClock(as_of + timedelta(days=6))
        received = []

        sweep_outcomes([idea1, idea2], clock, on_ready=received.append)

        assert len(received) == 2
        tickers = {e.ticker for e in received}
        assert tickers == {"AAPL", "MSFT"}

    def test_on_ready_exception_does_not_abort_sweep(self):
        """A callback that raises must not prevent other events from being processed."""
        as_of = datetime(2024, 1, 1, tzinfo=_UTC)
        idea1 = _make_monitored_idea("AAPL", as_of, horizon_days=5)
        idea2 = _make_monitored_idea("MSFT", as_of, horizon_days=5)

        clock = BacktestClock(as_of + timedelta(days=6))

        call_count = {"n": 0}

        def bad_callback(event: OutcomeReadyEvent) -> None:
            call_count["n"] += 1
            raise RuntimeError("callback exploded")

        # Should not raise; both ideas should be marked ready
        events = sweep_outcomes([idea1, idea2], clock, on_ready=bad_callback)

        assert len(events) == 2
        assert idea1.state is IdeaState.OUTCOME_READY
        assert idea2.state is IdeaState.OUTCOME_READY
        assert call_count["n"] == 2  # both callbacks fired

    def test_no_callback_when_none(self):
        """Passing on_ready=None should work with no errors."""
        as_of = datetime(2024, 1, 1, tzinfo=_UTC)
        idea = _make_monitored_idea("AAPL", as_of, horizon_days=5)
        clock = BacktestClock(as_of + timedelta(days=6))

        events = sweep_outcomes([idea], clock, on_ready=None)
        assert len(events) == 1


# ---------------------------------------------------------------------------
# Mixed bag — some ready, some not
# ---------------------------------------------------------------------------

class TestMixedIdeas:
    def test_only_elapsed_ideas_marked_ready(self):
        as_of = datetime(2024, 1, 1, tzinfo=_UTC)
        idea_short = _make_monitored_idea("AAPL", as_of, horizon_days=5)
        idea_long = _make_monitored_idea("MSFT", as_of, horizon_days=30)

        # Clock at day 6: short elapsed, long not yet
        clock = BacktestClock(as_of + timedelta(days=6))
        events = sweep_outcomes([idea_short, idea_long], clock)

        assert len(events) == 1
        assert events[0].ticker == "AAPL"
        assert idea_short.state is IdeaState.OUTCOME_READY
        assert idea_long.state is IdeaState.MONITORED
