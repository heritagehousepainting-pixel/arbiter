"""Tests for PITGateway and FixtureSource — Lane 3 core.

Covers INTERFACES.md §3, §11 convention 1 (no look-ahead).

Critical: the look-ahead canary test verifies that data timestamped AFTER
as_of is NEVER returned by PITGateway.get().
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from arbiter.data.pit import Bar, FixtureSource, PITGateway


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = timezone.utc

def _ts(year, month, day, hour=0, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=_UTC)


def _make_gateway_with_fixture(field, ticker, ts, value) -> tuple[PITGateway, FixtureSource]:
    """Create a PITGateway with one FixtureSource entry."""
    src = FixtureSource()
    src.add(field, ticker, ts, value)
    gw = PITGateway()
    gw.register_source(field, src)
    return gw, src


# ---------------------------------------------------------------------------
# Bar dataclass
# ---------------------------------------------------------------------------

class TestBar:
    def test_construct(self):
        bar = Bar(
            ticker="AAPL",
            timestamp=_ts(2026, 1, 15),
            open=150.0,
            high=155.0,
            low=148.0,
            close=153.0,
            volume=1_000_000.0,
        )
        assert bar.ticker == "AAPL"
        assert bar.close == 153.0

    def test_mutable(self):
        """Bar is a regular dataclass (mutable — used as intermediate object)."""
        bar = Bar(
            ticker="GOOG",
            timestamp=_ts(2026, 1, 15),
            open=100.0,
            high=105.0,
            low=99.0,
            close=103.0,
            volume=500_000.0,
        )
        bar.close = 104.0
        assert bar.close == 104.0


# ---------------------------------------------------------------------------
# FixtureSource
# ---------------------------------------------------------------------------

class TestFixtureSource:
    def test_returns_value_when_known(self):
        src = FixtureSource()
        ts = _ts(2026, 1, 15)
        src.add("price_close", "AAPL", ts, 153.0)
        result = src.get_pit("price_close", "AAPL", ts)
        assert result == 153.0

    def test_returns_none_before_data_exists(self):
        src = FixtureSource()
        ts = _ts(2026, 1, 15)
        src.add("price_close", "AAPL", ts, 153.0)
        # Query before the data point
        earlier = _ts(2026, 1, 14)
        result = src.get_pit("price_close", "AAPL", earlier)
        assert result is None

    def test_returns_most_recent_at_exact_timestamp(self):
        src = FixtureSource()
        ts1 = _ts(2026, 1, 14)
        ts2 = _ts(2026, 1, 15)
        src.add("price_close", "AAPL", ts1, 150.0)
        src.add("price_close", "AAPL", ts2, 155.0)
        result = src.get_pit("price_close", "AAPL", ts2)
        assert result == 155.0

    def test_returns_most_recent_before_as_of(self):
        """Returns the most recent value whose timestamp ≤ as_of."""
        src = FixtureSource()
        ts1 = _ts(2026, 1, 14)
        ts2 = _ts(2026, 1, 15)
        src.add("price_close", "AAPL", ts1, 150.0)
        src.add("price_close", "AAPL", ts2, 155.0)
        # Ask for 14th — should get the 14th value only
        result = src.get_pit("price_close", "AAPL", _ts(2026, 1, 14, 23, 59))
        assert result == 150.0

    def test_lookahead_canary_future_value_not_returned(self):
        """CRITICAL: A value timestamped AFTER as_of must NEVER be returned."""
        src = FixtureSource()
        future_ts = _ts(2026, 1, 20)
        src.add("price_close", "AAPL", future_ts, 999.0)

        # Query with as_of = Jan 15 (5 days before the data point)
        as_of = _ts(2026, 1, 15)
        result = src.get_pit("price_close", "AAPL", as_of)
        assert result is None, (
            "LOOK-AHEAD VIOLATION: returned a value timestamped after as_of"
        )

    def test_returns_none_for_unknown_ticker(self):
        src = FixtureSource()
        src.add("price_close", "AAPL", _ts(2026, 1, 15), 153.0)
        result = src.get_pit("price_close", "GOOG", _ts(2026, 1, 15))
        assert result is None

    def test_returns_none_for_unknown_field(self):
        src = FixtureSource()
        src.add("price_close", "AAPL", _ts(2026, 1, 15), 153.0)
        result = src.get_pit("price_open", "AAPL", _ts(2026, 1, 15))
        assert result is None


# ---------------------------------------------------------------------------
# PITGateway
# ---------------------------------------------------------------------------

class TestPITGateway:
    def test_get_returns_none_for_no_source(self):
        """No source registered → get() returns None."""
        gw = PITGateway()
        result = gw.get("price_close", "AAPL", _ts(2026, 1, 15))
        assert result is None

    def test_get_returns_fixture_value_when_known(self):
        ts = _ts(2026, 1, 15)
        gw, _ = _make_gateway_with_fixture("price_close", "AAPL", ts, 153.0)
        result = gw.get("price_close", "AAPL", ts)
        assert result == 153.0

    def test_get_returns_none_before_data_exists(self):
        ts = _ts(2026, 1, 15)
        gw, _ = _make_gateway_with_fixture("price_close", "AAPL", ts, 153.0)
        # Query 1 day before the data exists
        as_of = _ts(2026, 1, 14)
        result = gw.get("price_close", "AAPL", as_of)
        assert result is None

    def test_get_returns_none_for_unknown_field(self):
        """Unknown field names return None (not an error)."""
        gw = PITGateway()
        result = gw.get("nonexistent_field", "AAPL", _ts(2026, 1, 15))
        assert result is None

    def test_lookahead_canary_gateway_level(self):
        """CRITICAL: PITGateway.get() must NEVER return data after as_of."""
        future_ts = _ts(2026, 6, 1)
        gw, _ = _make_gateway_with_fixture("price_close", "AAPL", future_ts, 200.0)

        # Query at a date well before the fixture timestamp
        as_of = _ts(2026, 1, 1)
        result = gw.get("price_close", "AAPL", as_of)
        assert result is None, (
            "LOOK-AHEAD VIOLATION at gateway level: "
            "data from future was returned for a past as_of"
        )

    def test_register_source_unknown_field_raises(self):
        """register_source raises ValueError for unsupported field names."""
        gw = PITGateway()
        src = FixtureSource()
        with pytest.raises(ValueError, match="Unknown PIT field"):
            gw.register_source("not_a_real_field", src)

    def test_all_supported_fields_can_be_registered(self):
        """All INTERFACES.md §3 fields can be registered without error."""
        from arbiter.data.pit import _SUPPORTED_FIELDS
        gw = PITGateway()
        for field in _SUPPORTED_FIELDS:
            src = FixtureSource()
            gw.register_source(field, src)  # must not raise

    def test_get_price_open(self):
        ts = _ts(2026, 1, 15)
        gw, _ = _make_gateway_with_fixture("price_open", "AAPL", ts, 148.5)
        result = gw.get("price_open", "AAPL", ts)
        assert result == 148.5

    def test_get_adv_20d(self):
        ts = _ts(2026, 1, 15)
        gw, _ = _make_gateway_with_fixture("adv_20d", "AAPL", ts, 5_000_000.0)
        result = gw.get("adv_20d", "AAPL", ts)
        assert result == 5_000_000.0

    def test_get_spread(self):
        ts = _ts(2026, 1, 15)
        gw, _ = _make_gateway_with_fixture("spread", "AAPL", ts, 0.02)
        result = gw.get("spread", "AAPL", ts)
        assert result == 0.02

    def test_get_filing(self):
        ts = _ts(2026, 1, 15)
        filing_data = {"form_type": "4", "insider_id": "0001234567"}
        gw, _ = _make_gateway_with_fixture("filing", "AAPL", ts, filing_data)
        result = gw.get("filing", "AAPL", ts)
        assert result == filing_data

    def test_multiple_sources_independent(self):
        """Different fields use different sources independently."""
        gw = PITGateway()
        ts = _ts(2026, 1, 15)

        src_close = FixtureSource()
        src_close.add("price_close", "AAPL", ts, 153.0)
        gw.register_source("price_close", src_close)

        src_open = FixtureSource()
        src_open.add("price_open", "AAPL", ts, 148.5)
        gw.register_source("price_open", src_open)

        assert gw.get("price_close", "AAPL", ts) == 153.0
        assert gw.get("price_open", "AAPL", ts) == 148.5

    def test_exact_as_of_boundary_inclusive(self):
        """Data with timestamp == as_of must be returned (inclusive boundary)."""
        exact_ts = _ts(2026, 1, 15, 9, 30)
        gw, _ = _make_gateway_with_fixture("price_close", "AAPL", exact_ts, 150.0)
        result = gw.get("price_close", "AAPL", exact_ts)
        assert result == 150.0

    def test_one_second_after_as_of_not_returned(self):
        """Data at as_of+1s must NOT be returned."""
        data_ts = _ts(2026, 1, 15, 9, 30) + timedelta(seconds=1)
        as_of = _ts(2026, 1, 15, 9, 30)
        gw, _ = _make_gateway_with_fixture("price_close", "AAPL", data_ts, 999.0)
        result = gw.get("price_close", "AAPL", as_of)
        assert result is None
