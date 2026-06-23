"""Point-in-time replay engine — Lane 14b.

Steps ``BacktestClock`` through a calendar of ``as_of`` dates and calls the
injected ``run_cycle`` callable with each ``as_of``.  The replay drives the
SAME code path as live operation — the only difference is the clock is frozen
at the current simulation date rather than returning wall-clock time.

No-look-ahead guarantee
-----------------------
The replay NEVER injects future data: ``BacktestClock.now()`` is pinned to the
current step's ``as_of``, and all data reads go through ``PITGateway.get()``,
which enforces the ``timestamp ≤ as_of`` invariant structurally.

The no-look-ahead canary (``tests/evaluation/test_backtest.py``) verifies this
by registering a sentinel value with a future timestamp and asserting it is
never returned during replay.

Usage::

    from arbiter.evaluation.backtest.replay import BacktestReplay

    replay = BacktestReplay(
        start_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end_date=datetime(2024, 3, 31, tzinfo=timezone.utc),
        step_days=1,
        pit=my_pit_gateway,
        run_cycle=my_run_cycle,   # callable(as_of, pit, clock) -> dict
    )
    result = replay.run()
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from arbiter.data.clock import BacktestClock
from arbiter.data.pit import PITGateway

logger = logging.getLogger(__name__)


@dataclass
class ReplayResult:
    """Outcome of a full backtest replay run.

    Attributes
    ----------
    steps:
        List of ``as_of`` datetimes that were stepped through, in order.
    cycle_outputs:
        Per-step return values from ``run_cycle``, keyed by ``as_of``.
    errors:
        Any exceptions raised during a cycle, keyed by ``as_of``.
        Replay continues past errors (fail-open for evaluation purposes).
    """

    steps: list[datetime] = field(default_factory=list)
    cycle_outputs: dict[datetime, Any] = field(default_factory=dict)
    errors: dict[datetime, Exception] = field(default_factory=dict)

    @property
    def n_steps(self) -> int:
        """Number of time steps executed."""
        return len(self.steps)

    @property
    def n_errors(self) -> int:
        """Number of cycles that raised an exception."""
        return len(self.errors)

    @property
    def success_rate(self) -> float:
        """Fraction of cycles that completed without error."""
        if self.n_steps == 0:
            return 1.0
        return (self.n_steps - self.n_errors) / self.n_steps


class BacktestReplay:
    """Point-in-time replay engine.

    Steps ``BacktestClock`` through the half-open interval
    ``[start_date, end_date)`` in increments of ``step_days``, calling
    ``run_cycle(as_of, pit, clock)`` at each step.

    The ``run_cycle`` callable is injected — this class does NOT import the
    orchestrator.  Any callable with the signature::

        run_cycle(as_of: datetime, pit: PITGateway, clock: BacktestClock) -> Any

    is accepted.  The return value is stored in ``ReplayResult.cycle_outputs``.

    No-look-ahead guarantee
    -----------------------
    ``BacktestClock`` is advanced BEFORE ``run_cycle`` is called.  The clock
    and the pit gateway together enforce that no data timestamped after
    ``as_of`` can be read.  See ``PITGateway.get()`` for the structural guard.

    Parameters
    ----------
    start_date:
        First ``as_of`` timestamp (inclusive, tz-aware UTC).
    end_date:
        Last ``as_of`` timestamp (exclusive, tz-aware UTC).
    step_days:
        Calendar days between replay steps.  Must be >= 1.
    pit:
        ``PITGateway`` pre-loaded with historical data.  The same gateway is
        passed to every ``run_cycle`` call; it enforces the PIT invariant.
    run_cycle:
        Callable with signature ``(as_of, pit, clock) -> Any``.
        Do NOT import the orchestrator here — accept it as a parameter.
    skip_weekends:
        When True (default), Saturday and Sunday steps are skipped.  The
        clock still advances by ``step_days`` but no ``run_cycle`` is called.
    """

    def __init__(
        self,
        *,
        start_date: datetime,
        end_date: datetime,
        step_days: int = 1,
        pit: PITGateway,
        run_cycle: Callable[[datetime, PITGateway, BacktestClock], Any],
        skip_weekends: bool = True,
    ) -> None:
        if start_date.tzinfo is None:
            raise ValueError("start_date must be tz-aware UTC")
        if end_date.tzinfo is None:
            raise ValueError("end_date must be tz-aware UTC")
        if end_date <= start_date:
            raise ValueError("end_date must be after start_date")
        if step_days < 1:
            raise ValueError("step_days must be >= 1")

        self._start = start_date
        self._end = end_date
        self._step = timedelta(days=step_days)
        self._pit = pit
        self._run_cycle = run_cycle
        self._skip_weekends = skip_weekends

    def _build_schedule(self) -> list[datetime]:
        """Return the ordered list of as_of datetimes to replay."""
        schedule: list[datetime] = []
        current = self._start
        while current < self._end:
            if not (self._skip_weekends and current.weekday() >= 5):
                schedule.append(current)
            current = current + self._step
        return schedule

    def run(self) -> ReplayResult:
        """Execute the replay and return a ``ReplayResult``.

        The clock is created fresh for the replay and advanced at each step.
        All ``run_cycle`` calls receive the SAME clock instance (now pinned to
        the current ``as_of``), so downstream code that calls ``clock.now()``
        sees only the simulated timestamp — never wall-clock time.

        Returns
        -------
        ReplayResult
            Aggregated outputs and any errors from the replay.
        """
        schedule = self._build_schedule()
        if not schedule:
            logger.warning("BacktestReplay: empty schedule (start=%s, end=%s)", self._start, self._end)
            return ReplayResult()

        result = ReplayResult()
        # Initialise clock at the first step; advance it at each step.
        clock = BacktestClock(schedule[0])

        for as_of in schedule:
            # Pin the clock to this step BEFORE calling run_cycle.
            # This is the structural no-look-ahead guarantee:
            # clock.now() == as_of for the entire duration of this cycle.
            clock.set_as_of(as_of)
            result.steps.append(as_of)

            logger.debug("BacktestReplay: step as_of=%s", as_of.date())

            try:
                output = self._run_cycle(as_of, self._pit, clock)
                result.cycle_outputs[as_of] = output
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "BacktestReplay: run_cycle raised at as_of=%s: %s",
                    as_of.date(),
                    exc,
                    exc_info=True,
                )
                result.errors[as_of] = exc

        logger.info(
            "BacktestReplay finished: %d steps, %d errors, success_rate=%.1f%%",
            result.n_steps,
            result.n_errors,
            result.success_rate * 100,
        )
        return result
