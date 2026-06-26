"""Tests for ticker-detail endpoint (step 1–4 of the ticker-detail feature).

Covers:
- day_change_pct surfaced from change_today on /positions (TestDayChangePct, TestPositionsParser)
- TickerDetail schema (TestTickerDetailSchema)
- build_ticker_detail() happy + degrade paths (TestTickerDetail)
- GET /ticker/{symbol} route (TestTickerRoute)
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

# Ensure packages importable
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ARBITER_ROOT = _REPO_ROOT / "arbiter"
for _p in (_REPO_ROOT, _ARBITER_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# ---------------------------------------------------------------------------
# Shared client fixture (reuses the same pattern as test_api.py)
# ---------------------------------------------------------------------------
import os
import sqlite3
import tempfile
from collections.abc import Generator

from fastapi.testclient import TestClient


def _build_minimal_db(path: str) -> None:
    from arbiter.db.connection import get_connection
    from arbiter.db.migrate import run_migrations
    conn = get_connection(path)
    run_migrations(conn)
    conn.commit()
    conn.close()


@pytest.fixture()
def fixture_db(tmp_path: Path) -> Generator[str, None, None]:
    db_file = tmp_path / "test_ticker.db"
    _build_minimal_db(str(db_file))
    original = os.environ.get("COCKPIT_DB_PATH")
    os.environ["COCKPIT_DB_PATH"] = str(db_file)
    yield str(db_file)
    if original is None:
        os.environ.pop("COCKPIT_DB_PATH", None)
    else:
        os.environ["COCKPIT_DB_PATH"] = original


@pytest.fixture()
def client(fixture_db: str) -> Generator[TestClient, None, None]:
    with patch("cockpit.api.state._alpaca_snapshot") as mock_snap:
        mock_snap.return_value = (
            __import__("cockpit.api.contract", fromlist=["Account"]).Account(
                equity=10000.0, daily_pl=5.0
            ),
            [],
            [],
            False,
        )
        from cockpit.api.main import app
        with TestClient(app) as c:
            yield c


# ---------------------------------------------------------------------------
# Step 1 — day_change_pct on OpenPosition
# ---------------------------------------------------------------------------

class TestDayChangePct:
    def test_day_change_pct_present_when_change_today_supplied(self, client):
        """When raw /v2/positions carries change_today, it appears in the response."""
        # Patch in main's namespace (main.py does `from .positions import build_positions`)
        with patch("cockpit.api.main.build_positions") as mock_bp:
            from cockpit.api.contract import OpenPosition, Portfolio, PositionsResponse
            mock_bp.return_value = PositionsResponse(
                positions=[
                    OpenPosition(
                        ticker="MS", side="long", qty=10.0, avg_entry=100.0,
                        current_price=101.0, day_change_pct=0.0099,
                    )
                ],
                portfolio=Portfolio(n_open=1),
                as_of="2026-06-26T00:00:00Z",
                alpaca_ok=True,
            )
            r = client.get("/positions")
        assert r.status_code == 200
        positions = r.json()["positions"]
        assert positions[0]["day_change_pct"] == pytest.approx(0.0099, abs=1e-6)

    def test_day_change_pct_null_when_change_today_absent(self, client):
        """When raw /v2/positions has no change_today, day_change_pct is None."""
        with patch("cockpit.api.main.build_positions") as mock_bp:
            from cockpit.api.contract import OpenPosition, Portfolio, PositionsResponse
            mock_bp.return_value = PositionsResponse(
                positions=[
                    OpenPosition(
                        ticker="MS", side="long", qty=10.0, avg_entry=100.0,
                    )
                ],
                portfolio=Portfolio(n_open=1),
                as_of="2026-06-26T00:00:00Z",
                alpaca_ok=True,
            )
            r = client.get("/positions")
        assert r.status_code == 200
        assert r.json()["positions"][0]["day_change_pct"] is None


class TestPositionsParser:
    def test_change_today_fraction_parsed(self):
        """positions.py _f(p.get('change_today')) returns a float fraction."""
        with patch("arbiter.config.load_config") as mock_cfg_fn, \
             patch("arbiter.engine.build_executor") as mock_ex_fn:
            mock_ex = MagicMock()
            mock_ex.http_get.return_value = [{
                "symbol": "MS", "qty": "10.0", "side": "long",
                "avg_entry_price": "100.0", "current_price": "101.0",
                "market_value": "1010.0", "cost_basis": "1000.0",
                "unrealized_pl": "10.0", "unrealized_plpc": "0.01",
                "change_today": "0.0099",
            }]
            mock_ex._base.return_value = "https://paper-api.alpaca.markets"
            mock_ex._headers.return_value = {}
            mock_ex.get_account.return_value = MagicMock(equity=10000.0, daily_pl=5.0)
            mock_ex_fn.return_value = mock_ex
            mock_cfg_fn.return_value = MagicMock()

            from cockpit.api.positions import build_positions
            result = build_positions()

        assert len(result.positions) == 1
        assert result.positions[0].day_change_pct == pytest.approx(0.0099, abs=1e-6)

    def test_change_today_absent_gives_none(self):
        """change_today missing from raw payload → day_change_pct = None."""
        with patch("arbiter.config.load_config") as mock_cfg_fn, \
             patch("arbiter.engine.build_executor") as mock_ex_fn:
            mock_ex = MagicMock()
            mock_ex.http_get.return_value = [{
                "symbol": "MS", "qty": "10.0", "avg_entry_price": "100.0",
                "current_price": "101.0", "market_value": "1010.0",
                "cost_basis": "1000.0", "unrealized_pl": "10.0",
                "unrealized_plpc": "0.01",
                # no change_today key
            }]
            mock_ex._base.return_value = "https://paper-api.alpaca.markets"
            mock_ex._headers.return_value = {}
            mock_ex.get_account.return_value = MagicMock(equity=10000.0, daily_pl=None)
            mock_ex_fn.return_value = mock_ex
            mock_cfg_fn.return_value = MagicMock()

            from cockpit.api.positions import build_positions
            result = build_positions()

        assert result.positions[0].day_change_pct is None


# ---------------------------------------------------------------------------
# Step 2 — TickerDetail schema
# ---------------------------------------------------------------------------

class TestCleanName:
    """The Alpaca security-type suffix is trimmed for clean display."""

    @pytest.mark.parametrize("raw,expected", [
        ("Apple Inc. Common Stock", "Apple Inc."),
        ("lululemon athletica inc. Common Stock", "lululemon athletica inc."),
        ("Alphabet Inc. Class A Common Stock", "Alphabet Inc."),
        ("Morgan Stanley", "Morgan Stanley"),            # no suffix → unchanged
        ("Bank of America Corporation", "Bank of America Corporation"),
        ("  Tesla, Inc. Common Stock  ", "Tesla, Inc."),  # whitespace trimmed
    ])
    def test_trims_security_type_suffix(self, raw, expected):
        from cockpit.api.ticker import _clean_name
        assert _clean_name(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", 123])
    def test_empty_or_nonstr_returns_none(self, raw):
        from cockpit.api.ticker import _clean_name
        assert _clean_name(raw) is None


class TestTickerDetailSchema:
    def test_ticker_detail_can_be_constructed_minimal(self):
        """TickerDetail with only required fields is valid."""
        from cockpit.api.contract import TickerDetail
        d = TickerDetail(symbol="MS", as_of="2026-06-26T00:00:00Z")
        assert d.symbol == "MS"
        assert d.name is None
        assert d.month_return_pct is None
        assert d.current_price is None

    def test_ticker_detail_can_be_constructed_full(self):
        from cockpit.api.contract import TickerDetail
        d = TickerDetail(
            symbol="MS", name="Morgan Stanley",
            month_return_pct=0.0423, current_price=101.5,
            as_of="2026-06-26T00:00:00Z",
        )
        assert d.name == "Morgan Stanley"
        assert d.month_return_pct == pytest.approx(0.0423, abs=1e-6)


# ---------------------------------------------------------------------------
# Step 3 — build_ticker_detail() happy + degrade paths
# ---------------------------------------------------------------------------

def _make_mock_ex(http_get_side_effect):
    mock_ex = MagicMock()
    mock_ex._base.return_value = "https://paper-api.alpaca.markets"
    mock_ex._headers.return_value = {"APCA-API-KEY-ID": "test", "APCA-API-SECRET-KEY": "test"}
    mock_ex.http_get.side_effect = http_get_side_effect
    return mock_ex


def _mock_cfg():
    cfg = MagicMock()
    cfg.alpaca_data_base_url = "https://data.alpaca.markets"
    return cfg


_BARS_FIXTURE = {
    "bars": [
        {"t": "2026-05-22T04:00:00Z", "o": 98.0, "h": 103.0, "l": 97.0, "c": 100.0},
        {"t": "2026-06-25T04:00:00Z", "o": 110.0, "h": 115.0, "l": 109.0, "c": 112.0},
    ],
    "symbol": "MS",
    "next_page_token": None,
}
_ASSET_FIXTURE = {"name": "Morgan Stanley", "symbol": "MS", "status": "active"}


class TestTickerDetail:
    # ---- happy path ---------------------------------------------------------

    def test_happy_path_name_and_month_return(self):
        """Mocked assets + bars → correct name and month_return_pct."""
        def http_get(url, headers):
            if "/v2/assets/" in url:
                return _ASSET_FIXTURE
            if "/bars" in url:
                return _BARS_FIXTURE
            return {}

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            detail = build_ticker_detail("ms")  # lowercase → must upper-case

        assert detail.symbol == "MS"
        assert detail.name == "Morgan Stanley"
        expected_month = (112.0 - 100.0) / 100.0
        assert detail.month_return_pct == pytest.approx(expected_month, abs=1e-6)
        assert detail.current_price == pytest.approx(112.0, abs=1e-6)
        assert detail.as_of  # non-empty

    def test_symbol_is_upper_cased(self):
        """Any casing of input symbol → TickerDetail.symbol is upper."""
        def http_get(url, headers):
            return {} if "/assets/" in url else {"bars": []}

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            detail = build_ticker_detail("aapl")

        assert detail.symbol == "AAPL"

    def test_url_contains_correct_symbol(self):
        """The HTTP call to assets endpoint uses the upper-cased symbol."""
        called_urls = []

        def http_get(url, headers):
            called_urls.append(url)
            if "/v2/assets/" in url:
                return {"name": "Apple Inc.", "symbol": "AAPL", "status": "active"}
            return {"bars": []}

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            build_ticker_detail("aapl")

        assets_calls = [u for u in called_urls if "/v2/assets/" in u]
        assert len(assets_calls) == 1
        assert assets_calls[0].endswith("/v2/assets/AAPL")

    def test_bars_url_has_required_params(self):
        """Bars URL contains timeframe, start, feed, adjustment params."""
        called_urls = []

        def http_get(url, headers):
            called_urls.append(url)
            if "/v2/assets/" in url:
                return {"name": "Test Corp", "symbol": "TST"}
            return {"bars": []}

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            build_ticker_detail("TST")

        bars_calls = [u for u in called_urls if "/bars" in u]
        assert len(bars_calls) == 1
        u = bars_calls[0]
        assert "timeframe=1Day" in u
        assert "start=" in u
        assert "adjustment=all" in u
        assert "feed=" in u

    # ---- degrade paths ------------------------------------------------------

    def test_alpaca_down_returns_null_fields_http200(self):
        """Any exception from http_get → null name + null month_return_pct, HTTP 200."""
        def http_get(url, headers):
            raise RuntimeError("connection refused")

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            detail = build_ticker_detail("MS")

        assert detail.symbol == "MS"
        assert detail.name is None
        assert detail.month_return_pct is None
        assert detail.current_price is None

    def test_load_config_raises_returns_null_detail(self):
        """Exception in load_config (no .env) → null-field TickerDetail, no crash."""
        with patch("arbiter.config.load_config", side_effect=Exception("no config")):
            from cockpit.api.ticker import build_ticker_detail
            detail = build_ticker_detail("MS")

        assert detail.symbol == "MS"
        assert detail.name is None
        assert detail.month_return_pct is None

    def test_missing_bars_gives_null_month_return(self):
        """bars key present but list is empty → month_return_pct = None."""
        def http_get(url, headers):
            if "/v2/assets/" in url:
                return {"name": "Morgan Stanley", "symbol": "MS"}
            return {"bars": [], "next_page_token": None}

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            detail = build_ticker_detail("MS")

        assert detail.name == "Morgan Stanley"  # name still populated
        assert detail.month_return_pct is None
        assert detail.current_price is None

    def test_single_bar_gives_null_month_return(self):
        """Only 1 bar in window → cannot compute return, month_return_pct = None."""
        def http_get(url, headers):
            if "/v2/assets/" in url:
                return {"name": "Morgan Stanley", "symbol": "MS"}
            return {"bars": [{"t": "2026-06-25T04:00:00Z", "c": 112.0}]}

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            detail = build_ticker_detail("MS")

        assert detail.month_return_pct is None

    def test_bars_missing_key_returns_none_gracefully(self):
        """If bars response lacks 'bars' key entirely, month_return_pct = None."""
        def http_get(url, headers):
            if "/v2/assets/" in url:
                return {"name": "Corp", "symbol": "X"}
            return {}  # no 'bars' key

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            detail = build_ticker_detail("X")

        assert detail.month_return_pct is None

    def test_bars_explicit_null_returns_none_gracefully(self):
        """bars response {'bars': None} (JSON null, not missing key) → month_return_pct None, no raise."""
        def http_get(url, headers):
            if "/v2/assets/" in url:
                return {"name": "Corp", "symbol": "X"}
            return {"bars": None, "next_page_token": None}  # explicit JSON null

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            detail = build_ticker_detail("X")

        assert detail.name == "Corp"            # name still populated
        assert detail.month_return_pct is None  # null bars → no return
        assert detail.current_price is None

    def test_assets_returns_no_name_key(self):
        """Assets response without 'name' → name = None, no crash."""
        def http_get(url, headers):
            if "/v2/assets/" in url:
                return {"symbol": "MS", "status": "active"}  # no 'name'
            return _BARS_FIXTURE

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            detail = build_ticker_detail("MS")

        assert detail.name is None
        assert detail.month_return_pct is not None  # bars still computed


# ---------------------------------------------------------------------------
# Step 4 — GET /ticker/{symbol} route
# ---------------------------------------------------------------------------

class TestTickerRoute:
    def test_ticker_route_returns_200_offline(self, client):
        """GET /ticker/{symbol} with Alpaca offline → 200 with null fields."""
        # Patch in main's namespace (main.py does `from .ticker import build_ticker_detail`)
        with patch("cockpit.api.main.build_ticker_detail") as mock_build:
            from cockpit.api.contract import TickerDetail
            mock_build.return_value = TickerDetail(symbol="MS", as_of="2026-06-26T00:00:00Z")
            r = client.get("/ticker/MS")

        assert r.status_code == 200
        data = r.json()
        assert data["symbol"] == "MS"
        assert data["name"] is None
        assert data["month_return_pct"] is None
        assert data["current_price"] is None
        assert data["as_of"]

    def test_ticker_route_returns_200_live(self, client):
        """GET /ticker/{symbol} with mocked live data → 200 with populated fields."""
        with patch("cockpit.api.main.build_ticker_detail") as mock_build:
            from cockpit.api.contract import TickerDetail
            mock_build.return_value = TickerDetail(
                symbol="MS", name="Morgan Stanley",
                month_return_pct=0.042, current_price=112.0,
                as_of="2026-06-26T00:00:00Z",
            )
            r = client.get("/ticker/ms")  # lowercase input

        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "Morgan Stanley"
        assert data["month_return_pct"] == pytest.approx(0.042, abs=1e-6)

    def test_ticker_route_upper_cases_symbol(self, client):
        """Route passes upper-cased symbol to build_ticker_detail."""
        with patch("cockpit.api.main.build_ticker_detail") as mock_build:
            from cockpit.api.contract import TickerDetail
            mock_build.return_value = TickerDetail(symbol="AAPL", as_of="2026-06-26T00:00:00Z")
            client.get("/ticker/aapl")

        mock_build.assert_called_once_with("AAPL")

    def test_ticker_route_schema(self, client):
        """Response contains all required TickerDetail fields."""
        with patch("cockpit.api.main.build_ticker_detail") as mock_build:
            from cockpit.api.contract import TickerDetail
            mock_build.return_value = TickerDetail(symbol="X", as_of="2026-06-26T00:00:00Z")
            r = client.get("/ticker/X")

        data = r.json()
        for field in ("symbol", "name", "month_return_pct", "current_price", "as_of"):
            assert field in data, f"Missing field: {field}"
