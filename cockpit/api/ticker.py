"""Lazy ticker detail — company name + 1-month return for one symbol.

Called ONLY on accordion expand; never polled.  Degrades to null fields
on any Alpaca failure (HTTP 200 always returned, never 404 / 500).
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone

from .contract import TickerDetail
from .db import DEFAULT_DB_PATH

_ARBITER_PKG_ROOT = DEFAULT_DB_PATH.parents[1]  # <repo>/arbiter

# Alpaca asset names carry a verbose security-type suffix (e.g.
# "Apple Inc. Common Stock", "… Class A Common Stock") — strip it for display.
_NAME_SUFFIX_RE = re.compile(
    r"\s+(Class\s+[A-Z]\s+)?(Common Stock|Common Shares|Ordinary Shares)\s*$",
    re.IGNORECASE,
)


def _clean_name(raw: object) -> str | None:
    """Trim Alpaca's security-type suffix; return None for empty/non-str."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    return _NAME_SUFFIX_RE.sub("", raw).strip() or raw.strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_ticker_detail(symbol: str) -> TickerDetail:
    """Fetch company name + 1-month bars return for *symbol*.

    Returns a TickerDetail with null fields on any error — never raises.
    Symbol is always upper-cased in the returned DTO.
    """
    sym = symbol.strip().upper()
    if not sym:
        return TickerDetail(symbol=sym, as_of=_now())

    try:
        if str(_ARBITER_PKG_ROOT) not in sys.path:
            sys.path.insert(0, str(_ARBITER_PKG_ROOT))
        from arbiter.config import load_config  # noqa: PLC0415
        from arbiter.engine import build_executor  # noqa: PLC0415

        cfg = load_config()
        ex = build_executor(cfg)
        data_base = cfg.alpaca_data_base_url.rstrip("/")
    except Exception:
        return TickerDetail(symbol=sym, as_of=_now())

    # --- Company name via trading API ----------------------------------------
    name: str | None = None
    try:
        asset = ex.http_get(  # type: ignore[attr-defined]
            f"{ex._base()}/v2/assets/{sym}",  # type: ignore[attr-defined]
            ex._headers(),  # type: ignore[attr-defined]
        )
        if isinstance(asset, dict):
            name = _clean_name(asset.get("name"))
    except Exception:
        pass  # name stays None

    # --- 1-month bars via data API -------------------------------------------
    month_return_pct: float | None = None
    day_change_pct: float | None = None
    current_price: float | None = None
    try:
        start_iso = (datetime.now(timezone.utc) - timedelta(days=35)).strftime("%Y-%m-%d")
        feed = os.getenv("ALPACA_DATA_FEED", "iex")
        bars_url = (
            f"{data_base}/v2/stocks/{sym}/bars"
            f"?timeframe=1Day&start={start_iso}&feed={feed}&adjustment=all"
        )
        bars_resp = ex.http_get(bars_url, ex._headers())  # type: ignore[attr-defined]
        bars = (bars_resp or {}).get("bars") or []

        if len(bars) >= 2:
            ref_close = _f((bars[0] or {}).get("c"))
            prev_close = _f((bars[-2] or {}).get("c"))
            latest_close = _f((bars[-1] or {}).get("c"))
            if ref_close is not None and ref_close > 0 and latest_close is not None:
                month_return_pct = (latest_close - ref_close) / ref_close
                current_price = latest_close
            # day change = latest close vs the prior day's close
            if prev_close is not None and prev_close > 0 and latest_close is not None:
                day_change_pct = (latest_close - prev_close) / prev_close
    except Exception:
        pass  # month/day fields stay None

    return TickerDetail(
        symbol=sym,
        name=name,
        month_return_pct=month_return_pct,
        day_change_pct=day_change_pct,
        current_price=current_price,
        as_of=_now(),
    )
