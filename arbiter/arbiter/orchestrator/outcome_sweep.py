"""Outcome sweep — Lane 13.

Periodically (~60s) marks ideas as OUTCOME_READY when their stated horizon
has elapsed.

Key contract points:
- The sweep emits the idea's ORIGINAL ``as_of`` timestamp (set at idea
  creation) plus the stated ``horizon_days`` to Lane 14 (outcome labeler).
  This preserves the exact information window that was live when the idea
  was generated — no look-ahead.
- The sweep only advances MONITORED → OUTCOME_READY; it never skips states
  or performs any other transition.
- Clock is always injected (never ``datetime.now()`` directly).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta, timezone

from arbiter.contract.seams import Idea
from arbiter.data.clock import Clock
from arbiter.orchestrator.lifecycle import transition
from arbiter.types import IdeaState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutcomeReadyEvent:
    """Emitted for each idea marked OUTCOME_READY by the sweep.

    Passed to Lane 14 so it can compute alpha and update trust.

    Attributes
    ----------
    idea_id:
        Unique identifier of the idea.
    ticker:
        Exchange ticker symbol.
    original_as_of:
        The idea's ORIGINAL information timestamp — not the current wall clock.
        This is the ``as_of`` value that was set when the idea was first created.
    horizon_days:
        Stated horizon in calendar days.  Lane 14 uses this to compute
        the labelling window: [original_as_of, original_as_of + horizon_days].
    """
    idea_id: str
    ticker: str
    original_as_of: object  # datetime, kept as object to avoid import cycle
    horizon_days: int


def sweep_outcomes(
    ideas: list[Idea],
    clock: Clock,
    *,
    on_ready: object | None = None,
) -> list[OutcomeReadyEvent]:
    """Mark elapsed MONITORED ideas as OUTCOME_READY.

    Iterates *ideas* and for each idea in MONITORED state, checks whether
    ``original_as_of + horizon_days <= clock.now()``.  If so, transitions
    the idea to OUTCOME_READY and emits an ``OutcomeReadyEvent``.

    Parameters
    ----------
    ideas:
        All currently active ideas.  Only MONITORED ideas are inspected;
        others are ignored.
    clock:
        Injected clock (never ``datetime.now()`` directly per §11.1).
    on_ready:
        Optional callable(OutcomeReadyEvent) invoked for each ready event
        (e.g. to forward to Lane 14 or persist to DB).  If None, events
        are only returned.

    Returns
    -------
    list[OutcomeReadyEvent]
        Events for ideas that were just marked OUTCOME_READY this sweep.
        Empty list if no ideas are ready.
    """
    now = clock.now()
    events: list[OutcomeReadyEvent] = []

    for idea in ideas:
        if idea.state is not IdeaState.MONITORED:
            continue

        # Ensure as_of is tz-aware before comparison
        as_of = idea.as_of
        if as_of.tzinfo is None:
            # Log and skip — as_of should always be tz-aware (§11)
            logger.warning(
                "Idea %s has naive as_of datetime — skipping sweep check",
                idea.idea_id,
            )
            continue

        horizon_elapsed = as_of + timedelta(days=idea.horizon_days)

        if now >= horizon_elapsed:
            transition(idea, IdeaState.OUTCOME_READY)

            event = OutcomeReadyEvent(
                idea_id=idea.idea_id,
                ticker=idea.ticker,
                original_as_of=idea.as_of,
                horizon_days=idea.horizon_days,
            )
            events.append(event)

            logger.info(
                "Idea %s (%s) marked OUTCOME_READY at %s (original_as_of=%s, horizon=%dd)",
                idea.idea_id,
                idea.ticker,
                now.isoformat(),
                idea.as_of.isoformat(),
                idea.horizon_days,
            )

            if on_ready is not None:
                try:
                    on_ready(event)  # type: ignore[operator]
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "on_ready callback raised %s for idea %s: %s",
                        type(exc).__name__,
                        idea.idea_id,
                        exc,
                    )

    return events
