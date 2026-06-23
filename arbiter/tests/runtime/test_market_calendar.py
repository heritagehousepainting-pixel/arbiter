"""Market-calendar tests — sub-project #3 (Decision 2/6)."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from arbiter.config import Config, load_config
from arbiter.runtime.market_calendar import (
    AlpacaMarketCalendar,
    MarketCalendar,
    MarketSession,
    OfflineMarketCalendar,
)

_ET = ZoneInfo("America/New_York")


def _utc(y, mo, d, h, mi=0) -> datetime:
    # Build a UTC instant from an ET wall-clock time.
    et = datetime(y, mo, d, h, mi, tzinfo=_ET)
    return et.astimezone(timezone.utc)


def _cfg() -> Config:
    return dataclasses.replace(
        load_config(), executor_backend="alpaca_paper",
        alpaca_api_key="k", alpaca_secret_key="s",
    )


class TestOfflineCalendar:
    def test_open_during_regular_session_edt(self):
        # 2025-06-18 (Wed) 11:00 ET — EDT (DST), no holiday. Must be open.
        # (Note: 2025-06-19 is Juneteenth, a closure — see test below.)
        cal = OfflineMarketCalendar()
        s = cal.session(_utc(2025, 6, 18, 11, 0))
        assert s.is_open is True

    def test_open_during_regular_session_est(self):
        # 2025-01-15 (Wed) 11:00 ET — EST (no DST). Must be open.
        cal = OfflineMarketCalendar()
        s = cal.session(_utc(2025, 1, 15, 11, 0))
        assert s.is_open is True

    def test_closed_before_open(self):
        cal = OfflineMarketCalendar()
        s = cal.session(_utc(2025, 1, 15, 8, 0))
        assert s.is_open is False
        assert s.next_open is not None

    def test_closed_on_weekend(self):
        cal = OfflineMarketCalendar()
        # 2025-01-18 is a Saturday.
        s = cal.session(_utc(2025, 1, 18, 11, 0))
        assert s.is_open is False

    def test_closed_on_holiday(self):
        cal = OfflineMarketCalendar()
        # 2025-12-25 Christmas.
        s = cal.session(_utc(2025, 12, 25, 11, 0))
        assert s.is_open is False

    def test_early_close_half_day(self):
        cal = OfflineMarketCalendar()
        # 2025-12-24 Christmas Eve early close at 13:00 ET.
        # 12:30 ET → still open; 13:30 ET → closed.
        assert cal.session(_utc(2025, 12, 24, 12, 30)).is_open is True
        assert cal.session(_utc(2025, 12, 24, 13, 30)).is_open is False

    def test_juneteenth_2026_closed(self):
        # 2026-06-19 (Juneteenth, today) is a full-day closure.
        cal = OfflineMarketCalendar()
        s = cal.session(_utc(2026, 6, 19, 11, 0))
        assert s.is_open is False

    def test_veterans_day_2026_open(self):
        # Veterans Day (2026-11-11) is a regular trading day — NYSE is OPEN.
        cal = OfflineMarketCalendar()
        s = cal.session(_utc(2026, 11, 11, 11, 0))
        assert s.is_open is True

    def test_2027_floating_holiday_closed(self):
        # Thanksgiving 2027 (2027-11-25) — generated arithmetically, no cliff.
        cal = OfflineMarketCalendar()
        s = cal.session(_utc(2027, 11, 25, 11, 0))
        assert s.is_open is False

    def test_2027_normal_weekday_open(self):
        cal = OfflineMarketCalendar()
        s = cal.session(_utc(2027, 6, 15, 11, 0))  # Tuesday, no holiday
        assert s.is_open is True

    def test_post_curated_range_warns(self, capsys):
        cal = OfflineMarketCalendar()
        cal.session(_utc(2030, 6, 19, 11, 0))
        captured = capsys.readouterr()
        assert "offline_curated_data_stale" in (captured.out + captured.err)

    def test_satisfies_protocol(self):
        assert isinstance(OfflineMarketCalendar(), MarketCalendar)


class _FakeClockHTTP:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    def __call__(self, url: str, headers: dict):
        assert url.endswith("/v2/clock")
        self.calls += 1
        return self.payload


class TestAlpacaCalendar:
    def test_parses_clock_json(self):
        fake = _FakeClockHTTP({
            "is_open": True,
            "next_open": "2025-06-20T13:30:00Z",
            "next_close": "2025-06-19T20:00:00Z",
        })
        cal = AlpacaMarketCalendar(_cfg(), http_get=fake)
        s = cal.session(_utc(2025, 6, 19, 11, 0))
        assert s.is_open is True
        assert s.next_close == datetime(2025, 6, 19, 20, 0, tzinfo=timezone.utc)

    def test_caches_until_next_close(self):
        fake = _FakeClockHTTP({
            "is_open": True,
            "next_open": "2025-06-20T13:30:00Z",
            "next_close": "2025-06-19T20:00:00Z",
        })
        cal = AlpacaMarketCalendar(_cfg(), http_get=fake)
        # Many same-session calls → ONE http_get.
        for h in (10, 11, 12, 13, 14, 15):
            cal.session(_utc(2025, 6, 19, h, 0))
        assert fake.calls == 1

    def test_refetches_after_boundary(self):
        fake = _FakeClockHTTP({
            "is_open": True,
            "next_open": "2025-06-20T13:30:00Z",
            "next_close": "2025-06-19T20:00:00Z",
        })
        cal = AlpacaMarketCalendar(_cfg(), http_get=fake)
        cal.session(_utc(2025, 6, 19, 11, 0))
        # After the cached next_close, re-fetch.
        cal.session(datetime(2025, 6, 19, 21, 0, tzinfo=timezone.utc))
        assert fake.calls == 2

    def test_closed_session_refetches_at_next_open(self):
        """Regression: a CLOSED session cached pre-open MUST refetch once the
        market opens (next_open), not stay 'closed' until next_close hours later.
        (A long-running daemon otherwise sleeps through the whole session.)"""
        responses = [
            {"is_open": False, "next_open": "2026-06-22T13:30:00Z",
             "next_close": "2026-06-22T20:00:00Z"},   # pre-open: CLOSED
            {"is_open": True, "next_open": "2026-06-23T13:30:00Z",
             "next_close": "2026-06-22T20:00:00Z"},    # after open: OPEN
        ]
        calls = {"n": 0}

        def fake(url, headers):
            r = responses[min(calls["n"], len(responses) - 1)]
            calls["n"] += 1
            return r

        cal = AlpacaMarketCalendar(_cfg(), http_get=fake)
        s1 = cal.session(_utc(2026, 6, 22, 13, 0))   # 09:00 ET pre-open
        assert s1.is_open is False
        s2 = cal.session(_utc(2026, 6, 22, 13, 35))  # 09:35 ET — market now open
        assert s2.is_open is True                     # MUST have refetched
        assert calls["n"] == 2

    def test_falls_back_to_offline_on_error(self):
        def boom(url, headers):
            raise RuntimeError("clock api down")

        cal = AlpacaMarketCalendar(_cfg(), http_get=boom)
        s = cal.session(_utc(2025, 6, 18, 11, 0))
        # Offline says open at 11:00 ET on a non-holiday weekday.
        assert s.is_open is True
