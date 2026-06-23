"""Deterministic technical (price-action) features from daily bars.

Pure: given bars already filtered to <= as_of and sorted ascending, compute
every TechnicalFeatures field. None where history is insufficient. Raises
ValueError ONLY when bars is empty (the caller degrades to no technical view).

ISOLATION: pure stdlib + mirofish.types. Never imports arbiter.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from mirofish.types import Bar, TechnicalFeatures, ensure_utc


def _simple_ma(closes: list[float], window: int) -> float | None:
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def _wilder_rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI over `period`. Needs >= period+1 closes."""
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for prev, cur in zip(closes[:-1], closes[1:]):
        delta = cur - prev
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    # Seed with the simple average of the first `period` deltas.
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Wilder smoothing across the remaining deltas.
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _realized_vol_annualized(closes: list[float], window: int = 20) -> float | None:
    """stdev(daily log-returns over `window`) * sqrt(252). Needs window+1 closes."""
    if len(closes) < window + 1:
        return None
    rets: list[float] = []
    for prev, cur in zip(closes[-(window + 1):-1], closes[-window:]):
        if prev <= 0.0 or cur <= 0.0:
            return None
        rets.append(math.log(cur / prev))
    n = len(rets)
    if n < 2:
        return None
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)  # sample stdev
    return math.sqrt(var) * math.sqrt(252.0)


def compute_technical(bars: list[Bar], as_of: datetime) -> TechnicalFeatures:
    """Compute every TechnicalFeatures field from bars <= as_of (ascending).

    None where history is insufficient. ValueError ONLY if bars is empty.
    """
    if not bars:
        raise ValueError("compute_technical requires at least one bar")

    as_of = ensure_utc(as_of)
    closes = [b.c for b in bars]
    volumes = [b.v for b in bars]
    last_close = closes[-1]
    n_bars = len(bars)

    ma_50 = _simple_ma(closes, 50)
    ma_200 = _simple_ma(closes, 200)
    pct_vs_ma_50 = (last_close / ma_50 - 1.0) if ma_50 else None
    pct_vs_ma_200 = (last_close / ma_200 - 1.0) if ma_200 else None

    # 20-trading-day momentum: close_t / close_t-20 - 1.
    momentum_20d: float | None = None
    if len(closes) >= 21:
        ref = closes[-21]
        if ref > 0.0:
            momentum_20d = last_close / ref - 1.0

    rsi_14 = _wilder_rsi(closes, 14)
    realized_vol_annualized = _realized_vol_annualized(closes, 20)

    # 52-week window: bars with t within the trailing 365 days of as_of.
    cutoff = as_of - timedelta(days=365)
    window_bars = [b for b in bars if b.t >= cutoff]
    if not window_bars:
        window_bars = bars
    highs_52w = max(b.h for b in window_bars)
    lows_52w = min(b.l for b in window_bars)
    pct_from_52w_high = (last_close / highs_52w - 1.0) if highs_52w > 0.0 else None
    pct_from_52w_low = (last_close / lows_52w - 1.0) if lows_52w > 0.0 else None

    # Volume surge: last volume / avg(trailing 20d volume, excluding last).
    volume_surge_ratio: float | None = None
    if len(volumes) >= 21:
        trailing = volumes[-21:-1]
        avg_vol = sum(trailing) / len(trailing)
        if avg_vol > 0.0:
            volume_surge_ratio = volumes[-1] / avg_vol

    return TechnicalFeatures(
        last_close=last_close,
        ma_50=ma_50,
        ma_200=ma_200,
        pct_vs_ma_50=pct_vs_ma_50,
        pct_vs_ma_200=pct_vs_ma_200,
        momentum_20d=momentum_20d,
        rsi_14=rsi_14,
        realized_vol_annualized=realized_vol_annualized,
        pct_from_52w_high=pct_from_52w_high,
        pct_from_52w_low=pct_from_52w_low,
        volume_surge_ratio=volume_surge_ratio,
        n_bars=n_bars,
    )
