"""Shared fixtures for A3 adapter tests.

All tests are OFFLINE — no real HTTP calls.  The http_get callable on
FinnhubClient is replaced with a mock that returns pre-baked JSON.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from arbiter.data.clock import BacktestClock, Clock
from arbiter.ingest.finnhub.client import FinnhubClient
from arbiter.adapters.a3.source_finnhub import FinnhubNewsSource


# ---------------------------------------------------------------------------
# Reference timestamps
# ---------------------------------------------------------------------------

# Fixed decision time (tz-aware UTC) used across all tests.
AS_OF = datetime(2026, 6, 22, 14, 0, 0, tzinfo=timezone.utc)

# Article published 2 hours before AS_OF — well within lookback, not future.
ARTICLE_TS_RECENT = AS_OF - timedelta(hours=2)

# Article published 36 hours before AS_OF — older, lower recency score.
ARTICLE_TS_OLD = AS_OF - timedelta(hours=36)

# Article published 1 hour AFTER AS_OF — must be filtered (no look-ahead).
ARTICLE_TS_FUTURE = AS_OF + timedelta(hours=1)


# ---------------------------------------------------------------------------
# Minimal fake config objects
# ---------------------------------------------------------------------------

class FakeConfigWithKey:
    """Config stub with a real-looking (but fake) Finnhub key.

    Thresholds are set to the production defaults so the gate is active
    in all standard tests.  Use ``FakeConfigWithKeyAndThresholds`` when
    you need to override them per-test.
    """
    finnhub_api_key = "test_api_key_12345"
    a3_min_stance: float = 0.25
    a3_min_confidence: float = 0.0


class FakeConfigNoKey:
    """Config stub with empty API key → A3 inert."""
    finnhub_api_key = ""


# ---------------------------------------------------------------------------
# Minimal Finnhub API response builders
# ---------------------------------------------------------------------------

def make_news_response(
    articles: list[dict] | None = None,
) -> str:
    """Return JSON string mimicking Finnhub /api/v1/company-news response."""
    if articles is None:
        articles = []
    return json.dumps(articles)


def make_article(
    headline: str = "Test headline",
    summary: str = "Test summary",
    url: str = "https://reuters.com/article/1",
    source: str = "Reuters",
    published_epoch: int | None = None,
) -> dict:
    """Build a single article dict as returned by Finnhub."""
    if published_epoch is None:
        published_epoch = int(ARTICLE_TS_RECENT.timestamp())
    return {
        "category": "company",
        "datetime": published_epoch,
        "headline": headline,
        "id": 1,
        "image": "",
        "related": "AAPL",
        "source": source,
        "summary": summary,
        "url": url,
    }


def make_sentiment_response(
    bullish: float = 0.6,
    bearish: float = 0.2,
) -> str:
    """Return JSON string mimicking Finnhub /api/v1/news-sentiment response."""
    return json.dumps({
        "buzz": {"articlesInLastWeek": 10, "weeklyAverage": 8.0, "buzz": 1.25},
        "companyNewsScore": 0.75,
        "sectorAverageBullishPercent": 0.55,
        "sectorAverageNewsScore": 0.5,
        "sentiment": {
            "bearishPercent": bearish,
            "bullishPercent": bullish,
        },
        "symbol": "AAPL",
    })


# ---------------------------------------------------------------------------
# Injectable http_get factories
# ---------------------------------------------------------------------------

def http_get_with_responses(
    news_json: str,
    sentiment_json: str,
) -> Any:
    """Return an http_get callable that returns pre-baked responses.

    Routes by URL fragment:
    - ``/company-news`` → news_json
    - ``/news-sentiment`` → sentiment_json
    """
    def _http_get(url: str, params: dict) -> str:
        if "company-news" in url:
            return news_json
        if "news-sentiment" in url:
            return sentiment_json
        raise ValueError(f"Unexpected URL in test mock: {url}")
    return _http_get


def http_get_bad_json(url: str, params: dict) -> str:
    """Return malformed JSON — used to test fail-closed behaviour."""
    return "NOT VALID JSON {{{{"


def http_get_empty_array(url: str, params: dict) -> str:
    """Return an empty array — used to test no-articles path."""
    if "company-news" in url:
        return "[]"
    return make_sentiment_response()


# ---------------------------------------------------------------------------
# Build a FinnhubNewsSource with injected http_get
# ---------------------------------------------------------------------------

def make_source(
    api_key: str = "test_key",
    news_json: str | None = None,
    sentiment_json: str | None = None,
    *,
    http_get_fn: Any = None,
) -> FinnhubNewsSource:
    """Build a FinnhubNewsSource backed by a mock http_get."""
    if http_get_fn is None:
        _news = news_json if news_json is not None else make_news_response()
        _sent = sentiment_json if sentiment_json is not None else make_sentiment_response()
        http_get_fn = http_get_with_responses(_news, _sent)

    client = FinnhubClient(
        api_key=api_key,
        http_get=http_get_fn,
        sleep_fn=lambda _: None,  # no real sleep in tests
    )
    return FinnhubNewsSource(client)
