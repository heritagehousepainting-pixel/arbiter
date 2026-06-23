"""Offline tests for AlpacaBarsClient (httpx.MockTransport, no network)."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from mirofish.clients.alpaca import AlpacaBarsClient


def _client(handler, **kwargs) -> AlpacaBarsClient:
    return AlpacaBarsClient(
        api_key="k",
        secret_key="s",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _bar(t: str, c: float) -> dict:
    return {"t": t, "o": c, "h": c, "l": c, "c": c, "v": 1000.0}


def test_pagination_concatenates_and_sorts():
    pages = {
        None: {
            "bars": [_bar("2024-01-03T05:00:00Z", 3), _bar("2024-01-02T05:00:00Z", 2)],
            "next_page_token": "PAGE2",
        },
        "PAGE2": {
            "bars": [_bar("2024-01-04T05:00:00Z", 4)],
            "next_page_token": None,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("page_token")
        return httpx.Response(200, json=pages[token])

    bars = _client(handler).bars(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 10, tzinfo=timezone.utc),
    )
    assert [b.c for b in bars] == [2.0, 3.0, 4.0]  # sorted ascending


def test_bars_strictly_before_end_dropped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "bars": [
                    _bar("2024-01-09T05:00:00Z", 9),
                    _bar("2024-01-10T05:00:00Z", 10),  # t >= end -> dropped
                ],
                "next_page_token": None,
            },
        )

    bars = _client(handler).bars(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 10, 5, 0, 0, tzinfo=timezone.utc),
    )
    assert [b.c for b in bars] == [9.0]


def test_404_returns_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    assert _client(handler).bars(
        "ZZZZ",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 10, tzinfo=timezone.utc),
    ) == []


def test_422_returns_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "bad"})

    assert _client(handler).bars(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 10, tzinfo=timezone.utc),
    ) == []


def test_network_error_returns_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    assert _client(handler).bars(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 10, tzinfo=timezone.utc),
    ) == []


def test_429_then_200_retried(monkeypatch):
    calls = {"n": 0}
    slept: list[float] = []
    monkeypatch.setattr("mirofish.clients.alpaca.time.sleep", lambda s: slept.append(s))

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(
            200, json={"bars": [_bar("2024-01-02T05:00:00Z", 2)], "next_page_token": None}
        )

    bars = _client(handler).bars(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 10, tzinfo=timezone.utc),
    )
    assert [b.c for b in bars] == [2.0]
    assert slept == [2.0]  # honored Retry-After


def test_429_exhausts_retries_returns_empty(monkeypatch):
    monkeypatch.setattr("mirofish.clients.alpaca.time.sleep", lambda s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    bars = _client(handler, max_retries=2).bars(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 10, tzinfo=timezone.utc),
    )
    assert bars == []


def test_bars_as_of_pit_filter(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "bars": [
                    _bar("2026-05-30T04:00:00Z", 1),  # before as_of -> kept
                    _bar("2026-06-01T00:00:00Z", 2),  # == as_of -> kept (t <= as_of)
                    _bar("2026-06-01T12:00:00Z", 3),  # after as_of -> dropped
                ],
                "next_page_token": None,
            },
        )

    as_of = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    bars = _client(handler).bars_as_of("AAPL", as_of)
    assert [b.c for b in bars] == [1.0, 2.0]
    assert all(b.t <= as_of for b in bars)


def test_never_raises_on_garbage_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    assert _client(handler).bars(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 10, tzinfo=timezone.utc),
    ) == []
