"""Idea factory and helpers — Lane 13.

Implements INTERFACES.md §7: Idea object factory, ULID generation, dedupe_key.

Dedupe key = (ticker, horizon_bucket.value).
Concurrent ideas on one ticker in DIFFERENT buckets are allowed.
A duplicate is defined as having the same (ticker, bucket) when the existing
idea is in a pre-EXECUTED active state.
"""
from __future__ import annotations

from datetime import datetime

from arbiter.contract.seams import Idea
from arbiter.db.helpers import generate_ulid
from arbiter.types import HorizonBucket, IdeaState, bucket_for_days


# States considered "active" for dedupe purposes (pre-execution pipeline).
_ACTIVE_STATES: frozenset[IdeaState] = frozenset({
    IdeaState.NASCENT,
    IdeaState.GATHERING,
    IdeaState.PROVISIONAL_DECIDED,
    IdeaState.FINAL_DECIDED,
})


def dedupe_key_for(ticker: str, bucket: HorizonBucket) -> tuple[str, str]:
    """Return the canonical dedupe key for (ticker, bucket).

    Parameters
    ----------
    ticker:
        Exchange ticker symbol (e.g. "AAPL").
    bucket:
        HorizonBucket enum value.

    Returns
    -------
    tuple[str, str]
        ``(ticker, bucket.value)`` — matches ``Idea.dedupe_key``.
    """
    return (ticker, bucket.value)


def make_idea(
    ticker: str,
    thesis: str,
    horizon_days: int,
    as_of: datetime,
    *,
    state: IdeaState = IdeaState.NASCENT,
    idea_id: str | None = None,
) -> Idea:
    """Create a new Idea with a fresh ULID and computed dedupe_key.

    Parameters
    ----------
    ticker:
        Exchange ticker symbol.
    thesis:
        Human-readable thesis for this trade idea.
    horizon_days:
        Stated horizon in calendar days (1–365).  Determines the
        HorizonBucket and hence the dedupe_key.
    as_of:
        Original information timestamp (tz-aware UTC).  Passed to Lane 14
        on OUTCOME_READY so the outcome labeler knows which as-of to use.
    state:
        Initial FSM state.  Defaults to NASCENT.
    idea_id:
        Explicit ULID; generated if not supplied (production always lets
        this default; explicit IDs are for tests and replay).

    Returns
    -------
    Idea
        Fully populated Idea ready to enter the FSM.

    Raises
    ------
    ValueError
        If ``horizon_days`` is out of range (delegated to bucket_for_days).
    ValueError
        If ``as_of`` is not tz-aware.
    """
    if as_of.tzinfo is None:
        raise ValueError(
            "make_idea: as_of must be tz-aware UTC; received a naive datetime"
        )

    bucket = bucket_for_days(horizon_days)
    return Idea(
        idea_id=idea_id or generate_ulid(),
        ticker=ticker,
        thesis=thesis,
        horizon_days=horizon_days,
        state=state,
        as_of=as_of,
        dedupe_key=dedupe_key_for(ticker, bucket),
    )


def is_duplicate(idea: Idea, active_ideas: list[Idea]) -> bool:
    """Return True if *idea* is a duplicate of any active idea.

    A duplicate means the same ``(ticker, horizon_bucket.value)`` key exists
    in an active pre-EXECUTED state.  Different horizon buckets on the same
    ticker are NOT duplicates.

    Parameters
    ----------
    idea:
        The candidate idea to check.
    active_ideas:
        All currently active ideas to search.

    Returns
    -------
    bool
    """
    for existing in active_ideas:
        if (
            existing.idea_id != idea.idea_id
            and existing.dedupe_key == idea.dedupe_key
            and existing.state in _ACTIVE_STATES
        ):
            return True
    return False
