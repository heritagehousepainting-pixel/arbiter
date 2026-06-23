"""Current-price provider + PIT-purity guard tests — sub-project #3 (Dec 1/6)."""
from __future__ import annotations

import dataclasses
import pathlib

import pytest

from arbiter.config import Config, load_config
from arbiter.data.current_price import (
    AlpacaCurrentPriceSource,
    CurrentPriceProvider,
    NullCurrentPriceProvider,
)
from arbiter.data.pit import _SUPPORTED_FIELDS


def _cfg() -> Config:
    return dataclasses.replace(
        load_config(),
        executor_backend="alpaca_paper",
        alpaca_api_key="key",
        alpaca_secret_key="secret",
    )


class _FakeTradesHTTP:
    """Records URLs and returns scripted latest-trades JSON."""

    def __init__(self, trades: dict[str, float], quotes: dict[str, tuple[float, float]] | None = None):
        self.trades = trades
        self.quotes = quotes or {}
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict):
        self.calls.append(url)
        if "/trades/latest" in url:
            return {"trades": {s: {"p": p} for s, p in self.trades.items()}}
        if "/quotes/latest" in url:
            return {"quotes": {s: {"bp": b, "ap": a} for s, (b, a) in self.quotes.items()}}
        raise AssertionError(f"unexpected url {url}")


class TestNullProvider:
    def test_returns_none(self):
        p = NullCurrentPriceProvider()
        assert p.current_price("AAPL") is None
        assert p.current_prices(["AAPL", "MSFT"]) == {}

    def test_satisfies_protocol(self):
        assert isinstance(NullCurrentPriceProvider(), CurrentPriceProvider)


class TestAlpacaCurrentPriceSource:
    def test_multi_symbol_batch_single_call(self):
        """C1: all tickers fetched in ONE latest-trades call."""
        fake = _FakeTradesHTTP({"AAPL": 100.0, "MSFT": 200.0, "TSLA": 300.0})
        src = AlpacaCurrentPriceSource(_cfg(), http_get=fake)

        prices = src.current_prices(["AAPL", "MSFT", "TSLA"])

        assert prices == {"AAPL": 100.0, "MSFT": 200.0, "TSLA": 300.0}
        # Exactly one call for trades (no per-symbol fan-out).
        assert len([c for c in fake.calls if "/trades/latest" in c]) == 1
        assert "symbols=AAPL,MSFT,TSLA" in fake.calls[0]
        assert "feed=iex" in fake.calls[0]

    def test_single_ticker_delegates_to_batch(self):
        fake = _FakeTradesHTTP({"AAPL": 123.5})
        src = AlpacaCurrentPriceSource(_cfg(), http_get=fake)
        assert src.current_price("AAPL") == 123.5

    def test_quote_mid_fallback_when_no_trade(self):
        fake = _FakeTradesHTTP({}, quotes={"AAPL": (99.0, 101.0)})
        src = AlpacaCurrentPriceSource(_cfg(), http_get=fake)
        assert src.current_price("AAPL") == 100.0

    def test_missing_ticker_returns_none(self):
        fake = _FakeTradesHTTP({"AAPL": 100.0})
        src = AlpacaCurrentPriceSource(_cfg(), http_get=fake)
        assert src.current_price("ZZZZ") is None

    def test_http_error_yields_empty(self):
        def boom(url, headers):
            raise RuntimeError("network down")

        src = AlpacaCurrentPriceSource(_cfg(), http_get=boom)
        assert src.current_prices(["AAPL"]) == {}


class TestPITPurityGuard:
    def test_current_price_not_a_pit_field(self):
        assert "current_price" not in _SUPPORTED_FIELDS

    def test_no_lookahead_strings_in_module(self):
        """current_price.py must contain no datetime.now()/get_latest() (Dec 1c)."""
        path = pathlib.Path(__file__).resolve().parents[2] / "arbiter" / "data" / "current_price.py"
        src = path.read_text()
        assert "datetime.now(" not in src
        assert "datetime.utcnow(" not in src
        assert "get_latest(" not in src
