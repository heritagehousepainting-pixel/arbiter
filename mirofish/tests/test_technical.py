"""Tests for compute_technical (pure, deterministic)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from mirofish.evidence.technical import compute_technical
from mirofish.types import Bar


def _series(closes: list[float], *, volumes: list[float] | None = None,
            highs: list[float] | None = None, lows: list[float] | None = None,
            end: datetime | None = None) -> list[Bar]:
    """Build ascending daily bars ending at `end` (default as_of below)."""
    end = end or datetime(2026, 6, 1, tzinfo=timezone.utc)
    n = len(closes)
    vols = volumes or [1000.0] * n
    his = highs or closes
    los = lows or closes
    bars: list[Bar] = []
    for i, c in enumerate(closes):
        t = end - timedelta(days=(n - 1 - i))
        bars.append(Bar(t=t, o=c, h=his[i], l=los[i], c=c, v=vols[i]))
    return bars


def test_empty_bars_raises():
    with pytest.raises(ValueError):
        compute_technical([], datetime(2026, 6, 1, tzinfo=timezone.utc))


def test_basic_fields_and_n_bars():
    closes = [float(x) for x in range(1, 31)]  # 1..30 ascending
    bars = _series(closes)
    tf = compute_technical(bars, datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert tf.last_close == 30.0
    assert tf.n_bars == 30
    # momentum_20d = close[-1]/close[-21] - 1 = 30/10 - 1 = 2.0
    assert tf.momentum_20d == pytest.approx(30.0 / 10.0 - 1.0)


def test_ma50_set_ma200_none_under_200_bars():
    closes = [100.0] * 60
    tf = compute_technical(_series(closes), datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert tf.ma_50 == pytest.approx(100.0)
    assert tf.ma_200 is None
    assert tf.pct_vs_ma_50 == pytest.approx(0.0)
    assert tf.pct_vs_ma_200 is None


def test_ma200_set_with_220_bars():
    closes = [50.0] * 220
    tf = compute_technical(_series(closes), datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert tf.ma_200 == pytest.approx(50.0)
    assert tf.pct_vs_ma_200 == pytest.approx(0.0)


def test_rsi_all_gains_is_100():
    closes = [float(x) for x in range(1, 40)]  # strictly increasing
    tf = compute_technical(_series(closes), datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert tf.rsi_14 == pytest.approx(100.0)


def test_rsi_known_value():
    # Alternating +1/-1 deltas around a flat-ish series -> RSI ~ 50.
    closes = []
    price = 100.0
    for i in range(40):
        price += 1.0 if i % 2 == 0 else -1.0
        closes.append(price)
    tf = compute_technical(_series(closes), datetime(2026, 6, 1, tzinfo=timezone.utc))
    # Equal up/down magnitudes -> RSI hovers near (but not exactly) 50.
    assert tf.rsi_14 == pytest.approx(50.0, abs=5.0)


def test_realized_vol_zero_for_flat_series():
    closes = [100.0] * 40
    tf = compute_technical(_series(closes), datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert tf.realized_vol_annualized == pytest.approx(0.0)


def test_realized_vol_matches_hand_computation():
    # Build returns then derive closes; check stdev*sqrt(252).
    closes = []
    price = 100.0
    log_rets = []
    for i in range(25):
        factor = 1.01 if i % 2 == 0 else 0.99
        price *= factor
        closes.append(price)
        if i > 0:
            log_rets.append(math.log(closes[i] / closes[i - 1]))
    tf = compute_technical(_series(closes), datetime(2026, 6, 1, tzinfo=timezone.utc))
    # recompute expected over the trailing 20 returns
    rets = log_rets[-20:]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    expected = math.sqrt(var) * math.sqrt(252.0)
    assert tf.realized_vol_annualized == pytest.approx(expected)


def test_52w_high_low_and_volume_surge():
    closes = [10.0] * 30 + [20.0]  # last close 20, prior 10
    highs = [12.0] * 30 + [21.0]
    lows = [8.0] * 30 + [19.0]
    vols = [100.0] * 30 + [400.0]  # last volume 4x the trailing avg of 100
    tf = compute_technical(
        _series(closes, volumes=vols, highs=highs, lows=lows),
        datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    # 52w high = 21, last_close 20 -> pct_from_52w_high = 20/21 - 1 (negative)
    assert tf.pct_from_52w_high == pytest.approx(20.0 / 21.0 - 1.0)
    assert tf.pct_from_52w_high < 0
    # 52w low = 8, last_close 20 -> pct_from_52w_low = 20/8 - 1 (positive)
    assert tf.pct_from_52w_low == pytest.approx(20.0 / 8.0 - 1.0)
    assert tf.pct_from_52w_low > 0
    # volume surge = 400 / avg(trailing 20 vols = 100) = 4.0
    assert tf.volume_surge_ratio == pytest.approx(4.0)


def test_short_history_leaves_optional_fields_none():
    closes = [100.0, 101.0, 102.0]  # only 3 bars
    tf = compute_technical(_series(closes), datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert tf.last_close == 102.0
    assert tf.ma_50 is None
    assert tf.ma_200 is None
    assert tf.momentum_20d is None
    assert tf.rsi_14 is None
    assert tf.realized_vol_annualized is None
    assert tf.volume_surge_ratio is None
    # 52w fields still computable from available bars
    assert tf.pct_from_52w_high is not None
