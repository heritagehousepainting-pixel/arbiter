"""Authoritative market-hours / calendar — sub-project #3 (Decision 2).

Replaces the coarse ``engine._us_market_open`` heuristic (hardcoded UTC-5, no
DST, no holidays, no early closes) for the daemon's scheduling decisions.

Two implementations behind one Protocol:

* ``AlpacaMarketCalendar`` — the LIVE source of truth (``GET /v2/clock`` on the
  trading host), cached per session boundary (amendment C1) so we hit the API
  O(once per boundary), not every iteration.  On a fetch error it falls back to
  the offline calendar for that query (never crash the loop).
* ``OfflineMarketCalendar`` — deterministic, no network, ``zoneinfo`` ET with
  proper DST, reusing ``replay_clock`` holidays + the ``_EARLY_CLOSE`` map.  Logs
  a loud WARNING when ``now.year`` exceeds the curated range (amendment C3).

``session(now)`` takes the injected ``clock.now()`` so it is fully testable with
a frozen clock — no internal wall-clock read, no ``datetime.now()``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import structlog

from arbiter.config import Config
from arbiter.data.replay_clock import (
    CURATED_HOLIDAY_MAX_YEAR,
    _EARLY_CLOSE,
    _is_trading_day,
)

log = structlog.get_logger(__name__)

_ET = ZoneInfo("America/New_York")
_OPEN = time(9, 30)
_CLOSE = time(16, 0)
_EARLY_CLOSE_TIME = time(13, 0)


@dataclass(frozen=True)
class MarketSession:
    """A point-in-time view of the US equity market session.

    All datetimes are tz-aware UTC.
    """

    is_open: bool
    next_open: datetime | None
    next_close: datetime | None


@runtime_checkable
class MarketCalendar(Protocol):
    def session(self, now: datetime) -> MarketSession: ...


# ---------------------------------------------------------------------------
# Offline calendar (deterministic, sim/tests/fallback)
# ---------------------------------------------------------------------------

class OfflineMarketCalendar:
    """Deterministic ET market calendar — no network.

    Models the regular 09:30–16:00 ET session with real DST handling via
    ``zoneinfo`` and curated holidays/early-closes.  Best-effort beyond the
    curated range (logs a WARNING).
    """

    def _close_time_for(self, d) -> time:
        return _EARLY_CLOSE_TIME if d in _EARLY_CLOSE else _CLOSE

    def _maybe_warn_stale(self, now: datetime) -> None:
        if now.year > CURATED_HOLIDAY_MAX_YEAR:
            log.warning(
                "market_calendar.offline_curated_data_stale",
                year=now.year,
                curated_max_year=CURATED_HOLIDAY_MAX_YEAR,
                detail=(
                    "offline full-day holidays are generated arithmetically and "
                    "remain correct, but the curated EARLY-CLOSE (half-day) map "
                    f"is hand-listed only through {CURATED_HOLIDAY_MAX_YEAR}; "
                    "refresh it yearly (amendment C3) — prefer the live Alpaca "
                    "calendar"
                ),
            )

    def session(self, now: datetime) -> MarketSession:
        self._maybe_warn_stale(now)
        now_et = now.astimezone(_ET)
        today = now_et.date()

        is_open = False
        if _is_trading_day(today):
            close_t = self._close_time_for(today)
            open_dt = datetime.combine(today, _OPEN, tzinfo=_ET)
            close_dt = datetime.combine(today, close_t, tzinfo=_ET)
            is_open = open_dt <= now_et < close_dt

        next_open = self._next_open(now_et)
        next_close = self._next_close(now_et)
        return MarketSession(
            is_open=is_open,
            next_open=next_open.astimezone(timezone.utc) if next_open else None,
            next_close=next_close.astimezone(timezone.utc) if next_close else None,
        )

    def _next_open(self, now_et: datetime) -> datetime | None:
        """First regular-session open strictly in the future (ET)."""
        today = now_et.date()
        if _is_trading_day(today):
            open_dt = datetime.combine(today, _OPEN, tzinfo=_ET)
            if now_et < open_dt:
                return open_dt
        d = today + timedelta(days=1)
        for _ in range(370):
            if _is_trading_day(d):
                return datetime.combine(d, _OPEN, tzinfo=_ET)
            d = d + timedelta(days=1)
        return None

    def _next_close(self, now_et: datetime) -> datetime | None:
        """Close of the session in progress, else the next session's close."""
        today = now_et.date()
        if _is_trading_day(today):
            close_dt = datetime.combine(today, self._close_time_for(today), tzinfo=_ET)
            if now_et < close_dt:
                return close_dt
        d = today + timedelta(days=1)
        for _ in range(370):
            if _is_trading_day(d):
                return datetime.combine(d, self._close_time_for(d), tzinfo=_ET)
            d = d + timedelta(days=1)
        return None


# ---------------------------------------------------------------------------
# Alpaca live calendar (source of truth)
# ---------------------------------------------------------------------------

def _parse_clock_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _default_http_get(url: str, headers: dict) -> Any:
    import httpx  # lazy

    resp = httpx.get(url, headers=headers, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


class AlpacaMarketCalendar:
    """Live market calendar via Alpaca ``GET /v2/clock`` (trading host).

    Authoritative for DST, holidays, and early-close days.  The result is cached
    and only re-fetched when ``now >= cached.next_close`` (or the cache is empty)
    so the API is hit O(once per session boundary), not every iteration (C1).
    Falls back to ``OfflineMarketCalendar`` for any query that errors.
    """

    def __init__(self, config: Config, *, http_get: Any = None) -> None:
        self._base_url = config.alpaca_paper_base_url.rstrip("/")
        self._headers = {
            "APCA-API-KEY-ID": config.alpaca_api_key,
            "APCA-API-SECRET-KEY": config.alpaca_secret_key,
            "Accept": "application/json",
        }
        self._http_get = http_get if http_get is not None else _default_http_get
        self._offline = OfflineMarketCalendar()
        self._cached: MarketSession | None = None

    def session(self, now: datetime) -> MarketSession:
        # Reuse the cache only until the NEXT boundary for the cached STATE
        # passes. An OPEN session is valid until next_close; a CLOSED session
        # until next_open. (Bug fix: keying only on next_close meant a pre-open
        # fetch — is_open=False, next_close hours away — was reused all day, so a
        # long-running daemon never noticed the 09:30 open.)
        cached = self._cached
        if cached is not None:
            boundary = cached.next_close if cached.is_open else cached.next_open
            if boundary is not None and now < boundary:
                return cached

        try:
            data = self._http_get(f"{self._base_url}/v2/clock", self._headers)
        except Exception as exc:  # noqa: BLE001
            log.warning("market_calendar.clock_fetch_failed", error=str(exc))
            return self._offline.session(now)

        try:
            is_open = bool(data.get("is_open"))
            next_open = _parse_clock_dt(data.get("next_open"))
            next_close = _parse_clock_dt(data.get("next_close"))
        except Exception as exc:  # noqa: BLE001
            log.warning("market_calendar.clock_parse_failed", error=str(exc))
            return self._offline.session(now)

        session = MarketSession(is_open=is_open, next_open=next_open, next_close=next_close)
        self._cached = session
        return session
