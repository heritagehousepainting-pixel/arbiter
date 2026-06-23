"""A3 news advisor pipeline — gather_a3_opinions().

Entry point for the engine (and tests).  Corroborates Finnhub news tips via
the diversity gate and emits validated Opinion objects.

Key contracts
-------------
- INERT: returns [] when ``config.finnhub_api_key`` is empty.
- NETWORK-GATED: returns [] under ``BacktestClock`` (no network, no look-ahead).
- FAIL-CLOSED: any unhandled exception → [] (never raises).
- as_of = clock.now() — the decision timestamp.  Article publish time gates
  INCLUSION and drives recency confidence, but Opinion.as_of is always the
  engine's decision timestamp, matching A1/A2.
- horizon_days = 7 (SHORT bucket, constant).
- confidence = clamp(0.4*source_tier + 0.4*corroboration + 0.2*recency, 0.05, 0.85).

No ``datetime.now()`` calls — ``clock.now()`` is the only allowed clock read,
and it flows in from the caller.  Passes ``check_no_lookahead.sh``.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime

import structlog

from arbiter.contract.opinion import Opinion, default_registry, validate_opinion
from arbiter.data.clock import BacktestClock, Clock
from arbiter.db.helpers import generate_ulid
from arbiter.ingest.finnhub.client import FinnhubClient
from arbiter.tips.diversity import DiversityGate
from arbiter.tips.source import UnverifiedTip
from arbiter.types import ConfidenceSource

from .source_finnhub import FinnhubNewsSource
from .stance import extract_stance

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADVISOR_ID = "A3.news"
_HORIZON_DAYS = 7           # SHORT bucket (1–30 days)
_CONF_MIN = 0.05
_CONF_MAX = 0.85

# Fallback strength-gate thresholds used when the config object does not carry
# the a3_min_stance / a3_min_confidence attributes (e.g. legacy test stubs that
# pre-date the fields).  These mirror the Config dataclass defaults so
# behaviour is identical whether the field comes from config or the fallback.
_DEFAULT_MIN_STANCE = 0.25
_DEFAULT_MIN_CONFIDENCE = 0.0

# Source-tier score table.  Unknown domains → "low" (_TIER_DEFAULT).
_SOURCE_TIER: dict[str, float] = {
    # High-tier editorial publishers
    "reuters.com": 0.80,
    "bloomberg.com": 0.80,
    "wsj.com": 0.80,
    "ft.com": 0.80,
    "nytimes.com": 0.70,
    "cnbc.com": 0.70,
    "apnews.com": 0.75,
    "bbc.com": 0.70,
    "barrons.com": 0.75,
    # Medium-tier
    "seekingalpha.com": 0.50,
    "motleyfool.com": 0.50,
    "investopedia.com": 0.45,
    "marketwatch.com": 0.55,
    "thestreet.com": 0.45,
    "benzinga.com": 0.45,
    "zacks.com": 0.45,
    "fool.com": 0.50,
    "yahoofinance.com": 0.45,
    "finance.yahoo.com": 0.45,
}
_TIER_DEFAULT = 0.25  # low / unknown


def _source_tier_score(source_ids: frozenset[str]) -> float:
    """Return the max tier score across contributing source_ids."""
    best = _TIER_DEFAULT
    for sid in source_ids:
        # sid is like "finnhub:reuters.com" → extract domain part
        domain = sid.split(":", 1)[1] if ":" in sid else sid
        tier = _SOURCE_TIER.get(domain, _TIER_DEFAULT)
        if tier > best:
            best = tier
    return best


def _corroboration_score(n_sources: int) -> float:
    """Map number of distinct independent sources to a corroboration score."""
    if n_sources >= 3:
        return 0.90
    if n_sources == 2:
        return 0.65
    # n_sources < 2: gate failed — should not reach here; guard anyway.
    return 0.20


def _recency_score(as_of: datetime, published_at: datetime) -> float:
    """Map article age (as_of − published_at) to a recency score."""
    age_seconds = max(0.0, (as_of - published_at).total_seconds())
    if age_seconds <= 4 * 3600:
        return 1.00
    if age_seconds <= 24 * 3600:
        return 0.70
    if age_seconds <= 72 * 3600:
        return 0.40
    return 0.10


def _compute_confidence(
    source_ids: frozenset[str],
    n_sources: int,
    as_of: datetime,
    tips: list[UnverifiedTip],
) -> float:
    """Compute confidence for a corroborated ticker.

    confidence = clamp(0.4*source_tier + 0.4*corroboration + 0.2*recency, 0.05, 0.85)
    """
    tier = _source_tier_score(source_ids)
    corr = _corroboration_score(n_sources)
    # Recency: use the most recent tip timestamp.
    if tips:
        most_recent_ts = max(t.ts for t in tips)
        recency = _recency_score(as_of, most_recent_ts)
    else:
        recency = 0.10
    raw = 0.4 * tier + 0.4 * corr + 0.2 * recency
    return max(_CONF_MIN, min(_CONF_MAX, raw))


def _source_fingerprint(tips: list[UnverifiedTip]) -> str:
    """SHA-256 of the sorted, concatenated tip fingerprints."""
    fps = sorted(t.fingerprint() for t in tips)
    blob = "|".join(fps)
    return hashlib.sha256(blob.encode()).hexdigest()


def _build_rationale(
    ticker: str,
    stance_method: str,
    raw_polarity: float,
    n_sources: int,
    tips: list[UnverifiedTip],
) -> str:
    """Build a concise rationale string (≤ 500 chars)."""
    headlines = []
    for t in tips[:3]:
        short_claim = t.claim[:120].replace("\n", " ")
        headlines.append(f"[{t.source_id}] {short_claim}")
    summary = " || ".join(headlines)
    rationale = (
        f"A3.news {ticker}: {stance_method} polarity={raw_polarity:.3f} "
        f"sources={n_sources} | {summary}"
    )
    return rationale[:500]


# ---------------------------------------------------------------------------
# Register advisor at import time (mirrors mirofish pattern)
# ---------------------------------------------------------------------------

default_registry.register(ADVISOR_ID)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def gather_a3_opinions(
    conn: sqlite3.Connection,
    clock: Clock,
    config: object,
    watchlist: list[str],
    *,
    _source_override: FinnhubNewsSource | None = None,
) -> list[Opinion]:
    """Corroborated A3.news opinions — 0..N, one per corroborated ticker.

    Parameters
    ----------
    conn:
        SQLite connection (reserved for future unverified_tips persistence;
        signature matches A1/A2 convention).
    clock:
        Live ``Clock`` or ``BacktestClock``.  ``clock.now()`` is the
        decision timestamp (Opinion.as_of).
    config:
        Arbiter config object.  Must have attribute ``finnhub_api_key: str``.
    watchlist:
        List of ticker symbols to sweep.
    _source_override:
        Injected ``FinnhubNewsSource`` for testing (skips real network).

    Returns
    -------
    list[Opinion]
        Empty list on any error or inert condition (fail-closed; never raises).
    """
    try:
        return _gather_impl(conn, clock, config, watchlist, _source_override)
    except Exception as exc:
        log.warning(
            "a3.pipeline.unexpected",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return []


def _gather_impl(
    conn: sqlite3.Connection,
    clock: Clock,
    config: object,
    watchlist: list[str],
    source_override: FinnhubNewsSource | None,
) -> list[Opinion]:
    """Core pipeline; wrapped by fail-closed boundary in gather_a3_opinions."""
    # --- INERT: no API key ---
    api_key: str = getattr(config, "finnhub_api_key", "") or ""
    if not api_key:
        log.debug("a3.pipeline.inert_no_key")
        return []

    # --- NETWORK-GATED: backtest mode ---
    if isinstance(clock, BacktestClock):
        log.debug("a3.pipeline.skipped_backtest")
        return []

    as_of: datetime = clock.now()
    run_group_id = generate_ulid()

    # --- Build source ---
    if source_override is not None:
        source = source_override
    else:
        client = FinnhubClient(api_key)
        source = FinnhubNewsSource(client)

    gate = DiversityGate()
    opinions: list[Opinion] = []

    min_stance: float = getattr(config, "a3_min_stance", _DEFAULT_MIN_STANCE)
    min_confidence: float = getattr(config, "a3_min_confidence", _DEFAULT_MIN_CONFIDENCE)

    for ticker in watchlist:
        try:
            ticker_opinions = _process_ticker(
                ticker=ticker,
                source=source,
                gate=gate,
                as_of=as_of,
                run_group_id=run_group_id,
                min_stance=min_stance,
                min_confidence=min_confidence,
            )
            opinions.extend(ticker_opinions)
        except Exception as exc:
            log.warning(
                "a3.pipeline.ticker_error",
                ticker=ticker,
                error=str(exc),
            )
            continue

    log.info(
        "a3.pipeline.complete",
        opinion_count=len(opinions),
        tickers=len(watchlist),
        run_group_id=run_group_id,
    )
    return opinions


def _process_ticker(
    *,
    ticker: str,
    source: FinnhubNewsSource,
    gate: DiversityGate,
    as_of: datetime,
    run_group_id: str,
    min_stance: float = _DEFAULT_MIN_STANCE,
    min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
) -> list[Opinion]:
    """Fetch, gate, score, and emit opinions for one ticker."""
    tips = source.fetch(ticker, as_of)
    if not tips:
        log.debug("a3.pipeline.no_tips", ticker=ticker)
        return []

    # Diversity gate: require ≥2 independent publisher source_ids.
    result = gate.evaluate(ticker, tips)
    if not result.corroborated:
        log.debug(
            "a3.pipeline.not_corroborated",
            ticker=ticker,
            n_sources=result.n_sources,
        )
        return []

    # Retrieve symbol-level sentiment from the source's cache.
    sentiment = source.get_cached_sentiment(ticker)

    # Build aggregate text from all tips for lexicon fallback.
    combined_text = " ".join(t.claim for t in tips)[:3000]
    stance = extract_stance(text=combined_text, sentiment=sentiment)

    # Abstain if stance is neutral (stance_score == 0.0).
    if stance.stance_score == 0.0:
        log.debug("a3.pipeline.neutral_stance", ticker=ticker, method=stance.method)
        return []

    confidence = _compute_confidence(
        source_ids=result.independent_sources,
        n_sources=result.n_sources,
        as_of=as_of,
        tips=tips,
    )

    # --- STRENGTH GATE: abstain when signal is too weak ---
    # Primary filter: abs(stance_score) must meet the minimum threshold.
    # Secondary filter: confidence must meet the minimum floor (default 0.0).
    # Below threshold → drop silently (not a failure; just not strong enough).
    if abs(stance.stance_score) < min_stance:
        log.debug(
            "a3.pipeline.stance_below_threshold",
            ticker=ticker,
            stance_score=stance.stance_score,
            min_stance=min_stance,
        )
        return []
    if confidence < min_confidence:
        log.debug(
            "a3.pipeline.confidence_below_threshold",
            ticker=ticker,
            confidence=confidence,
            min_confidence=min_confidence,
        )
        return []

    fp = _source_fingerprint(tips)
    rationale = _build_rationale(
        ticker=ticker,
        stance_method=stance.method,
        raw_polarity=stance.raw_polarity,
        n_sources=result.n_sources,
        tips=tips,
    )

    op = Opinion(
        advisor_id=ADVISOR_ID,
        ticker=ticker,
        stance_score=stance.stance_score,
        confidence=confidence,
        confidence_source=ConfidenceSource.MODELED,
        horizon_days=_HORIZON_DAYS,
        as_of=as_of,
        rationale=rationale,
        source_fingerprint=fp,
        run_group_id=run_group_id,
    )
    validate_opinion(op)

    log.info(
        "a3.pipeline.opinion_emitted",
        ticker=ticker,
        stance_score=stance.stance_score,
        confidence=confidence,
        n_sources=result.n_sources,
        method=stance.method,
    )
    return [op]
