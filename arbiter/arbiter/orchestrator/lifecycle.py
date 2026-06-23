"""Idea FSM lifecycle — Lane 13.

Enforces legal state transitions per INTERFACES.md §7.

FSM graph:
    NASCENT → GATHERING
    GATHERING → PROVISIONAL_DECIDED | ABANDONED
    PROVISIONAL_DECIDED → FINAL_DECIDED | ABANDONED
    FINAL_DECIDED → EXECUTED | ABANDONED
    EXECUTED → MONITORED
    MONITORED → OUTCOME_READY
    OUTCOME_READY → CLOSED

ABANDONED is a terminal sink — no transitions out.
CLOSED is a terminal sink — no transitions out.

All other transitions (including backwards transitions) are illegal and
raise ``IllegalTransitionError``.
"""
from __future__ import annotations

from arbiter.contract.seams import Idea
from arbiter.types import IdeaState


class IllegalTransitionError(ValueError):
    """Raised when an FSM transition is not permitted.

    Carries the from-state, to-state, and idea_id so callers can log context.
    """

    def __init__(
        self,
        from_state: IdeaState,
        to_state: IdeaState,
        idea_id: str,
    ) -> None:
        self.from_state = from_state
        self.to_state = to_state
        self.idea_id = idea_id
        super().__init__(
            f"Illegal FSM transition for idea {idea_id!r}: "
            f"{from_state.value!r} → {to_state.value!r}"
        )


# ---------------------------------------------------------------------------
# Legal transitions table
# ---------------------------------------------------------------------------

#: Maps each state to the set of states it is allowed to transition INTO.
LEGAL_TRANSITIONS: dict[IdeaState, frozenset[IdeaState]] = {
    IdeaState.NASCENT: frozenset({
        IdeaState.GATHERING,
        IdeaState.ABANDONED,
    }),
    IdeaState.GATHERING: frozenset({
        IdeaState.PROVISIONAL_DECIDED,
        IdeaState.ABANDONED,
    }),
    IdeaState.PROVISIONAL_DECIDED: frozenset({
        IdeaState.FINAL_DECIDED,
        IdeaState.ABANDONED,
    }),
    IdeaState.FINAL_DECIDED: frozenset({
        IdeaState.EXECUTED,
        IdeaState.ABANDONED,
    }),
    IdeaState.EXECUTED: frozenset({
        IdeaState.MONITORED,
    }),
    IdeaState.MONITORED: frozenset({
        IdeaState.OUTCOME_READY,
    }),
    IdeaState.OUTCOME_READY: frozenset({
        IdeaState.CLOSED,
    }),
    # Terminal states — no outbound transitions
    IdeaState.CLOSED: frozenset(),
    IdeaState.ABANDONED: frozenset(),
}


def can_transition(from_state: IdeaState, to_state: IdeaState) -> bool:
    """Return True if ``from_state → to_state`` is a legal transition."""
    return to_state in LEGAL_TRANSITIONS.get(from_state, frozenset())


def transition(idea: Idea, to_state: IdeaState) -> Idea:
    """Transition *idea* to *to_state*, mutating ``idea.state`` in-place.

    ``Idea`` has a mutable ``state`` field (the only mutable field per the
    contract).  This function enforces the FSM rules then applies the mutation.

    Parameters
    ----------
    idea:
        The Idea to transition.
    to_state:
        Target state.

    Returns
    -------
    Idea
        The same object (mutated in-place) for convenience in chained calls.

    Raises
    ------
    IllegalTransitionError
        If the transition is not permitted by the FSM.
    """
    if not can_transition(idea.state, to_state):
        raise IllegalTransitionError(idea.state, to_state, idea.idea_id)
    idea.state = to_state
    return idea


def abandon(idea: Idea) -> Idea:
    """Convenience: transition idea to ABANDONED if it is in a pre-EXECUTED state.

    If the idea is already ABANDONED this is a no-op (idempotent).
    If the idea is in EXECUTED or later, this raises ``IllegalTransitionError``
    because abandonment is only valid in the gathering/decision phase.

    Parameters
    ----------
    idea:
        The Idea to abandon.

    Returns
    -------
    Idea
        The same object, in ABANDONED state.

    Raises
    ------
    IllegalTransitionError
        If the current state cannot transition to ABANDONED.
    """
    if idea.state is IdeaState.ABANDONED:
        return idea
    return transition(idea, IdeaState.ABANDONED)
