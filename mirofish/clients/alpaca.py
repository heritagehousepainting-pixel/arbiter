"""Alpaca daily-bars client (PIT-correct, never-raises).

Fetches split-adjusted daily OHLCV bars from the Alpaca market-data API using
the IEX feed. Designed to NEVER raise: any HTTP/network/parse failure degrades
to an empty list so the caller can fail-closed (abstain).

ISOLATION: pure stdlib + httpx + mirofish.types. Never imports arbiter.
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta

import httpx

from mirofish.types import Bar, ensure_utc


def _parse_bar_t(raw: str) -> datetime:
    """Parse an Alpaca RFC-3339 bar timestamp into tz-aware UTC."""
    # Alpaca emits e.g. "2024-01-02T05:00:00Z".
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return ensure_utc(datetime.fromisoformat(raw))


class AlpacaBarsClient:
    """Daily-bars client for Alpaca market data (IEX feed mandatory)."""

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        feed: str = "iex",
        base_url: str = "https://data.alpaca.markets",
        timeout: float = 30.0,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.feed = feed
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        # `transport` is a test seam (httpx.MockTransport); None -> real network.
        self._transport = transport

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Accept": "application/json",
        }

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout,
            transport=self._transport,
        )

    def _sleep_for_retry(self, attempt: int, retry_after: str | None) -> None:
        """Honor Retry-After if present, else exp-backoff + jitter (base..cap)."""
        delay: float
        if retry_after is not None:
            try:
                delay = float(retry_after)
            except (TypeError, ValueError):
                delay = self.backoff_base
        else:
            delay = min(self.backoff_cap, self.backoff_base * (2 ** attempt))
            delay += random.uniform(0.0, self.backoff_base)
        delay = min(delay, self.backoff_cap)
        time.sleep(delay)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def bars(self, ticker: str, start: datetime, end: datetime) -> list[Bar]:
        """Fetch daily bars in [start, end), split-adjusted, IEX feed.

        Paginates via page_token<-next_page_token. 429 -> Retry-After/backoff
        and retry the SAME paged request. 404/422 -> []. Network error -> [].
        Drops bars with t >= end (strict). Sorts ascending. NEVER raises.
        """
        start = ensure_utc(start)
        end = ensure_utc(end)
        url = f"{self.base_url}/v2/stocks/{ticker}/bars"
        out: list[Bar] = []
        page_token: str | None = None

        try:
            with self._client() as client:
                while True:
                    params: dict[str, object] = {
                        "timeframe": "1Day",
                        "adjustment": "split",
                        "limit": 10000,
                        "feed": self.feed,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                    }
                    if page_token:
                        params["page_token"] = page_token

                    body = self._request_page(client, url, params)
                    if body is None:
                        # Non-retryable error already handled -> degrade.
                        return []

                    raw_bars = body.get("bars") or []
                    for rb in raw_bars:
                        bar = self._parse_one(rb)
                        if bar is not None and bar.t < end:
                            out.append(bar)

                    page_token = body.get("next_page_token")
                    if not page_token:
                        break
        except Exception:
            # Belt-and-suspenders: NEVER raise out of this client.
            return []

        out.sort(key=lambda b: b.t)
        return out

    def _request_page(
        self, client: httpx.Client, url: str, params: dict[str, object]
    ) -> dict | None:
        """One paged GET with 429 retry. Returns parsed body or None to degrade."""
        for attempt in range(self.max_retries + 1):
            try:
                resp = client.get(url, params=params, headers=self._headers())
            except httpx.HTTPError:
                return None

            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception:
                    return None

            if resp.status_code == 429:
                if attempt >= self.max_retries:
                    return None
                self._sleep_for_retry(attempt, resp.headers.get("Retry-After"))
                continue

            # 404 / 422 / any other non-200 -> degrade.
            return None

        return None

    @staticmethod
    def _parse_one(rb: dict) -> Bar | None:
        try:
            return Bar(
                t=_parse_bar_t(rb["t"]),
                o=float(rb["o"]),
                h=float(rb["h"]),
                l=float(rb["l"]),
                c=float(rb["c"]),
                v=float(rb["v"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def bars_as_of(
        self, ticker: str, as_of: datetime, *, lookback_days: int = 300
    ) -> list[Bar]:
        """Fetch [as_of-lookback, as_of+1d) then PIT filter to t <= as_of.

        NEVER raises.
        """
        as_of = ensure_utc(as_of)
        start = as_of - timedelta(days=lookback_days)
        end = as_of + timedelta(days=1)
        fetched = self.bars(ticker, start, end)
        return [b for b in fetched if b.t <= as_of]
