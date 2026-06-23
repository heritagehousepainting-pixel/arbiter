"""Coverage term — Lane 11 / trust sub-module.

Coverage defeats the strategy of abstaining on hard calls:
    coverage = opined_count / eligible_count   ∈ [0, 1]

``eligible_count`` = number of ideas the advisor COULD have opined on
during the coverage term (passed in from the eligible-idea roster).
This is a Wave-C wiring point: the roster comes from Lane 13 (Orchestrator)
or Lane 14 (Outcome Labeler) and is passed as a parameter, NOT imported.

A coverage of 1.0 = opined on every eligible idea.
A coverage of 0.0 = abstained on everything (weight → 0.02 floor).

No datetime.now() — callers supply as_of and the roster.
"""
from __future__ import annotations

from typing import Sequence

from arbiter.contract.seams import ResolvedOutcome

# Minimum coverage value (avoids division edge cases; still penalised heavily)
_EPSILON: float = 1e-9


def coverage_score(
    outcomes: Sequence[ResolvedOutcome],
    eligible_idea_ids: Sequence[str],
) -> float:
    """Compute coverage for one advisor over a coverage term.

    Parameters
    ----------
    outcomes:
        All ResolvedOutcome rows for the advisor in the coverage window.
        Rows with abstained=True count as opined (the advisor was assigned
        the idea and chose to abstain) vs truly ineligible ideas.
    eligible_idea_ids:
        The roster of idea_ids the advisor was eligible to opine on during
        the coverage term.  Source: Lane 13 (Orchestrator) or Lane 14 —
        passed in at call site (Wave-C wiring point).

    Returns
    -------
    float
        Coverage ratio in [0.0, 1.0].  Returns 0.0 when eligible_count == 0
        (avoid ZeroDivision; logged by caller).
    """
    eligible_count = len(eligible_idea_ids)
    if eligible_count == 0:
        return 0.0

    eligible_set = set(eligible_idea_ids)

    # Count distinct ideas this advisor actually opined on (incl. abstains that
    # were assigned — abstain=True means they touched the idea, not that they
    # were never assigned).  Only count ideas that were in the eligible set.
    opined_idea_ids = {
        outcome.idea_id
        for outcome in outcomes
        if outcome.idea_id in eligible_set
    }
    opined_count = len(opined_idea_ids)

    return min(1.0, opined_count / eligible_count)
