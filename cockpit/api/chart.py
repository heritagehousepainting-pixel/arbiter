"""Live OHLCV chart data for one ticker, sourced from the Alpaca data API.

Served at GET /chart/{symbol}?range=live|5d|1m|3m|6m
Fail-closed: any Alpaca failure returns an empty ChartSeries (alpaca_ok=False).
Never raises, never returns 404.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .contract import Candle, ChartSeries
from .db import DEFAULT_DB_PATH

_ARBITER_PKG_ROOT = DEFAULT_DB_PATH.parents[1]  # <repo>/arbiter
_ET = ZoneInfo("America/New_York")

# --- In-process TTL cache (key=(symbol, range_), value=(ChartSeries, monotonic_ts)) ---
_CACHE: dict[tuple[str, str], tuple[ChartSeries, float]] = {}
_TTL: dict[str, float] = {
    "live": 60.0,
    "5d": 180.0,
    "1m": 600.0,
    "3m": 600.0,
    "6m": 600.0,
}
_VALID_RANGES = frozenset({"live", "5d", "1m", "3m", "6m"})
_MAX_PAGES = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _classify_session(bar_t: str) -> str:
    """Classify a bar's trading session from its UTC ISO timestamp.

    DST-correct via zoneinfo America/New_York.
    Returns "pre" (04:00–09:30 ET), "regular" (09:30–16:00 ET),
    "post" (16:00–20:00 ET), or "regular" as a fallback.
    """
    try:
        # Accept both 'Z' suffix and explicit '+00:00' offset
        ts = bar_t.replace("Z", "+00:00")
        dt_utc = datetime.fromisoformat(ts)
        dt_et = dt_utc.astimezone(_ET)
        t = dt_et.time()
        from datetime import time as dtime  # noqa: PLC0415
        PRE_START = dtime(4, 0)
        REG_START = dtime(9, 30)
        REG_END = dtime(16, 0)
        POST_END = dtime(20, 0)
        if PRE_START <= t < REG_START:
            return "pre"
        elif REG_START <= t < REG_END:
            return "regular"
        elif REG_END <= t < POST_END:
            return "post"
        else:
            return "regular"  # midnight / overnight → fallback
    except Exception:
        return "regular"


def _build_range_params(range_: str, feed: str) -> dict[str, str]:
    """Return Alpaca bars query parameters for the requested range."""
    now_utc = datetime.now(timezone.utc)

    if range_ == "live":
        # Start at today 04:00 ET (pre-market open), converted to UTC
        now_et = datetime.now(_ET)
        today_et = now_et.date()
        start_et = datetime(
            today_et.year, today_et.month, today_et.day, 4, 0, 0, tzinfo=_ET
        )
        start_utc = start_et.astimezone(timezone.utc)
        return {
            "timeframe": "5Min",
            "extended_hours": "true",
            "start": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "feed": feed,
        }
    elif range_ == "5d":
        return {
            "timeframe": "15Min",
            "start": (now_utc - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "feed": feed,
        }
    elif range_ == "1m":
        return {
            "timeframe": "1Day",
            "adjustment": "all",
            "start": (now_utc - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "feed": feed,
        }
    elif range_ == "3m":
        return {
            "timeframe": "1Day",
            "adjustment": "all",
            "start": (now_utc - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "feed": feed,
        }
    else:  # 6m
        return {
            "timeframe": "1Day",
            "adjustment": "all",
            "start": (now_utc - timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "feed": feed,
        }


def build_chart_series(symbol: str, range_: str) -> ChartSeries:
    """Fetch OHLCV bars for *symbol* over the requested *range_*.

    Returns a ChartSeries with candles=[] and alpaca_ok=False on any failure.
    Never raises. Symbol is always upper-cased in the returned DTO.
    """
    sym = symbol.strip().upper()
    if not sym:
        return ChartSeries(symbol=sym, range=range_, as_of=_now())

    # Coerce invalid range to "live"
    if range_ not in _VALID_RANGES:
        range_ = "live"

    # --- TTL cache check -------------------------------------------------------
    cache_key = (sym, range_)
    ttl = _TTL.get(range_, 60.0)
    cached = _CACHE.get(cache_key)
    if cached is not None:
        series, ts = cached
        if time.monotonic() - ts < ttl:
            return series

    # --- Build executor (same lazy-import pattern as ticker.py) ----------------
    try:
        if str(_ARBITER_PKG_ROOT) not in sys.path:
            sys.path.insert(0, str(_ARBITER_PKG_ROOT))
        from arbiter.config import load_config  # noqa: PLC0415
        from arbiter.engine import build_executor  # noqa: PLC0415

        cfg = load_config()
        ex = build_executor(cfg)
        data_base = cfg.alpaca_data_base_url.rstrip("/")
    except Exception:
        return ChartSeries(symbol=sym, range=range_, as_of=_now())

    # --- Fetch bars (with single-level pagination) ------------------------------
    try:
        feed = os.getenv("ALPACA_DATA_FEED", "iex")
        params = _build_range_params(range_, feed)
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        base_url = f"{data_base}/v2/stocks/{sym}/bars?{qs}"

        all_bars: list[dict] = []
        pages = 0
        next_token: str | None = None
        while pages < _MAX_PAGES:
            if next_token:
                fetch_url = f"{base_url}&page_token={next_token}"
            else:
                fetch_url = base_url
            resp = ex.http_get(fetch_url, ex._headers())  # type: ignore[attr-defined]
            resp = resp or {}
            raw_bars = resp.get("bars") or []
            all_bars.extend(raw_bars)
            pages += 1
            next_token = resp.get("next_page_token") or None
            if not next_token:
                break

        candles: list[Candle] = []
        for bar in all_bars:
            if not isinstance(bar, dict):
                continue
            t_str = str(bar.get("t", ""))
            candles.append(
                Candle(
                    t=t_str,
                    o=float(bar.get("o", 0.0)),
                    h=float(bar.get("h", 0.0)),
                    l=float(bar.get("l", 0.0)),
                    c=float(bar.get("c", 0.0)),
                    v=float(bar.get("v", 0.0)),
                    session=_classify_session(t_str),
                )
            )

        extended_available = any(c.session in ("pre", "post") for c in candles)

        series = ChartSeries(
            symbol=sym,
            range=range_,
            candles=candles,
            extended_available=extended_available,
            as_of=_now(),
            alpaca_ok=True,
        )
        _CACHE[cache_key] = (series, time.monotonic())
        return series

    except Exception:
        return ChartSeries(
            symbol=sym,
            range=range_,
            candles=[],
            extended_available=False,
            as_of=_now(),
            alpaca_ok=False,
        )
