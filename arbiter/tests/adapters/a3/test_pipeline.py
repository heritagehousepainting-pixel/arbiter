"""Tests for gather_a3_opinions — the frozen public entry point.

All tests are OFFLINE (no real HTTP).  Tests cover:
1. Inert without API key → []
2. BacktestClock → []
3. Diversity gate: 1 publisher → abstain; 2 publishers → opinion emitted
4. No-lookahead: article ts > as_of is filtered
5. Fail-closed on malformed JSON
6. Emitted Opinion passes validate_opinion with horizon_days=7
7. Stance sign (bullish sentiment → positive stance_score)
8. Neutral sentiment falls back to lexicon
9. Confidence range validation
10. Empty watchlist → []
11. Strength gate — stance threshold (below/at/above; bearish; configurable; getattr fallback; multi-ticker)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from arbiter.adapters.a3 import gather_a3_opinions, ADVISOR_ID
from arbiter.adapters.a3.source_finnhub import FinnhubNewsSource
from arbiter.contract.opinion import validate_opinion
from arbiter.data.clock import BacktestClock, Clock
from arbiter.ingest.finnhub.client import FinnhubClient
from arbiter.types import HorizonBucket

from .conftest import (
    AS_OF,
    ARTICLE_TS_FUTURE,
    ARTICLE_TS_OLD,
    ARTICLE_TS_RECENT,
    FakeConfigNoKey,
    FakeConfigWithKey,
    http_get_bad_json,
    make_article,
    make_news_response,
    make_sentiment_response,
    make_source,
    http_get_with_responses,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _live_clock(as_of: datetime = AS_OF) -> Clock:
    """Thin Clock whose now() returns a fixed datetime without calling datetime.now()."""
    class _FixedClock(Clock):
        def now(self) -> datetime:
            return as_of
    return _FixedClock()


def _memory_conn() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


# ---------------------------------------------------------------------------
# 1. Inert without API key
# ---------------------------------------------------------------------------

def test_inert_without_api_key() -> None:
    """gather_a3_opinions returns [] when finnhub_api_key is empty."""
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        FakeConfigNoKey(),
        ["AAPL"],
    )
    assert result == []


def test_inert_without_api_key_does_not_raise() -> None:
    """No exception, even with broken watchlist entries."""
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        FakeConfigNoKey(),
        ["AAPL", "TSLA", ""],
    )
    assert result == []


# ---------------------------------------------------------------------------
# 2. BacktestClock → []
# ---------------------------------------------------------------------------

def test_backtest_clock_returns_empty() -> None:
    """gather_a3_opinions returns [] under BacktestClock (no network, no look-ahead)."""
    bt_clock = BacktestClock(AS_OF)
    result = gather_a3_opinions(
        _memory_conn(),
        bt_clock,
        FakeConfigWithKey(),
        ["AAPL"],
    )
    assert result == []


# ---------------------------------------------------------------------------
# 3a. Diversity gate: 1 publisher → abstain
# ---------------------------------------------------------------------------

def test_single_publisher_abstains() -> None:
    """When all articles come from one publisher domain, diversity gate fails."""
    # Two articles, both from reuters.com → same source_id → 1 voice → abstain
    article1 = make_article(
        url="https://reuters.com/article/1",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    article2 = make_article(
        headline="Another headline",
        url="https://reuters.com/article/2",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([article1, article2])
    sent = make_sentiment_response(bullish=0.9, bearish=0.1)

    source = make_source(news_json=news, sentiment_json=sent)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        FakeConfigWithKey(),
        ["AAPL"],
        _source_override=source,
    )
    assert result == []


# ---------------------------------------------------------------------------
# 3b. Diversity gate: 2 publishers → opinion emitted
# ---------------------------------------------------------------------------

def test_two_publishers_emits_opinion() -> None:
    """When articles come from 2 distinct publisher domains, an opinion is emitted."""
    article1 = make_article(
        headline="Strong earnings beat at Apple",
        url="https://reuters.com/article/1",
        source="Reuters",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    article2 = make_article(
        headline="Apple beats expectations significantly",
        url="https://bloomberg.com/article/2",
        source="Bloomberg",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([article1, article2])
    # Bullish sentiment → positive stance_score
    sent = make_sentiment_response(bullish=0.8, bearish=0.1)

    source = make_source(news_json=news, sentiment_json=sent)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        FakeConfigWithKey(),
        ["AAPL"],
        _source_override=source,
    )
    assert len(result) == 1
    op = result[0]
    assert op.advisor_id == ADVISOR_ID
    assert op.ticker == "AAPL"
    assert op.horizon_days == 7
    assert op.horizon_bucket == HorizonBucket.SHORT
    validate_opinion(op)


# ---------------------------------------------------------------------------
# 4. No-lookahead: future-timestamped article is filtered
# ---------------------------------------------------------------------------

def test_no_lookahead_future_article_filtered() -> None:
    """Articles with published_at > as_of are filtered; cannot cause opinion."""
    # Only article has a future timestamp → should be dropped → abstain.
    future_article = make_article(
        url="https://reuters.com/article/future",
        published_epoch=int(ARTICLE_TS_FUTURE.timestamp()),
    )
    # Add a second one from bloomberg, also in the future
    future_article2 = make_article(
        url="https://bloomberg.com/article/future",
        source="Bloomberg",
        published_epoch=int(ARTICLE_TS_FUTURE.timestamp()),
    )
    news = make_news_response([future_article, future_article2])
    sent = make_sentiment_response(bullish=0.9, bearish=0.0)

    source = make_source(news_json=news, sentiment_json=sent)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(AS_OF),
        FakeConfigWithKey(),
        ["AAPL"],
        _source_override=source,
    )
    assert result == [], "Future-dated articles must not leak into opinions"


def test_no_lookahead_mixed_keeps_only_past() -> None:
    """When one article is past and one is future, only the past one is kept.

    If only past articles come from one publisher domain, diversity gate fails
    (1 source), so abstain.  This verifies the future article wasn't counted.
    """
    past_article = make_article(
        url="https://reuters.com/article/past",
        source="Reuters",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    future_article = make_article(
        headline="Future news",
        url="https://bloomberg.com/article/future",
        source="Bloomberg",
        published_epoch=int(ARTICLE_TS_FUTURE.timestamp()),
    )
    news = make_news_response([past_article, future_article])
    sent = make_sentiment_response(bullish=0.9, bearish=0.0)

    source = make_source(news_json=news, sentiment_json=sent)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(AS_OF),
        FakeConfigWithKey(),
        ["AAPL"],
        _source_override=source,
    )
    # Only reuters article passes → 1 source → gate fails → abstain
    assert result == [], "Future article must not count toward diversity"


# ---------------------------------------------------------------------------
# 5. Fail-closed on malformed JSON
# ---------------------------------------------------------------------------

def test_fail_closed_on_malformed_news_json() -> None:
    """Malformed JSON from company-news returns [] — no exception."""
    source = make_source(http_get_fn=http_get_bad_json)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        FakeConfigWithKey(),
        ["AAPL"],
        _source_override=source,
    )
    assert result == []


def test_fail_closed_on_network_error() -> None:
    """Network exception returns [] — no exception propagates."""
    def _exploding_get(url: str, params: dict) -> str:
        raise ConnectionError("Simulated network failure")

    source = make_source(http_get_fn=_exploding_get)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        FakeConfigWithKey(),
        ["AAPL"],
        _source_override=source,
    )
    assert result == []


# ---------------------------------------------------------------------------
# 6. Emitted Opinion passes validate_opinion with horizon_days=7
# ---------------------------------------------------------------------------

def test_emitted_opinion_validates() -> None:
    """The emitted Opinion must pass validate_opinion (full contract check)."""
    article1 = make_article(
        headline="Big gains expected for Apple",
        url="https://reuters.com/article/1",
        source="Reuters",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    article2 = make_article(
        headline="Apple stock surges on strong results",
        url="https://bloomberg.com/article/2",
        source="Bloomberg",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([article1, article2])
    sent = make_sentiment_response(bullish=0.8, bearish=0.1)

    source = make_source(news_json=news, sentiment_json=sent)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        FakeConfigWithKey(),
        ["AAPL"],
        _source_override=source,
    )
    assert len(result) == 1
    op = result[0]
    # Must not raise
    validate_opinion(op)
    assert op.horizon_days == 7
    assert op.horizon_bucket == HorizonBucket.SHORT
    assert op.confidence_source.value == "modeled"
    assert op.advisor_id == "A3.news"
    assert op.source_fingerprint  # non-empty
    assert op.run_group_id        # non-empty
    assert op.as_of.tzinfo is not None  # tz-aware


# ---------------------------------------------------------------------------
# 7. Stance sign (bullish sentiment → positive stance_score)
# ---------------------------------------------------------------------------

def test_bullish_sentiment_produces_positive_stance() -> None:
    """Strongly bullish Finnhub sentiment → positive stance_score on the Opinion."""
    article1 = make_article(
        headline="Apple surges on record revenue",
        url="https://reuters.com/article/1",
        source="Reuters",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    article2 = make_article(
        headline="Apple beats earnings expectations massively",
        url="https://bloomberg.com/article/2",
        source="Bloomberg",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([article1, article2])
    # Strong bullish signal: 0.8 − 0.1 = 0.7 sentiment_score
    sent = make_sentiment_response(bullish=0.8, bearish=0.1)

    source = make_source(news_json=news, sentiment_json=sent)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        FakeConfigWithKey(),
        ["AAPL"],
        _source_override=source,
    )
    assert len(result) == 1
    assert result[0].stance_score > 0.0, "Bullish sentiment should yield positive stance"


def test_bearish_sentiment_produces_negative_stance() -> None:
    """Strongly bearish Finnhub sentiment → negative stance_score on the Opinion."""
    article1 = make_article(
        headline="Apple misses earnings, stock falls",
        url="https://reuters.com/article/3",
        source="Reuters",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    article2 = make_article(
        headline="Apple disappoints investors with weak guidance",
        url="https://bloomberg.com/article/4",
        source="Bloomberg",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([article1, article2])
    # Strong bearish signal: 0.1 − 0.8 = -0.7 sentiment_score
    sent = make_sentiment_response(bullish=0.1, bearish=0.8)

    source = make_source(news_json=news, sentiment_json=sent)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        FakeConfigWithKey(),
        ["AAPL"],
        _source_override=source,
    )
    assert len(result) == 1
    assert result[0].stance_score < 0.0, "Bearish sentiment should yield negative stance"


# ---------------------------------------------------------------------------
# 8. Neutral sentiment falls back to lexicon
# ---------------------------------------------------------------------------

def test_neutral_finnhub_falls_back_to_lexicon() -> None:
    """When Finnhub sentiment is neutral (0.5/0.5), lexicon is the tiebreaker."""
    # Strong bullish headlines should activate the lexicon fallback.
    article1 = make_article(
        headline="Apple stock surges to record high on earnings beat",
        url="https://reuters.com/article/5",
        source="Reuters",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    article2 = make_article(
        headline="Apple reports strong quarterly gains and growth",
        url="https://bloomberg.com/article/6",
        source="Bloomberg",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([article1, article2])
    # Exactly neutral — no Finnhub signal
    sent = make_sentiment_response(bullish=0.5, bearish=0.5)

    source = make_source(news_json=news, sentiment_json=sent)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        FakeConfigWithKey(),
        ["AAPL"],
        _source_override=source,
    )
    # Should emit a bullish opinion from the lexicon fallback
    assert len(result) == 1
    assert result[0].stance_score > 0.0, "Lexicon should detect bullish headlines"


# ---------------------------------------------------------------------------
# 9. Confidence range validation
# ---------------------------------------------------------------------------

def test_confidence_in_valid_range() -> None:
    """Confidence must be in [0.05, 0.85]."""
    article1 = make_article(
        url="https://reuters.com/article/1",
        source="Reuters",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    article2 = make_article(
        headline="Test article from Bloomberg",
        url="https://bloomberg.com/article/2",
        source="Bloomberg",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([article1, article2])
    sent = make_sentiment_response(bullish=0.8, bearish=0.1)

    source = make_source(news_json=news, sentiment_json=sent)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        FakeConfigWithKey(),
        ["AAPL"],
        _source_override=source,
    )
    assert len(result) == 1
    op = result[0]
    assert 0.05 <= op.confidence <= 0.85


# ---------------------------------------------------------------------------
# 10. Empty watchlist → []
# ---------------------------------------------------------------------------

def test_empty_watchlist_returns_empty() -> None:
    """Empty watchlist → [] with no errors."""
    source = make_source()
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        FakeConfigWithKey(),
        [],
        _source_override=source,
    )
    assert result == []


# ---------------------------------------------------------------------------
# 11. Strength gate — stance threshold
# ---------------------------------------------------------------------------

class _FakeConfigLowThreshold(FakeConfigWithKey):
    """Config stub with a very low stance threshold (0.01) to let mild signals through."""
    a3_min_stance: float = 0.01
    a3_min_confidence: float = 0.0


class _FakeConfigHighThreshold(FakeConfigWithKey):
    """Config stub with a very high stance threshold (0.99) to block almost everything."""
    a3_min_stance: float = 0.99
    a3_min_confidence: float = 0.0


class _FakeConfigExactThreshold(FakeConfigWithKey):
    """Config stub with threshold equal to the expected stance score (0.25)."""
    a3_min_stance: float = 0.25
    a3_min_confidence: float = 0.0


def _make_corroborated_source(bullish: float, bearish: float) -> "FinnhubNewsSource":
    """Build a 2-publisher source with the given sentiment."""
    article1 = make_article(
        headline="Test headline reuters",
        url="https://reuters.com/article/1",
        source="Reuters",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    article2 = make_article(
        headline="Test headline bloomberg",
        url="https://bloomberg.com/article/2",
        source="Bloomberg",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([article1, article2])
    sent = make_sentiment_response(bullish=bullish, bearish=bearish)
    return make_source(news_json=news, sentiment_json=sent)


def test_stance_below_threshold_abstains() -> None:
    """When stance_score is below a3_min_stance, the ticker is NOT emitted.

    Finnhub sentiment 0.6 bullish / 0.5 bearish → score = 0.1
    which is below the default gate of 0.25 → abstain.
    """
    # score = 0.6 - 0.5 = 0.1  (well below 0.25 default)
    source = _make_corroborated_source(bullish=0.6, bearish=0.5)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        FakeConfigWithKey(),   # default 0.25 threshold
        ["AAPL"],
        _source_override=source,
    )
    assert result == [], (
        "Stance score 0.1 is below the 0.25 gate — must not emit an Opinion"
    )


def test_stance_at_threshold_emits() -> None:
    """When abs(stance_score) is exactly at a3_min_stance, an Opinion IS emitted.

    We set threshold=0.25 and use bullish=0.625, bearish=0.375 → score=0.25.
    """
    # score = 0.625 - 0.375 = 0.25 — exactly at threshold (inclusive)
    article1 = make_article(
        headline="Test headline reuters",
        url="https://reuters.com/article/1",
        source="Reuters",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    article2 = make_article(
        headline="Test headline bloomberg",
        url="https://bloomberg.com/article/2",
        source="Bloomberg",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news = make_news_response([article1, article2])
    sent = make_sentiment_response(bullish=0.625, bearish=0.375)
    source = make_source(news_json=news, sentiment_json=sent)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        _FakeConfigExactThreshold(),   # threshold == 0.25
        ["AAPL"],
        _source_override=source,
    )
    assert len(result) == 1, "abs(stance_score)=0.25 equals threshold — must emit"
    assert abs(result[0].stance_score) >= 0.25


def test_stance_above_threshold_emits() -> None:
    """When abs(stance_score) exceeds a3_min_stance, an Opinion IS emitted.

    sentiment 0.8 bullish / 0.1 bearish → score=0.7 > 0.25 → emit.
    """
    source = _make_corroborated_source(bullish=0.8, bearish=0.1)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        FakeConfigWithKey(),
        ["AAPL"],
        _source_override=source,
    )
    assert len(result) == 1, "Strong sentiment (score=0.7) must pass the gate"
    assert abs(result[0].stance_score) >= 0.25


def test_bearish_below_threshold_abstains() -> None:
    """Weak bearish signals are also suppressed (absolute value gate)."""
    # score = 0.4 - 0.55 = -0.15  (abs=0.15 < 0.25)
    source = _make_corroborated_source(bullish=0.4, bearish=0.55)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        FakeConfigWithKey(),
        ["AAPL"],
        _source_override=source,
    )
    assert result == [], "Weak bearish score -0.15 is below the |0.25| gate"


def test_bearish_above_threshold_emits() -> None:
    """Strong bearish signals pass the gate (absolute value check)."""
    # score = 0.1 - 0.8 = -0.7  (abs=0.7 > 0.25)
    source = _make_corroborated_source(bullish=0.1, bearish=0.8)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        FakeConfigWithKey(),
        ["AAPL"],
        _source_override=source,
    )
    assert len(result) == 1, "Strong bearish score -0.7 must pass the gate"
    assert result[0].stance_score < -0.25


def test_threshold_configurable_low_emits_mild_signal() -> None:
    """A low a3_min_stance (0.01) lets a mild signal through."""
    # score = 0.6 - 0.5 = 0.1 — would be blocked at default 0.25
    source = _make_corroborated_source(bullish=0.6, bearish=0.5)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        _FakeConfigLowThreshold(),   # threshold=0.01
        ["AAPL"],
        _source_override=source,
    )
    assert len(result) == 1, "With threshold=0.01, score=0.1 must be emitted"


def test_threshold_configurable_high_blocks_strong_signal() -> None:
    """An extreme a3_min_stance (0.99) blocks even a strong signal."""
    # score = 0.8 - 0.1 = 0.7 — strong but below 0.99
    source = _make_corroborated_source(bullish=0.8, bearish=0.1)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        _FakeConfigHighThreshold(),  # threshold=0.99
        ["AAPL"],
        _source_override=source,
    )
    assert result == [], "With threshold=0.99, score=0.7 must be suppressed"


def test_gate_uses_getattr_fallback_for_missing_config_field() -> None:
    """Pipeline reads thresholds via getattr with a fallback — works with a bare stub."""

    class _BareConfig:
        """Oldest-style stub: only api key, no threshold fields."""
        finnhub_api_key = "test_api_key_12345"
        # No a3_min_stance / a3_min_confidence attributes.

    # score = 0.8 - 0.1 = 0.7 → above _DEFAULT_MIN_STANCE (0.25)
    source = _make_corroborated_source(bullish=0.8, bearish=0.1)
    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        _BareConfig(),
        ["AAPL"],
        _source_override=source,
    )
    assert len(result) == 1, (
        "With bare config (no threshold attrs), strong signal must still emit"
    )


def test_multi_ticker_gate_filters_selectively() -> None:
    """Gate filters weak tickers while passing strong ones in the same cycle."""
    # AAPL: score=0.1 (weak, blocked)
    article_aapl_1 = make_article(
        headline="Apple news",
        url="https://reuters.com/aapl/1",
        source="Reuters",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    article_aapl_2 = make_article(
        headline="Apple more news",
        url="https://bloomberg.com/aapl/2",
        source="Bloomberg",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news_aapl = make_news_response([article_aapl_1, article_aapl_2])
    sent_aapl = make_sentiment_response(bullish=0.55, bearish=0.45)  # score=0.1

    # TSLA: score=0.7 (strong, passes)
    article_tsla_1 = make_article(
        headline="Tesla surges",
        url="https://reuters.com/tsla/1",
        source="Reuters",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    article_tsla_2 = make_article(
        headline="Tesla beats estimates",
        url="https://bloomberg.com/tsla/2",
        source="Bloomberg",
        published_epoch=int(ARTICLE_TS_RECENT.timestamp()),
    )
    news_tsla = make_news_response([article_tsla_1, article_tsla_2])
    sent_tsla = make_sentiment_response(bullish=0.85, bearish=0.15)  # score=0.7

    # Route each ticker to its own mock.
    def _http_get(url: str, params: dict) -> str:
        sym = params.get("symbol", "")
        if sym == "AAPL":
            if "company-news" in url:
                return news_aapl
            return sent_aapl
        if sym == "TSLA":
            if "company-news" in url:
                return news_tsla
            return sent_tsla
        return "[]"

    client = FinnhubClient(api_key="test_key", http_get=_http_get, sleep_fn=lambda _: None)
    source = FinnhubNewsSource(client)

    result = gather_a3_opinions(
        _memory_conn(),
        _live_clock(),
        FakeConfigWithKey(),
        ["AAPL", "TSLA"],
        _source_override=source,
    )
    tickers = [op.ticker for op in result]
    assert "TSLA" in tickers, "TSLA (score=0.7) should pass the gate"
    assert "AAPL" not in tickers, "AAPL (score=0.1) should be blocked by the gate"
