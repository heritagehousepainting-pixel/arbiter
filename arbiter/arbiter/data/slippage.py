"""Slippage model — Lane 3 core.

Implements INTERFACES.md §3 and §10b.3::

    def model_slippage(price: float, spread: float) -> float

Formula: adjusted_fill_price = price × (1 + 0.0005) + 0.5 × spread

Where:
    - 0.0005 = 5 basis points (5bps)
    - 0.5 × spread = half the bid-ask spread

This is the Phase-1 slippage model (simple, conservative).  The Lane 3
``SimExecutor`` receives the adjusted price as ``limit_price`` on the
``OrderIntent`` (INTERFACES.md §10b.3).

Side awareness (exit-monitor amendment B1):
    The default (BUY) model biases the limit UP (worse price for the buyer →
    conservative).  A SELL must bias DOWN instead — overstating proceeds in
    sim and, worse, leaving a non-marketable limit unfilled on a real broker.
    ``model_slippage(price, spread, side=OrderSide.SELL)`` (or
    ``model_slippage_sell``) applies ``price × (1 − 5bps) − 0.5 × spread``.

Note on Phase 1 vs future:
    The design spec §6 residual risks notes the slippage model should be
    upgraded to volume-adjusted market impact before live capital.  This
    function is the Phase-1 stub; replace the body in a future lane without
    changing the signature.
"""
from __future__ import annotations

from arbiter.types import OrderSide

_BPS = 0.0005


def model_slippage(
    price: float,
    spread: float,
    side: OrderSide = OrderSide.BUY,
) -> float:
    """Return the slippage-adjusted fill price for *side*.

    Formula (INTERFACES.md §3 / §10b.3, amended by exit-monitor B1):
        BUY  : adjusted = price × (1 + 5bps) + 0.5 × spread  (bias UP)
        SELL : adjusted = price × (1 − 5bps) − 0.5 × spread  (bias DOWN)

    Parameters
    ----------
    price:
        Raw (pre-slippage) execution price.  Typically the next-day open for
        a BUY entry; the current PIT close for a SELL exit.
    spread:
        Bid-ask spread in price units (same currency as ``price``).
    side:
        Order side.  Defaults to ``OrderSide.BUY`` so existing callers keep
        the unchanged buy-side behaviour.

    Returns
    -------
    float
        Slippage-adjusted fill price.  For BUY, always ≥ price when spread ≥ 0;
        for SELL, always ≤ price when spread ≥ 0.
    """
    if side == OrderSide.SELL:
        return price * (1.0 - _BPS) - 0.5 * spread
    return price * (1.0 + _BPS) + 0.5 * spread


def model_slippage_sell(price: float, spread: float) -> float:
    """Sell-side slippage helper: bias the limit DOWN (B1).

    Equivalent to ``model_slippage(price, spread, side=OrderSide.SELL)``.
    """
    return model_slippage(price, spread, side=OrderSide.SELL)
