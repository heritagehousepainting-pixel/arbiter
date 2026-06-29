"""Chart backend tests — cockpit read-only API.

Test categories:
(a) build_chart_series → ChartSeries shape + symbol upper-cased.
(b) Session classification — _classify_session for pre/regular/post.
(c) extended_available flag behaviour.
(d) Fail-closed — Alpaca http_get raises → candles=[], alpaca_ok=False, no raise.
(e) GET /chart/{symbol} route via TestClient.

All tests are OFFLINE (no network).  Alpaca calls are monkeypatched out.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

# --- path setup ---------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_ARBITER_ROOT = _REPO_ROOT / "arbiter"
if str(_ARBITER_ROOT) not in sys.path:
    sys.path.insert(0, str(_ARBITER_ROOT))

# Pre-import so patch() can target these modules
import arbiter.config  # noqa: F401, E402
import arbiter.engine  # noqa: F401, E402

import cockpit.api.chart as chart_mod  # noqa: E402
from cockpit.api.contract import ChartSeries  # noqa: E402


# =============================================================================
# Helpers
# =============================================================================

def _make_executor(bars: list[dict], next_page_token: str | None = None) -> MagicMock:
    """Mock executor whose http_get returns a canned bars payload."""
    ex = MagicMock()
    ex._headers.return_value = {"Authorization": "Bearer test"}
    ex.http_get.return_value = {
        "bars": bars,
        "next_page_token": next_page_token,
    }
    return ex


def _make_failing_executor() -> MagicMock:
    """Mock executor whose http_get raises a ConnectionError."""
    ex = MagicMock()
    ex._headers.return_value = {}
    ex.http_get.side_effect = ConnectionError("Alpaca unavailable")
    return ex


def _make_mock_config(data_base: str = "https://data.alpaca.markets") -> MagicMock:
    cfg = MagicMock()
    cfg.alpaca_data_base_url = data_base
    return cfg


def _run(symbol: str, range_: str, ex: MagicMock, cfg: MagicMock | None = None) -> ChartSeries:
    """Run build_chart_series with Alpaca patched offline, cache cleared."""
    chart_mod._CACHE.clear()
    if cfg is None:
        cfg = _make_mock_config()
    with patch("arbiter.config.load_config", return_value=cfg), \
         patch("arbiter.engine.build_executor", return_value=ex):
        return chart_mod.build_chart_series(symbol, range_)


# --- Canonical bar payloads for session tests (late June EDT = UTC−4) ---------
# 2026-06-29T12:00:00Z = 08:00 EDT → pre
# 2026-06-29T15:00:00Z = 11:00 EDT → regular
# 2026-06-29T23:00:00Z = 19:00 EDT → post
_BAR_PRE = {"t": "2026-06-29T12:00:00Z", "o": 100.0, "h": 105.0, "l": 99.0, "c": 103.0, "v": 500.0}
_BAR_REG = {"t": "2026-06-29T15:00:00Z", "o": 103.0, "h": 108.0, "l": 102.0, "c": 107.0, "v": 2000.0}
_BAR_POST = {"t": "2026-06-29T23:00:00Z", "o": 107.0, "h": 109.0, "l": 106.0, "c": 108.0, "v": 300.0}
_BAR_DAY = {"t": "2026-06-27T00:00:00Z", "o": 200.0, "h": 210.0, "l": 195.0, "c": 205.0, "v": 5000.0}


# =============================================================================
# (a) Shape + symbol upper-casing
# =============================================================================

class TestChartSeriesShape:
    def test_returns_chart_series_type(self) -> None:
        result = _run("AAPL", "1m", _make_executor([_BAR_DAY]))
        assert isinstance(result, ChartSeries)

    def test_symbol_is_uppercased(self) -> None:
        result = _run("aapl", "1m", _make_executor([_BAR_DAY]))
        assert result.symbol == "AAPL"

    def test_range_preserved(self) -> None:
        for range_ in ("live", "5d", "1m", "3m", "6m"):
            result = _run("MSFT", range_, _make_executor([_BAR_DAY]))
            assert result.range == range_

    def test_candles_populated(self) -> None:
        result = _run("AAPL", "1m", _make_executor([_BAR_DAY, _BAR_DAY]))
        assert len(result.candles) == 2

    def test_candle_fields_present(self) -> None:
        result = _run("AAPL", "1m", _make_executor([_BAR_DAY]))
        c = result.candles[0]
        assert c.t == _BAR_DAY["t"]
        assert c.o == pytest.approx(_BAR_DAY["o"])
        assert c.h == pytest.approx(_BAR_DAY["h"])
        assert c.l == pytest.approx(_BAR_DAY["l"])
        assert c.c == pytest.approx(_BAR_DAY["c"])
        assert c.v == pytest.approx(_BAR_DAY["v"])

    def test_alpaca_ok_true_on_success(self) -> None:
        result = _run("AAPL", "1m", _make_executor([_BAR_DAY]))
        assert result.alpaca_ok is True

    def test_as_of_is_set(self) -> None:
        result = _run("AAPL", "1m", _make_executor([_BAR_DAY]))
        assert result.as_of
        # Must look like an ISO timestamp
        assert "T" in result.as_of or "t" in result.as_of.lower()

    def test_empty_symbol_returns_gracefully(self) -> None:
        chart_mod._CACHE.clear()
        result = chart_mod.build_chart_series("", "1m")
        assert isinstance(result, ChartSeries)
        assert result.candles == []

    def test_invalid_range_coerced_to_live(self) -> None:
        result = _run("AAPL", "bogus", _make_executor([_BAR_REG]))
        # Coerced range should be "live", not "bogus"
        assert result.range == "live"


# =============================================================================
# (b) Session classification — _classify_session
# =============================================================================

class TestSessionClassification:
    """Test _classify_session directly with unambiguous EDT timestamps."""

    def test_pre_market(self) -> None:
        # 2026-06-29T12:00:00Z = 08:00 EDT → pre (04:00–09:30 ET)
        assert chart_mod._classify_session("2026-06-29T12:00:00Z") == "pre"

    def test_regular_market(self) -> None:
        # 2026-06-29T15:00:00Z = 11:00 EDT → regular (09:30–16:00 ET)
        assert chart_mod._classify_session("2026-06-29T15:00:00Z") == "regular"

    def test_post_market(self) -> None:
        # 2026-06-29T23:00:00Z = 19:00 EDT → post (16:00–20:00 ET)
        assert chart_mod._classify_session("2026-06-29T23:00:00Z") == "post"

    def test_market_open_boundary(self) -> None:
        # 2026-06-29T13:30:00Z = 09:30 EDT → regular (exact open)
        assert chart_mod._classify_session("2026-06-29T13:30:00Z") == "regular"

    def test_market_close_boundary(self) -> None:
        # 2026-06-29T20:00:00Z = 16:00 EDT → post (exactly at close → post starts)
        assert chart_mod._classify_session("2026-06-29T20:00:00Z") == "post"

    def test_pre_open_boundary(self) -> None:
        # 2026-06-29T08:00:00Z = 04:00 EDT → pre (exact pre-market start)
        assert chart_mod._classify_session("2026-06-29T08:00:00Z") == "pre"

    def test_overnight_fallback(self) -> None:
        # 2026-06-30T03:00:00Z = 23:00 EDT → outside all sessions → "regular" fallback
        assert chart_mod._classify_session("2026-06-30T03:00:00Z") == "regular"

    def test_invalid_ts_fallback(self) -> None:
        assert chart_mod._classify_session("NOT_A_TIMESTAMP") == "regular"

    def test_candle_session_assigned_from_bar_ts(self) -> None:
        """end-to-end: session is correctly assigned through build_chart_series."""
        result = _run("AAPL", "live", _make_executor([_BAR_PRE, _BAR_REG, _BAR_POST]))
        sessions = [c.session for c in result.candles]
        assert sessions == ["pre", "regular", "post"]


# =============================================================================
# (c) extended_available flag
# =============================================================================

class TestExtendedAvailable:
    def test_extended_available_true_when_pre_bar_present(self) -> None:
        result = _run("AAPL", "live", _make_executor([_BAR_PRE, _BAR_REG]))
        assert result.extended_available is True

    def test_extended_available_true_when_post_bar_present(self) -> None:
        result = _run("AAPL", "live", _make_executor([_BAR_REG, _BAR_POST]))
        assert result.extended_available is True

    def test_extended_available_false_when_only_regular_bars(self) -> None:
        result = _run("AAPL", "1m", _make_executor([_BAR_DAY, _BAR_DAY]))
        assert result.extended_available is False

    def test_extended_available_false_on_empty_candles(self) -> None:
        result = _run("AAPL", "1m", _make_executor([]))
        assert result.extended_available is False


# =============================================================================
# (d) Fail-closed
# =============================================================================

class TestFailClosed:
    def test_http_get_raises_returns_empty_candles(self) -> None:
        """http_get raising → candles=[], alpaca_ok=False, no exception bubbles."""
        result = _run("AAPL", "1m", _make_failing_executor())
        assert result.candles == []
        assert result.alpaca_ok is False

    def test_http_get_raises_does_not_propagate(self) -> None:
        """Must not raise even when Alpaca is down."""
        try:
            result = _run("AAPL", "1m", _make_failing_executor())
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"build_chart_series raised unexpectedly: {exc!r}")

    def test_load_config_raises_returns_empty(self) -> None:
        """Config failure (arbiter not set up) → graceful empty result."""
        chart_mod._CACHE.clear()
        with patch("arbiter.config.load_config", side_effect=RuntimeError("no config")):
            result = chart_mod.build_chart_series("AAPL", "1m")
        assert result.candles == []
        assert result.alpaca_ok is False
        assert result.symbol == "AAPL"

    def test_fail_closed_symbol_preserved(self) -> None:
        result = _run("tsla", "1m", _make_failing_executor())
        assert result.symbol == "TSLA"

    def test_fail_closed_range_preserved(self) -> None:
        result = _run("AAPL", "3m", _make_failing_executor())
        assert result.range == "3m"


# =============================================================================
# (e) GET /chart/{symbol} via TestClient
# =============================================================================

class TestChartRoute:
    def test_chart_returns_200(self) -> None:
        from fastapi.testclient import TestClient  # noqa: PLC0415
        from cockpit.api.main import app  # noqa: PLC0415

        chart_mod._CACHE.clear()
        ex = _make_executor([_BAR_REG])
        cfg = _make_mock_config()
        with patch("arbiter.config.load_config", return_value=cfg), \
             patch("arbiter.engine.build_executor", return_value=ex):
            with TestClient(app) as client:
                r = client.get("/chart/AAPL?range=1m")
        assert r.status_code == 200

    def test_chart_response_shape(self) -> None:
        from fastapi.testclient import TestClient  # noqa: PLC0415
        from cockpit.api.main import app  # noqa: PLC0415

        chart_mod._CACHE.clear()
        ex = _make_executor([_BAR_REG])
        cfg = _make_mock_config()
        with patch("arbiter.config.load_config", return_value=cfg), \
             patch("arbiter.engine.build_executor", return_value=ex):
            with TestClient(app) as client:
                data = client.get("/chart/aapl?range=1m").json()

        assert data["symbol"] == "AAPL"
        assert data["range"] == "1m"
        assert "candles" in data
        assert "extended_available" in data
        assert "as_of" in data
        assert "alpaca_ok" in data

    def test_chart_invalid_range_coerced(self) -> None:
        """Invalid range query param must not 500."""
        from fastapi.testclient import TestClient  # noqa: PLC0415
        from cockpit.api.main import app  # noqa: PLC0415

        chart_mod._CACHE.clear()
        ex = _make_executor([_BAR_REG])
        cfg = _make_mock_config()
        with patch("arbiter.config.load_config", return_value=cfg), \
             patch("arbiter.engine.build_executor", return_value=ex):
            with TestClient(app) as client:
                r = client.get("/chart/AAPL?range=invalid_range")
        assert r.status_code == 200
        assert r.json()["range"] == "live"

    def test_chart_default_range_is_live(self) -> None:
        """Omitting range query param defaults to live."""
        from fastapi.testclient import TestClient  # noqa: PLC0415
        from cockpit.api.main import app  # noqa: PLC0415

        chart_mod._CACHE.clear()
        ex = _make_executor([_BAR_REG])
        cfg = _make_mock_config()
        with patch("arbiter.config.load_config", return_value=cfg), \
             patch("arbiter.engine.build_executor", return_value=ex):
            with TestClient(app) as client:
                r = client.get("/chart/AAPL")
        assert r.status_code == 200
        assert r.json()["range"] == "live"

    def test_chart_fail_closed_via_route(self) -> None:
        """Alpaca down → 200 with empty candles (never 500)."""
        from fastapi.testclient import TestClient  # noqa: PLC0415
        from cockpit.api.main import app  # noqa: PLC0415

        chart_mod._CACHE.clear()
        ex = _make_failing_executor()
        cfg = _make_mock_config()
        with patch("arbiter.config.load_config", return_value=cfg), \
             patch("arbiter.engine.build_executor", return_value=ex):
            with TestClient(app) as client:
                r = client.get("/chart/AAPL?range=1m")
        assert r.status_code == 200
        data = r.json()
        assert data["candles"] == []
        assert data["alpaca_ok"] is False
