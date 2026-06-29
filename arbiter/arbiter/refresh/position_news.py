"""Per-open-position news scan via the existing Finnhub client (fail-closed)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog

from arbiter.refresh.types import PositionFinding, Severity

log = structlog.get_logger(__name__)


def _severity(sentiment: float) -> Severity:
    mag = abs(sentiment)
    if mag >= 0.5:
        return Severity.HIGH
    if mag >= 0.2:
        return Severity.MEDIUM
    return Severity.LOW


def scan_position_news(tickers: list[str], as_of: datetime,
                       client: Any) -> list[PositionFinding]:
    out: list[PositionFinding] = []
    frm = (as_of - timedelta(days=7)).date().isoformat()
    to = as_of.date().isoformat()
    for ticker in tickers:
        try:
            articles = client.get_company_news(ticker, frm, to) or []
            sentiment = client.get_news_sentiment(ticker) or {}
            score = float(sentiment.get("sentiment_score", 0.0) or 0.0)
            headlines = [a.get("headline", "") for a in articles[:5] if a.get("headline")]
            out.append(PositionFinding(ticker=ticker, headlines=headlines,
                                       sentiment=score, severity=_severity(score),
                                       available=True))
        except Exception as exc:  # fail-closed per ticker
            log.warning("refresh.position_news.failed", ticker=ticker, error=str(exc))
            out.append(PositionFinding(ticker=ticker, headlines=[], sentiment=0.0,
                                       severity=Severity.LOW, available=False))
    return out
