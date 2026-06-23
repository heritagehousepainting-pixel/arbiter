"""SEC EDGAR companyfacts client (never-raises, rate-limited).

Resolves a ticker to its zero-padded CIK and fetches the XBRL companyfacts
JSON. Designed to NEVER raise: any failure degrades to None. Honors the SEC
fair-access policy (User-Agent header, ~10 req/s) plus 429 backoff.

ISOLATION: pure stdlib + httpx + mirofish.types. Never imports arbiter.
"""
from __future__ import annotations

import random
import time
from datetime import datetime

import httpx

# Minimum spacing between SEC requests to stay under ~10 req/s.
_MIN_INTERVAL_SECONDS = 0.11


class SecFactsClient:
    """Thin, never-raising SEC companyfacts fetcher."""

    def __init__(
        self,
        *,
        user_agent: str,
        base_url: str = "https://data.sec.gov",
        tickers_url: str = "https://www.sec.gov/files/company_tickers.json",
        timeout: float = 30.0,
        max_retries: int = 5,
        backoff_base: float = 0.2,
        backoff_cap: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.base_url = base_url.rstrip("/")
        self.tickers_url = tickers_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._transport = transport
        # ticker(upper) -> 10-digit CIK; populated lazily, cached on instance.
        self._cik_map: dict[str, str] | None = None
        self._last_request_ts: float = 0.0

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept": "application/json"}

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, transport=self._transport)

    def _throttle(self) -> None:
        """Sleep so consecutive requests are >= _MIN_INTERVAL_SECONDS apart."""
        now = time.monotonic()
        elapsed = now - self._last_request_ts
        if elapsed < _MIN_INTERVAL_SECONDS:
            time.sleep(_MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_ts = time.monotonic()

    def _sleep_for_retry(self, attempt: int, retry_after: str | None) -> None:
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

    def _get_json(self, client: httpx.Client, url: str) -> dict | None:
        """One GET with throttle + 429 retry. Returns parsed JSON or None."""
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = client.get(url, headers=self._headers())
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

            # 404 / any other non-200 -> degrade.
            return None

        return None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def cik_for_ticker(self, ticker: str) -> str | None:
        """Resolve ticker -> 10-digit zero-padded CIK. None if unknown.

        Fetches company_tickers.json once and caches it on the instance.
        NEVER raises -> None.
        """
        try:
            if self._cik_map is None:
                self._cik_map = self._load_cik_map()
            if self._cik_map is None:
                return None
            return self._cik_map.get(ticker.upper())
        except Exception:
            return None

    def _load_cik_map(self) -> dict[str, str] | None:
        with self._client() as client:
            body = self._get_json(client, self.tickers_url)
        if not isinstance(body, dict):
            return None
        out: dict[str, str] = {}
        # company_tickers.json is {"0": {"cik_str": 320193, "ticker": "AAPL", ...}, ...}
        for entry in body.values():
            if not isinstance(entry, dict):
                continue
            tk = entry.get("ticker")
            cik = entry.get("cik_str")
            if tk is None or cik is None:
                continue
            try:
                out[str(tk).upper()] = f"{int(cik):010d}"
            except (TypeError, ValueError):
                continue
        return out

    def company_facts(self, cik: str) -> dict | None:
        """GET /api/xbrl/companyfacts/CIK{cik}.json. None on any failure.

        NEVER raises. Respects ~10 req/s + 429 backoff.
        """
        try:
            url = f"{self.base_url}/api/xbrl/companyfacts/CIK{cik}.json"
            with self._client() as client:
                return self._get_json(client, url)
        except Exception:
            return None

    def facts_as_of(self, ticker: str, as_of: datetime) -> dict | None:
        """cik_for_ticker -> company_facts -> raw facts dict (no PIT filter here).

        The filed<=as_of filter lives in fundamentals.py. None if no CIK / no
        facts. NEVER raises. (`as_of` is accepted for signature symmetry; the
        raw facts dict is returned unfiltered for the caller to filter.)
        """
        try:
            cik = self.cik_for_ticker(ticker)
            if cik is None:
                return None
            return self.company_facts(cik)
        except Exception:
            return None
