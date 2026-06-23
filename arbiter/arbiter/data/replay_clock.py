"""Backtest replay clock driver — Lane 3 core.

Provides a deterministic date iterator for the backtest orchestrator::

    from arbiter.data.replay_clock import iter_trading_days

    for as_of_date in iter_trading_days(start, end):
        as_of = datetime(as_of_date.year, as_of_date.month, as_of_date.day,
                         tzinfo=timezone.utc)
        clock.set_as_of(as_of)
        # run one cycle with pit.get(..., as_of=as_of)

Design rules (INTERFACES.md §3, §11):
- No ``datetime.now()``.  The driver advances the BacktestClock externally.
- All as_of values produced here are ``date`` objects; callers convert to
  tz-aware ``datetime`` before passing to PITGateway.
- Skips weekends (Saturday = 5, Sunday = 6).
- Skips a curated list of US market holidays; expand as needed.
- ``BacktestClock`` is imported from ``arbiter.data.clock`` — NOT redefined.

See INTERFACES.md §3 (BacktestClock) and design spec §4.2 (replay semantics).
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from functools import lru_cache

from arbiter.data.clock import BacktestClock  # re-export so callers can import both

__all__ = ["BacktestClock", "iter_trading_days"]


# ---------------------------------------------------------------------------
# US market holiday generation (arithmetic, any year)
# ---------------------------------------------------------------------------
#
# NYSE full-day closures are computed arithmetically for ANY year rather than
# hardcoded — no cliff after a curated max year, no external calendar library.
#
# Fixed-date holidays (with weekend-observed shift):
#   - New Year's Day  (Jan 1)
#   - Juneteenth      (Jun 19, NYSE-observed 2022+ only)
#   - Independence    (Jul 4)
#   - Christmas       (Dec 25)
# Floating holidays:
#   - MLK Day         (3rd Monday of January)
#   - Presidents' Day (3rd Monday of February)
#   - Good Friday     (Easter Sunday − 2 days; Easter via Anonymous Gregorian
#                      computus)
#   - Memorial Day    (last Monday of May)
#   - Labor Day       (1st Monday of September)
#   - Thanksgiving    (4th Thursday of November)
#
# Veterans Day is intentionally NOT a closure — the NYSE trades that day.
#
# The LIVE Alpaca /v2/clock + /v2/calendar are always authoritative; this is
# the deterministic offline fallback used by sims, tests, and outages.  It does
# NOT model one-off ad-hoc closures (e.g. national days of mourning); those are
# rare and the live calendar handles them.

_JUNETEENTH_FIRST_OBSERVED_YEAR = 2022


def _weekend_observed(d: date) -> date:
    """Shift a fixed-date holiday to its NYSE-observed date.

    Saturday → the prior Friday; Sunday → the following Monday; otherwise the
    date itself.
    """
    if d.weekday() == 5:  # Saturday
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the date of the ``n``-th ``weekday`` (Mon=0..Sun=6) of a month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the date of the last ``weekday`` (Mon=0..Sun=6) of a month."""
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    last = next_month_first - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def _easter_sunday(year: int) -> date:
    """Easter Sunday via the Anonymous Gregorian computus (no external lib)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


@lru_cache(maxsize=None)
def _holidays_for_year(year: int) -> frozenset[date]:
    """Compute the set of NYSE full-day closures for ``year`` arithmetically."""
    holidays: set[date] = set()

    # Fixed-date holidays with weekend-observed shift.
    holidays.add(_weekend_observed(date(year, 1, 1)))    # New Year's Day
    if year >= _JUNETEENTH_FIRST_OBSERVED_YEAR:
        holidays.add(_weekend_observed(date(year, 6, 19)))  # Juneteenth
    holidays.add(_weekend_observed(date(year, 7, 4)))    # Independence Day
    holidays.add(_weekend_observed(date(year, 12, 25)))  # Christmas Day

    # Floating holidays.
    holidays.add(_nth_weekday(year, 1, 0, 3))    # MLK Day (3rd Mon Jan)
    holidays.add(_nth_weekday(year, 2, 0, 3))    # Presidents' Day (3rd Mon Feb)
    holidays.add(_easter_sunday(year) - timedelta(days=2))  # Good Friday
    holidays.add(_last_weekday(year, 5, 0))      # Memorial Day (last Mon May)
    holidays.add(_nth_weekday(year, 9, 0, 1))    # Labor Day (1st Mon Sep)
    holidays.add(_nth_weekday(year, 11, 3, 4))   # Thanksgiving (4th Thu Nov)

    return frozenset(holidays)


# Curated early-close (half-day) sessions — regular open, 13:00 ET close.
# Best-effort for the OFFLINE calendar fallback; the LIVE Alpaca /v2/calendar
# is always authoritative.  Refresh yearly (amendment C3).
_EARLY_CLOSE: frozenset[date] = frozenset({
    date(2024, 7, 3),    # day before Independence Day
    date(2024, 11, 29),  # day after Thanksgiving
    date(2024, 12, 24),  # Christmas Eve
    date(2025, 7, 3),    # day before Independence Day
    date(2025, 11, 28),  # day after Thanksgiving
    date(2025, 12, 24),  # Christmas Eve
    date(2026, 11, 27),  # day after Thanksgiving
    date(2026, 12, 24),  # Christmas Eve
})

# Full-day holiday closures are now generated arithmetically for ANY year
# (see ``_holidays_for_year``), so there is no holiday cliff.  This constant now
# tracks only the curated EARLY-CLOSE (half-day) data, which is still a hand
# list refreshed yearly (amendment C3).  Past it, ``OfflineMarketCalendar`` logs
# a WARNING noting that only the half-day early-close map may be stale — regular
# holidays remain correct.
CURATED_HOLIDAY_MAX_YEAR: int = 2026


def _is_trading_day(d: date) -> bool:
    """Return True if ``d`` is a NYSE trading day.

    Skips:
    - Saturday (weekday 5) and Sunday (weekday 6).
    - NYSE full-day holiday closures, generated arithmetically for the year of
      ``d`` (fixed-date holidays with weekend-observed shift + floating
      holidays incl. Good Friday).  Veterans Day is NOT a closure.

    Parameters
    ----------
    d:
        Calendar date to check.
    """
    # Weekends
    if d.weekday() >= 5:
        return False

    # Arithmetic NYSE holiday closures for this year (any year).
    if d in _holidays_for_year(d.year):
        return False

    return True


def iter_trading_days(
    start: date,
    end: date,
) -> Iterator[date]:
    """Yield NYSE trading days in [start, end] inclusive, ascending.

    Skips weekends and the arithmetically-generated US market holidays
    (see :func:`_holidays_for_year`).  The sequence is deterministic for any
    given (start, end) pair — backtests produce the same step sequence
    every run.

    Parameters
    ----------
    start:
        First calendar date (inclusive).
    end:
        Last calendar date (inclusive).

    Yields
    ------
    date
        Each trading day in [start, end], skipping weekends and holidays.

    Raises
    ------
    ValueError
        If ``start`` is after ``end``.
    """
    if start > end:
        raise ValueError(
            f"iter_trading_days: start ({start}) must not be after end ({end})"
        )

    current = start
    while current <= end:
        if _is_trading_day(current):
            yield current
        current = current + timedelta(days=1)
