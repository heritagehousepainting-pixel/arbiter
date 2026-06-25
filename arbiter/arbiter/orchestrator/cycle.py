"""Cycle runner — Lane 13.

``run_cycle`` is the top-level coordinator that ties all Phase-1 lanes together
for one decision cycle:

    1. Gather opinions from advisors (fault-isolated via scheduler)
    2. Fuse opinions into a FusionOutput per bucket (injected callable)
    3. Decide sizing/action via policy (injected callable)
    4. Submit order via executor (injected callable)
    5. Persist idea state changes to DB and audit log

Dependency injection contract:
    All lanes (fusion, policy, executor, advisors) are injected as callables.
    This module never imports them directly.  This makes the cycle testable
    in isolation and keeps Lane 13 decoupled from concurrent development.

Dedupe rule:
    (ticker, horizon_bucket) must be unique in active ideas.  If the incoming
    idea set contains a duplicate for the same (ticker, bucket), the newer
    attempt is skipped (first-in wins for the active window).  Different
    buckets on the same ticker are allowed concurrently.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from arbiter.contract.opinion import Opinion
from arbiter.contract.seams import FusionOutput, Idea, PaperOrder
from arbiter.data.clock import Clock
from arbiter.orchestrator.idea import is_duplicate
from arbiter.orchestrator.lifecycle import transition
from arbiter.orchestrator.scheduler import run_named_advisors_parallel
from arbiter.types import HorizonBucket, IdeaState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Type aliases for the injected callables
# ---------------------------------------------------------------------------

#: An advisor emitter: zero-arg callable returning Opinion | None
AdvisorEmitter = Callable[[], Opinion | None]

#: Fusion callable: (opinions, bucket) → FusionOutput
FuseCallable = Callable[[list[Opinion], HorizonBucket], FusionOutput]

#: Policy/decide callable: (fusion_output, idea) → PaperOrder | None
DecideCallable = Callable[[FusionOutput, Idea], PaperOrder | None]

#: Order submission callable: (order) → bool (True = success)
SubmitCallable = Callable[[PaperOrder], bool]

#: Options-expression callback: (fusion_output, idea) → None.  Runs AFTER the
#: equity decide/submit for each idea (overlay; never affects the equity flow).
ExpressCallable = Callable[[FusionOutput, Idea], None]


# ---------------------------------------------------------------------------
# Cycle result
# ---------------------------------------------------------------------------

@dataclass
class CycleResult:
    """Summary of one completed cycle run.

    Attributes
    ----------
    ideas_processed:
        Number of ideas that entered the cycle.
    ideas_skipped_dedupe:
        Number of ideas skipped because a duplicate was already active.
    opinions_gathered:
        Total opinions collected (including None/null opinions from faults).
    opinions_null:
        Number of null (faulted/abstained) opinion slots.
    orders_submitted:
        Number of orders successfully submitted to the executor.
    errors:
        Any non-fatal errors encountered (advisor faults are not recorded
        here — they produce null opinions and a log line).
    """
    ideas_processed: int = 0
    ideas_skipped_dedupe: int = 0
    opinions_gathered: int = 0
    opinions_null: int = 0
    orders_submitted: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core cycle function
# ---------------------------------------------------------------------------

def run_cycle(
    ideas: Sequence[Idea],
    advisor_map: dict[str, AdvisorEmitter],
    fuse: FuseCallable,
    decide: DecideCallable,
    submit: SubmitCallable,
    clock: Clock,
    *,
    active_ideas: list[Idea] | None = None,
    advisor_timeout: float = 30.0,
    max_advisor_workers: int = 8,
    on_new_idea: Callable[[Idea], None] | None = None,
    on_transition: Callable[[Idea, IdeaState], None] | None = None,
    express: ExpressCallable | None = None,
) -> CycleResult:
    """Run one full decision cycle.

    This is the Phase-1 implementation.  Later phases wire trust-weighted
    fusion and calibration by passing different ``fuse`` and ``decide``
    callables — the cycle runner itself does not change.

    Parameters
    ----------
    ideas:
        Ideas to process this cycle (typically NASCENT → GATHERING).
    advisor_map:
        Dict of ``advisor_id → zero-arg callable``.  The callable is invoked
        once per cycle per advisor and must return ``Opinion | None``.
        Callables are run in a bounded thread pool; faults → null opinion.
    fuse:
        Injected fusion callable.  Signature: ``(opinions, bucket) → FusionOutput``.
        Called once per horizon bucket for which opinions exist.
    decide:
        Injected policy callable.  Signature: ``(fusion_output, idea) → PaperOrder | None``.
        Returns None if the policy decides not to trade.
    submit:
        Injected order submission callable.  ``(order) → bool``.
    clock:
        Injected clock (never ``datetime.now()``).
    active_ideas:
        All currently active ideas for dedupe checking.  If None, only the
        ``ideas`` list is used for intra-batch dedupe.
    advisor_timeout:
        Per-advisor timeout in seconds.
    max_advisor_workers:
        Thread pool size for advisor concurrency.
    on_new_idea:
        Optional ``callable(idea) -> None`` invoked the moment an idea is
        confirmed non-duplicate and added to the pending set — BEFORE its
        ``NASCENT -> GATHERING`` transition, so persistence captures the idea
        in its NASCENT state.  Default ``None`` (no-op, unchanged behavior).
        A callback exception is logged and does NOT abort the cycle.
    on_transition:
        Optional ``callable(idea, new_state) -> None`` invoked immediately
        after every FSM ``transition(...)`` in this function.  Default ``None``
        (no-op).  A callback exception is logged and does NOT abort the cycle.

    Returns
    -------
    CycleResult
        Summary statistics for the cycle.
    """
    result = CycleResult()
    now = clock.now()
    all_active = list(active_ideas or [])

    def _emit_new_idea(idea: Idea) -> None:
        """Invoke the on_new_idea callback fail-safe (never aborts the cycle)."""
        if on_new_idea is None:
            return
        try:
            on_new_idea(idea)
        except Exception as exc:  # noqa: BLE001
            logger.error("on_new_idea callback failed for idea %s: %s", idea.idea_id, exc)

    def _emit_transition(idea: Idea, new_state: IdeaState) -> None:
        """Invoke the on_transition callback fail-safe (never aborts the cycle)."""
        if on_transition is None:
            return
        try:
            on_transition(idea, new_state)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "on_transition callback failed for idea %s -> %s: %s",
                idea.idea_id,
                new_state.value,
                exc,
            )

    # ------------------------------------------------------------------
    # 1. Dedupe — skip duplicate (ticker, bucket) ideas
    # ------------------------------------------------------------------
    pending_ideas: list[Idea] = []
    for idea in ideas:
        if is_duplicate(idea, all_active):
            logger.info(
                "Skipping duplicate idea %s (%s, %s) — active idea already exists",
                idea.idea_id,
                idea.ticker,
                idea.dedupe_key[1],
            )
            result.ideas_skipped_dedupe += 1
            continue

        # Persist the idea in its NASCENT state BEFORE the first transition.
        _emit_new_idea(idea)

        # Advance NASCENT → GATHERING
        if idea.state is IdeaState.NASCENT:
            transition(idea, IdeaState.GATHERING)
            _emit_transition(idea, IdeaState.GATHERING)

        pending_ideas.append(idea)
        all_active.append(idea)  # prevent intra-batch dupes

    result.ideas_processed = len(pending_ideas)

    if not pending_ideas:
        logger.debug("No non-duplicate ideas to process this cycle")
        return result

    # ------------------------------------------------------------------
    # 2. Gather opinions from all advisors (fault-isolated)
    # ------------------------------------------------------------------
    raw_opinions = run_named_advisors_parallel(
        advisor_map,
        timeout_seconds=advisor_timeout,
        max_workers=max_advisor_workers,
    )

    valid_opinions: list[Opinion] = []
    for advisor_id, opinion in raw_opinions.items():
        result.opinions_gathered += 1
        if opinion is None:
            result.opinions_null += 1
            logger.debug("Advisor %s returned null opinion this cycle", advisor_id)
        else:
            valid_opinions.append(opinion)

    logger.info(
        "Cycle: %d valid opinions from %d advisors (%d null)",
        len(valid_opinions),
        len(raw_opinions),
        result.opinions_null,
    )

    # ------------------------------------------------------------------
    # 3. Fuse opinions per horizon bucket and decide per idea
    # ------------------------------------------------------------------
    # Group opinions by bucket
    opinions_by_bucket: dict[HorizonBucket, list[Opinion]] = {}
    for op in valid_opinions:
        bucket = op.horizon_bucket
        opinions_by_bucket.setdefault(bucket, []).append(op)

    for idea in pending_ideas:
        bucket = HorizonBucket(idea.dedupe_key[1])
        bucket_opinions = opinions_by_bucket.get(bucket, [])

        if not bucket_opinions:
            logger.debug(
                "No opinions for bucket %s — skipping fusion/decision for idea %s",
                bucket.value,
                idea.idea_id,
            )
            continue

        # Fuse
        try:
            fusion_output = fuse(bucket_opinions, bucket)
        except Exception as exc:  # noqa: BLE001
            msg = f"Fusion failed for idea {idea.idea_id} (bucket {bucket.value}): {exc}"
            logger.error(msg)
            result.errors.append(msg)
            continue

        # The equity decide/submit block is wrapped in try/finally so the
        # OPTIONS EXPRESSION overlay (``express``) runs AFTER equity handling for
        # every idea that fused — even on the equity `continue`/`break` paths
        # (a `continue`/`break` inside a `try` still runs its `finally`). The
        # overlay is a strict no-op for the equity flow.
        try:
            # Advance GATHERING → PROVISIONAL_DECIDED
            if idea.state is IdeaState.GATHERING:
                transition(idea, IdeaState.PROVISIONAL_DECIDED)
                _emit_transition(idea, IdeaState.PROVISIONAL_DECIDED)

            # Decide
            try:
                order = decide(fusion_output, idea)
            except Exception as exc:  # noqa: BLE001
                msg = f"Policy decision failed for idea {idea.idea_id}: {exc}"
                logger.error(msg)
                result.errors.append(msg)
                continue

            # Advance to FINAL_DECIDED
            if idea.state is IdeaState.PROVISIONAL_DECIDED:
                transition(idea, IdeaState.FINAL_DECIDED)
                _emit_transition(idea, IdeaState.FINAL_DECIDED)

            if order is None:
                logger.info("Policy decided no trade for idea %s", idea.idea_id)
                continue

            # Submit
            try:
                success = submit(order)
            except Exception as exc:  # noqa: BLE001
                msg = f"Order submission failed for idea {idea.idea_id}: {exc}"
                logger.error(msg)
                result.errors.append(msg)
                # A broker-fatal error latches the breaker; abort the REST of this
                # cycle rather than letting another order slip through before the
                # next gate read. (Name-checked to avoid importing the exec lane.)
                if type(exc).__name__ == "BrokerError":
                    logger.error("Broker-fatal error — halting remaining submissions this cycle")
                    break
                continue

            if success:
                # Advance FINAL_DECIDED → EXECUTED → MONITORED
                if idea.state is IdeaState.FINAL_DECIDED:
                    transition(idea, IdeaState.EXECUTED)
                    _emit_transition(idea, IdeaState.EXECUTED)
                    transition(idea, IdeaState.MONITORED)
                    _emit_transition(idea, IdeaState.MONITORED)
                result.orders_submitted += 1
                logger.info(
                    "Order submitted for idea %s (%s) — idea now MONITORED",
                    idea.idea_id,
                    idea.ticker,
                )
            else:
                logger.warning(
                    "Order submission returned False for idea %s — not advancing state",
                    idea.idea_id,
                )
        finally:
            # Options expression overlay — fail-safe; never disrupts the cycle.
            if express is not None:
                try:
                    express(fusion_output, idea)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "Options express callback failed for idea %s: %s",
                        idea.idea_id, exc,
                    )

    return result
