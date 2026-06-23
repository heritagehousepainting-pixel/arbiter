"""Tests for arbiter.execution.alpaca_adapter.

Network is fully mocked — no live HTTP calls.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from arbiter.config import Config
from arbiter.execution.alpaca_adapter import AlpacaAdapter, BrokerError, build_executor
from arbiter.shared.executor import OrderIntent
from arbiter.shared.sim_executor import SimExecutor
from arbiter.types import OrderSide


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(live: bool = True) -> Config:
    return Config(
        live_trading=live,
        executor_backend="sim",
        db_path=":memory:",
        audit_path="/tmp/audit.jsonl",
        metrics_path="/tmp/metrics.jsonl",
        max_position_pct=0.05,
        max_sector_pct=0.20,
        max_gross_pct=0.80,
        max_open_positions=20,
        adv_cap_pct=0.02,
        alpaca_api_key="test_key",
        alpaca_secret_key="test_secret",
        alpaca_paper_base_url="https://paper-api.alpaca.markets",
        alpaca_data_base_url="https://data.alpaca.markets",
        alpaca_timeout=20.0,
        edgar_user_agent="test@test.com",
        kill_switch_url="",
        alert_webhook_url="",
    )


def _make_intent(ticker: str = "AAPL") -> OrderIntent:
    return OrderIntent(
        order_id="test-order-ulid",
        ticker=ticker,
        side=OrderSide.BUY,
        qty=10.0,
        limit_price=150.25,
    )


# ---------------------------------------------------------------------------
# AlpacaAdapter.place — success path
# ---------------------------------------------------------------------------

class TestAlpacaAdapterPlace:
    def test_place_success(self):
        """A successful broker response produces a filled ExecutionReport."""
        mock_post = MagicMock(return_value={
            "id": "broker-order-id",
            "filled_qty": "10",
            "filled_avg_price": "150.30",
            "status": "filled",
        })
        adapter = AlpacaAdapter(config=_make_config(), http_post=mock_post)
        intent = _make_intent()
        report = adapter.place(intent)

        assert report.status == "filled"
        assert report.filled_qty == 10.0
        assert abs(report.avg_fill_price - 150.30) < 1e-9
        assert report.order_id == intent.order_id
        mock_post.assert_called_once()

    def test_place_sends_limit_price(self):
        """limit_price from OrderIntent is sent to the broker."""
        captured: list[dict] = []

        def mock_post(url, headers, json_body):
            captured.append(json_body)
            return {"filled_qty": "10", "filled_avg_price": "150.25"}

        adapter = AlpacaAdapter(config=_make_config(), http_post=mock_post)
        adapter.place(_make_intent())

        assert captured[0]["limit_price"] == "150.25"
        assert captured[0]["type"] == "limit"

    def test_place_market_order_when_no_limit_price(self):
        """No limit_price → market order type sent to broker."""
        def mock_post(url, headers, json_body):
            return {"filled_qty": "10", "filled_avg_price": None}

        adapter = AlpacaAdapter(config=_make_config(), http_post=mock_post)
        intent = OrderIntent(
            order_id="oid",
            ticker="AAPL",
            side=OrderSide.BUY,
            qty=10.0,
            limit_price=None,
        )
        report = adapter.place(intent)
        assert report.status in ("filled", "pending")


# ---------------------------------------------------------------------------
# AlpacaAdapter.place — retry / halt on non-200
# ---------------------------------------------------------------------------

class TestAlpacaAdapterRetry:
    def test_broker_non200_retries_then_raises(self):
        """Broker failing raises BrokerError after exactly 1 retry (2 total calls)."""
        call_count = 0

        def failing_post(url, headers, json_body):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("HTTP 503")

        adapter = AlpacaAdapter(config=_make_config(), http_post=failing_post)
        with pytest.raises(BrokerError, match="2 attempts"):
            adapter._post_with_retry("https://example.com/v2/orders", {})

        assert call_count == 2  # initial + 1 retry

    def test_place_returns_rejected_on_broker_error(self):
        """place() catches BrokerError and returns a rejected report."""
        def failing_post(url, headers, json_body):
            raise RuntimeError("HTTP 500")

        adapter = AlpacaAdapter(config=_make_config(), http_post=failing_post)
        report = adapter.place(_make_intent())
        assert report.status == "rejected"
        assert "BrokerError" in report.reject_reason or "attempts" in report.reject_reason

    def test_broker_succeeds_on_second_attempt(self):
        """Adapter succeeds when 1st call fails but 2nd succeeds."""
        call_count = 0

        def flaky_post(url, headers, json_body):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient")
            return {"filled_qty": "10", "filled_avg_price": "150.00"}

        adapter = AlpacaAdapter(config=_make_config(), http_post=flaky_post)
        result = adapter._post_with_retry("https://x", {})
        assert result["filled_qty"] == "10"
        assert call_count == 2


# ---------------------------------------------------------------------------
# AlpacaAdapter.get_positions / get_account — mocked
# ---------------------------------------------------------------------------

class TestAlpacaAdapterPositions:
    def test_get_positions_empty(self):
        """Empty broker response yields empty dict."""
        mock_get = MagicMock(return_value=[])
        adapter = AlpacaAdapter(config=_make_config(), http_get=mock_get)
        assert adapter.get_positions() == {}

    def test_get_positions_parses_correctly(self):
        """Positions are parsed from Alpaca's list format."""
        mock_get = MagicMock(return_value=[
            {"symbol": "AAPL", "qty": "10", "avg_entry_price": "150.00"},
            {"symbol": "MSFT", "qty": "5", "avg_entry_price": "300.00"},
        ])
        adapter = AlpacaAdapter(config=_make_config(), http_get=mock_get)
        positions = adapter.get_positions()
        assert set(positions.keys()) == {"AAPL", "MSFT"}
        assert positions["AAPL"].shares == 10.0

    def test_get_positions_network_error_returns_empty(self):
        """Network failure returns empty dict (fail-closed)."""
        def bad_get(url, headers):
            raise RuntimeError("timeout")

        adapter = AlpacaAdapter(config=_make_config(), http_get=bad_get)
        assert adapter.get_positions() == {}

    def test_get_account_parsed(self):
        """Account data is parsed from Alpaca response.

        When ``position_count`` is present in the account response, it is
        used directly (no extra GET to /v2/positions).  The old field
        ``position_market_value`` was a dollar value, not a count — it is
        no longer used for open_positions (Finding 8 fix).
        """
        account_data = {
            "cash": "50000",
            "buying_power": "100000",
            "equity": "150000",
            "last_equity": "148000",
            "position_count": "3",  # correct field; used directly as int
        }
        mock_get = MagicMock(return_value=account_data)
        adapter = AlpacaAdapter(config=_make_config(), http_get=mock_get)
        account = adapter.get_account()
        assert account.cash == 50000.0
        assert account.equity == 150000.0
        assert account.daily_pl == 2000.0  # 150000 - 148000
        assert account.open_positions == 3   # from position_count, not position_market_value

    def test_get_account_falls_back_to_get_positions_count(self):
        """When position_count is absent, open_positions falls back to len(get_positions())."""
        account_data = {
            "cash": "10000",
            "buying_power": "20000",
            "equity": "30000",
            "last_equity": "29000",
            # position_count absent — should call get_positions() instead
        }
        positions_data = [
            {"symbol": "AAPL", "qty": "5", "avg_entry_price": "150.00"},
            {"symbol": "MSFT", "qty": "3", "avg_entry_price": "300.00"},
        ]

        call_count = [0]
        def mock_get(url, headers):
            call_count[0] += 1
            if "positions" in url:
                return positions_data
            return account_data

        adapter = AlpacaAdapter(config=_make_config(), http_get=mock_get)
        account = adapter.get_account()
        assert account.open_positions == 2  # len(positions_data)
