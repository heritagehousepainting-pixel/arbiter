"""Alpaca Markets data API price source — Lane 3 Wave-B client.

Implements ``PriceSource`` (INTERFACES.md §3).

As-of semantics
---------------
Callers supply a ``[start, end)`` window.  The source returns only bars whose
timestamps are strictly less than ``end`` and no look-ahead occurs because
bars represent historical close prices already published before the request.

Survivorship
------------
Delisted tickers are passed through as-is.  The Alpaca API may return partial
data or raise an HTTP 422/404; those cases propagate as empty lists (fail-soft
on 404/422) or re-raise on unexpected errors.

Network
-------
Uses ``httpx`` (sync client) for one-shot JSON fetch.  Timeout comes from
``Config.alpaca_timeout``.  Authentication via Alpaca paper-broker headers:
``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY``.

Spec references
---------------
INTERFACES.md §3, §10b note 5 (Config field names), §11 convention 1.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from arbiter.config import Config
from arbiter.data.pit import Bar

log = logging.getLogger(__name__)

# Alpaca v2 data endpoint for historical bars.
_BARS_PATH = "/v2/stocks/{ticker}/bars"

# ISO-8601 format Alpaca expects.
_DT_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _to_utc(dt: datetime) -> datetime:
    """Ensure *dt* is tz-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_bar(ticker: str, raw: dict[str, Any]) -> Bar:
    """Parse one Alpaca bar dict into a ``Bar``.

    Alpaca v2 bar keys: t (RFC-3339 timestamp), o, h, l, c, v.
    """
    ts_raw: str = raw["t"]
    # Alpaca returns RFC-3339 like "2026-01-15T00:00:00Z".
    # Use fromisoformat for py3.11+ (handles Z suffix).
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError:
        # Fallback: strip trailing Z and attach UTC.
        ts = datetime.strptime(ts_raw.rstrip("Z"), "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    return Bar(
        ticker=ticker,
        timestamp=ts,
        open=float(raw["o"]),
        high=float(raw["h"]),
        low=float(raw["l"]),
        close=float(raw["c"]),
        volume=float(raw["v"]),
    )


class AlpacaPriceSource:
    """Alpaca data API price source.

    Parameters
    ----------
    config:
        Runtime config (provides API keys, base URL, timeout).

    Usage::

        src = AlpacaPriceSource(config)
        bars = src.bars("AAPL", start, end)
    """

    def __init__(self, config: Config) -> None:
        self._base_url = config.alpaca_data_base_url.rstrip("/")
        self._headers = {
            "APCA-API-KEY-ID": config.alpaca_api_key,
            "APCA-API-SECRET-KEY": config.alpaca_secret_key,
            "Accept": "application/json",
        }
        self._timeout = config.alpaca_timeout
        # Data feed: the FREE Alpaca plan only allows "iex"; omitting it defaults
        # to "sip" (paid) and returns 403 Forbidden. Override via ALPACA_DATA_FEED
        # (e.g. "sip") if you have a paid market-data subscription.
        self._feed = os.getenv("ALPACA_DATA_FEED", "iex")

    def bars(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        """Return daily OHLCV bars for ``ticker`` in [start, end).

        Parameters
        ----------
        ticker:
            Exchange ticker symbol (may be delisted — passed through).
        start:
            Inclusive start (tz-aware UTC).
        end:
            Exclusive end (tz-aware UTC).  Bars at or after this timestamp
            are dropped (look-ahead guard).

        Returns
        -------
        list[Bar]
            Bars sorted ascending by timestamp.  Empty on 404/422 (delisted,
            unknown ticker, or date range with no data).

        Raises
        ------
        httpx.HTTPStatusError
            On unexpected HTTP errors (5xx, 429, etc.).
        """
        start_utc = _to_utc(start)
        end_utc = _to_utc(end)

        url = self._base_url + _BARS_PATH.format(ticker=ticker)
        params: dict[str, str | int] = {
            "start": start_utc.strftime(_DT_FMT),
            "end": end_utc.strftime(_DT_FMT),
            "timeframe": "1Day",
            "adjustment": "split",
            "limit": 10_000,
            "feed": self._feed,
        }

        bars: list[Bar] = []
        next_page_token: str | None = None

        with httpx.Client(timeout=self._timeout) as client:
            while True:
                if next_page_token:
                    params["page_token"] = next_page_token

                try:
                    resp = client.get(url, headers=self._headers, params=params)
                except httpx.RequestError as exc:
                    log.warning(
                        "alpaca_bars_request_error ticker=%s error=%s",
                        ticker,
                        exc,
                    )
                    return []

                # Graceful degradation: delisted / unknown ticker → empty.
                if resp.status_code in (404, 422):
                    log.info(
                        "alpaca_bars_not_found ticker=%s status=%s",
                        ticker,
                        resp.status_code,
                    )
                    return []

                resp.raise_for_status()

                data = resp.json()
                raw_bars: list[dict[str, Any]] = data.get("bars") or []
                for raw in raw_bars:
                    bar = _parse_bar(ticker, raw)
                    # Strict look-ahead guard: drop bars at or after end.
                    if bar.timestamp < end_utc:
                        bars.append(bar)

                next_page_token = data.get("next_page_token")
                if not next_page_token:
                    break

        bars.sort(key=lambda b: b.timestamp)
        log.debug(
            "alpaca_bars_fetched ticker=%s count=%d start=%s end=%s",
            ticker,
            len(bars),
            start_utc.isoformat(),
            end_utc.isoformat(),
        )
        return bars

    # ------------------------------------------------------------------
    # PITGateway adapter
    # ------------------------------------------------------------------

    def get_pit(
        self,
        field: str,
        ticker: str,
        as_of: datetime,
    ) -> object | None:
        """Return scalar value for *field* at *as_of* by fetching recent bars.

        This adapter lets ``AlpacaPriceSource`` be registered directly with
        ``PITGateway.register_source(field, source)``.

        Supported fields: ``price_open``, ``price_close``, ``spread``.
        (``adv_20d`` is handled by the spec-compliant ``adv.py`` path via
        ``register_adv_source()`` — not here.)

        Returns ``None`` if no bars exist as of *as_of*.
        """
        from datetime import timedelta

        as_of_utc = _to_utc(as_of)

        # For open/close/spread, fetch just the last few days.
        # adv_20d is no longer handled here — routed via adv.py instead.
        window_start = as_of_utc - timedelta(days=5)

        # Use as_of + 1 day as the exclusive end so that daily bars
        # timestamped at as_of midnight (T00:00:00Z) are included by
        # bars()'s strict ``timestamp < end`` guard, while bars strictly
        # after as_of are excluded by the downstream eligibility filter.
        # Do NOT use ``as_of + 1 second`` — that is fragile and would
        # permit a refactor to accidentally drop the eligibility guard.
        window_end = as_of_utc + timedelta(days=1)

        fetched = self.bars(ticker, window_start, window_end)
        if not fetched:
            return None

        # Latest bar at or before as_of (explicit PIT eligibility guard).
        eligible = [b for b in fetched if b.timestamp <= as_of_utc]
        if not eligible:
            return None

        latest = eligible[-1]

        if field == "price_close":
            return latest.close
        if field == "price_open":
            return latest.open
        if field == "spread":
            # Estimate bid-ask spread as (high - low) / close.
            if latest.close > 0:
                return (latest.high - latest.low) / latest.close
            return None

        return None

    def get_bar(
        self,
        ticker: str,
        as_of: datetime,
    ) -> Bar | None:
        """Return the latest full OHLCV ``Bar`` at or before *as_of*.

        Unlike :meth:`get_pit` (which returns a scalar ``close``/``open``),
        this returns the whole bar so callers that need **volume** (ADV and the
        volume-anomaly breaker) get ``close`` AND ``volume`` together.  The
        scalar ``get_pit("price_close")`` path deliberately stays scalar
        because exit_monitor / outcome_labeler / beta depend on it.

        Enforces the same PIT eligibility guard (``timestamp <= as_of``) — no
        look-ahead.  Returns ``None`` when no eligible bar exists.
        """
        from datetime import timedelta

        as_of_utc = _to_utc(as_of)
        window_start = as_of_utc - timedelta(days=5)
        window_end = as_of_utc + timedelta(days=1)

        fetched = self.bars(ticker, window_start, window_end)
        if not fetched:
            return None

        eligible = [b for b in fetched if b.timestamp <= as_of_utc]
        if not eligible:
            return None
        return eligible[-1]
