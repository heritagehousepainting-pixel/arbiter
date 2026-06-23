"""Finnhub HTTP client — rate-limit aware, mirrors EdgarClient style.

This module is the ONLY place that makes network calls to Finnhub.
All tests mock ``http_get`` — no real HTTP in unit tests.

NOTE (ToS): Finnhub free tier is for personal / non-commercial use only.
See https://finnhub.io/pricing before commercial deployment.

Rate limits (free tier)
-----------------------
60 API calls per minute.  We enforce a minimum 1.1 s gap between calls
(≈ 54 req/min — well under 60) with exponential back-off on 429 / 5xx.

Endpoints used
--------------
Company news:
    GET /api/v1/company-news?symbol=&from=&to=&token=
    Returns a JSON array of article objects.  Each item has:
        id, category, datetime (Unix epoch), headline, image, related,
        source, summary, url
    Note: per-article sentiment (bullish/bearish) lives in the *separate*
    news-sentiment endpoint, NOT company-news.  We fetch sentiment via:
    GET /api/v1/news-sentiment?symbol=&token=
    which returns aggregated bullish/bearish percents for the symbol.

    The adapter combines both: fetch news articles from company-news, then
    fetch the symbol-level sentiment once and attach it to each article.
    (Finnhub does not return per-article sentiment on the free tier.)

Fields exposed
--------------
Article dict keys after normalization:
    headline:  str
    summary:   str
    url:       str (canonical article URL)
    source:    str (publisher name, e.g. "Reuters")
    published_at: int (Unix epoch UTC)
    image:     str (may be empty)

Sentiment dict keys after normalization (symbol-level, not per-article):
    bullish_percent:  float [0, 1]  (fraction of bullish signals)
    bearish_percent:  float [0, 1]
    sentiment_score:  float         bullish_percent − bearish_percent
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog

log = structlog.get_logger(__name__)

_BASE_URL = "https://finnhub.io"
_MIN_INTERVAL_SEC = 1.1   # ≈ 54 req/min, safely under 60/min cap
_MAX_RETRIES = 3
_BACKOFF_BASE = 2.0       # seconds; doubles per retry

# Ticker symbols allowed: 1–5 uppercase letters (+ . or - for some ETFs).
# Validated before URL interpolation (SSRF guard).
import re as _re
_TICKER_RE = _re.compile(r"^[A-Z]{1,5}([.\-][A-Z]{0,4})?$")


class FinnhubError(Exception):
    """Raised when a Finnhub request fails after all retries."""


def _sanitize_ticker(ticker: str) -> str:
    """Return ticker uppercased if safe, else raise FinnhubError."""
    t = (ticker or "").strip().upper()
    if not _TICKER_RE.match(t):
        raise FinnhubError(f"Refusing unsafe ticker value for URL: {ticker!r}")
    return t


def _extract_domain(url: str) -> str:
    """Return the registerable domain of *url*, e.g. 'reuters.com'."""
    try:
        host = urlparse(url).hostname or ""
        # Strip leading 'www.'
        parts = host.lower().split(".")
        if len(parts) > 2 and parts[0] == "www":
            parts = parts[1:]
        return ".".join(parts)
    except Exception:
        return ""


class FinnhubClient:
    """Thin Finnhub HTTP wrapper.

    Parameters
    ----------
    api_key:
        Finnhub API key (env ``FINNHUB_API_KEY``).  Empty string → all
        methods return [] (inert mode; A3 pipeline guards this upstream).
    base_url:
        Override for testing.
    http_get:
        Injectable callable ``(url: str, params: dict) -> str`` for tests.
        Signature matches the ``httpx.Client.get`` pattern; default builds a
        real ``httpx.Client``.
    sleep_fn:
        Injected sleep callable (swap out in tests).
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _BASE_URL,
        http_get: Callable[[str, dict[str, Any]], str] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._sleep = sleep_fn
        self._last_ts: float = 0.0

        if http_get is not None:
            self._http_get = http_get
        else:
            client = httpx.Client(timeout=30.0, follow_redirects=True)
            self._http_get = lambda url, params: client.get(url, params=params).text

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_company_news(
        self,
        ticker: str,
        from_date: str,
        to_date: str,
    ) -> list[dict]:
        """Return company-news articles for *ticker* in the date range.

        Parameters
        ----------
        ticker:
            Exchange ticker (e.g. ``"AAPL"``).
        from_date:
            ISO date string ``YYYY-MM-DD`` (inclusive).
        to_date:
            ISO date string ``YYYY-MM-DD`` (inclusive).

        Returns
        -------
        list[dict]
            Each dict has keys: ``headline``, ``summary``, ``url``,
            ``source``, ``published_at`` (Unix epoch int), ``image``.
            Returns ``[]`` on any error (fail-closed).
        """
        if not self._api_key:
            return []
        try:
            safe_ticker = _sanitize_ticker(ticker)
        except FinnhubError:
            log.warning("finnhub.client.bad_ticker", ticker=ticker)
            return []

        url = f"{self._base_url}/api/v1/company-news"
        params = {
            "symbol": safe_ticker,
            "from": from_date,
            "to": to_date,
            "token": self._api_key,
        }
        try:
            raw = self._get(url, params)
        except FinnhubError as exc:
            log.warning("finnhub.client.news_fetch_failed", ticker=ticker, error=str(exc))
            return []

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            log.warning("finnhub.client.news_bad_json", ticker=ticker)
            return []

        if not isinstance(data, list):
            log.warning("finnhub.client.news_unexpected_shape", ticker=ticker)
            return []

        results: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            results.append({
                "headline": str(item.get("headline") or ""),
                "summary": str(item.get("summary") or ""),
                "url": str(item.get("url") or ""),
                "source": str(item.get("source") or ""),
                "published_at": int(item.get("datetime") or 0),
                "image": str(item.get("image") or ""),
            })
        return results

    def get_news_sentiment(self, ticker: str) -> dict:
        """Return aggregated news sentiment for *ticker*.

        Endpoint: ``GET /api/v1/news-sentiment?symbol=&token=``

        Returns
        -------
        dict with keys:
            ``bullish_percent`` (float, 0–1),
            ``bearish_percent`` (float, 0–1),
            ``sentiment_score`` (float, bullish − bearish ∈ [-1, 1]).
        Returns a neutral zero-score dict on any error (fail-closed).
        """
        neutral = {"bullish_percent": 0.0, "bearish_percent": 0.0, "sentiment_score": 0.0}
        if not self._api_key:
            return neutral
        try:
            safe_ticker = _sanitize_ticker(ticker)
        except FinnhubError:
            log.warning("finnhub.client.bad_ticker_sentiment", ticker=ticker)
            return neutral

        url = f"{self._base_url}/api/v1/news-sentiment"
        params = {"symbol": safe_ticker, "token": self._api_key}
        try:
            raw = self._get(url, params)
        except FinnhubError as exc:
            log.warning("finnhub.client.sentiment_fetch_failed", ticker=ticker, error=str(exc))
            return neutral

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return neutral

        if not isinstance(data, dict):
            return neutral

        sentiment = data.get("sentiment") or {}
        if not isinstance(sentiment, dict):
            sentiment = {}

        bullish = float(sentiment.get("bullishPercent") or 0.0)
        bearish = float(sentiment.get("bearishPercent") or 0.0)
        score = bullish - bearish
        return {
            "bullish_percent": max(0.0, min(1.0, bullish)),
            "bearish_percent": max(0.0, min(1.0, bearish)),
            "sentiment_score": max(-1.0, min(1.0, score)),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, url: str, params: dict[str, Any]) -> str:
        """GET *url* with rate-limiting and retry logic."""
        self._rate_limit()
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                text = self._http_get(url, params)
                return text
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in {429, 503}:
                    wait = _BACKOFF_BASE ** (attempt + 1)
                    log.warning(
                        "finnhub.client.rate_limited",
                        status=status,
                        attempt=attempt,
                        wait=wait,
                    )
                    self._sleep(wait)
                    continue
                last_exc = exc
                break
            except httpx.HTTPError as exc:
                last_exc = exc
                self._sleep(_BACKOFF_BASE ** (attempt + 1))
        raise FinnhubError(
            f"Failed to GET {url!r} after {_MAX_RETRIES} attempts"
        ) from last_exc

    def _rate_limit(self) -> None:
        """Sleep if we're calling faster than _MIN_INTERVAL_SEC."""
        elapsed = time.monotonic() - self._last_ts
        if elapsed < _MIN_INTERVAL_SEC:
            self._sleep(_MIN_INTERVAL_SEC - elapsed)
        self._last_ts = time.monotonic()
