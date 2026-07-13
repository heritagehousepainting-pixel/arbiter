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
from datetime import datetime, timedelta, timezone

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


def _http_429(url: str = "https://data.example/v2/stocks/trades/latest") -> httpx.HTTPStatusError:
    """A transient rate-limit failure, built offline (no network)."""
    request = httpx.Request("GET", url)
    response = httpx.Response(429, request=request)
    return httpx.HTTPStatusError("429 Too Many Requests", request=request, response=response)


def _http_400(url: str = "https://data.example/v2/stocks/trades/latest") -> httpx.HTTPStatusError:
    """A hard client error (invalid symbol in a batch) — built offline."""
    request = httpx.Request("GET", url)
    response = httpx.Response(400, request=request)
    return httpx.HTTPStatusError("400 Bad Request", request=request, response=response)


class _Flaky429ThenOk:
    """Raises 429 on the FIRST call, then serves trades — exercises the retry."""

    def __init__(self, trades: dict[str, float]):
        self.trades = trades
        self.calls = 0

    def __call__(self, url: str, headers: dict):
        self.calls += 1
        if self.calls == 1:
            raise _http_429()
        if "/trades/latest" in url:
            return {"trades": {s: {"p": p} for s, p in self.trades.items()}}
        return {"quotes": {}}


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

    def test_option_occ_symbol_excluded_from_stock_batch(self):
        """An OCC option symbol must NEVER be sent to the Alpaca STOCKS endpoint:
        Alpaca 400s the WHOLE batch, blinding every equity and firing a false
        feed-outage page every cycle.  Options are filtered out so equities still
        price and no alert fires.  Regression: a held PFE270319C00023000 paged a
        critical 'stop-losses BLIND' every ~5 minutes."""
        option = "PFE270319C00023000"

        def http(url, headers):
            syms = url.split("symbols=", 1)[1].split("&", 1)[0].split(",")
            if option in syms:
                raise _http_400(url)  # Alpaca rejects the entire mixed batch
            if "/trades/latest" in url:
                return {"trades": {"AMZN": {"p": 240.0}}}
            return {"quotes": {}}

        recorder = _AlertRecorder()
        src = AlpacaCurrentPriceSource(_cfg(), http_get=http, alerting=recorder)

        prices = src.current_prices(["AMZN", option])

        assert prices == {"AMZN": 240.0}, "equity must still price despite a held option"
        assert recorder.calls == [], "an option symbol must not trigger a feed-outage page"


class TestFeedFallbackAndOutageAlert:
    """2026-07-10 incident hardening: sip-403 → iex fallback; outage → alert."""

    def _src(self, http_get, *, feed: str, monkeypatch: pytest.MonkeyPatch, sleep=None):
        monkeypatch.setenv("ALPACA_DATA_FEED", feed)
        recorder = _AlertRecorder()
        src = AlpacaCurrentPriceSource(
            _cfg(),
            http_get=http_get,
            alerting=recorder,
            clock=BacktestClock(_AS_OF),
            sleep=sleep if sleep is not None else (lambda _s: None),
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

    def test_outage_alert_latched_once_per_cooldown(self, monkeypatch):
        """A burst / persistent outage pages ONCE within the cooldown (not per
        symbol); after the cooldown elapses a still-broken feed pages again."""
        fake = _FeedFakeHTTP({"iex": _http_403()})
        src, recorder = self._src(fake, feed="iex", monkeypatch=monkeypatch)

        # Burst: several per-symbol outage reads in one sweep → ONE page.
        src.current_prices(["AAPL"])
        src.current_prices(["MSFT"])
        src.current_prices(["TSLA"])
        assert len(recorder.calls) == 1

        # A healthy read no longer re-arms — that was the per-symbol double-page bug.
        fake.behavior["iex"] = {"AAPL": 101.0}
        assert src.current_prices(["AAPL"]) == {"AAPL": 101.0}
        fake.behavior["iex"] = _http_403()
        src.current_prices(["AAPL"])  # still within cooldown → suppressed
        assert len(recorder.calls) == 1

        # Advance past the cooldown → a persistent outage re-pages.
        src._clock.advance(timedelta(seconds=src._outage_alert_cooldown_s + 1))
        src.current_prices(["AAPL"])
        assert len(recorder.calls) == 2

    def test_rate_limit_429_is_transient_no_alert(self, monkeypatch):
        """A 429 is transient: retried once, and if still 429 it is NOT escalated
        to a feed-outage page (empty result → monitor fails closed to daily PIT)."""
        fake = _FeedFakeHTTP({"iex": _http_429()})
        sleeps: list = []
        src, recorder = self._src(
            fake, feed="iex", monkeypatch=monkeypatch, sleep=sleeps.append
        )
        assert src.current_prices(["AAPL"]) == {}
        assert recorder.calls == []      # no critical page for a transient 429
        assert sleeps                    # a backoff/retry was attempted

    def test_rate_limit_429_then_success_returns_price(self, monkeypatch):
        """429 on the first attempt, success on the retry → price flows, no alert."""
        fake = _Flaky429ThenOk({"AAPL": 100.0})
        src, recorder = self._src(fake, feed="iex", monkeypatch=monkeypatch)
        assert src.current_prices(["AAPL"]) == {"AAPL": 100.0}
        assert recorder.calls == []


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
