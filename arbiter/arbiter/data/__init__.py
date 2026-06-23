"""Point-in-time data interfaces — Lane 3 core.

The ONLY sanctioned way for the rest of the system to read price, filing,
news, and trust data.  No ``get_latest()``, no ``datetime.now()`` outside
``clock.py``.  See INTERFACES.md §3 and design spec §4.2.

Public surface::

    from arbiter.data import Clock, BacktestClock, PITGateway, Bar, PriceSource
    from arbiter.data import beta_252d, model_slippage
"""
from __future__ import annotations

from arbiter.data.beta import beta_252d
from arbiter.data.clock import BacktestClock, Clock
from arbiter.data.pit import Bar, FixtureSource, PITGateway, PriceSource
from arbiter.data.slippage import model_slippage

__all__ = [
    "Clock",
    "BacktestClock",
    "Bar",
    "PriceSource",
    "PITGateway",
    "FixtureSource",
    "beta_252d",
    "model_slippage",
]
