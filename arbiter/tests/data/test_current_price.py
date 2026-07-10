"""Current-price provider + PIT-purity guard tests — sub-project #3 (Dec 1/6).

Feed-outage hardening tests (2026-07-10 incident): a mis-set
``ALPACA_DATA_FEED=sip`` 403'd every latest-trades call for 8 trading days and
SILENTLY blinded the exit monitor's stop-losses.  The provider now retries once
on ``feed=iex`` when the configured feed ERRORS, and escalates a total outage
(all attempted feeds errored, zero prices) to a critical alert — while a clean
HTTP-200-but-empty response (market closed) stays quiet.
"""
from __future__ import annotations

import dataclasses
import pathlib
from datetime import datetime, timezone

import httpx
import pytest

from arbiter.config import Config, load_config
from arbiter.data.clock import BacktestClock
from arbiter.data.current_price import (
    AlpacaCurrentPriceSource,
    CurrentPriceProvider,
    NullCurrentPriceProvider,
)
from arbiter.data.pit import _SUPPORTED_FIELDS

_AS_OF = datetime(2026, 7, 10, 14, 30, tzinfo=timezone.utc)


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


class _FeedFakeHTTP:
    """Per-feed scripted fake: ``feed=sip`` 403s / ``feed=iex`` serves trades.

    ``behavior`` maps feed name → either a dict of {symbol: price} (served as
    latest-trades JSON, empty quotes) or an Exception instance to raise.
    Records every URL so tests can assert which feeds were attempted.
    """

    def __init__(self, behavior: dict[str, dict[str, float] | Exception]):
        self.behavior = behavior
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict):
        self.calls.append(url)
        feed = url.rsplit("feed=", 1)[1]
        action = self.behavior[feed]
        if isinstance(action, Exception):
            raise action
        if "/trades/latest" in url:
            return {"trades": {s: {"p": p} for s, p in action.items()}}
        if "/quotes/latest" in url:
            return {"quotes": {}}
        raise AssertionError(f"unexpected url {url}")


class _AlertRecorder:
    """Duck-types ``Alerting.alert`` — records calls, never touches the network."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict, datetime]] = []

    def alert(self, tier, message, ctx, *, as_of):
        self.calls.append((tier, message, ctx, as_of))
        return None


def _http_403(url: str = "https://data.example/v2/stocks/trades/latest") -> httpx.HTTPStatusError:
    """A realistic entitlement failure, built offline (no network)."""
    request = httpx.Request("GET", url)
    response = httpx.Response(403, request=request)
    return httpx.HTTPStatusError("403 Forbidden", request=request, response=response)


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

        # Inject the recorder so the outage path can't lazily build a real
        # Alerting (audit-file write) inside the test.
        src = AlpacaCurrentPriceSource(_cfg(), http_get=boom, alerting=_AlertRecorder())
        assert src.current_prices(["AAPL"]) == {}


class TestFeedFallbackAndOutageAlert:
    """2026-07-10 incident hardening: sip-403 → iex fallback; outage → alert."""

    def _src(self, http_get, *, feed: str, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ALPACA_DATA_FEED", feed)
        recorder = _AlertRecorder()
        src = AlpacaCurrentPriceSource(
            _cfg(),
            http_get=http_get,
            alerting=recorder,
            clock=BacktestClock(_AS_OF),
        )
        return src, recorder

    def test_sip_403_falls_back_to_iex_and_returns_prices(self, monkeypatch):
        """The incident shape: feed=sip 403s, feed=iex works → prices flow."""
        fake = _FeedFakeHTTP({"sip": _http_403(), "iex": {"AAPL": 100.0, "MSFT": 200.0}})
        src, recorder = self._src(fake, feed="sip", monkeypatch=monkeypatch)

        prices = src.current_prices(["AAPL", "MSFT"])

        assert prices == {"AAPL": 100.0, "MSFT": 200.0}
        assert any("feed=sip" in c for c in fake.calls)
        assert any("feed=iex" in c for c in fake.calls)
        # Fallback SUCCEEDED → stop-losses still see prices → no outage alert.
        assert recorder.calls == []

    def test_both_feeds_fail_returns_empty_and_alerts(self, monkeypatch):
        """sip errors AND the iex fallback errors → feed is BROKEN → critical alert."""
        fake = _FeedFakeHTTP({"sip": _http_403(), "iex": _http_403()})
        src, recorder = self._src(fake, feed="sip", monkeypatch=monkeypatch)

        assert src.current_prices(["AAPL"]) == {}

        assert len(recorder.calls) == 1
        tier, message, ctx, as_of = recorder.calls[0]
        assert tier == "critical"
        assert ctx["code"] == "current_price.feed_outage"
        assert ctx["primary_feed"] == "sip"
        assert ctx["fallback_attempted"] is True
        assert as_of == _AS_OF

    def test_http_200_empty_trades_market_closed_no_alert(self, monkeypatch):
        """Clean 200 with no trades/quotes = closed market → empty, NO alert."""
        fake = _FakeTradesHTTP({}, quotes={})
        src, recorder = self._src(fake, feed="iex", monkeypatch=monkeypatch)

        assert src.current_prices(["AAPL", "MSFT"]) == {}
        assert recorder.calls == []

    def test_http_200_empty_on_sip_does_not_fall_back_or_alert(self, monkeypatch):
        """200-but-empty on the PRIMARY feed is not an error → no iex retry, no alert."""
        fake = _FakeTradesHTTP({}, quotes={})
        src, recorder = self._src(fake, feed="sip", monkeypatch=monkeypatch)

        assert src.current_prices(["AAPL"]) == {}
        assert all("feed=sip" in c for c in fake.calls)
        assert recorder.calls == []

    def test_primary_iex_failure_no_fallback_still_alerts(self, monkeypatch):
        """Primary already iex → no retry loop, but the outage still pages."""
        fake = _FeedFakeHTTP({"iex": _http_403()})
        src, recorder = self._src(fake, feed="iex", monkeypatch=monkeypatch)

        assert src.current_prices(["AAPL"]) == {}

        # Exactly one trades + one quotes attempt, all on iex — no second pass.
        assert len(fake.calls) == 2
        assert all("feed=iex" in c for c in fake.calls)
        assert len(recorder.calls) == 1
        tier, _, ctx, _ = recorder.calls[0]
        assert tier == "critical"
        assert ctx["fallback_attempted"] is False

    def test_outage_alert_latched_once_per_episode_and_rearmed(self, monkeypatch):
        """A persistent outage pages ONCE; a healthy read re-arms the latch."""
        fake = _FeedFakeHTTP({"iex": _http_403()})
        src, recorder = self._src(fake, feed="iex", monkeypatch=monkeypatch)

        src.current_prices(["AAPL"])
        src.current_prices(["AAPL"])  # still broken → suppressed by the latch
        assert len(recorder.calls) == 1

        fake.behavior["iex"] = {"AAPL": 101.0}  # feed recovers
        assert src.current_prices(["AAPL"]) == {"AAPL": 101.0}

        fake.behavior["iex"] = _http_403()  # breaks AGAIN → new episode pages
        src.current_prices(["AAPL"])
        assert len(recorder.calls) == 2


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
