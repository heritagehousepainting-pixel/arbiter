"""Tests for Lane 3 Wave-B price sources: Alpaca + Stooq + gateway builder.

Coverage targets
----------------
* AlpacaPriceSource.bars() parses JSON → Bar list correctly.
* StooqPriceSource.bars() parses CSV → Bar list correctly.
* Fallback: when Alpaca raises, Stooq result is returned.
* Delisted tickers are NOT filtered (pass-through).
* Bars with timestamps at or after the requested ``end`` are NEVER returned
  (look-ahead guard).
* ``build_price_gateway`` wires Alpaca primary + Stooq fallback into
  a PITGateway whose ``get()`` returns scalar values.

All network calls are mocked — no real Alpaca or Stooq requests.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, PropertyMock

import httpx
import pytest
import requests

from arbiter.config import Config
from arbiter.data.pit import Bar, PITGateway
from arbiter.data.sources.alpaca import AlpacaPriceSource, _parse_bar
from arbiter.data.sources.stooq import StooqPriceSource, _parse_stooq_csv
from arbiter.data.sources._gateway import build_price_gateway, _FallbackPriceAdapter

_UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=_UTC)


def _make_config(**overrides: object) -> Config:
    """Build a minimal Config for tests."""
    defaults: dict[str, object] = dict(
        live_trading=False,
        executor_backend="sim",
        db_path=":memory:",
        audit_path="/tmp/audit.jsonl",
        metrics_path="/tmp/metrics.jsonl",
        max_position_pct=0.05,
        max_sector_pct=0.20,
        max_gross_pct=0.80,
        max_open_positions=20,
        adv_cap_pct=0.02,
        alpaca_api_key="test-key",
        alpaca_secret_key="test-secret",
        alpaca_paper_base_url="https://paper-api.alpaca.markets",
        alpaca_data_base_url="https://data.alpaca.markets",
        alpaca_timeout=5.0,
        edgar_user_agent="test-agent",
        kill_switch_url="",
        alert_webhook_url="",
    )
    defaults.update(overrides)
    return Config(**defaults)  # type: ignore[arg-type]


def _alpaca_json(bars: list[dict]) -> dict:
    """Wrap raw bar dicts in the Alpaca v2 response envelope."""
    return {"bars": bars, "next_page_token": None}


def _alpaca_bar_dict(
    t: str,
    o: float = 100.0,
    h: float = 105.0,
    l: float = 98.0,
    c: float = 102.0,
    v: float = 1_000_000.0,
) -> dict:
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


STOOQ_CSV_HEADER = "Date,Open,High,Low,Close,Volume\n"


def _stooq_csv(*rows: tuple) -> str:
    """Build a Stooq-style CSV string from (date, o, h, l, c, v) tuples."""
    lines = [STOOQ_CSV_HEADER]
    for row in rows:
        lines.append(",".join(str(x) for x in row) + "\n")
    return "".join(lines)


# ---------------------------------------------------------------------------
# AlpacaPriceSource — unit-level parse tests (no HTTP)
# ---------------------------------------------------------------------------

class TestAlpacaParseBar:
    def test_parses_all_fields(self):
        raw = _alpaca_bar_dict(t="2026-01-15T00:00:00Z", o=148.0, h=155.0, l=147.0, c=153.0, v=2_000_000.0)
        bar = _parse_bar("AAPL", raw)
        assert bar.ticker == "AAPL"
        assert bar.open == 148.0
        assert bar.high == 155.0
        assert bar.low == 147.0
        assert bar.close == 153.0
        assert bar.volume == 2_000_000.0

    def test_timestamp_is_utc(self):
        raw = _alpaca_bar_dict(t="2026-01-15T00:00:00Z")
        bar = _parse_bar("AAPL", raw)
        assert bar.timestamp.tzinfo is not None
        assert bar.timestamp.utcoffset().total_seconds() == 0

    def test_timestamp_correct_date(self):
        raw = _alpaca_bar_dict(t="2026-03-10T00:00:00Z")
        bar = _parse_bar("AAPL", raw)
        assert bar.timestamp.year == 2026
        assert bar.timestamp.month == 3
        assert bar.timestamp.day == 10


# ---------------------------------------------------------------------------
# AlpacaPriceSource — mocked HTTP tests
# ---------------------------------------------------------------------------

class TestAlpacaPriceSource:
    """All tests mock httpx.Client to avoid real network calls."""

    def _make_source(self, **cfg_overrides) -> AlpacaPriceSource:
        return AlpacaPriceSource(_make_config(**cfg_overrides))

    def _mock_response(self, data: dict, status_code: int = 200) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = data
        resp.raise_for_status = MagicMock()
        return resp

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_bars_parses_response(self, mock_client_cls):
        """Mocked Alpaca response is parsed into Bar objects."""
        raw_bars = [
            _alpaca_bar_dict(t="2026-01-13T00:00:00Z", c=150.0),
            _alpaca_bar_dict(t="2026-01-14T00:00:00Z", c=151.0),
            _alpaca_bar_dict(t="2026-01-15T00:00:00Z", c=153.0),
        ]
        mock_resp = self._mock_response(_alpaca_json(raw_bars))
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp

        src = self._make_source()
        bars = src.bars("AAPL", _ts(2026, 1, 13), _ts(2026, 1, 16))

        assert len(bars) == 3
        assert all(isinstance(b, Bar) for b in bars)
        assert bars[0].close == 150.0
        assert bars[2].close == 153.0

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_bars_sorted_ascending(self, mock_client_cls):
        """Bars returned in ascending timestamp order."""
        raw_bars = [
            _alpaca_bar_dict(t="2026-01-15T00:00:00Z", c=153.0),
            _alpaca_bar_dict(t="2026-01-13T00:00:00Z", c=150.0),
        ]
        mock_resp = self._mock_response(_alpaca_json(raw_bars))
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp

        bars = self._make_source().bars("AAPL", _ts(2026, 1, 13), _ts(2026, 1, 16))
        ts_list = [b.timestamp for b in bars]
        assert ts_list == sorted(ts_list)

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_bar_at_end_is_excluded(self, mock_client_cls):
        """Bar with timestamp == end must be excluded (look-ahead guard)."""
        end = _ts(2026, 1, 15)
        raw_bars = [
            _alpaca_bar_dict(t="2026-01-14T00:00:00Z", c=150.0),
            _alpaca_bar_dict(t="2026-01-15T00:00:00Z", c=999.0),  # exactly at end — must be excluded
        ]
        mock_resp = self._mock_response(_alpaca_json(raw_bars))
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp

        bars = self._make_source().bars("AAPL", _ts(2026, 1, 13), end)
        assert len(bars) == 1
        assert bars[0].close == 150.0

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_bar_after_end_is_excluded(self, mock_client_cls):
        """Bar with timestamp > end must be excluded."""
        end = _ts(2026, 1, 14)
        raw_bars = [
            _alpaca_bar_dict(t="2026-01-13T00:00:00Z", c=150.0),
            _alpaca_bar_dict(t="2026-01-15T00:00:00Z", c=999.0),  # after end
        ]
        mock_resp = self._mock_response(_alpaca_json(raw_bars))
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp

        bars = self._make_source().bars("AAPL", _ts(2026, 1, 13), end)
        assert len(bars) == 1
        assert bars[0].close == 150.0

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_delisted_ticker_not_filtered(self, mock_client_cls):
        """Delisted ticker with valid data is returned as-is (survivorship neutral)."""
        raw_bars = [_alpaca_bar_dict(t="2022-06-01T00:00:00Z", c=5.0)]
        mock_resp = self._mock_response(_alpaca_json(raw_bars))
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp

        bars = self._make_source().bars("DELISTED", _ts(2022, 6, 1), _ts(2022, 6, 2))
        assert len(bars) == 1
        assert bars[0].ticker == "DELISTED"

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_404_returns_empty(self, mock_client_cls):
        """HTTP 404 (unknown/delisted ticker) returns empty list, no exception."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp

        bars = self._make_source().bars("GONE", _ts(2020, 1, 1), _ts(2020, 1, 10))
        assert bars == []

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_422_returns_empty(self, mock_client_cls):
        """HTTP 422 (invalid date range) returns empty list."""
        mock_resp = MagicMock()
        mock_resp.status_code = 422
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp

        bars = self._make_source().bars("AAPL", _ts(2030, 1, 1), _ts(2030, 1, 10))
        assert bars == []

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_empty_bars_response(self, mock_client_cls):
        """Response with empty bars list → empty result."""
        mock_resp = self._mock_response({"bars": [], "next_page_token": None})
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp

        bars = self._make_source().bars("AAPL", _ts(2026, 1, 1), _ts(2026, 1, 5))
        assert bars == []

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_request_exception_returns_empty(self, mock_client_cls):
        """Network error returns empty list (graceful degradation)."""
        mock_client_cls.return_value.__enter__.return_value.get.side_effect = (
            httpx.ConnectError("refused")
        )

        bars = self._make_source().bars("AAPL", _ts(2026, 1, 1), _ts(2026, 1, 5))
        assert bars == []

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_pagination(self, mock_client_cls):
        """Two pages of results are combined into one list."""
        page1 = {"bars": [_alpaca_bar_dict(t="2026-01-13T00:00:00Z", c=150.0)], "next_page_token": "tok1"}
        page2 = {"bars": [_alpaca_bar_dict(t="2026-01-14T00:00:00Z", c=151.0)], "next_page_token": None}

        mock_get = MagicMock()
        mock_resp1 = MagicMock()
        mock_resp1.status_code = 200
        mock_resp1.json.return_value = page1
        mock_resp1.raise_for_status = MagicMock()
        mock_resp2 = MagicMock()
        mock_resp2.status_code = 200
        mock_resp2.json.return_value = page2
        mock_resp2.raise_for_status = MagicMock()

        mock_get.side_effect = [mock_resp1, mock_resp2]
        mock_client_cls.return_value.__enter__.return_value.get = mock_get

        bars = self._make_source().bars("AAPL", _ts(2026, 1, 13), _ts(2026, 1, 16))
        assert len(bars) == 2


# ---------------------------------------------------------------------------
# StooqPriceSource — CSV parsing (no HTTP)
# ---------------------------------------------------------------------------

class TestStooqParseCSV:
    def test_parses_standard_csv(self):
        end = _ts(2026, 1, 20)
        csv_text = _stooq_csv(
            ("2026-01-13", 100.0, 105.0, 98.0, 102.0, 1_000_000),
            ("2026-01-14", 102.0, 107.0, 100.0, 105.0, 1_100_000),
        )
        bars = _parse_stooq_csv("AAPL", csv_text, end)
        assert len(bars) == 2
        assert bars[0].date_str if hasattr(bars[0], "date_str") else bars[0].timestamp.day == 13

    def test_bar_at_end_excluded(self):
        end = _ts(2026, 1, 14)
        csv_text = _stooq_csv(
            ("2026-01-13", 100.0, 105.0, 98.0, 102.0, 1_000_000),
            ("2026-01-14", 999.0, 999.0, 999.0, 999.0, 1),  # exactly at end — excluded
        )
        bars = _parse_stooq_csv("AAPL", csv_text, end)
        assert len(bars) == 1
        assert bars[0].close == 102.0

    def test_bar_after_end_excluded(self):
        end = _ts(2026, 1, 13)
        csv_text = _stooq_csv(
            ("2026-01-12", 100.0, 105.0, 98.0, 102.0, 1_000_000),
            ("2026-01-15", 999.0, 999.0, 999.0, 999.0, 1),  # after end
        )
        bars = _parse_stooq_csv("AAPL", csv_text, end)
        assert len(bars) == 1

    def test_empty_csv_returns_empty(self):
        bars = _parse_stooq_csv("AAPL", "", _ts(2026, 1, 20))
        assert bars == []

    def test_no_data_row_skipped(self):
        csv_text = "Date,Open,High,Low,Close,Volume\nNo data\n"
        bars = _parse_stooq_csv("AAPL", csv_text, _ts(2026, 1, 20))
        assert bars == []

    def test_bad_numeric_row_skipped(self):
        csv_text = "Date,Open,High,Low,Close,Volume\n2026-01-13,INVALID,105,98,102,1000000\n"
        bars = _parse_stooq_csv("AAPL", csv_text, _ts(2026, 1, 20))
        assert bars == []

    def test_all_bar_fields_correct(self):
        end = _ts(2026, 1, 20)
        csv_text = _stooq_csv(("2026-01-15", 148.0, 156.0, 147.0, 153.0, 2_000_000))
        bars = _parse_stooq_csv("MSFT", csv_text, end)
        assert len(bars) == 1
        b = bars[0]
        assert b.ticker == "MSFT"
        assert b.open == 148.0
        assert b.high == 156.0
        assert b.low == 147.0
        assert b.close == 153.0
        assert b.volume == 2_000_000.0

    def test_sorted_ascending(self):
        end = _ts(2026, 1, 20)
        csv_text = _stooq_csv(
            ("2026-01-15", 102.0, 107.0, 100.0, 105.0, 1_000_000),
            ("2026-01-13", 100.0, 105.0, 98.0, 102.0, 900_000),
        )
        bars = _parse_stooq_csv("AAPL", csv_text, end)
        ts_list = [b.timestamp for b in bars]
        assert ts_list == sorted(ts_list)


# ---------------------------------------------------------------------------
# StooqPriceSource — mocked HTTP tests
# ---------------------------------------------------------------------------

class TestStooqPriceSource:
    def _make_source(self) -> StooqPriceSource:
        session = requests.Session()
        src = StooqPriceSource(timeout=5.0, session=session)
        return src

    def _mock_session_get(self, src: StooqPriceSource, text: str, status_code: int = 200):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.text = text
        mock_resp.raise_for_status = MagicMock()
        src._session.get = MagicMock(return_value=mock_resp)
        return mock_resp

    def test_bars_parses_csv(self):
        """Mocked Stooq CSV response is parsed into Bar objects."""
        src = self._make_source()
        csv_text = _stooq_csv(
            ("2026-01-13", 100.0, 105.0, 98.0, 102.0, 1_000_000),
            ("2026-01-14", 102.0, 107.0, 100.0, 105.0, 1_100_000),
        )
        self._mock_session_get(src, csv_text)

        bars = src.bars("AAPL.US", _ts(2026, 1, 13), _ts(2026, 1, 16))
        assert len(bars) == 2
        assert all(isinstance(b, Bar) for b in bars)
        assert bars[0].close == 102.0
        assert bars[1].close == 105.0

    def test_bar_after_requested_end_excluded(self):
        """Bars returned by Stooq at or after end are dropped."""
        src = self._make_source()
        end = _ts(2026, 1, 14)
        csv_text = _stooq_csv(
            ("2026-01-13", 100.0, 105.0, 98.0, 102.0, 1_000_000),
            ("2026-01-14", 999.0, 999.0, 999.0, 999.0, 1),  # at end — excluded
            ("2026-01-15", 999.0, 999.0, 999.0, 999.0, 1),  # after end — excluded
        )
        self._mock_session_get(src, csv_text)

        bars = src.bars("AAPL.US", _ts(2026, 1, 12), end)
        assert len(bars) == 1
        assert bars[0].close == 102.0

    def test_delisted_ticker_not_filtered(self):
        """Delisted ticker data is returned as-is (no survivorship filter)."""
        src = self._make_source()
        csv_text = _stooq_csv(("2020-01-10", 5.0, 6.0, 4.5, 5.5, 500_000))
        self._mock_session_get(src, csv_text)

        bars = src.bars("GONE.US", _ts(2020, 1, 10), _ts(2020, 1, 12))
        assert len(bars) == 1
        assert bars[0].ticker == "GONE.US"

    def test_404_returns_empty(self):
        src = self._make_source()
        self._mock_session_get(src, "", status_code=404)
        bars = src.bars("AAPL.US", _ts(2026, 1, 1), _ts(2026, 1, 5))
        assert bars == []

    def test_empty_response_returns_empty(self):
        src = self._make_source()
        self._mock_session_get(src, "")
        bars = src.bars("AAPL.US", _ts(2026, 1, 1), _ts(2026, 1, 5))
        assert bars == []

    def test_request_exception_returns_empty(self):
        src = self._make_source()
        src._session.get = MagicMock(side_effect=requests.ConnectionError("refused"))
        bars = src.bars("AAPL.US", _ts(2026, 1, 1), _ts(2026, 1, 5))
        assert bars == []


# ---------------------------------------------------------------------------
# Fallback adapter tests
# ---------------------------------------------------------------------------

class TestFallbackPriceAdapter:
    def _make_primary(self, return_value=None, raise_exc=None):
        m = MagicMock()
        if raise_exc is not None:
            m.get_pit.side_effect = raise_exc
        else:
            m.get_pit.return_value = return_value
        return m

    def test_primary_result_returned_when_available(self):
        primary = self._make_primary(return_value=150.0)
        secondary = self._make_primary(return_value=200.0)
        adapter = _FallbackPriceAdapter(primary=primary, secondary=secondary)

        result = adapter.get_pit("price_close", "AAPL", _ts(2026, 1, 15))
        assert result == 150.0
        secondary.get_pit.assert_not_called()

    def test_falls_back_when_primary_returns_none(self):
        primary = self._make_primary(return_value=None)
        secondary = self._make_primary(return_value=145.0)
        adapter = _FallbackPriceAdapter(primary=primary, secondary=secondary)

        result = adapter.get_pit("price_close", "AAPL", _ts(2026, 1, 15))
        assert result == 145.0

    def test_falls_back_when_primary_raises(self):
        """Critical: if Alpaca raises, Stooq fallback is used."""
        primary = self._make_primary(raise_exc=RuntimeError("alpaca down"))
        secondary = self._make_primary(return_value=148.0)
        adapter = _FallbackPriceAdapter(primary=primary, secondary=secondary)

        result = adapter.get_pit("price_close", "AAPL", _ts(2026, 1, 15))
        assert result == 148.0

    def test_returns_none_when_both_fail(self):
        """Both primary and secondary fail → None (fail-closed)."""
        primary = self._make_primary(raise_exc=RuntimeError("alpaca down"))
        secondary = self._make_primary(return_value=None)
        adapter = _FallbackPriceAdapter(primary=primary, secondary=secondary)

        result = adapter.get_pit("price_close", "AAPL", _ts(2026, 1, 15))
        assert result is None

    def test_returns_none_when_both_raise(self):
        primary = self._make_primary(raise_exc=RuntimeError("alpaca down"))
        secondary = self._make_primary(raise_exc=RuntimeError("stooq down"))
        adapter = _FallbackPriceAdapter(primary=primary, secondary=secondary)

        result = adapter.get_pit("price_close", "AAPL", _ts(2026, 1, 15))
        assert result is None


# ---------------------------------------------------------------------------
# build_price_gateway integration
# ---------------------------------------------------------------------------

class TestBuildPriceGateway:
    def test_returns_pit_gateway(self):
        config = _make_config()
        pit = build_price_gateway(config)
        assert isinstance(pit, PITGateway)

    def test_gateway_has_price_fields_registered(self):
        """All four price fields must be registered so get() can delegate."""
        config = _make_config()
        pit = build_price_gateway(config)

        # We don't hit the network; we just verify the fields are registered
        # by checking that get() doesn't raise (returns None when no data).
        as_of = _ts(2026, 1, 15)
        for field in ("price_open", "price_close", "spread", "adv_20d"):
            # Will call Alpaca then Stooq; both fail cleanly → None.
            # We patch both sources to return None to avoid real network calls.
            with (
                patch(
                    "arbiter.data.sources.alpaca.httpx.Client",
                ) as mock_alpaca,
            ):
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {"bars": [], "next_page_token": None}
                mock_resp.raise_for_status = MagicMock()
                mock_alpaca.return_value.__enter__.return_value.get.return_value = mock_resp

                with patch("requests.Session.get") as mock_stooq:
                    stooq_resp = MagicMock()
                    stooq_resp.status_code = 200
                    stooq_resp.text = ""
                    stooq_resp.raise_for_status = MagicMock()
                    mock_stooq.return_value = stooq_resp

                    result = pit.get(field, "AAPL", as_of)
                    assert result is None  # fail-closed when no data

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_gateway_returns_alpaca_value(self, mock_client_cls):
        """gateway.get('price_close', ...) returns Alpaca close price."""
        as_of = _ts(2026, 1, 15)

        # Alpaca returns one bar at Jan 15.
        raw_bars = [_alpaca_bar_dict(t="2026-01-15T00:00:00Z", c=153.0)]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _alpaca_json(raw_bars)
        mock_resp.raise_for_status = MagicMock()
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp

        config = _make_config()
        pit = build_price_gateway(config)
        close = pit.get("price_close", "AAPL", as_of)
        assert close == 153.0

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_gateway_falls_back_to_stooq(self, mock_client_cls):
        """When Alpaca raises, the gateway returns the Stooq value."""
        mock_client_cls.return_value.__enter__.return_value.get.side_effect = (
            httpx.ConnectError("alpaca unreachable")
        )

        as_of = _ts(2026, 1, 15)
        stooq_csv = _stooq_csv(("2026-01-15", 148.0, 156.0, 147.0, 149.0, 1_000_000))

        config = _make_config()
        pit = build_price_gateway(config)

        # Patch Stooq's session inside the adapter's secondary source.
        with patch("requests.Session.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = stooq_csv
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            close = pit.get("price_close", "AAPL", as_of)

        assert close == 149.0

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_gateway_returns_none_when_both_fail(self, mock_client_cls):
        """Fail-closed: both sources empty → gateway returns None."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"bars": [], "next_page_token": None}
        mock_resp.raise_for_status = MagicMock()
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp

        config = _make_config()
        pit = build_price_gateway(config)

        with patch("requests.Session.get") as mock_get:
            stooq_resp = MagicMock()
            stooq_resp.status_code = 200
            stooq_resp.text = ""
            stooq_resp.raise_for_status = MagicMock()
            mock_get.return_value = stooq_resp

            result = pit.get("price_close", "AAPL", _ts(2026, 1, 15))

        assert result is None


# ---------------------------------------------------------------------------
# PIT look-ahead canary — sources layer
# ---------------------------------------------------------------------------

class TestLookAheadGuard:
    """End-to-end look-ahead checks at the source layer."""

    def test_alpaca_bar_after_end_never_returned(self):
        """Bar dated after requested end must NEVER appear in result."""
        end = _ts(2026, 1, 14)

        with patch("arbiter.data.sources.alpaca.httpx.Client") as mock_cls:
            raw_bars = [
                _alpaca_bar_dict(t="2026-01-13T00:00:00Z", c=100.0),
                _alpaca_bar_dict(t="2026-01-15T00:00:00Z", c=999.0),  # future
            ]
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = _alpaca_json(raw_bars)
            mock_resp.raise_for_status = MagicMock()
            mock_cls.return_value.__enter__.return_value.get.return_value = mock_resp

            bars = AlpacaPriceSource(_make_config()).bars("AAPL", _ts(2026, 1, 10), end)

        assert all(b.timestamp < end for b in bars), (
            "LOOK-AHEAD VIOLATION: Alpaca source returned bar after requested end"
        )
        closes = [b.close for b in bars]
        assert 999.0 not in closes

    def test_stooq_bar_after_end_never_returned(self):
        """Stooq CSV bar dated after requested end must NEVER appear."""
        end = _ts(2026, 1, 14)
        csv_text = _stooq_csv(
            ("2026-01-13", 100.0, 105.0, 98.0, 100.0, 1_000_000),
            ("2026-01-15", 999.0, 999.0, 999.0, 999.0, 1),
        )

        src = StooqPriceSource()
        with patch.object(src, "_session") as mock_session:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = csv_text
            mock_resp.raise_for_status = MagicMock()
            mock_session.get.return_value = mock_resp

            bars = src.bars("AAPL.US", _ts(2026, 1, 10), end)

        assert all(b.timestamp < end for b in bars), (
            "LOOK-AHEAD VIOLATION: Stooq source returned bar after requested end"
        )
        closes = [b.close for b in bars]
        assert 999.0 not in closes


# ---------------------------------------------------------------------------
# Window-end regression: +1 day (not +1 second) still includes as_of bar
# ---------------------------------------------------------------------------

class TestWindowEndFix:
    """Regression suite for the window_end = as_of + 1 day fix.

    Verifies that:
    (a) The bar timestamped exactly at as_of midnight IS returned via get_pit().
    (b) A bar timestamped one day AFTER as_of is NOT returned via get_pit().
    (c) The eligibility filter (timestamp <= as_of) is the final guard.
    """

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_alpaca_get_pit_returns_as_of_bar(self, mock_client_cls):
        """Bar exactly at as_of midnight must be returned by get_pit()."""
        as_of = _ts(2026, 1, 15)

        raw_bars = [_alpaca_bar_dict(t="2026-01-15T00:00:00Z", c=155.0)]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _alpaca_json(raw_bars)
        mock_resp.raise_for_status = MagicMock()
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp

        src = AlpacaPriceSource(_make_config())
        result = src.get_pit("price_close", "AAPL", as_of)
        assert result == 155.0, (
            "REGRESSION: bar at as_of midnight must be included after window_end fix"
        )

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_alpaca_get_pit_excludes_next_day_bar(self, mock_client_cls):
        """Bar one day after as_of must NOT be returned by get_pit()."""
        as_of = _ts(2026, 1, 15)

        # The mock returns only the next-day bar — get_pit should return None.
        raw_bars = [_alpaca_bar_dict(t="2026-01-16T00:00:00Z", c=999.0)]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _alpaca_json(raw_bars)
        mock_resp.raise_for_status = MagicMock()
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp

        src = AlpacaPriceSource(_make_config())
        result = src.get_pit("price_close", "AAPL", as_of)
        assert result is None, (
            "LOOK-AHEAD VIOLATION: next-day bar must not be returned by get_pit()"
        )

    def test_stooq_get_pit_returns_as_of_bar(self):
        """Bar exactly at as_of midnight must be returned by Stooq get_pit()."""
        as_of = _ts(2026, 1, 15)
        csv_text = _stooq_csv(("2026-01-15", 155.0, 158.0, 153.0, 155.0, 1_000_000))

        src = StooqPriceSource()
        with patch.object(src, "_session") as mock_session:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = csv_text
            mock_resp.raise_for_status = MagicMock()
            mock_session.get.return_value = mock_resp

            result = src.get_pit("price_close", "AAPL.US", as_of)

        assert result == 155.0, (
            "REGRESSION: Stooq bar at as_of midnight must be included after window_end fix"
        )

    def test_stooq_get_pit_excludes_next_day_bar(self):
        """Stooq bar one day after as_of must NOT be returned by get_pit()."""
        as_of = _ts(2026, 1, 15)
        # CSV only has the next-day bar.
        csv_text = _stooq_csv(("2026-01-16", 999.0, 999.0, 999.0, 999.0, 1))

        src = StooqPriceSource()
        with patch.object(src, "_session") as mock_session:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = csv_text
            mock_resp.raise_for_status = MagicMock()
            mock_session.get.return_value = mock_resp

            result = src.get_pit("price_close", "AAPL.US", as_of)

        assert result is None, (
            "LOOK-AHEAD VIOLATION: Stooq next-day bar must not be returned by get_pit()"
        )


# ---------------------------------------------------------------------------
# adv_20d look-ahead canary — gateway routes via adv.py (as_of-1 window)
# ---------------------------------------------------------------------------

class TestAdvLookAheadCanary:
    """Structural proof that adv_20d from build_price_gateway never includes
    the as_of day's volume and uses the as_of−1-ending window.

    Strategy: register a FixtureSource for price_close via the PITGateway
    directly (bypassing network) and verify that the adv_20d result excludes
    the current-day bar.
    """

    def test_adv_20d_excludes_as_of_day_volume(self):
        """adv_20d must NOT include the bar whose timestamp == as_of.

        We inject a 'poison' bar at as_of with a volume that would visibly
        shift the ADV result.  The canonical adv.py path ends the window at
        as_of (exclusive), so cursor < as_of means the last counted bar is
        as_of−1.
        """
        from arbiter.data.adv import adv_20d, make_adv_fixture_pit

        as_of = _ts(2026, 2, 28)  # Saturday — non-trading, but that's OK for this unit
        # Build 25 trading-day bars ending the day before as_of.
        bars: list[tuple[datetime, float, float]] = []
        # Walk backward 35 calendar days from as_of to build history.
        from datetime import timedelta
        for i in range(35, 0, -1):
            day = as_of - timedelta(days=i)
            bars.append((day, 100.0, 1_000_000.0))  # close=100, vol=1M

        pit = make_adv_fixture_pit("AAPL", bars)

        # Now inject the as_of-day bar with a wildly different volume.
        from arbiter.data.pit import Bar, FixtureSource
        poison_vol = 999_999_999.0
        poison_bar = Bar(
            ticker="AAPL", timestamp=as_of, open=100.0, high=100.0, low=100.0,
            close=100.0, volume=poison_vol,
        )
        # Retrieve the existing close source and add the poison bar.
        close_src: FixtureSource = pit._sources["price_close"]  # type: ignore[assignment]
        close_src.add("price_close", "AAPL", as_of, poison_bar)

        result = adv_20d("AAPL", as_of, pit)
        # If adv included the poison bar, result would be enormously large.
        assert result is not None
        expected_adv = 100.0 * 1_000_000.0  # 100M — only from clean history bars
        assert result == pytest.approx(expected_adv, rel=1e-6), (
            f"LOOK-AHEAD VIOLATION: adv_20d={result} includes as_of bar "
            f"(expected ~{expected_adv}, poison volume={poison_vol})"
        )

    def test_adv_20d_gateway_field_routes_via_adv_module(self):
        """build_price_gateway() must register adv_20d via adv.py, not the raw adapter.

        After calling build_price_gateway(), reading adv_20d via pit.get() must
        go through the spec-compliant _ADVSource path.  We verify this by
        checking that the registered source for adv_20d is NOT a _FallbackPriceAdapter.
        """
        from arbiter.data.adv import _ADVSource
        from arbiter.data.sources._gateway import _FallbackPriceAdapter

        config = _make_config()
        pit = build_price_gateway(config)

        # Internal check: adv_20d source must be _ADVSource, not _FallbackPriceAdapter.
        adv_source = pit._sources.get("adv_20d")
        assert adv_source is not None, "adv_20d must be registered in gateway"
        assert isinstance(adv_source, _ADVSource), (
            f"adv_20d source must be _ADVSource (got {type(adv_source).__name__}). "
            "The raw _FallbackPriceAdapter was incorrectly registered."
        )
        assert not isinstance(adv_source, _FallbackPriceAdapter), (
            "adv_20d must NOT be routed through _FallbackPriceAdapter "
            "(would bypass as_of-1 window requirement)"
        )


# ===========================================================================
# W-DATA — Stooq .US symbol mapping (audit C4)
# ===========================================================================

class TestStooqSymbolMapping:
    """Bare ticker → TICKER.US mapping so the Stooq fallback actually resolves.

    Before the fix, production handed the bare ticker (e.g. "AAPL") to Stooq,
    which requires "AAPL.US" → "bars not found" → fallback silently dead.
    """

    def _make_source(self) -> StooqPriceSource:
        return StooqPriceSource(timeout=5.0, session=requests.Session())

    def test_to_stooq_symbol_appends_us_for_bare_ticker(self):
        from arbiter.data.sources.stooq import _to_stooq_symbol
        assert _to_stooq_symbol("AAPL") == "AAPL.US"

    def test_to_stooq_symbol_passes_through_dotted(self):
        from arbiter.data.sources.stooq import _to_stooq_symbol
        # Already-suffixed callers (and tests hardcoding .US) untouched.
        assert _to_stooq_symbol("AAPL.US") == "AAPL.US"
        assert _to_stooq_symbol("brk.b") == "brk.b"

    def test_bare_ticker_request_uses_us_symbol(self):
        """The HTTP request param ``s`` must carry the .US-mapped symbol."""
        src = self._make_source()
        csv_text = _stooq_csv(("2026-01-15", 148.0, 156.0, 147.0, 153.0, 1_000_000))
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = csv_text
        mock_resp.raise_for_status = MagicMock()
        src._session.get = MagicMock(return_value=mock_resp)

        bars = src.bars("AAPL", _ts(2026, 1, 14), _ts(2026, 1, 16))

        # The outgoing request param must be the .US symbol, not the bare ticker.
        _, kwargs = src._session.get.call_args
        assert kwargs["params"]["s"] == "AAPL.US", (
            "Stooq request must map bare ticker → AAPL.US (audit C4)"
        )
        # Parsed bar keeps the ORIGINAL ticker (downstream PIT keying).
        assert len(bars) == 1
        assert bars[0].ticker == "AAPL"

    def test_bare_ticker_resolves_via_get_pit(self):
        """End-to-end: bare ticker now resolves a close through Stooq get_pit."""
        src = self._make_source()
        csv_text = _stooq_csv(("2026-01-15", 148.0, 156.0, 147.0, 153.0, 1_000_000))
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = csv_text
        mock_resp.raise_for_status = MagicMock()
        src._session.get = MagicMock(return_value=mock_resp)

        close = src.get_pit("price_close", "AAPL", _ts(2026, 1, 15))
        assert close == 153.0


# ===========================================================================
# W-DATA — get_bar accessor: real (close, volume) for ADV / volume-anomaly
# ===========================================================================

class TestGetBarAccessor:
    """The new Bar accessor returns close AND volume, unlike scalar get_pit."""

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_alpaca_get_bar_returns_full_bar(self, mock_client_cls):
        as_of = _ts(2026, 1, 15)
        raw_bars = [_alpaca_bar_dict(t="2026-01-15T00:00:00Z", c=153.0, v=2_000_000.0)]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _alpaca_json(raw_bars)
        mock_resp.raise_for_status = MagicMock()
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp

        src = AlpacaPriceSource(_make_config())
        bar = src.get_bar("AAPL", as_of)
        assert isinstance(bar, Bar)
        assert bar.close == 153.0
        assert bar.volume == 2_000_000.0
        # Critically: scalar get_pit returns ONLY the close (no volume).
        assert src.get_pit("price_close", "AAPL", as_of) == 153.0

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_alpaca_get_bar_excludes_future(self, mock_client_cls):
        as_of = _ts(2026, 1, 15)
        raw_bars = [_alpaca_bar_dict(t="2026-01-16T00:00:00Z", c=999.0)]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _alpaca_json(raw_bars)
        mock_resp.raise_for_status = MagicMock()
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp

        bar = AlpacaPriceSource(_make_config()).get_bar("AAPL", as_of)
        assert bar is None, "get_bar must enforce timestamp <= as_of (no look-ahead)"

    def test_stooq_get_bar_returns_full_bar(self):
        src = StooqPriceSource(timeout=5.0, session=requests.Session())
        csv_text = _stooq_csv(("2026-01-15", 148.0, 156.0, 147.0, 153.0, 3_000_000))
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = csv_text
        mock_resp.raise_for_status = MagicMock()
        src._session.get = MagicMock(return_value=mock_resp)

        bar = src.get_bar("AAPL", _ts(2026, 1, 15))
        assert isinstance(bar, Bar)
        assert bar.close == 153.0
        assert bar.volume == 3_000_000.0


class TestFallbackGetBar:
    """_FallbackPriceAdapter.get_bar: Alpaca→Stooq, both-down vs no-data."""

    def _stub(self, *, bar=None, raise_exc=None):
        m = MagicMock()
        if raise_exc is not None:
            m.get_bar.side_effect = raise_exc
        else:
            m.get_bar.return_value = bar
        return m

    def _bar(self, close=100.0, vol=1_000_000.0):
        return Bar(ticker="AAPL", timestamp=_ts(2026, 1, 15),
                   open=close, high=close, low=close, close=close, volume=vol)

    def test_primary_bar_returned(self):
        primary = self._stub(bar=self._bar(close=100.0))
        secondary = self._stub(bar=self._bar(close=200.0))
        adapter = _FallbackPriceAdapter(primary=primary, secondary=secondary)
        bar = adapter.get_bar("AAPL", _ts(2026, 1, 15))
        assert bar.close == 100.0
        secondary.get_bar.assert_not_called()

    def test_falls_back_when_primary_none(self):
        primary = self._stub(bar=None)
        secondary = self._stub(bar=self._bar(close=200.0))
        adapter = _FallbackPriceAdapter(primary=primary, secondary=secondary)
        assert adapter.get_bar("AAPL", _ts(2026, 1, 15)).close == 200.0

    def test_falls_back_when_primary_raises(self):
        primary = self._stub(raise_exc=RuntimeError("alpaca down"))
        secondary = self._stub(bar=self._bar(close=200.0))
        adapter = _FallbackPriceAdapter(primary=primary, secondary=secondary)
        assert adapter.get_bar("AAPL", _ts(2026, 1, 15)).close == 200.0

    def test_no_data_returns_none_quietly(self, caplog):
        """Neither source raises but both return None → no outage signal."""
        import logging as _logging
        primary = self._stub(bar=None)
        secondary = self._stub(bar=None)
        adapter = _FallbackPriceAdapter(primary=primary, secondary=secondary)
        with caplog.at_level(_logging.WARNING):
            assert adapter.get_bar("AAPL", _ts(2026, 1, 15)) is None
        assert "price_both_sources_down" not in caplog.text

    def test_both_down_emits_outage_signal(self, caplog):
        """Both sources RAISE → distinct both-sources-down signal (not silent)."""
        import logging as _logging
        primary = self._stub(raise_exc=RuntimeError("alpaca down"))
        secondary = self._stub(raise_exc=RuntimeError("stooq down"))
        adapter = _FallbackPriceAdapter(primary=primary, secondary=secondary)
        with caplog.at_level(_logging.WARNING):
            result = adapter.get_bar("AAPL", _ts(2026, 1, 15))
        assert result is None  # still fail-closed
        assert "price_both_sources_down" in caplog.text, (
            "An outage (both sources down) must be distinguishable from no-data"
        )


# ===========================================================================
# W-DATA — production-shape ADV: dollar-volume, not price (P0 #1)
# ===========================================================================

class TestADVProductionShape:
    """Prove ADV from a PRODUCTION gateway is dollar-volume (~$100M), not price.

    This is the regression the Bar-fixture tests HID: production
    get_pit("price_close") returns a scalar close, so a naive ADV walk yields
    ~price ($115). With the bar provider, ADV = mean(close*volume).
    """

    def _alpaca_history_response(self, as_of, days, close, volume):
        """Build a mocked Alpaca response with `days` weekday bars ending as_of."""
        from datetime import timedelta
        raw = []
        cursor = as_of
        added = 0
        while added < days:
            if cursor.weekday() < 5:
                raw.append(_alpaca_bar_dict(
                    t=cursor.strftime("%Y-%m-%dT00:00:00Z"), c=close, v=volume,
                ))
                added += 1
            cursor -= timedelta(days=1)
        return _alpaca_json(raw)

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_adv_is_dollar_volume_not_price(self, mock_client_cls):
        from arbiter.data.adv import adv_20d
        as_of = _ts(2026, 6, 1)
        close, volume = 115.0, 1_000_000.0  # dollar-vol = 115_000_000

        # Alpaca returns a generous history; bars() filters by window each day.
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = self._alpaca_history_response(
            as_of, days=40, close=close, volume=volume
        )
        resp.raise_for_status = MagicMock()
        mock_client_cls.return_value.__enter__.return_value.get.return_value = resp

        pit = build_price_gateway(_make_config())
        adv = adv_20d("AAPL", as_of, pit)

        assert adv is not None
        # The bug: ADV ≈ price (~115). The fix: ADV ≈ close*volume (~115M).
        assert adv == pytest.approx(close * volume, rel=1e-6), (
            f"ADV must be dollar-volume (~{close*volume:.0f}), got {adv}"
        )
        assert adv > 1_000_000.0, "ADV must be of $-volume magnitude, not price"

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_2pct_adv_cap_binds_on_thin_name(self, mock_client_cls):
        """With correct dollar ADV, the 2%-ADV cap binds on a thin name.

        Thin name: $50 close, 40k shares/day → ADV = $2,000,000.
        adv_cap_pct = 0.02 → max participation = $40,000.
        A $100,000 desired order is CAPPED to $40,000.
        """
        from arbiter.data.adv import adv_20d
        as_of = _ts(2026, 6, 1)
        close, volume = 50.0, 40_000.0  # thin: dollar ADV = 2_000_000

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = self._alpaca_history_response(
            as_of, days=40, close=close, volume=volume
        )
        resp.raise_for_status = MagicMock()
        mock_client_cls.return_value.__enter__.return_value.get.return_value = resp

        config = _make_config()  # adv_cap_pct = 0.02
        pit = build_price_gateway(config)
        adv = adv_20d("AAPL", as_of, pit)

        assert adv == pytest.approx(2_000_000.0, rel=1e-6)
        cap_dollars = adv * config.adv_cap_pct  # 40_000
        desired = 100_000.0
        allowed = min(desired, cap_dollars)
        assert allowed == pytest.approx(40_000.0), (
            "2%-ADV cap must BIND on a thin name once ADV is real dollar-volume"
        )
        # Sanity: had ADV been the broken ~price ($50), the cap would be $1 and
        # would (wrongly) crush every order — proving the magnitude matters.
        assert cap_dollars > 1.0


# ===========================================================================
# W-DATA — production-shape volume-anomaly: gets real volume (P0 #1)
# ===========================================================================

class TestVolumeAnomalyProductionShape:
    """The volume-anomaly gate gets REAL volume from production sources."""

    def _alpaca_history(self, as_of, baseline_vol, today_vol, days=40):
        from datetime import timedelta
        raw = []
        cursor = as_of
        added = 0
        while added < days:
            if cursor.weekday() < 5:
                v = today_vol if cursor.date() == as_of.date() else baseline_vol
                raw.append(_alpaca_bar_dict(
                    t=cursor.strftime("%Y-%m-%dT00:00:00Z"), c=100.0, v=v,
                ))
                added += 1
            cursor -= timedelta(days=1)
        return _alpaca_json(raw)

    @patch("arbiter.data.sources.alpaca.httpx.Client")
    def test_gate_flags_spike_from_production_source(self, mock_client_cls):
        from arbiter.defenses import VolumeAnomalyGate
        as_of = _ts(2026, 6, 1)

        resp = MagicMock()
        resp.status_code = 200
        # Baseline ~1M with slight jitter for non-zero sigma; today = 20M spike.
        resp.json.return_value = self._alpaca_history(
            as_of, baseline_vol=1_000_000.0, today_vol=20_000_000.0
        )
        resp.raise_for_status = MagicMock()
        mock_client_cls.return_value.__enter__.return_value.get.return_value = resp

        # Inject tiny jitter so sigma != 0: patch via separate close values is
        # complex; instead vary baseline through a custom response.
        from datetime import timedelta
        raw = []
        cursor = as_of
        added = 0
        while added < 40:
            if cursor.weekday() < 5:
                if cursor.date() == as_of.date():
                    v = 20_000_000.0
                else:
                    v = 1_000_000.0 + (added % 3) * 10_000.0
                raw.append(_alpaca_bar_dict(
                    t=cursor.strftime("%Y-%m-%dT00:00:00Z"), c=100.0, v=v))
                added += 1
            cursor -= timedelta(days=1)
        resp.json.return_value = _alpaca_json(raw)

        pit = build_price_gateway(_make_config())
        gate = VolumeAnomalyGate(z_threshold=3.0, baseline_days=20)
        assert gate.is_anomalous("AAPL", as_of, pit) is True, (
            "Gate must see REAL volume (20M spike vs ~1M baseline) from prod source"
        )
