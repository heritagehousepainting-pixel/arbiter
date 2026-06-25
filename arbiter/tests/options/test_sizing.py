"""Tests for arbiter/options/sizing.py — size_option()."""
from __future__ import annotations

import datetime
import math

import pytest

from arbiter.config import load_config
from arbiter.options.sizing import size_option
from arbiter.options.types import OptionContract, OptionSide


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_contract(
    *,
    delta: float | None = 0.75,
    bid: float | None = 3.00,
    ask: float | None = 3.20,
    side: OptionSide = OptionSide.CALL,
) -> OptionContract:
    """Build a minimal OptionContract for sizing tests."""
    return OptionContract(
        occ_symbol="AAPL240119C00150000",
        underlying="AAPL",
        side=side,
        strike=150.0,
        expiry=datetime.date(2024, 1, 19),
        delta=delta,
        iv=0.30,
        bid=bid,
        ask=ask,
        open_interest=500,
        volume=50,
    )


@pytest.fixture()
def default_config():
    """Return a Config with default options_sleeve_pct=0.35."""
    return load_config()


# ---------------------------------------------------------------------------
# Normal sizing
# ---------------------------------------------------------------------------

class TestSizeOptionNormal:
    def test_returns_option_order(self, default_config):
        contract = _make_contract()
        order = size_option(
            contract,
            portfolio_equity=100_000.0,
            open_options_premium=0.0,
            underlying_price=150.0,
            config=default_config,
        )
        assert order is not None

    def test_contracts_qty_floor_math(self, default_config):
        """sleeve = 35_000; mid = 3.10; cost_per = 310; floor(35000/310) = 112."""
        contract = _make_contract(bid=3.00, ask=3.20)  # mid = 3.10
        order = size_option(
            contract,
            portfolio_equity=100_000.0,
            open_options_premium=0.0,
            underlying_price=150.0,
            config=default_config,
        )
        assert order is not None
        expected_qty = math.floor(35_000.0 / (3.10 * 100))
        assert order.contracts_qty == expected_qty

    def test_est_premium_equals_qty_times_cost(self, default_config):
        contract = _make_contract(bid=3.00, ask=3.20)  # mid = 3.10
        order = size_option(
            contract,
            portfolio_equity=100_000.0,
            open_options_premium=0.0,
            underlying_price=150.0,
            config=default_config,
        )
        assert order is not None
        expected_premium = order.contracts_qty * 3.10 * 100
        assert abs(order.est_premium - expected_premium) < 1e-6

    def test_side_matches_contract(self, default_config):
        contract = _make_contract(side=OptionSide.PUT, delta=-0.75)
        order = size_option(
            contract,
            portfolio_equity=100_000.0,
            open_options_premium=0.0,
            underlying_price=150.0,
            config=default_config,
        )
        assert order is not None
        assert order.side is OptionSide.PUT

    def test_contract_reference_preserved(self, default_config):
        contract = _make_contract()
        order = size_option(
            contract,
            portfolio_equity=100_000.0,
            open_options_premium=0.0,
            underlying_price=150.0,
            config=default_config,
        )
        assert order is not None
        assert order.contract is contract


# ---------------------------------------------------------------------------
# Delta-adjusted notional formula
# ---------------------------------------------------------------------------

class TestDeltaAdjustedNotional:
    def test_notional_formula(self, default_config):
        """delta_adjusted_notional = |delta| × 100 × underlying_price × contracts_qty."""
        contract = _make_contract(delta=0.75, bid=3.00, ask=3.20)
        underlying_price = 200.0
        order = size_option(
            contract,
            portfolio_equity=100_000.0,
            open_options_premium=0.0,
            underlying_price=underlying_price,
            config=default_config,
        )
        assert order is not None
        expected = abs(0.75) * 100.0 * underlying_price * order.contracts_qty
        assert abs(order.delta_adjusted_notional - expected) < 1e-6

    def test_put_delta_absolute_value(self, default_config):
        """Put delta is negative; notional must use absolute value."""
        contract = _make_contract(delta=-0.75, side=OptionSide.PUT)
        order = size_option(
            contract,
            portfolio_equity=100_000.0,
            open_options_premium=0.0,
            underlying_price=150.0,
            config=default_config,
        )
        assert order is not None
        assert order.delta_adjusted_notional > 0.0
        expected = 0.75 * 100.0 * 150.0 * order.contracts_qty
        assert abs(order.delta_adjusted_notional - expected) < 1e-6


# ---------------------------------------------------------------------------
# Sleeve cap binding
# ---------------------------------------------------------------------------

class TestSleeveCapBinding:
    def test_open_premium_reduces_budget(self, default_config):
        """With open_options_premium close to the ceiling, fewer contracts are sized."""
        contract = _make_contract(bid=3.00, ask=3.20)  # mid = 3.10, cost_per = 310
        # sleeve = 35_000; open = 34_000; remaining = 1_000; floor(1000/310) = 3
        order = size_option(
            contract,
            portfolio_equity=100_000.0,
            open_options_premium=34_000.0,
            underlying_price=150.0,
            config=default_config,
        )
        assert order is not None
        assert order.contracts_qty == math.floor(1_000.0 / 310.0)

    def test_open_premium_nearly_exhausted_gives_fewer_contracts(self, default_config):
        """open_premium leaves room for exactly 1 contract."""
        contract = _make_contract(bid=3.00, ask=3.20)  # cost_per = 310
        # sleeve = 35_000; remaining = 310 → floor(310/310) = 1
        open_prem = 35_000.0 - 310.0
        order = size_option(
            contract,
            portfolio_equity=100_000.0,
            open_options_premium=open_prem,
            underlying_price=150.0,
            config=default_config,
        )
        assert order is not None
        assert order.contracts_qty == 1

    def test_sleeve_fully_exhausted_returns_none(self, default_config):
        """open_options_premium == sleeve ceiling → cannot afford 1 contract."""
        contract = _make_contract(bid=3.00, ask=3.20)
        order = size_option(
            contract,
            portfolio_equity=100_000.0,
            open_options_premium=35_000.0,  # exactly at ceiling
            underlying_price=150.0,
            config=default_config,
        )
        assert order is None

    def test_sleeve_over_exhausted_returns_none(self, default_config):
        """open_options_premium > ceiling → negative remaining → None."""
        contract = _make_contract(bid=3.00, ask=3.20)
        order = size_option(
            contract,
            portfolio_equity=100_000.0,
            open_options_premium=40_000.0,
            underlying_price=150.0,
            config=default_config,
        )
        assert order is None


# ---------------------------------------------------------------------------
# Can't afford one contract
# ---------------------------------------------------------------------------

class TestCannotAffordOneContract:
    def test_expensive_contract_returns_none(self, default_config):
        """If one contract costs more than the remaining budget, return None."""
        # portfolio_equity = 100 → sleeve = 35; cost_per = 3.10*100=310 > 35
        contract = _make_contract(bid=3.00, ask=3.20)
        order = size_option(
            contract,
            portfolio_equity=100.0,  # tiny portfolio
            open_options_premium=0.0,
            underlying_price=150.0,
            config=default_config,
        )
        assert order is None

    def test_one_cent_budget_returns_none(self, default_config):
        contract = _make_contract(bid=3.00, ask=3.20)
        order = size_option(
            contract,
            portfolio_equity=0.01,
            open_options_premium=0.0,
            underlying_price=150.0,
            config=default_config,
        )
        assert order is None


# ---------------------------------------------------------------------------
# Guard: mid_price <= 0 → None
# ---------------------------------------------------------------------------

class TestMidPriceGuard:
    def test_zero_bid_zero_ask_returns_none(self, default_config):
        contract = _make_contract(bid=0.0, ask=0.0)
        order = size_option(
            contract,
            portfolio_equity=100_000.0,
            open_options_premium=0.0,
            underlying_price=150.0,
            config=default_config,
        )
        assert order is None

    def test_none_bid_returns_none(self, default_config):
        contract = _make_contract(bid=None, ask=3.20)
        order = size_option(
            contract,
            portfolio_equity=100_000.0,
            open_options_premium=0.0,
            underlying_price=150.0,
            config=default_config,
        )
        assert order is None

    def test_none_ask_returns_none(self, default_config):
        contract = _make_contract(bid=3.00, ask=None)
        order = size_option(
            contract,
            portfolio_equity=100_000.0,
            open_options_premium=0.0,
            underlying_price=150.0,
            config=default_config,
        )
        assert order is None

    def test_none_delta_returns_none(self, default_config):
        contract = _make_contract(delta=None)
        order = size_option(
            contract,
            portfolio_equity=100_000.0,
            open_options_premium=0.0,
            underlying_price=150.0,
            config=default_config,
        )
        assert order is None
