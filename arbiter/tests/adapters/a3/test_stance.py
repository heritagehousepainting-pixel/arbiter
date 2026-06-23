"""Tests for the A3 stance extraction module.

Tests are fully offline — no network, no external dependencies.
"""
from __future__ import annotations

import pytest

from arbiter.adapters.a3.stance import (
    StanceResult,
    extract_stance,
    score_from_finnhub_sentiment,
    score_from_lexicon,
    _NEUTRAL_DEAD_ZONE,
)


# ---------------------------------------------------------------------------
# Finnhub sentiment scorer
# ---------------------------------------------------------------------------

def test_bullish_sentiment_positive_score() -> None:
    sent = {"bullish_percent": 0.8, "bearish_percent": 0.1, "sentiment_score": 0.7}
    result = score_from_finnhub_sentiment(sent)
    assert result.stance_score > 0.0
    assert result.method == "finnhub_sentiment"


def test_bearish_sentiment_negative_score() -> None:
    sent = {"bullish_percent": 0.1, "bearish_percent": 0.8, "sentiment_score": -0.7}
    result = score_from_finnhub_sentiment(sent)
    assert result.stance_score < 0.0
    assert result.method == "finnhub_sentiment"


def test_neutral_sentiment_zero_score() -> None:
    sent = {"bullish_percent": 0.5, "bearish_percent": 0.5, "sentiment_score": 0.0}
    result = score_from_finnhub_sentiment(sent)
    assert result.stance_score == 0.0


def test_dead_zone_applied() -> None:
    """A sentiment_score just below the dead-zone threshold → 0.0."""
    tiny = _NEUTRAL_DEAD_ZONE * 0.5
    sent = {"bullish_percent": 0.52, "bearish_percent": 0.5, "sentiment_score": tiny}
    result = score_from_finnhub_sentiment(sent)
    assert result.stance_score == 0.0
    assert result.raw_polarity == pytest.approx(tiny, abs=1e-9)


def test_dead_zone_boundary_exactly() -> None:
    """A score exactly AT the dead-zone threshold passes through (strict <)."""
    sent = {"bullish_percent": 0.525, "bearish_percent": 0.5, "sentiment_score": _NEUTRAL_DEAD_ZONE}
    result = score_from_finnhub_sentiment(sent)
    # dead-zone uses strict |score| < 0.05, so exactly 0.05 is NOT zeroed out.
    assert result.stance_score == pytest.approx(_NEUTRAL_DEAD_ZONE, abs=1e-9)


def test_sentiment_clamped_to_minus_one_one() -> None:
    """Out-of-range raw scores are clamped defensively."""
    sent = {"bullish_percent": 1.0, "bearish_percent": 0.0, "sentiment_score": 2.0}
    result = score_from_finnhub_sentiment(sent)
    assert result.stance_score <= 1.0


def test_missing_sentiment_score_key_returns_zero() -> None:
    """Missing sentiment_score key → gracefully neutral."""
    sent = {"bullish_percent": 0.6}  # no sentiment_score
    result = score_from_finnhub_sentiment(sent)
    assert result.stance_score == 0.0


# ---------------------------------------------------------------------------
# Keyword lexicon scorer
# ---------------------------------------------------------------------------

def test_bullish_keywords() -> None:
    result = score_from_lexicon("Apple stock surge rally beats earnings")
    assert result.stance_score > 0.0
    assert result.method == "keyword_lexicon"


def test_bearish_keywords() -> None:
    result = score_from_lexicon("Apple misses earnings crash plunge losses")
    assert result.stance_score < 0.0
    assert result.method == "keyword_lexicon"


def test_empty_text_neutral() -> None:
    result = score_from_lexicon("")
    assert result.stance_score == 0.0


def test_no_matching_words_neutral() -> None:
    result = score_from_lexicon("the cat sat on the mat and ate a sandwich")
    assert result.stance_score == 0.0


def test_lexicon_result_clamped() -> None:
    """Lexicon score must always be in [-1, 1]."""
    result = score_from_lexicon(
        "surge soar rally gain gains beat bullish breakthrough record positive"
    )
    assert -1.0 <= result.stance_score <= 1.0


# ---------------------------------------------------------------------------
# extract_stance: primary / fallback routing
# ---------------------------------------------------------------------------

def test_extract_stance_uses_finnhub_when_available() -> None:
    sent = {"bullish_percent": 0.8, "bearish_percent": 0.1, "sentiment_score": 0.7}
    result = extract_stance(text="random unrelated text", sentiment=sent)
    assert result.method == "finnhub_sentiment"
    assert result.stance_score > 0.0


def test_extract_stance_falls_back_to_lexicon_when_neutral() -> None:
    """Neutral Finnhub sentiment (0.5/0.5) → lexicon used for bullish headline."""
    sent = {"bullish_percent": 0.5, "bearish_percent": 0.5, "sentiment_score": 0.0}
    result = extract_stance(
        text="Apple stock surges to record highs on earnings beat",
        sentiment=sent,
    )
    assert result.method == "keyword_lexicon"
    assert result.stance_score > 0.0


def test_extract_stance_falls_back_when_no_sentiment() -> None:
    """No sentiment provided → lexicon used."""
    result = extract_stance(
        text="Apple plunges on massive earnings miss",
        sentiment=None,
    )
    assert result.method == "keyword_lexicon"
    assert result.stance_score < 0.0


def test_extract_stance_neutral_both() -> None:
    """Neutral Finnhub + neutral text → 0.0."""
    sent = {"bullish_percent": 0.5, "bearish_percent": 0.5, "sentiment_score": 0.0}
    result = extract_stance(text="the company held its annual meeting", sentiment=sent)
    assert result.stance_score == 0.0
