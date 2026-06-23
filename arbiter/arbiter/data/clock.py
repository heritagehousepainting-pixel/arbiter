"""Clock abstraction — Lane 3 core.

THIS IS THE ONLY FILE IN THE ENTIRE CODEBASE ALLOWED TO CALL ``datetime.now()``.
The no-look-ahead lint (scripts/check_no_lookahead.sh) enforces this via AST analysis.

Live code path:  Clock().now() → real UTC time.
Backtest path:   BacktestClock(as_of).now() → fixed simulated as_of.

All other modules receive a clock instance as a parameter; they never call
``datetime.now()`` directly.  This makes look-ahead structurally impossible:
in backtest mode the clock is frozen at the simulation as_of date.

See INTERFACES.md §3, §11 convention 1, and design spec §4.2.
"""
from __future__ import annotations

from datetime import datetime, timezone


class Clock:
    """Live clock — returns real UTC time.

    This is the production implementation.  Use ``BacktestClock`` in
    backtests and anywhere a fixed as_of is needed (e.g. replay, tests).
    """

    def now(self) -> datetime:
        """Return the current UTC time as a tz-aware datetime.

        This is the ONLY call to ``datetime.now()`` permitted in the
        entire codebase (enforced by check_no_lookahead.sh).
        """
        return datetime.now(timezone.utc)


class BacktestClock(Clock):
    """Simulated clock for backtesting and replay.

    Initialized with a fixed ``as_of`` timestamp.  ``now()`` always
    returns that fixed value until ``advance()`` or ``set_as_of()``
    is called.

    Parameters
    ----------
    as_of:
        The simulated "current time".  Must be tz-aware UTC.
    """

    def __init__(self, as_of: datetime) -> None:
        if as_of.tzinfo is None:
            raise ValueError(
                "BacktestClock requires a tz-aware datetime; received a naive datetime"
            )
        self._as_of = as_of

    def now(self) -> datetime:
        """Return the fixed simulated as_of timestamp."""
        return self._as_of

    def advance(self, delta: object) -> None:
        """Advance the simulated clock by a timedelta.

        Parameters
        ----------
        delta:
            A ``datetime.timedelta`` to add to the current as_of.
        """
        self._as_of = self._as_of + delta  # type: ignore[operator]

    def set_as_of(self, as_of: datetime) -> None:
        """Set the simulated as_of to an explicit datetime.

        Parameters
        ----------
        as_of:
            New tz-aware UTC datetime to use as the simulated current time.
        """
        if as_of.tzinfo is None:
            raise ValueError(
                "BacktestClock.set_as_of requires a tz-aware datetime"
            )
        self._as_of = as_of
