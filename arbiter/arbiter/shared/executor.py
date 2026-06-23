"""Execution abstraction for Arbiter.

Adapted from stockbot/src/executor.py — options-specific logic removed;
equity-only interface kept.  Strategy/policy code depends on this module,
never on a broker-specific implementation.

Per INTERFACES.md §9: executors are copied/adapted from stockbot.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from arbiter.types import OrderSide


OrderStatus = Literal["filled", "partial", "rejected", "cancelled", "pending"]


@dataclass(frozen=True)
class OrderIntent:
    """A request to submit an equity order."""

    order_id: str           # ULID
    ticker: str
    side: OrderSide
    qty: float              # number of shares
    limit_price: float | None  # None = market order


@dataclass(frozen=True)
class ExecutionReport:
    """Result of a submitted order."""

    order_id: str
    ticker: str
    side: OrderSide
    status: OrderStatus
    filled_qty: float
    avg_fill_price: float | None
    gross_notional: float
    realized_pl: float | None
    reject_reason: str
    executor: str
    paper_only: bool


@dataclass(frozen=True)
class PositionSnapshot:
    """Current state of a single equity position."""

    ticker: str
    shares: float
    avg_price: float

    def market_value(self, price: float) -> float:
        return self.shares * price

    def unrealized_pl(self, price: float) -> float:
        return (price - self.avg_price) * self.shares


@dataclass(frozen=True)
class AccountSnapshot:
    """Aggregate account state."""

    cash: float
    buying_power: float
    realized_pl: float
    daily_pl: float
    open_positions: int
    paper_only: bool
    equity: float | None = None


class Executor(ABC):
    """Narrow execution interface used by policy/runtime code.

    Concrete implementations: SimExecutor (this lane), AlpacaAdapter (lane 12).
    """

    name: str

    @abstractmethod
    def place(self, intent: OrderIntent) -> ExecutionReport:
        """Submit an order; return the execution report."""
        raise NotImplementedError

    @abstractmethod
    def cancel(self, order_id: str) -> ExecutionReport:
        """Cancel a pending order by its ULID."""
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> dict[str, PositionSnapshot]:
        """Return current open positions keyed by ticker."""
        raise NotImplementedError

    @abstractmethod
    def get_account(self) -> AccountSnapshot:
        """Return a snapshot of the account state."""
        raise NotImplementedError
