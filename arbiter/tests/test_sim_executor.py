"""Tests for arbiter/shared/sim_executor.py — in-memory paper broker."""
from __future__ import annotations

import pytest

from arbiter.shared.executor import AccountSnapshot, OrderIntent, PositionSnapshot
from arbiter.shared.sim_executor import SimExecutor
from arbiter.types import OrderSide


def _buy_intent(ticker: str, qty: float, price: float, order_id: str = "01HX") -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        ticker=ticker,
        side=OrderSide.BUY,
        qty=qty,
        limit_price=price,
    )


def _sell_intent(ticker: str, qty: float, price: float, order_id: str = "01HY") -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        ticker=ticker,
        side=OrderSide.SELL,
        qty=qty,
        limit_price=price,
    )


class TestSimExecutorBuy:
    def test_buy_fills_and_tracks_position(self) -> None:
        sim = SimExecutor(starting_cash=100_000.0)
        report = sim.place(_buy_intent("AAPL", qty=10, price=150.0))

        assert report.status == "filled"
        assert report.filled_qty == pytest.approx(10.0)
        assert report.avg_fill_price == pytest.approx(150.0)
        assert report.ticker == "AAPL"
        assert report.paper_only is True

    def test_buy_reduces_cash(self) -> None:
        sim = SimExecutor(starting_cash=10_000.0)
        sim.place(_buy_intent("AAPL", qty=10, price=150.0))

        account = sim.get_account()
        assert account.cash == pytest.approx(10_000.0 - 1_500.0)

    def test_buy_appears_in_positions(self) -> None:
        sim = SimExecutor(starting_cash=100_000.0)
        sim.place(_buy_intent("AAPL", qty=5, price=200.0))

        positions = sim.get_positions()
        assert "AAPL" in positions
        pos = positions["AAPL"]
        assert pos.shares == pytest.approx(5.0)
        assert pos.avg_price == pytest.approx(200.0)

    def test_buy_rejected_insufficient_cash(self) -> None:
        sim = SimExecutor(starting_cash=100.0)
        report = sim.place(_buy_intent("AAPL", qty=10, price=150.0))  # costs 1500

        assert report.status == "rejected"
        assert "cash" in report.reject_reason.lower()

    def test_buy_rejected_zero_price(self) -> None:
        sim = SimExecutor(starting_cash=100_000.0)
        report = sim.place(_buy_intent("AAPL", qty=10, price=0.0))

        assert report.status == "rejected"

    def test_multiple_buys_avg_price(self) -> None:
        sim = SimExecutor(starting_cash=100_000.0)
        sim.place(_buy_intent("MSFT", qty=10, price=100.0, order_id="A1"))
        sim.place(_buy_intent("MSFT", qty=10, price=200.0, order_id="A2"))

        positions = sim.get_positions()
        pos = positions["MSFT"]
        assert pos.shares == pytest.approx(20.0)
        assert pos.avg_price == pytest.approx(150.0)  # (100*10 + 200*10) / 20


class TestSimExecutorSell:
    def test_sell_fills_and_removes_position(self) -> None:
        sim = SimExecutor(starting_cash=100_000.0)
        sim.place(_buy_intent("AAPL", qty=10, price=150.0, order_id="B1"))
        report = sim.place(_sell_intent("AAPL", qty=10, price=160.0, order_id="B2"))

        assert report.status == "filled"
        assert report.filled_qty == pytest.approx(10.0)
        assert report.realized_pl == pytest.approx(100.0)  # (160-150)*10

    def test_sell_increases_cash(self) -> None:
        sim = SimExecutor(starting_cash=10_000.0)
        sim.place(_buy_intent("AAPL", qty=10, price=100.0, order_id="C1"))
        sim.place(_sell_intent("AAPL", qty=10, price=110.0, order_id="C2"))

        account = sim.get_account()
        # Started 10000, bought 10@100 (cash=9000), sold 10@110 (cash=10100)
        assert account.cash == pytest.approx(10_100.0)

    def test_sell_rejected_no_position(self) -> None:
        sim = SimExecutor(starting_cash=100_000.0)
        report = sim.place(_sell_intent("AAPL", qty=5, price=100.0))

        assert report.status == "rejected"

    def test_partial_sell_keeps_remaining_position(self) -> None:
        sim = SimExecutor(starting_cash=100_000.0)
        sim.place(_buy_intent("AAPL", qty=10, price=100.0, order_id="D1"))
        sim.place(_sell_intent("AAPL", qty=6, price=110.0, order_id="D2"))

        positions = sim.get_positions()
        assert "AAPL" in positions
        assert positions["AAPL"].shares == pytest.approx(4.0)

    def test_sell_records_realized_pl_in_account(self) -> None:
        sim = SimExecutor(starting_cash=100_000.0)
        sim.place(_buy_intent("TSLA", qty=5, price=200.0, order_id="E1"))
        sim.place(_sell_intent("TSLA", qty=5, price=300.0, order_id="E2"))  # +500 pl

        account = sim.get_account()
        assert account.realized_pl == pytest.approx(500.0)


class TestSimExecutorCancel:
    def test_cancel_returns_rejected(self) -> None:
        sim = SimExecutor()
        report = sim.cancel("nonexistent-order-id")
        assert report.status == "rejected"
        assert "no pending order book" in report.reject_reason.lower()


class TestSimExecutorAccount:
    def test_empty_account(self) -> None:
        sim = SimExecutor(starting_cash=50_000.0)
        account = sim.get_account()

        assert account.cash == pytest.approx(50_000.0)
        assert account.buying_power == pytest.approx(50_000.0)
        assert account.realized_pl == pytest.approx(0.0)
        assert account.open_positions == 0
        assert account.paper_only is True

    def test_open_positions_count(self) -> None:
        sim = SimExecutor(starting_cash=100_000.0)
        sim.place(_buy_intent("AAPL", qty=1, price=100.0, order_id="F1"))
        sim.place(_buy_intent("MSFT", qty=1, price=200.0, order_id="F2"))

        account = sim.get_account()
        assert account.open_positions == 2
