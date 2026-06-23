"""Stance extraction for A3 news items.

Strategy (two-layer, no Claude, no external sentiment API):
------------------------------------------------------------
1. PRIMARY — Finnhub symbol-level sentiment:
   ``bullish_percent - bearish_percent`` → stance_score ∈ [-1, 1].
   Dead-zone: |score| < 0.05 → 0.0 (noise suppression).

2. FALLBACK — built-in keyword-polarity lexicon:
   Applied when the Finnhub sentiment is unavailable or neutral (score == 0.0).
   Scores the article headline+summary using a small, frozen, domain-tuned
   financial keyword list.  Completely offline and deterministic — same input
   always produces the same output, required for backtest reproducibility.

No ``datetime.now()``.  No network calls.  No LLM calls.
"""
from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StanceResult:
    """Outcome of stance extraction for one news item.

    Fields
    ------
    stance_score:
        Directional signal in [-1.0, 1.0].  Positive = bullish/long,
        negative = bearish/short.  0.0 = neutral / abstain.
    raw_polarity:
        Raw scorer output before the dead-zone clamp (for audit).
    method:
        Which scorer produced the result: ``"finnhub_sentiment"``
        or ``"keyword_lexicon"``.
    """
    stance_score: float
    raw_polarity: float
    method: str


# ---------------------------------------------------------------------------
# Dead-zone constant
# ---------------------------------------------------------------------------

_NEUTRAL_DEAD_ZONE = 0.05  # |score| below this → 0.0 (noise)


# ---------------------------------------------------------------------------
# Finnhub sentiment scorer (primary)
# ---------------------------------------------------------------------------

def score_from_finnhub_sentiment(sentiment: dict) -> StanceResult:
    """Derive a stance_score from Finnhub symbol-level sentiment dict.

    Parameters
    ----------
    sentiment:
        Dict with keys ``bullish_percent``, ``bearish_percent``,
        ``sentiment_score`` (as returned by ``FinnhubClient.get_news_sentiment``).

    Returns
    -------
    StanceResult
        method = ``"finnhub_sentiment"``.
    """
    raw = float(sentiment.get("sentiment_score") or 0.0)
    # Clamp to [-1, 1] (defensive — should already be in range).
    raw = max(-1.0, min(1.0, raw))
    score = 0.0 if abs(raw) < _NEUTRAL_DEAD_ZONE else raw
    return StanceResult(stance_score=score, raw_polarity=raw, method="finnhub_sentiment")


# ---------------------------------------------------------------------------
# Built-in keyword-lexicon scorer (fallback)
# ---------------------------------------------------------------------------
# A small domain-tuned financial keyword list.  Scores are unit-less;
# the final stance_score is the mean of all matched word scores, clamped.
#
# Positive (bullish) words → positive weights.
# Negative (bearish) words → negative weights.
# Weights are in [-1, 1].  Unknown words → 0.
#
# This list is intentionally conservative and frozen — not learned from data,
# not pulled from a network.  Extend in future waves with empirical calibration.

_LEXICON: dict[str, float] = {
    # Strong bullish
    "beat": 0.7,
    "beats": 0.7,
    "surge": 0.8,
    "surges": 0.8,
    "soar": 0.8,
    "soars": 0.8,
    "rally": 0.6,
    "rallies": 0.6,
    "upgrade": 0.7,
    "upgrades": 0.7,
    "outperform": 0.6,
    "buy": 0.5,
    "bullish": 0.9,
    "record": 0.5,
    "growth": 0.5,
    "profit": 0.5,
    "gain": 0.5,
    "gains": 0.5,
    "strong": 0.4,
    "strength": 0.4,
    "exceed": 0.6,
    "exceeds": 0.6,
    "exceeded": 0.6,
    "acquisition": 0.4,
    "acquires": 0.4,
    "merger": 0.3,
    "partnership": 0.3,
    "contract": 0.3,
    "dividend": 0.4,
    "increase": 0.4,
    "increases": 0.4,
    "raised": 0.5,
    "raise": 0.5,
    "positive": 0.4,
    "optimistic": 0.5,
    "breakthrough": 0.6,
    "approval": 0.6,
    "approved": 0.6,
    "win": 0.5,
    "wins": 0.5,
    "expand": 0.4,
    "expansion": 0.4,
    # Moderate bullish
    "above": 0.2,
    "ahead": 0.2,
    "better": 0.3,
    "improved": 0.3,
    "improving": 0.3,
    # Strong bearish
    "miss": -0.7,
    "misses": -0.7,
    "missed": -0.7,
    "drop": -0.6,
    "drops": -0.6,
    "plunge": -0.8,
    "plunges": -0.8,
    "crash": -0.8,
    "crashes": -0.8,
    "downgrade": -0.7,
    "downgrades": -0.7,
    "sell": -0.5,
    "bearish": -0.9,
    "loss": -0.6,
    "losses": -0.6,
    "decline": -0.5,
    "declines": -0.5,
    "weak": -0.4,
    "weakness": -0.4,
    "disappoint": -0.6,
    "disappoints": -0.6,
    "disappointed": -0.6,
    "disappointing": -0.6,
    "cut": -0.5,
    "cuts": -0.5,
    "lowered": -0.5,
    "lower": -0.3,
    "warning": -0.5,
    "warns": -0.5,
    "risk": -0.3,
    "concern": -0.3,
    "concerns": -0.3,
    "investigation": -0.5,
    "lawsuit": -0.5,
    "fraud": -0.8,
    "default": -0.7,
    "bankrupt": -0.9,
    "bankruptcy": -0.9,
    "layoff": -0.5,
    "layoffs": -0.5,
    "restructuring": -0.3,
    "negative": -0.4,
    "pessimistic": -0.5,
    "recall": -0.5,
    "fine": -0.4,
    "penalty": -0.5,
    "violation": -0.5,
    # Moderate bearish
    "below": -0.2,
    "behind": -0.2,
    "worse": -0.3,
    "worsening": -0.3,
}

import re as _re

# Token extractor: lowercase words only (ignores punctuation/numbers).
_WORD_RE = _re.compile(r"[a-z]+")


def score_from_lexicon(text: str) -> StanceResult:
    """Score *text* using the built-in keyword lexicon.

    Parameters
    ----------
    text:
        Headline and/or summary concatenated.  May be empty.

    Returns
    -------
    StanceResult
        method = ``"keyword_lexicon"``.
        ``stance_score = 0.0`` when no lexicon words match (neutral abstain).
    """
    words = _WORD_RE.findall(text.lower())
    scores = [_LEXICON[w] for w in words if w in _LEXICON]

    if not scores:
        return StanceResult(stance_score=0.0, raw_polarity=0.0, method="keyword_lexicon")

    raw = sum(scores) / len(scores)  # mean of matched word scores
    raw = max(-1.0, min(1.0, raw))   # defensive clamp
    score = 0.0 if abs(raw) < _NEUTRAL_DEAD_ZONE else raw
    return StanceResult(stance_score=score, raw_polarity=raw, method="keyword_lexicon")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_stance(
    *,
    text: str,
    sentiment: dict | None = None,
) -> StanceResult:
    """Extract a stance_score for a news item.

    Tries Finnhub sentiment first (primary); falls back to the built-in
    keyword lexicon when sentiment is absent or yields a neutral score.

    Parameters
    ----------
    text:
        Concatenated headline + summary for lexicon fallback.
    sentiment:
        Finnhub sentiment dict (keys: ``bullish_percent``, ``bearish_percent``,
        ``sentiment_score``).  ``None`` → skip primary, go straight to fallback.

    Returns
    -------
    StanceResult
        The best stance result available.  ``stance_score = 0.0`` means neutral.
    """
    if sentiment:
        primary = score_from_finnhub_sentiment(sentiment)
        if primary.stance_score != 0.0:
            return primary

    # Fall through to lexicon.
    return score_from_lexicon(text)
