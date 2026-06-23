"""Tests for FinnhubNewsSource — the TipSource adapter.

All tests are OFFLINE (mock http_get).  Tests cover:
- Happy path: articles converted to UnverifiedTips
- No-lookahead: future articles filtered
- Blocked PR-wire domains dropped
- Malformed JSON → []
- Empty article list → []
- Source_id format: "finnhub:{domain}"
- tips sorted by ts ascending
- TipSource ABC compliance (source_id property)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from arbiter.adapters.a3.source_finnhub import FinnhubNewsSource
from arbiter.ingest.finnhub.client import FinnhubClient
from arbiter.tips.source import UnverifiedTip

from .conftest import (
    AS_OF,
    ARTICLE_TS_FUTURE,
    ARTICLE_TS_OLD,
    ARTICLE_TS_RECENT,
    http_get_bad_json,
    http_get_empty_array,
    make_article,
    make_news_response,
    make_sentiment_response,
    make_source,
    http_get_with_responses,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client_and_source(
    news_json: str = "[]",
    sentiment_json: str | None = None,
) -> FinnhubNewsSource:
    if sentiment_json is None:
        sentiment_json = make_sentiment_response()
    return make_source(news_json=news_json, sentiment_json=sentiment_json)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_fetch_returns_unverified_tips() -> None:
    """fetch() returns UnverifiedTip objects for valid articles."""
    article = make_article(
        headline="Apple beats earnings",
        url="https://reuters.com/article/1",
        source="Reuters",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([article])
    source = _make_client_and_source(news)
    tips = source.fetch("AAPL", AS_OF)

    assert len(tips) == 1
    tip = tips[0]
    assert isinstance(tip, UnverifiedTip)
    assert tip.ticker == "AAPL"
    assert tip.ts.tzinfo is not None  # tz-aware


def test_source_id_format() -> None:
    """source_id must be 'finnhub:{publisher}' from the article ``source`` field
    (Finnhub's url is its own finnhub.io redirect, so the publisher lives in
    ``source``, not the URL)."""
    article = make_article(
        url="https://finnhub.io/api/news?id=abc",
        source="Reuters",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([article])
    source = _make_client_and_source(news)
    tips = source.fetch("AAPL", AS_OF)

    assert len(tips) == 1
    assert tips[0].source_id == "finnhub:reuters"


def test_source_id_normalizes_publisher_name() -> None:
    """Distinct real publishers → distinct source_ids (lowercased), even though
    every Finnhub url shares the finnhub.io domain."""
    arts = [
        make_article(url="https://finnhub.io/api/news?id=1", source="Bloomberg",
                     published_epoch=int(ARTICLE_TS_RECENT.timestamp())),
        make_article(url="https://finnhub.io/api/news?id=2", source="CNBC",
                     published_epoch=int(ARTICLE_TS_RECENT.timestamp())),
    ]
    source = _make_client_and_source(make_news_response(arts))
    tips = source.fetch("AAPL", AS_OF)
    assert {t.source_id for t in tips} == {"finnhub:bloomberg", "finnhub:cnbc"}


def test_tips_sorted_ascending_by_ts() -> None:
    """Tips must be sorted by ts ascending (oldest first)."""
    old = make_article(
        headline="Old article",
        url="https://reuters.com/article/old",
        published_epoch=int(ARTICLE_TS_OLD.timestamp()),
    )
    recent = make_article(
        headline="Recent article",
        url="https://cnbc.com/article/recent",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([recent, old])  # note: recent listed first
    source = _make_client_and_source(news)
    tips = source.fetch("AAPL", AS_OF)

    assert len(tips) == 2
    assert tips[0].ts < tips[1].ts


# ---------------------------------------------------------------------------
# No-lookahead: future articles filtered
# ---------------------------------------------------------------------------

def test_future_article_dropped() -> None:
    """Article with published_at > as_of must be dropped (no look-ahead)."""
    future = make_article(
        url="https://reuters.com/future",
        published_epoch=int(ARTICLE_TS_FUTURE.timestamp()),
    )
    news = make_news_response([future])
    source = _make_client_and_source(news)
    tips = source.fetch("AAPL", AS_OF)

    assert tips == []


def test_article_at_exactly_as_of_is_included() -> None:
    """Article published exactly at as_of is NOT future — should be included."""
    at_as_of = make_article(
        url="https://reuters.com/exact",
        published_epoch=int(AS_OF.timestamp()),
    )
    news = make_news_response([at_as_of])
    source = _make_client_and_source(news)
    tips = source.fetch("AAPL", AS_OF)

    assert len(tips) == 1


def test_one_past_one_future_only_past_included() -> None:
    """Mixed past + future articles: only past should appear in tips."""
    past = make_article(
        url="https://reuters.com/past",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    future = make_article(
        headline="Future article",
        url="https://bloomberg.com/future",
        published_epoch=int(ARTICLE_TS_FUTURE.timestamp()),
    )
    news = make_news_response([past, future])
    source = _make_client_and_source(news)
    tips = source.fetch("AAPL", AS_OF)

    assert len(tips) == 1
    assert "bloomberg.com" not in tips[0].source_id


# ---------------------------------------------------------------------------
# Blocked PR-wire domains
# ---------------------------------------------------------------------------

def test_prnewswire_domain_blocked() -> None:
    """prnewswire.com articles are blocked (PR spam)."""
    article = make_article(
        url="https://prnewswire.com/release/123",
        source="PR Newswire",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([article])
    source = _make_client_and_source(news)
    tips = source.fetch("AAPL", AS_OF)

    assert tips == []


def test_businesswire_domain_blocked() -> None:
    article = make_article(
        url="https://businesswire.com/release/456",
        source="Business Wire",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([article])
    source = _make_client_and_source(news)
    tips = source.fetch("AAPL", AS_OF)

    assert tips == []


def test_editorial_publisher_not_blocked() -> None:
    """Reuters (editorial, not PR wire) must pass through."""
    article = make_article(
        url="https://reuters.com/article/1",
        source="Reuters",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([article])
    source = _make_client_and_source(news)
    tips = source.fetch("AAPL", AS_OF)

    assert len(tips) == 1


# ---------------------------------------------------------------------------
# Malformed JSON
# ---------------------------------------------------------------------------

def test_fetch_returns_empty_on_bad_json() -> None:
    """Malformed JSON from company-news → []."""
    source = make_source(http_get_fn=http_get_bad_json)
    tips = source.fetch("AAPL", AS_OF)
    assert tips == []


def test_fetch_returns_empty_on_empty_array() -> None:
    """Empty array from API → []."""
    source = make_source(http_get_fn=http_get_empty_array)
    tips = source.fetch("AAPL", AS_OF)
    assert tips == []


def test_fetch_handles_network_error() -> None:
    """Network exception → [] (fail-closed)."""
    def _explode(url: str, params: dict) -> str:
        raise ConnectionError("Down")

    source = make_source(http_get_fn=_explode)
    tips = source.fetch("AAPL", AS_OF)
    assert tips == []


# ---------------------------------------------------------------------------
# TipSource ABC compliance
# ---------------------------------------------------------------------------

def test_source_id_property() -> None:
    """FinnhubNewsSource.source_id returns 'finnhub'."""
    source = _make_client_and_source()
    assert source.source_id == "finnhub"


def test_tip_url_non_empty() -> None:
    """Every emitted tip must have a non-empty url."""
    article = make_article(
        url="https://reuters.com/article/1",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([article])
    source = _make_client_and_source(news)
    tips = source.fetch("AAPL", AS_OF)

    for tip in tips:
        assert tip.url, "tip.url must be non-empty"


def test_tip_ts_is_tz_aware() -> None:
    """Every emitted tip must have a tz-aware ts."""
    article = make_article(
        url="https://reuters.com/article/1",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([article])
    source = _make_client_and_source(news)
    tips = source.fetch("AAPL", AS_OF)

    for tip in tips:
        assert tip.ts.tzinfo is not None, "tip.ts must be tz-aware"


def test_dedup_by_url_within_fetch() -> None:
    """Two articles with identical URLs are deduped to one tip."""
    article = make_article(
        url="https://reuters.com/article/1",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    # Same URL, different headline
    article_dup = make_article(
        headline="Slightly different headline",
        url="https://reuters.com/article/1",  # same URL
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([article, article_dup])
    source = _make_client_and_source(news)
    tips = source.fetch("AAPL", AS_OF)

    assert len(tips) == 1, "Duplicate URLs must be deduped"


def test_get_cached_sentiment_returns_none_before_fetch() -> None:
    """get_cached_sentiment returns None before fetch is called."""
    source = _make_client_and_source()
    assert source.get_cached_sentiment("AAPL") is None


def test_get_cached_sentiment_returns_dict_after_fetch() -> None:
    """get_cached_sentiment returns the sentiment dict after fetch."""
    article = make_article(
        url="https://reuters.com/article/1",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([article])
    sent = make_sentiment_response(bullish=0.7, bearish=0.2)
    source = make_source(news_json=news, sentiment_json=sent)

    source.fetch("AAPL", AS_OF)
    cached = source.get_cached_sentiment("AAPL")

    assert cached is not None
    assert "bullish_percent" in cached
    assert "sentiment_score" in cached
