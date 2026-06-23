"""In-memory paper broker (SimExecutor) for Arbiter.

Adapted from stockbot/src/sim_executor.py — stripped to equity-only;
fills at the price provided in the OrderIntent (no slippage model here;
lane 3's slippage.py applies that before constructing the intent).

This is a working in-memory paper broker that:
- Fills orders at the given price
- Tracks positions and a cash ledger
- Computes realized P&L on sells
- Never calls datetime.now() — timestamps must be provided by the caller
"""
from __future__ import annotations

from dataclasses import dataclass, field

from arbiter.types import OrderSide

from .executor import (
    AccountSnapshot,
    ExecutionReport,
    Executor,
    OrderIntent,
    PositionSnapshot,
)


@dataclass
class _Position:
    ticker: str
    shares: float
    avg_price: float

    def update_buy(self, qty: float, price: float) -> None:
        total_cost = self.shares * self.avg_price + qty * price
        self.shares += qty
        self.avg_price = total_cost / self.shares if self.shares else 0.0

    def reduce(self, qty: float) -> tuple[float, float]:
        """Close ``qty`` shares; return (closed_qty, realized_pl)."""
        close_qty = min(qty, self.shares)
        return close_qty, 0.0  # realized_pl computed by caller with fill price


class SimExecutor(Executor):
    """In-memory paper broker.  Fills at the price provided in the intent."""

    name = "sim"

    def __init__(self, starting_cash: float = 100_000.0) -> None:
        self._cash: float = starting_cash
        self._positions: dict[str, _Position] = {}
        self._realized_pl: float = 0.0
        self._reports: list[ExecutionReport] = []
        self._daily_pl: float = 0.0

    # ------------------------------------------------------------------
    # Executor interface
    # ------------------------------------------------------------------

    def place(self, intent: OrderIntent) -> ExecutionReport:
        if intent.side == OrderSide.BUY:
            report = self._buy(intent)
        else:
            report = self._sell(intent)
        self._reports.append(report)
        return report

    def cancel(self, order_id: str) -> ExecutionReport:
        """SimExecutor has no pending order book; always returns rejected."""
        report = ExecutionReport(
            order_id=order_id,
            ticker="",
            side=OrderSide.BUY,
            status="rejected",
            filled_qty=0.0,
            avg_fill_price=None,
            gross_notional=0.0,
            realized_pl=None,
            reject_reason="SimExecutor has no pending order book to cancel",
            executor=self.name,
            paper_only=True,
        )
        self._reports.append(report)
        return report

    def get_positions(self) -> dict[str, PositionSnapshot]:
        return {
            ticker: PositionSnapshot(
                ticker=ticker,
                shares=pos.shares,
                avg_price=pos.avg_price,
            )
            for ticker, pos in self._positions.items()
            if pos.shares > 0
        }

    def get_account(self) -> AccountSnapshot:
        equity = self._cash + sum(
            p.shares * p.avg_price for p in self._positions.values()
        )
        return AccountSnapshot(
            cash=self._cash,
            buying_power=self._cash,
            realized_pl=self._realized_pl,
            daily_pl=self._daily_pl,
            open_positions=sum(1 for p in self._positions.values() if p.shares > 0),
            paper_only=True,
            equity=equity,
        )

    # ------------------------------------------------------------------
    # Durable-state serialization (WP-B — Phase-2 persistence)
    # ------------------------------------------------------------------

    def export_state(self) -> dict:
        """Serialize the broker's mutable state for durable snapshotting.

        Returns a plain-dict view (cash, realized P&L, and one entry per held
        position) suitable for ``position_store.snapshot_executor`` and for
        feeding straight back into ``restore_state``.

        Only positions with ``shares > 0`` are serialized — matching
        ``get_positions`` / ``open_position_count`` — so a lingering 0-share
        entry can't be restored and diverge from those views.
        """
        return {
            "cash": self._cash,
            "realized_pl": self._realized_pl,
            "positions": [
                {
                    "ticker": pos.ticker,
                    "shares": pos.shares,
                    "avg_price": pos.avg_price,
                }
                for pos in self._positions.values()
                if pos.shares > 0
            ],
        }

    def restore_state(self, state: dict) -> None:
        """Repopulate cash, realized P&L and positions from ``export_state``.

        Replaces any current in-memory state.  Used at ``build_engine`` to seed
        the executor from the durable snapshot so positions survive across runs.
        """
        self._cash = state["cash"]
        self._realized_pl = state["realized_pl"]
        self._positions = {
            p["ticker"]: _Position(
                ticker=p["ticker"],
                shares=p["shares"],
                avg_price=p["avg_price"],
            )
            for p in state["positions"]
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _buy(self, intent: OrderIntent) -> ExecutionReport:
        fill_price = intent.limit_price if intent.limit_price is not None else 0.0
        if fill_price <= 0.0:
            return self._rejected(intent, "SimExecutor requires a non-zero price for BUY orders")
        cost = fill_price * intent.qty
        if cost > self._cash:
            return self._rejected(intent, f"insufficient cash: need {cost:.2f}, have {self._cash:.2f}")

        pos = self._positions.setdefault(intent.ticker, _Position(intent.ticker, 0.0, 0.0))
        pos.update_buy(intent.qty, fill_price)
        self._cash -= cost

        return ExecutionReport(
            order_id=intent.order_id,
            ticker=intent.ticker,
            side=OrderSide.BUY,
            status="filled",
            filled_qty=intent.qty,
            avg_fill_price=fill_price,
            gross_notional=cost,
            realized_pl=None,
            reject_reason="",
            executor=self.name,
            paper_only=True,
        )

    def _sell(self, intent: OrderIntent) -> ExecutionReport:
        fill_price = intent.limit_price if intent.limit_price is not None else 0.0
        if fill_price <= 0.0:
            return self._rejected(intent, "SimExecutor requires a non-zero price for SELL orders")
        pos = self._positions.get(intent.ticker)
        if pos is None or pos.shares <= 0:
            return self._rejected(intent, f"no position in {intent.ticker}")

        sell_qty = min(intent.qty, pos.shares)
        proceeds = fill_price * sell_qty
        cost_basis = pos.avg_price * sell_qty
        pl = proceeds - cost_basis

        pos.shares -= sell_qty
        if pos.shares <= 0:
            del self._positions[intent.ticker]

        self._cash += proceeds
        self._realized_pl += pl
        self._daily_pl += pl

        return ExecutionReport(
            order_id=intent.order_id,
            ticker=intent.ticker,
            side=OrderSide.SELL,
            status="filled",
            filled_qty=sell_qty,
            avg_fill_price=fill_price,
            gross_notional=proceeds,
            realized_pl=pl,
            reject_reason="",
            executor=self.name,
            paper_only=True,
        )

    def _rejected(self, intent: OrderIntent, reason: str) -> ExecutionReport:
        return ExecutionReport(
            order_id=intent.order_id,
            ticker=intent.ticker,
            side=intent.side,
            status="rejected",
            filled_qty=0.0,
            avg_fill_price=None,
            gross_notional=0.0,
            realized_pl=None,
            reject_reason=reason,
            executor=self.name,
            paper_only=True,
        )
