"""Lane 3 price-source clients — Alpaca (primary) + Stooq (fallback/delisted).

Public surface
--------------
AlpacaPriceSource   — Alpaca data API; daily bars via ``bars(ticker, start, end)``.
StooqPriceSource    — Stooq CSV endpoint; daily bars; no yfinance.
build_price_gateway — Assembles a PITGateway with Alpaca primary + Stooq fallback.

Wire into the orchestrator::

    from arbiter.config import load_config
    from arbiter.data.sources import build_price_gateway

    config = load_config()
    pit = build_price_gateway(config)
    close = pit.get("price_close", "AAPL", as_of)
"""
from __future__ import annotations

from arbiter.data.sources.alpaca import AlpacaPriceSource
from arbiter.data.sources.stooq import StooqPriceSource
from arbiter.data.sources._gateway import build_price_gateway

__all__ = [
    "AlpacaPriceSource",
    "StooqPriceSource",
    "build_price_gateway",
]
