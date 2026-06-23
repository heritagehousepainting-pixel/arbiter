"""Tests for arbiter.orchestrator.lifecycle — FSM transitions (Lane 13).

Covers:
- All legal transitions advance state correctly
- All illegal transitions raise IllegalTransitionError
- Terminal states (CLOSED, ABANDONED) have no outbound transitions
- abandon() convenience helper
- ABANDONED is reachable from any pre-EXECUTED state
- EXECUTED+ states cannot be abandoned
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from arbiter.orchestrator.idea import make_idea
from arbiter.orchestrator.lifecycle import (
    LEGAL_TRANSITIONS,
    IllegalTransitionError,
    abandon,
    can_transition,
    transition,
)
from arbiter.types import IdeaState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = timezone.utc


def _idea(state: IdeaState = IdeaState.NASCENT):
    """Create a test idea in the given state."""
    idea = make_idea(
        ticker="AAPL",
        thesis="Test thesis",
        horizon_days=10,
        as_of=datetime(2024, 1, 1, tzinfo=_UTC),
        state=IdeaState.NASCENT,
    )
    idea.state = state  # set directly for test setup
    return idea


# ---------------------------------------------------------------------------
# Legal transitions
# ---------------------------------------------------------------------------

class TestLegalTransitions:
    def test_nascent_to_gathering(self):
        idea = _idea(IdeaState.NASCENT)
        result = transition(idea, IdeaState.GATHERING)
        assert idea.state is IdeaState.GATHERING
        assert result is idea  # same object

    def test_gathering_to_provisional_decided(self):
        idea = _idea(IdeaState.GATHERING)
        transition(idea, IdeaState.PROVISIONAL_DECIDED)
        assert idea.state is IdeaState.PROVISIONAL_DECIDED

    def test_provisional_to_final_decided(self):
        idea = _idea(IdeaState.PROVISIONAL_DECIDED)
        transition(idea, IdeaState.FINAL_DECIDED)
        assert idea.state is IdeaState.FINAL_DECIDED

    def test_final_to_executed(self):
        idea = _idea(IdeaState.FINAL_DECIDED)
        transition(idea, IdeaState.EXECUTED)
        assert idea.state is IdeaState.EXECUTED

    def test_executed_to_monitored(self):
        idea = _idea(IdeaState.EXECUTED)
        transition(idea, IdeaState.MONITORED)
        assert idea.state is IdeaState.MONITORED

    def test_monitored_to_outcome_ready(self):
        idea = _idea(IdeaState.MONITORED)
        transition(idea, IdeaState.OUTCOME_READY)
        assert idea.state is IdeaState.OUTCOME_READY

    def test_outcome_ready_to_closed(self):
        idea = _idea(IdeaState.OUTCOME_READY)
        transition(idea, IdeaState.CLOSED)
        assert idea.state is IdeaState.CLOSED

    def test_nascent_to_abandoned(self):
        idea = _idea(IdeaState.NASCENT)
        transition(idea, IdeaState.ABANDONED)
        assert idea.state is IdeaState.ABANDONED

    def test_gathering_to_abandoned(self):
        idea = _idea(IdeaState.GATHERING)
        transition(idea, IdeaState.ABANDONED)
        assert idea.state is IdeaState.ABANDONED

    def test_provisional_to_abandoned(self):
        idea = _idea(IdeaState.PROVISIONAL_DECIDED)
        transition(idea, IdeaState.ABANDONED)
        assert idea.state is IdeaState.ABANDONED

    def test_final_to_abandoned(self):
        idea = _idea(IdeaState.FINAL_DECIDED)
        transition(idea, IdeaState.ABANDONED)
        assert idea.state is IdeaState.ABANDONED


# ---------------------------------------------------------------------------
# Illegal transitions — forwards-skip
# ---------------------------------------------------------------------------

class TestIllegalTransitions:
    def test_nascent_directly_to_final_decided(self):
        idea = _idea(IdeaState.NASCENT)
        with pytest.raises(IllegalTransitionError) as exc_info:
            transition(idea, IdeaState.FINAL_DECIDED)
        assert exc_info.value.from_state is IdeaState.NASCENT
        assert exc_info.value.to_state is IdeaState.FINAL_DECIDED

    def test_nascent_directly_to_executed(self):
        idea = _idea(IdeaState.NASCENT)
        with pytest.raises(IllegalTransitionError):
            transition(idea, IdeaState.EXECUTED)

    def test_gathering_directly_to_executed(self):
        idea = _idea(IdeaState.GATHERING)
        with pytest.raises(IllegalTransitionError):
            transition(idea, IdeaState.EXECUTED)

    def test_executed_to_outcome_ready_skip_monitored(self):
        idea = _idea(IdeaState.EXECUTED)
        with pytest.raises(IllegalTransitionError):
            transition(idea, IdeaState.OUTCOME_READY)

    def test_monitored_to_closed_skip_outcome_ready(self):
        idea = _idea(IdeaState.MONITORED)
        with pytest.raises(IllegalTransitionError):
            transition(idea, IdeaState.CLOSED)

    def test_backwards_from_gathering_to_nascent(self):
        idea = _idea(IdeaState.GATHERING)
        with pytest.raises(IllegalTransitionError):
            transition(idea, IdeaState.NASCENT)

    def test_backwards_from_final_to_gathering(self):
        idea = _idea(IdeaState.FINAL_DECIDED)
        with pytest.raises(IllegalTransitionError):
            transition(idea, IdeaState.GATHERING)

    def test_backwards_from_monitored_to_executed(self):
        idea = _idea(IdeaState.MONITORED)
        with pytest.raises(IllegalTransitionError):
            transition(idea, IdeaState.EXECUTED)

    def test_error_carries_idea_id(self):
        idea = _idea(IdeaState.NASCENT)
        with pytest.raises(IllegalTransitionError) as exc_info:
            transition(idea, IdeaState.CLOSED)
        assert idea.idea_id in str(exc_info.value)


# ---------------------------------------------------------------------------
# Terminal states — no outbound transitions
# ---------------------------------------------------------------------------

class TestTerminalStates:
    def test_closed_has_no_outbound(self):
        assert LEGAL_TRANSITIONS[IdeaState.CLOSED] == frozenset()

    def test_abandoned_has_no_outbound(self):
        assert LEGAL_TRANSITIONS[IdeaState.ABANDONED] == frozenset()

    def test_closed_any_transition_raises(self):
        idea = _idea(IdeaState.CLOSED)
        for target in IdeaState:
            with pytest.raises(IllegalTransitionError):
                transition(idea, target)

    def test_abandoned_any_transition_raises(self):
        idea = _idea(IdeaState.ABANDONED)
        for target in IdeaState:
            with pytest.raises(IllegalTransitionError):
                transition(idea, target)

    def test_executed_cannot_be_abandoned(self):
        idea = _idea(IdeaState.EXECUTED)
        with pytest.raises(IllegalTransitionError):
            transition(idea, IdeaState.ABANDONED)

    def test_monitored_cannot_be_abandoned(self):
        idea = _idea(IdeaState.MONITORED)
        with pytest.raises(IllegalTransitionError):
            transition(idea, IdeaState.ABANDONED)


# ---------------------------------------------------------------------------
# can_transition predicate
# ---------------------------------------------------------------------------

class TestCanTransition:
    def test_legal_returns_true(self):
        assert can_transition(IdeaState.NASCENT, IdeaState.GATHERING) is True

    def test_illegal_returns_false(self):
        assert can_transition(IdeaState.NASCENT, IdeaState.EXECUTED) is False

    def test_terminal_all_false(self):
        for target in IdeaState:
            assert can_transition(IdeaState.CLOSED, target) is False
            assert can_transition(IdeaState.ABANDONED, target) is False


# ---------------------------------------------------------------------------
# abandon() helper
# ---------------------------------------------------------------------------

class TestAbandon:
    def test_abandon_from_nascent(self):
        idea = _idea(IdeaState.NASCENT)
        result = abandon(idea)
        assert result.state is IdeaState.ABANDONED

    def test_abandon_from_gathering(self):
        idea = _idea(IdeaState.GATHERING)
        abandon(idea)
        assert idea.state is IdeaState.ABANDONED

    def test_abandon_idempotent_if_already_abandoned(self):
        idea = _idea(IdeaState.ABANDONED)
        result = abandon(idea)  # should not raise
        assert result.state is IdeaState.ABANDONED

    def test_abandon_executed_raises(self):
        idea = _idea(IdeaState.EXECUTED)
        with pytest.raises(IllegalTransitionError):
            abandon(idea)
