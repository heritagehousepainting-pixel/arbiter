"""Tests for replay_clock — Lane 3 backtest driver.

Covers:
- iter_trading_days: yields dates in [start, end], skips weekends, is deterministic.
- BacktestClock integration: re-exported from replay_clock, wraps clock.py.
- register_spy_source: beta wiring helper for beta_252d fixture tests.

No network calls; all data via FixtureSource.
"""
from __future__ import annotations

import math
import pytest
from datetime import date, datetime, timedelta, timezone

from arbiter.data.replay_clock import (
    BacktestClock,
    _easter_sunday,
    _holidays_for_year,
    _is_trading_day,
    iter_trading_days,
)
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.data.beta import beta_252d

_UTC = timezone.utc


# ---------------------------------------------------------------------------
# Arithmetic holiday generation (W-CAL) — correctness for ANY year
# ---------------------------------------------------------------------------

class TestArithmeticHolidays:
    def test_juneteenth_2026_closed(self):
        """2026-06-19 (Juneteenth, today) is a full-day NYSE closure."""
        juneteenth = date(2026, 6, 19)
        assert juneteenth.weekday() < 5, "Expected a weekday"
        assert _is_trading_day(juneteenth) is False
        assert list(iter_trading_days(juneteenth, juneteenth)) == []

    def test_juneteenth_not_observed_before_2022(self):
        """Juneteenth was not an NYSE holiday before 2022."""
        # 2021-06-18 (Fri, the would-be observed date) must still trade.
        # 2021-06-19 is a Saturday, so test the prior weekday directly.
        assert _is_trading_day(date(2021, 6, 18)) is True

    def test_veterans_day_2026_open(self):
        """Veterans Day (2026-11-11) is NOT a closure — NYSE trades."""
        veterans = date(2026, 11, 11)
        assert veterans.weekday() < 5, "Expected a weekday"
        assert _is_trading_day(veterans) is True
        assert list(iter_trading_days(veterans, veterans)) == [veterans]

    def test_2027_floating_holidays_closed(self):
        """2027 floating holidays are generated (no curated cliff)."""
        for d in (
            date(2027, 1, 18),   # MLK Day (3rd Mon Jan)
            date(2027, 3, 26),   # Good Friday
            date(2027, 11, 25),  # Thanksgiving (4th Thu Nov)
        ):
            assert _is_trading_day(d) is False, f"{d} should be closed"

    def test_2027_normal_weekday_open(self):
        """A normal 2027 weekday is a trading day."""
        d = date(2027, 6, 15)  # Tuesday, no holiday
        assert d.weekday() < 5
        assert _is_trading_day(d) is True

    def test_weekend_observed_shift_saturday(self):
        """Jul-4 on a Saturday → observed on the prior Friday (closed)."""
        # 2026-07-04 is a Saturday → observed 2026-07-03 (Fri).
        assert date(2026, 7, 4).weekday() == 5
        assert _is_trading_day(date(2026, 7, 3)) is False
        # The Saturday itself is a weekend (closed); Monday Jul 6 trades.
        assert _is_trading_day(date(2026, 7, 6)) is True

    def test_weekend_observed_shift_sunday(self):
        """A Jan-1 on Sunday → observed on the following Monday (closed)."""
        # 2023-01-01 is a Sunday → observed 2023-01-02 (Mon).
        assert date(2023, 1, 1).weekday() == 6
        assert _is_trading_day(date(2023, 1, 2)) is False

    def test_easter_computus_known_values(self):
        """Anonymous Gregorian computus matches known Easter dates."""
        assert _easter_sunday(2026) == date(2026, 4, 5)
        assert _easter_sunday(2027) == date(2027, 3, 28)
        assert _easter_sunday(2024) == date(2024, 3, 31)

    def test_christmas_generated_any_year(self):
        """Christmas is generated for a far-future year (2030)."""
        assert _is_trading_day(date(2030, 12, 25)) is False

    def test_holiday_set_excludes_veterans_day(self):
        """Veterans Day must never appear in the generated holiday set."""
        for yr in (2025, 2026, 2027, 2030):
            assert date(yr, 11, 11) not in _holidays_for_year(yr)


# ---------------------------------------------------------------------------
# iter_trading_days — basic correctness
# ---------------------------------------------------------------------------

class TestIterTradingDays:
    def test_single_weekday(self):
        """Single weekday in range yields one date."""
        d = date(2026, 6, 1)  # Monday
        result = list(iter_trading_days(d, d))
        assert result == [d]

    def test_weekend_excluded(self):
        """Saturday and Sunday are always excluded."""
        # June 6-7, 2026 = Saturday + Sunday
        sat = date(2026, 6, 6)
        sun = date(2026, 6, 7)
        result = list(iter_trading_days(sat, sun))
        assert result == []

    def test_one_week_five_days(self):
        """Mon-Fri in a non-holiday week yields exactly 5 days."""
        # Week of June 1-5, 2026 (Mon-Fri; no holidays)
        start = date(2026, 6, 1)
        end = date(2026, 6, 5)
        result = list(iter_trading_days(start, end))
        assert len(result) == 5
        assert result[0] == start
        assert result[-1] == end

    def test_weekend_skipped_mid_week(self):
        """Two consecutive weeks (Mon-Mon) yield 6 trading days."""
        start = date(2026, 6, 1)   # Monday
        end = date(2026, 6, 8)     # Monday of next week
        result = list(iter_trading_days(start, end))
        # 5 days first week + 1 day (Mon of second week)
        assert len(result) == 6
        # No weekends
        for d in result:
            assert d.weekday() < 5, f"{d} is a weekend!"

    def test_all_days_are_weekdays(self):
        """Every yielded date must be a weekday (Mon=0..Fri=4)."""
        start = date(2026, 1, 1)
        end = date(2026, 3, 31)
        for d in iter_trading_days(start, end):
            assert d.weekday() < 5, f"{d} ({d.strftime('%A')}) is a weekend!"

    def test_new_years_day_excluded(self):
        """January 1 (fixed holiday) is excluded."""
        start = date(2026, 1, 1)
        end = date(2026, 1, 2)
        result = list(iter_trading_days(start, end))
        # Jan 1 is Thu, Jan 2 is Fri — Jan 1 is a holiday
        assert date(2026, 1, 1) not in result

    def test_christmas_excluded(self):
        """December 25 (fixed holiday) is excluded."""
        start = date(2025, 12, 24)
        end = date(2025, 12, 26)
        result = list(iter_trading_days(start, end))
        assert date(2025, 12, 25) not in result

    def test_known_closure_excluded(self):
        """A floating holiday (MLK Day) is excluded even if it's a weekday."""
        # Jan 20, 2025 = MLK Day (Monday) — generated arithmetically.
        mlk_2025 = date(2025, 1, 20)
        assert mlk_2025.weekday() == 0, "Expected Monday"
        result = list(iter_trading_days(mlk_2025, mlk_2025))
        assert result == []

    def test_start_after_end_raises(self):
        """start > end raises ValueError."""
        with pytest.raises(ValueError, match="must not be after"):
            list(iter_trading_days(date(2026, 6, 10), date(2026, 6, 1)))

    def test_empty_range_when_start_equals_end_weekend(self):
        """start == end on a weekend yields empty."""
        sat = date(2026, 6, 6)
        result = list(iter_trading_days(sat, sat))
        assert result == []

    def test_ascending_order(self):
        """Dates must be yielded in strictly ascending order."""
        start = date(2026, 1, 5)
        end = date(2026, 3, 31)
        days = list(iter_trading_days(start, end))
        for i in range(1, len(days)):
            assert days[i] > days[i - 1], "Dates must be strictly ascending"

    def test_inclusive_end(self):
        """end date is included when it is a trading day."""
        start = date(2026, 6, 1)  # Mon
        end = date(2026, 6, 5)    # Fri
        result = list(iter_trading_days(start, end))
        assert result[-1] == end

    def test_deterministic_across_calls(self):
        """Same inputs produce identical output on repeated calls."""
        start = date(2026, 1, 5)
        end = date(2026, 6, 30)
        first = list(iter_trading_days(start, end))
        second = list(iter_trading_days(start, end))
        assert first == second

    def test_typical_month_count(self):
        """A non-holiday month has roughly 20-23 trading days."""
        # June 2026: 30 days, starts Mon Jun 1
        start = date(2026, 6, 1)
        end = date(2026, 6, 30)
        result = list(iter_trading_days(start, end))
        # 4 full weeks = 20 days; June has partial 5th week (Mon-Tue = 2 days) = 22
        assert 18 <= len(result) <= 23


# ---------------------------------------------------------------------------
# BacktestClock re-export
# ---------------------------------------------------------------------------

class TestBacktestClockReExport:
    def test_import_from_replay_clock(self):
        """BacktestClock must be importable from replay_clock (re-export)."""
        # Already imported at top of module; just check it is Clock-compatible.
        as_of = datetime(2026, 6, 1, tzinfo=_UTC)
        clock = BacktestClock(as_of)
        assert clock.now() == as_of

    def test_advance_works(self):
        """BacktestClock.advance() from replay_clock re-export advances correctly."""
        as_of = datetime(2026, 6, 1, tzinfo=_UTC)
        clock = BacktestClock(as_of)
        clock.advance(timedelta(days=1))
        assert clock.now() == datetime(2026, 6, 2, tzinfo=_UTC)

    def test_set_as_of_works(self):
        """BacktestClock.set_as_of() works via replay_clock re-export."""
        clock = BacktestClock(datetime(2026, 1, 1, tzinfo=_UTC))
        new_date = datetime(2026, 6, 15, tzinfo=_UTC)
        clock.set_as_of(new_date)
        assert clock.now() == new_date

    def test_clock_drives_replay_loop(self):
        """iter_trading_days + BacktestClock can drive a deterministic replay loop."""
        start = date(2026, 6, 1)
        end = date(2026, 6, 5)
        clock = BacktestClock(datetime(start.year, start.month, start.day, tzinfo=_UTC))

        visited: list[date] = []
        for d in iter_trading_days(start, end):
            clock.set_as_of(datetime(d.year, d.month, d.day, tzinfo=_UTC))
            assert clock.now().date() == d
            visited.append(d)

        assert len(visited) == 5  # Mon-Fri, no holidays


# ---------------------------------------------------------------------------
# Beta wiring helper: register_spy_source
# ---------------------------------------------------------------------------

def register_spy_source(pit: PITGateway, bars: list[tuple[datetime, float]]) -> None:
    """Register SPY price_close fixture data on an existing PITGateway.

    Helper for beta wiring tests: adds a FixtureSource with SPY close
    prices so that ``beta_252d`` can find SPY returns alongside the
    ticker's returns.

    This does NOT modify beta.py.  It is a test-only utility.

    Parameters
    ----------
    pit:
        PITGateway that already has "price_close" registered for the
        ticker under test.  SPY is added to the same source or a
        separate FixtureSource registered on the same field.
    bars:
        List of (timestamp, close) pairs for SPY.
    """
    # Retrieve the existing source for "price_close" if it's a FixtureSource,
    # otherwise create a new one and register it (overwriting the field for
    # test simplicity — callers should use a single FixtureSource for both).
    # We pull the internal source dict via the public register path by
    # creating a fresh FixtureSource and re-registering.
    spy_src = FixtureSource()
    for ts, close in bars:
        spy_src.add("price_close", "SPY", ts, close)

    # To avoid clobbering the existing ticker source we need a combined source.
    # The cleanest approach: wrap both via a delegating source.
    existing_source = pit._sources.get("price_close")  # type: ignore[attr-defined]

    class _CombinedSource:
        def get_pit(self, field: str, ticker: str, as_of: datetime) -> object | None:
            if ticker == "SPY":
                return spy_src.get_pit(field, ticker, as_of)
            if existing_source is not None:
                return existing_source.get_pit(field, ticker, as_of)
            return None

    pit.register_source("price_close", _CombinedSource())


class TestBetaWiringFixture:
    """Verify beta_252d can compute a known beta via PIT fixtures.

    Uses register_spy_source (defined above) to add SPY data alongside
    ticker data without modifying beta.py.
    """

    def _build_ticker_and_spy_bars(
        self,
        n_bars: int = 253,
        base_date: date | None = None,
        ticker_beta: float = 1.5,
    ) -> tuple[list[tuple[datetime, float]], list[tuple[datetime, float]]]:
        """Build (ticker_bars, spy_bars) with a known synthetic beta.

        SPY returns are uniform 0.001/day.
        Ticker returns = ticker_beta * spy_return + tiny noise-free component.
        Prices are computed from cumulative returns starting at 100.
        """
        if base_date is None:
            base_date = date(2026, 6, 1)

        spy_prices: list[tuple[datetime, float]] = []
        ticker_prices: list[tuple[datetime, float]] = []

        spy_price = 100.0
        ticker_price = 100.0
        spy_return_per_day = 0.001

        # Walk backwards from base_date to get n_bars before as_of.
        day_offsets = []
        cursor = base_date - timedelta(days=1)
        while len(day_offsets) < n_bars:
            if cursor.weekday() < 5:
                day_offsets.append(cursor)
            cursor -= timedelta(days=1)
        day_offsets.reverse()

        # Build prices forward from the earliest date.
        for d in day_offsets:
            ts = datetime(d.year, d.month, d.day, tzinfo=_UTC)
            spy_prices.append((ts, spy_price))
            ticker_prices.append((ts, ticker_price))
            # Advance: spy by fixed return; ticker by beta * spy return
            spy_price = spy_price * (1 + spy_return_per_day)
            ticker_price = ticker_price * (1 + ticker_beta * spy_return_per_day)

        return ticker_prices, spy_prices

    def test_beta_near_known_value(self):
        """beta_252d returns a value close to the synthetic beta (1.5)."""
        as_of_date = date(2026, 6, 1)
        as_of = datetime(as_of_date.year, as_of_date.month, as_of_date.day, tzinfo=_UTC)
        target_beta = 1.5

        ticker_bars, spy_bars = self._build_ticker_and_spy_bars(
            n_bars=253, base_date=as_of_date, ticker_beta=target_beta
        )

        # Set up gateway with ticker source
        ticker_src = FixtureSource()
        for ts, close in ticker_bars:
            ticker_src.add("price_close", "TSLA", ts, close)

        pit = PITGateway()
        pit.register_source("price_close", ticker_src)

        # Wire SPY data in via helper (does NOT modify beta.py)
        register_spy_source(pit, spy_bars)

        result = beta_252d("TSLA", as_of, pit)

        # With a perfectly synthetic series, beta should be very close to 1.5.
        assert result == pytest.approx(target_beta, abs=0.05), (
            f"Expected beta ≈ {target_beta}, got {result}"
        )

    def test_beta_imputes_1_when_insufficient_data(self):
        """beta_252d imputes 1.0 when fewer than 63 usable pairs available."""
        as_of = datetime(2026, 6, 1, tzinfo=_UTC)
        pit = PITGateway()
        # No data registered at all
        result = beta_252d("NKLA", as_of, pit)
        assert result == 1.0

    def test_spy_source_doesnt_clobber_ticker(self):
        """register_spy_source preserves ticker data alongside SPY data."""
        as_of_date = date(2026, 6, 1)
        as_of = datetime(as_of_date.year, as_of_date.month, as_of_date.day, tzinfo=_UTC)

        ticker_bars, spy_bars = self._build_ticker_and_spy_bars(
            n_bars=253, base_date=as_of_date, ticker_beta=1.0
        )

        ticker_src = FixtureSource()
        for ts, close in ticker_bars:
            ticker_src.add("price_close", "TEST", ts, close)

        pit = PITGateway()
        pit.register_source("price_close", ticker_src)
        register_spy_source(pit, spy_bars)

        # Both ticker and SPY data must be accessible
        first_ticker_ts = ticker_bars[0][0]
        assert pit.get("price_close", "TEST", first_ticker_ts) is not None
        first_spy_ts = spy_bars[0][0]
        assert pit.get("price_close", "SPY", first_spy_ts) is not None

    def test_beta_of_1_for_identical_series(self):
        """Ticker tracking SPY exactly yields beta ≈ 1.0."""
        as_of_date = date(2026, 6, 1)
        as_of = datetime(as_of_date.year, as_of_date.month, as_of_date.day, tzinfo=_UTC)

        ticker_bars, spy_bars = self._build_ticker_and_spy_bars(
            n_bars=253, base_date=as_of_date, ticker_beta=1.0
        )

        ticker_src = FixtureSource()
        for ts, close in ticker_bars:
            ticker_src.add("price_close", "SPY_CLONE", ts, close)

        pit = PITGateway()
        pit.register_source("price_close", ticker_src)
        register_spy_source(pit, spy_bars)

        result = beta_252d("SPY_CLONE", as_of, pit)
        assert result == pytest.approx(1.0, abs=0.05)
