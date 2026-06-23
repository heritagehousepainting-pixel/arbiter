"""Offline tests for SecFactsClient (httpx.MockTransport, no network)."""
from __future__ import annotations

import httpx
import pytest

from mirofish.clients.sec_facts import SecFactsClient

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_TICKERS_BODY = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft"},
}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("mirofish.clients.sec_facts.time.sleep", lambda s: None)


def _client(handler) -> SecFactsClient:
    return SecFactsClient(
        user_agent="MiroFish test test@example.com",
        transport=httpx.MockTransport(handler),
    )


def test_cik_resolved_and_zero_padded():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "User-Agent" in request.headers
        return httpx.Response(200, json=_TICKERS_BODY)

    c = _client(handler)
    assert c.cik_for_ticker("AAPL") == "0000320193"
    assert c.cik_for_ticker("aapl") == "0000320193"  # case-insensitive
    assert c.cik_for_ticker("MSFT") == "0000789019"


def test_unknown_ticker_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_TICKERS_BODY)

    assert _client(handler).cik_for_ticker("ZZZZ") is None


def test_tickers_map_cached_once():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_TICKERS_BODY)

    c = _client(handler)
    c.cik_for_ticker("AAPL")
    c.cik_for_ticker("MSFT")
    assert calls["n"] == 1  # fetched once, cached on the instance


def test_company_facts_200_returns_dict():
    facts = {"cik": 320193, "facts": {"us-gaap": {}}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert "companyfacts/CIK0000320193.json" in str(request.url)
        return httpx.Response(200, json=facts)

    assert _client(handler).company_facts("0000320193") == facts


def test_company_facts_404_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    assert _client(handler).company_facts("0000320193") is None


def test_company_facts_network_error_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    assert _client(handler).company_facts("0000320193") is None


def test_facts_as_of_resolves_then_fetches():
    facts = {"facts": {"us-gaap": {"Revenues": {}}}}

    def handler(request: httpx.Request) -> httpx.Response:
        if "company_tickers" in str(request.url):
            return httpx.Response(200, json=_TICKERS_BODY)
        return httpx.Response(200, json=facts)

    from datetime import datetime, timezone

    out = _client(handler).facts_as_of("AAPL", datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert out == facts


def test_facts_as_of_unknown_ticker_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_TICKERS_BODY)

    from datetime import datetime, timezone

    assert (
        _client(handler).facts_as_of("ZZZZ", datetime(2026, 6, 1, tzinfo=timezone.utc))
        is None
    )


def test_429_then_200_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json=_TICKERS_BODY)

    assert _client(handler).cik_for_ticker("AAPL") == "0000320193"
    assert calls["n"] == 2
