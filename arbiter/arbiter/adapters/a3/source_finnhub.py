"""FinnhubNewsSource — A3 TipSource implementation for Finnhub company-news.

``source_id`` design
--------------------
We set ``source_id = f"finnhub:{publisher_domain}"`` so that distinct real
publishers (Reuters, Bloomberg, CNBC…) count as independent voices in the
diversity gate.  Finnhub is merely the transport; the *editorial independence*
lives with the publisher.  This is legitimate: Reuters and Bloomberg have
separate newsrooms; two Reuters articles republished via Finnhub would still
share ``source_id = "finnhub:reuters.com"`` and count as ONE voice (correct).

Look-ahead guard
----------------
``fetch(ticker, as_of)`` filters out any article whose ``published_at``
(real UTC timestamp from the source) is strictly greater than ``as_of``.
This mirrors the TipSource ABC contract and ``PITGateway`` discipline.

No ``datetime.now()`` — ``as_of`` is always injected by the caller.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import structlog

from arbiter.ingest.finnhub.client import FinnhubClient
from arbiter.tips.source import TipSource, UnverifiedTip

log = structlog.get_logger(__name__)

# Finnhub is the news *transport*, not the editorial publisher.  The real
# publisher is in the article ``source`` field ("Yahoo", "Benzinga", "CNBC"…);
# Finnhub's ``url`` is ALWAYS a finnhub.io redirect, so source identity + the
# PR-wire block MUST key on the ``source`` name, not the URL domain (keying on
# the domain collapses every publisher to one voice → the diversity gate could
# never pass — the live bug this replaced).
# Blocked PR-wire publishers (matched as a substring of the lowercased source).
_BLOCKED_PUBLISHERS: frozenset[str] = frozenset({
    "prnewswire", "pr newswire", "businesswire", "business wire",
    "globenewswire", "globe newswire", "einpresswire", "ein presswire",
    "accesswire", "access wire", "prlog", "newsfile",
})

# Lookback window (days) for company-news fetch.  One week covers SHORT horizon.
_LOOKBACK_DAYS = 7


class FinnhubNewsSource(TipSource):
    """``TipSource`` implementation that fetches company news from Finnhub.

    Parameters
    ----------
    client:
        Injected ``FinnhubClient``.  Pass a mock in tests.
    sentiment_cache:
        Optional pre-populated ``{ticker: sentiment_dict}`` to avoid
        a second network round-trip in tests (passed by the pipeline).
    """

    def __init__(
        self,
        client: FinnhubClient,
        *,
        sentiment_cache: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._client = client
        self._sentiment_cache: dict[str, dict[str, Any]] = sentiment_cache or {}

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get_cached_sentiment(self, ticker: str) -> dict | None:
        """Return the cached Finnhub sentiment dict for *ticker*, or None.

        The sentiment is populated during ``fetch()`` and stored in the
        internal cache keyed by ticker symbol.  Returns ``None`` when
        ``fetch()`` has not yet been called for *ticker* (e.g. in tests
        that pre-populate the sentiment_cache separately).
        """
        return self._sentiment_cache.get(ticker)

    # ------------------------------------------------------------------
    # TipSource interface
    # ------------------------------------------------------------------

    @property
    def source_id(self) -> str:
        # Base prefix; the per-article source_id is
        # ``f"finnhub:{publisher_domain}"`` (computed in fetch()).
        # This property satisfies the ABC; the per-article value is set
        # directly on UnverifiedTip in _make_tip().
        return "finnhub"

    def fetch(
        self,
        ticker: str,
        as_of: datetime,
    ) -> list[UnverifiedTip]:
        """Return UnverifiedTips for *ticker* published at or before *as_of*.

        Each tip's ``source_id`` is ``f"finnhub:{publisher_domain}"`` so
        distinct publishers count as distinct voices in the diversity gate.

        Parameters
        ----------
        ticker:
            Watchlist ticker symbol.
        as_of:
            Information timestamp ceiling (tz-aware UTC).  Articles with
            ``published_at > as_of`` are dropped (no look-ahead).

        Returns
        -------
        list[UnverifiedTip]
            Sorted ascending by ``ts``.  Empty list on any error.
        """
        try:
            return self._fetch_impl(ticker, as_of)
        except Exception as exc:
            log.warning(
                "finnhub.source.fetch_error",
                ticker=ticker,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return []

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _fetch_impl(self, ticker: str, as_of: datetime) -> list[UnverifiedTip]:
        # Build a 7-day lookback window [as_of - 7d, as_of].
        # Use as_of.date() (pure date arithmetic — no wall-clock call).
        from datetime import timedelta
        to_date = as_of.date().isoformat()
        from_date = (as_of.date() - timedelta(days=_LOOKBACK_DAYS)).isoformat()

        articles = self._client.get_company_news(ticker, from_date, to_date)
        if not articles:
            return []

        # Fetch (or look up cached) symbol-level sentiment once per ticker.
        sentiment = self._sentiment_cache.get(ticker)
        if sentiment is None:
            sentiment = self._client.get_news_sentiment(ticker)
            self._sentiment_cache[ticker] = sentiment

        tips: list[UnverifiedTip] = []
        seen_urls: set[str] = set()

        for article in articles:
            tip = self._make_tip(ticker, article, sentiment, as_of)
            if tip is None:
                continue
            # Dedup by URL within this fetch batch.
            if tip.url in seen_urls:
                continue
            seen_urls.add(tip.url)
            tips.append(tip)

        # Sort ascending by ts (oldest first — matches TipSource ABC contract).
        tips.sort(key=lambda t: t.ts)
        return tips

    def _make_tip(
        self,
        ticker: str,
        article: dict,
        sentiment: dict,
        as_of: datetime,
    ) -> UnverifiedTip | None:
        """Convert one Finnhub article dict into an UnverifiedTip, or None."""
        url = article.get("url") or ""
        headline = article.get("headline") or ""
        summary = article.get("summary") or ""
        source_name = article.get("source") or ""
        published_epoch = article.get("published_at") or 0

        # Require a non-empty URL and some content.
        if not url or (not headline and not summary):
            return None

        # Publisher identity = the ``source`` field (Finnhub's url is its own
        # redirect, so a URL domain would be "finnhub.io" for EVERY article).
        publisher = source_name.strip()
        if not publisher:
            return None
        pub_key = publisher.lower()

        # Block PR-wire / spam publishers (matched by name).
        if any(b in pub_key for b in _BLOCKED_PUBLISHERS):
            log.debug("finnhub.source.blocked_publisher", publisher=publisher, ticker=ticker)
            return None

        # Convert published_at (Unix epoch) to tz-aware UTC datetime.
        if not published_epoch:
            return None
        try:
            published_at = datetime.fromtimestamp(published_epoch, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            log.debug("finnhub.source.bad_timestamp", epoch=published_epoch, ticker=ticker)
            return None

        # NO LOOK-AHEAD: drop articles published after as_of.
        if published_at > as_of:
            log.debug(
                "finnhub.source.future_article_dropped",
                ticker=ticker,
                published_at=published_at.isoformat(),
                as_of=as_of.isoformat(),
            )
            return None

        # Build claim = headline + summary (cap to 2000 chars).
        claim_parts = []
        if headline:
            claim_parts.append(headline)
        if summary:
            claim_parts.append(summary)
        claim = " | ".join(claim_parts)[:2000]

        # Attach sentiment score as JSON in the raw field for audit.
        raw = json.dumps({
            "article_source": source_name,
            "url": url,
            "published_at": published_epoch,
            "sentiment": sentiment,
        })

        # source_id = "finnhub:{publisher}" so distinct publishers (Yahoo,
        # Benzinga, CNBC…) count as distinct voices in DiversityGate.
        per_article_source_id = f"finnhub:{pub_key}"

        return UnverifiedTip(
            ticker=ticker,
            claim=claim,
            account=publisher,
            ts=published_at,
            url=url,
            source_id=per_article_source_id,
        )
