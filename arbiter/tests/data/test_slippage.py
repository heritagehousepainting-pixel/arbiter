"""Tests for model_slippage — Lane 3 core.

Covers INTERFACES.md §3 and §10b.3.

Formula: adjusted = price × (1 + 5bps) + 0.5 × spread
"""
from __future__ import annotations

import pytest

from arbiter.data.slippage import model_slippage


class TestModelSlippage:
    def test_zero_spread(self):
        """With zero spread, result is price × (1 + 5bps)."""
        price = 100.0
        result = model_slippage(price, spread=0.0)
        expected = 100.0 * 1.0005
        assert result == pytest.approx(expected)

    def test_with_spread(self):
        """Full formula: price × 1.0005 + 0.5 × spread."""
        price = 100.0
        spread = 0.10
        result = model_slippage(price, spread)
        expected = 100.0 * 1.0005 + 0.5 * 0.10
        assert result == pytest.approx(expected)

    def test_adjusted_price_greater_than_raw(self):
        """Slippage-adjusted price is always ≥ raw price when spread ≥ 0."""
        price = 200.0
        result = model_slippage(price, spread=0.05)
        assert result > price

    def test_5bps_calculation(self):
        """Confirm the 5bps component: 5bps of $1000 = $0.50."""
        price = 1000.0
        result_no_spread = model_slippage(price, spread=0.0)
        slippage_component = result_no_spread - price
        assert slippage_component == pytest.approx(0.50)

    def test_half_spread_calculation(self):
        """Half-spread component: 0.5 × $0.04 = $0.02."""
        price = 100.0
        spread = 0.04
        result = model_slippage(price, spread)
        half_spread_component = result - price * 1.0005
        assert half_spread_component == pytest.approx(0.02)

    def test_realistic_stock_price(self):
        """Realistic stock: $150 price, $0.02 spread."""
        price = 150.0
        spread = 0.02
        result = model_slippage(price, spread)
        # 5bps = $0.075, half-spread = $0.01
        expected = 150.075 + 0.01
        assert result == pytest.approx(expected, abs=1e-6)

    def test_large_spread(self):
        """Large spread (illiquid stock): spread dominates."""
        price = 10.0
        spread = 2.0  # 20% spread — very illiquid
        result = model_slippage(price, spread)
        expected = 10.0 * 1.0005 + 1.0
        assert result == pytest.approx(expected)

    def test_fractional_price(self):
        """Works with fractional prices."""
        price = 0.50
        spread = 0.01
        result = model_slippage(price, spread)
        expected = 0.50 * 1.0005 + 0.005
        assert result == pytest.approx(expected)

    def test_returns_float(self):
        """Return type is float."""
        result = model_slippage(100.0, 0.05)
        assert isinstance(result, float)

    def test_zero_price_zero_spread(self):
        """Edge case: both inputs zero."""
        result = model_slippage(0.0, 0.0)
        assert result == pytest.approx(0.0)
