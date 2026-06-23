"""Failure-mapping tests for ``AlpacaAdapter.cancel`` / ``get_order``.

W-TESTHARDEN seam #2.  On a broker hiccup the status the adapter returns
decides ORPHAN-vs-DOUBLE-position downstream:

  - ``cancel`` on an HTTP error must map to ``rejected`` (so the engine does
    NOT believe a pending order was cancelled and then re-place / orphan it).
  - ``get_order`` on an HTTP error must map to ``pending`` ("not-yet-known",
    NEVER assert a fill) so reconciliation doesn't double-count a position.
  - ``get_order`` must also correctly map each Alpaca order status, missing
    fields, and the partial-vs-full distinction.

The existing ``test_alpaca_adapter.py`` covers ``place`` retry and
``get_positions``/``get_account`` errors but never these two methods' failure
branches.  Network is fully injected (``http_get`` / ``http_delete`` + FakeAlpaca);
no live HTTP.
"""
from __future__ import annotations

import pytest

from arbiter.config import Config
from arbiter.execution.alpaca_adapter import AlpacaAdapter
from arbiter.types import OrderSide

from tests.execution._fake_alpaca import FakeAlpaca


# ---------------------------------------------------------------------------
# Config helper (paper keys present)
# ---------------------------------------------------------------------------


def _config() -> Config:
    return Config(
        live_trading=False,
        executor_backend="alpaca_paper",
        db_path=":memory:",
        audit_path="/tmp/audit.jsonl",
        metrics_path="/tmp/metrics.jsonl",
        max_position_pct=0.05,
        max_sector_pct=0.20,
        max_gross_pct=0.80,
        max_open_positions=20,
        adv_cap_pct=0.02,
        alpaca_api_key="key",
        alpaca_secret_key="secret",
        alpaca_paper_base_url="https://paper-api.alpaca.markets",
        alpaca_data_base_url="https://data.alpaca.markets",
        alpaca_timeout=20.0,
        edgar_user_agent="test@test.com",
        kill_switch_url="",
        alert_webhook_url="",
    )


class _Boom(Exception):
    """Stand-in for a broker hiccup (timeout / non-200 raise_for_status)."""


# ---------------------------------------------------------------------------
# cancel() failure mapping
# ---------------------------------------------------------------------------


class TestCancelFailureMapping:
    def test_cancel_http_error_maps_to_rejected(self) -> None:
        """A DELETE that raises (timeout/non-200) → status='rejected', not 'cancelled'."""
        def bad_delete(url, headers):
            raise _Boom("HTTP 500 from broker")

        adapter = AlpacaAdapter(config=_config(), http_delete=bad_delete)
        report = adapter.cancel("order-xyz")

        assert report.status == "rejected", (
            "a failed cancel must NOT report 'cancelled' (would orphan a live order)"
        )
        assert report.reject_reason == "cancel failed"
        assert report.order_id == "order-xyz"
        assert report.filled_qty == 0.0
        assert report.paper_only is True

    def test_cancel_timeout_maps_to_rejected(self) -> None:
        """A timeout-style error is treated the same as any other failure."""
        def timeout_delete(url, headers):
            raise TimeoutError("read timed out")

        adapter = AlpacaAdapter(config=_config(), http_delete=timeout_delete)
        report = adapter.cancel("order-1")
        assert report.status == "rejected"

    def test_cancel_success_maps_to_cancelled(self) -> None:
        """A clean DELETE → status='cancelled', empty reject_reason (control case)."""
        fake = FakeAlpaca()
        # Seed an order so the fake DELETE finds it (not strictly required).
        fake.orders["order-1"] = {"id": "order-1", "status": "accepted"}
        adapter = AlpacaAdapter(config=_config(), http_delete=fake.http_delete)

        report = adapter.cancel("order-1")
        assert report.status == "cancelled"
        assert report.reject_reason == ""


# ---------------------------------------------------------------------------
# get_order() failure mapping
# ---------------------------------------------------------------------------


class TestGetOrderFailureMapping:
    def test_get_order_http_error_maps_to_pending(self) -> None:
        """A GET that raises → status='pending' (treat as not-yet-known, never a fill)."""
        def bad_get(url, headers):
            raise _Boom("HTTP 503")

        adapter = AlpacaAdapter(config=_config(), http_get=bad_get)
        report = adapter.get_order("order-1")

        assert report.status == "pending", (
            "an errored get_order must default to pending, NEVER assert a fill"
        )
        assert report.filled_qty == 0.0
        assert report.avg_fill_price is None
        assert report.order_id == "order-1"
        assert report.paper_only is True

    def test_get_order_timeout_maps_to_pending(self) -> None:
        def timeout_get(url, headers):
            raise TimeoutError("read timed out")

        adapter = AlpacaAdapter(config=_config(), http_get=timeout_get)
        assert adapter.get_order("order-1").status == "pending"

    def test_get_order_missing_status_field_maps_to_pending(self) -> None:
        """A 200 with NO status field and no fill → mapped to pending, not a crash."""
        adapter = AlpacaAdapter(config=_config(), http_get=lambda url, headers: {})
        report = adapter.get_order("order-1")
        assert report.status == "pending"
        assert report.filled_qty == 0.0

    def test_get_order_rejected_status_mapped(self) -> None:
        adapter = AlpacaAdapter(
            config=_config(),
            http_get=lambda url, headers: {"status": "rejected", "reject_reason": "no buying power"},
        )
        report = adapter.get_order("order-1")
        assert report.status == "rejected"
        assert report.reject_reason == "no buying power"

    @pytest.mark.parametrize("broker_status", ["canceled", "expired"])
    def test_get_order_terminal_maps_to_cancelled(self, broker_status: str) -> None:
        adapter = AlpacaAdapter(
            config=_config(),
            http_get=lambda url, headers: {"status": broker_status},
        )
        assert adapter.get_order("order-1").status == "cancelled"

    def test_get_order_partial_fill_mapped(self) -> None:
        """filled_qty < requested qty → status='partial'."""
        adapter = AlpacaAdapter(
            config=_config(),
            http_get=lambda url, headers: {
                "status": "partially_filled",
                "qty": "10",
                "filled_qty": "4",
                "filled_avg_price": "150.0",
                "symbol": "AAPL",
            },
        )
        report = adapter.get_order("order-1")
        assert report.status == "partial"
        assert report.filled_qty == 4.0
        assert report.avg_fill_price == 150.0
        assert report.ticker == "AAPL"
        # gross_notional reflects the partial: 4 * 150.
        assert report.gross_notional == 600.0

    def test_get_order_full_fill_mapped(self) -> None:
        """filled_qty >= requested qty → status='filled'."""
        adapter = AlpacaAdapter(
            config=_config(),
            http_get=lambda url, headers: {
                "status": "filled",
                "qty": "10",
                "filled_qty": "10",
                "filled_avg_price": "150.0",
            },
        )
        report = adapter.get_order("order-1")
        assert report.status == "filled"
        assert report.filled_qty == 10.0

    def test_get_order_missing_qty_field_treats_fill_as_full(self) -> None:
        """When 'qty' is absent but there IS a fill, req_qty falls back to fill_qty → filled."""
        adapter = AlpacaAdapter(
            config=_config(),
            http_get=lambda url, headers: {
                "status": "filled",
                "filled_qty": "3",
                "filled_avg_price": "100.0",
            },
        )
        report = adapter.get_order("order-1")
        assert report.status == "filled"
        assert report.filled_qty == 3.0

    def test_get_order_against_fake_alpaca_pending(self) -> None:
        """End-to-end through FakeAlpaca: a pending broker order maps to pending."""
        fake = FakeAlpaca(fill_mode="pending")
        adapter = AlpacaAdapter(
            config=_config(), http_post=fake.http_post, http_get=fake.http_get
        )
        # Place a pending order, then query it back.
        from arbiter.shared.executor import OrderIntent

        intent = OrderIntent(
            order_id="coid-1", ticker="AAPL", side=OrderSide.BUY, qty=10.0, limit_price=100.0
        )
        adapter.place(intent)
        report = adapter.get_order("coid-1")
        assert report.status == "pending"
        assert report.filled_qty == 0.0
