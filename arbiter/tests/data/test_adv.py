"""Tests for adv_20d — Lane 3 core.

Covers INTERFACES.md §3 (PITGateway), §9 (ADV cap), §11 convention 4
(fail-closed: None when data insufficient).

All tests use FixtureSource / make_adv_fixture_pit — no network calls.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from arbiter.data.pit import Bar, FixtureSource, PITGateway
from arbiter.data.adv import (
    adv_20d,
    make_adv_fixture_pit,
    register_adv_source,
    _REQUIRED_DAYS,
    _LOOKBACK_CALENDAR_DAYS,
)

_UTC = timezone.utc


def _ts(year: int, month: int, day: int) -> datetime:
    """Build a tz-aware UTC datetime at midnight."""
    return datetime(year, month, day, tzinfo=_UTC)


def _make_weekday_bars(
    ticker: str,
    as_of: datetime,
    count: int,
    close: float = 100.0,
    volume: float = 1_000_000.0,
) -> list[tuple[datetime, float, float]]:
    """Generate ``count`` weekday Bar tuples ending strictly before ``as_of``.

    Each bar is placed on a distinct calendar day, skipping weekends, so
    the window mirrors real trading-day behaviour.

    Returns list of (timestamp, close, volume) sorted ascending.
    """
    bars: list[tuple[datetime, float, float]] = []
    cursor = as_of - timedelta(days=1)
    while len(bars) < count:
        if cursor.weekday() < 5:  # Mon-Fri
            bars.append((cursor, close, volume))
        cursor -= timedelta(days=1)
    bars.reverse()
    return bars


# ---------------------------------------------------------------------------
# Happy-path: exactly 20 bars
# ---------------------------------------------------------------------------

class TestADV20dHappyPath:
    def test_returns_float_with_20_bars(self):
        """adv_20d returns a float when exactly 20 bars are available."""
        as_of = _ts(2026, 6, 1)
        bars = _make_weekday_bars("AAPL", as_of, 20, close=200.0, volume=500_000.0)
        pit = make_adv_fixture_pit("AAPL", bars)
        result = adv_20d("AAPL", as_of, pit)
        assert result is not None
        assert isinstance(result, float)

    def test_correct_mean_uniform_bars(self):
        """ADV = mean(close * volume) — all bars identical gives exact scalar."""
        as_of = _ts(2026, 6, 1)
        close, volume = 150.0, 2_000_000.0
        expected = close * volume  # 300_000_000.0

        bars = _make_weekday_bars("AAPL", as_of, 20, close=close, volume=volume)
        pit = make_adv_fixture_pit("AAPL", bars)
        result = adv_20d("AAPL", as_of, pit)

        assert result == pytest.approx(expected, rel=1e-9)

    def test_correct_mean_varying_bars(self):
        """ADV is the arithmetic mean of all 20 dollar-volume values."""
        as_of = _ts(2026, 6, 1)
        # Build bars with dollar volumes 1..20 (times 1_000_000 for realism).
        bars = _make_weekday_bars("AAPL", as_of, 20)
        # Patch each bar's close to produce dollar vol i * 1e6
        patched: list[tuple[datetime, float, float]] = []
        for i, (ts, _close, _vol) in enumerate(bars, start=1):
            patched.append((ts, float(i), 1_000_000.0))

        pit = make_adv_fixture_pit("AAPL", patched)
        result = adv_20d("AAPL", as_of, pit)

        expected = sum(range(1, 21)) / 20 * 1_000_000.0  # 10.5e6
        assert result == pytest.approx(expected, rel=1e-9)

    def test_uses_most_recent_20_bars_when_more_available(self):
        """When >20 bars exist in the window, only the 20 most recent are used."""
        as_of = _ts(2026, 6, 1)
        # Create 25 bars; first 5 have dolvol=1, last 20 have dolvol=200*1e6
        early_bars = _make_weekday_bars("AAPL", as_of, 25)
        patched: list[tuple[datetime, float, float]] = []
        for i, (ts, _c, _v) in enumerate(early_bars):
            if i < 5:
                patched.append((ts, 1.0, 1.0))  # dolvol = 1
            else:
                patched.append((ts, 200.0, 1_000_000.0))  # dolvol = 200e6

        pit = make_adv_fixture_pit("AAPL", patched)
        result = adv_20d("AAPL", as_of, pit)

        # Result should be 200e6, not affected by the early low-value bars
        assert result == pytest.approx(200.0 * 1_000_000.0, rel=1e-9)


# ---------------------------------------------------------------------------
# Fail-closed: None when data is insufficient
# ---------------------------------------------------------------------------

class TestADV20dFailClosed:
    def test_returns_none_with_zero_bars(self):
        """No data → None (fail-closed per INTERFACES.md §11 convention 4)."""
        as_of = _ts(2026, 6, 1)
        pit = make_adv_fixture_pit("AAPL", [])
        result = adv_20d("AAPL", as_of, pit)
        assert result is None

    def test_returns_none_with_19_bars(self):
        """19 bars (one short of 20) → None."""
        as_of = _ts(2026, 6, 1)
        bars = _make_weekday_bars("AAPL", as_of, 19)
        pit = make_adv_fixture_pit("AAPL", bars)
        result = adv_20d("AAPL", as_of, pit)
        assert result is None

    def test_returns_none_for_unknown_ticker(self):
        """No data registered for ticker → None."""
        as_of = _ts(2026, 6, 1)
        bars = _make_weekday_bars("AAPL", as_of, 20)
        pit = make_adv_fixture_pit("AAPL", bars)
        result = adv_20d("GOOGL", as_of, pit)
        assert result is None


# ---------------------------------------------------------------------------
# PIT look-ahead guard: bars after as_of are never counted
# ---------------------------------------------------------------------------

class TestADV20dLookaheadGuard:
    def test_bars_after_as_of_not_counted(self):
        """Bars timestamped >= as_of must NOT contribute to ADV computation."""
        as_of = _ts(2026, 6, 1)

        # 19 bars BEFORE as_of
        bars = _make_weekday_bars("AAPL", as_of, 19, close=100.0, volume=1_000_000.0)
        # 1 bar ON as_of (should be excluded — strict look-ahead guard)
        bars.append((as_of, 100.0, 1_000_000.0))
        # 1 bar AFTER as_of
        bars.append((as_of + timedelta(days=1), 100.0, 1_000_000.0))

        pit = make_adv_fixture_pit("AAPL", bars)
        result = adv_20d("AAPL", as_of, pit)
        # Only 19 bars are strictly before as_of; should return None.
        assert result is None

    def test_bars_exactly_at_as_of_minus_1_are_counted(self):
        """Bar at as_of − 1 day IS within the window (window ends as_of−1)."""
        as_of = _ts(2026, 6, 2)  # Monday
        # Last bar is Friday May 30 (as_of - 3 days — adjusted for weekend)
        bars = _make_weekday_bars("AAPL", as_of, 20, close=50.0, volume=1_000_000.0)
        pit = make_adv_fixture_pit("AAPL", bars)
        result = adv_20d("AAPL", as_of, pit)
        assert result is not None
        assert result == pytest.approx(50.0 * 1_000_000.0, rel=1e-9)


# ---------------------------------------------------------------------------
# register_adv_source helper
# ---------------------------------------------------------------------------

class TestRegisterADVSource:
    def test_register_adv_source_wires_pit_get(self):
        """pit.get('adv_20d', ticker, as_of) returns computed value after register."""
        as_of = _ts(2026, 6, 1)
        bars = _make_weekday_bars("AAPL", as_of, 20, close=100.0, volume=500_000.0)

        pit = make_adv_fixture_pit("AAPL", bars)
        register_adv_source(pit)

        result = pit.get("adv_20d", "AAPL", as_of)
        assert result is not None
        assert result == pytest.approx(100.0 * 500_000.0, rel=1e-9)

    def test_register_adv_source_returns_none_when_insufficient(self):
        """Registered adv_20d source propagates None on insufficient data."""
        as_of = _ts(2026, 6, 1)
        bars = _make_weekday_bars("AAPL", as_of, 5)

        pit = make_adv_fixture_pit("AAPL", bars)
        register_adv_source(pit)

        result = pit.get("adv_20d", "AAPL", as_of)
        assert result is None


# ---------------------------------------------------------------------------
# Bar-based extraction
# ---------------------------------------------------------------------------

class TestADVBarExtraction:
    def test_extracts_close_times_volume_from_bar(self):
        """_extract_dollar_volume correctly handles Bar objects."""
        from arbiter.data.adv import _extract_dollar_volume

        bar = Bar(
            ticker="AAPL",
            timestamp=_ts(2026, 1, 15),
            open=99.0,
            high=101.0,
            low=98.0,
            close=100.0,
            volume=250_000.0,
        )
        result = _extract_dollar_volume(bar)
        assert result == pytest.approx(25_000_000.0)

    def test_returns_none_for_zero_close(self):
        """Bar with close <= 0 yields None (guard against bad data)."""
        from arbiter.data.adv import _extract_dollar_volume

        bar = Bar(
            ticker="AAPL",
            timestamp=_ts(2026, 1, 15),
            open=0.0, high=0.0, low=0.0, close=0.0, volume=1_000_000.0,
        )
        result = _extract_dollar_volume(bar)
        assert result is None

    def test_scalar_treated_as_precomputed_dollar_volume(self):
        """A plain float is returned as-is (pre-computed dollar volume)."""
        from arbiter.data.adv import _extract_dollar_volume

        result = _extract_dollar_volume(5_000_000.0)
        assert result == 5_000_000.0


# ---------------------------------------------------------------------------
# W-DATA — bar-provider accessor (production path: real close*volume)
# ---------------------------------------------------------------------------

class TestBarProviderAccessor:
    """attach_bar_provider + _get_pit_bar: ADV uses real Bars from a provider.

    Production registers a provider whose get_bar() returns real (close,volume)
    bars; without one, ADV falls back to pit.get('price_close') (Bar fixtures).
    """

    def test_provider_supplies_volume_for_dollar_adv(self):
        from arbiter.data.adv import attach_bar_provider, adv_20d

        as_of = _ts(2026, 6, 1)
        close, volume = 200.0, 750_000.0  # dollar-vol = 150_000_000

        # Bars keyed by date, weekday history before as_of.
        history: dict = {}
        cursor = as_of - timedelta(days=1)
        made = 0
        while made < 25:
            if cursor.weekday() < 5:
                history[cursor.date()] = Bar(
                    ticker="AAPL", timestamp=cursor, open=close, high=close,
                    low=close, close=close, volume=volume,
                )
                made += 1
            cursor -= timedelta(days=1)

        class _Provider:
            def get_bar(self, ticker, as_of_):
                # Return latest bar at-or-before as_of_ (PIT guard).
                eligible = [b for d, b in history.items() if b.timestamp <= as_of_]
                eligible.sort(key=lambda b: b.timestamp)
                return eligible[-1] if eligible else None

        pit = PITGateway()  # no price_close source — provider is the only path
        attach_bar_provider(pit, _Provider())

        adv = adv_20d("AAPL", as_of, pit)
        assert adv is not None
        assert adv == pytest.approx(close * volume, rel=1e-6)
        assert adv > 1_000_000.0  # dollar magnitude, not ~price

    def test_get_pit_bar_falls_back_to_price_close_without_provider(self):
        """No provider attached → _get_pit_bar reads price_close (Bar fixtures)."""
        from arbiter.data.adv import _get_pit_bar

        as_of = _ts(2026, 6, 1)
        bars = _make_weekday_bars("AAPL", as_of, 1, close=100.0, volume=500_000.0)
        pit = make_adv_fixture_pit("AAPL", bars)
        ts, _c, _v = bars[0]
        value = _get_pit_bar(pit, "AAPL", ts)
        assert isinstance(value, Bar)
        assert value.volume == 500_000.0
