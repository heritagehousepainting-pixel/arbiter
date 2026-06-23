"""Tests for data.beta — beta is fit on LOG returns (E5 FROZEN convention).

The outcome labeler applies beta to LOG returns, so beta_252d MUST be fit on
log returns for the convention to be consistent (mixing log-fit beta with
simple-return alpha leaks market direction).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from arbiter.data.beta import beta_252d
from arbiter.data.pit import FixtureSource, PITGateway

UTC = timezone.utc


def _ts(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def _build_pit(ticker_closes: list[float], spy_closes: list[float], as_of: datetime) -> PITGateway:
    """Register one close per trading-ish day ending as_of-1.

    We register consecutive CALENDAR days (the beta probe walks day-by-day and
    _align_returns dedups by date), enough to clear the _MIN_PAIRS=63 gate.
    """
    pit = PITGateway()
    src = FixtureSource()
    n = len(ticker_closes)
    # Place the series so the last point is as_of - 1 day.
    for i in range(n):
        day = as_of - timedelta(days=(n - i))
        src.add("price_close", ticker_closes_ticker, day, ticker_closes[i])
        src.add("price_close", "SPY", day, spy_closes[i])
    pit.register_source("price_close", src)
    return pit


ticker_closes_ticker = "ABC"


def _ols_log_beta(t: list[float], s: list[float]) -> float:
    rt = [math.log(t[i] / t[i - 1]) for i in range(1, len(t))]
    rs = [math.log(s[i] / s[i - 1]) for i in range(1, len(s))]
    mt = sum(rt) / len(rt)
    ms = sum(rs) / len(rs)
    cov = sum((rt[i] - mt) * (rs[i] - ms) for i in range(len(rt)))
    var = sum((rs[i] - ms) ** 2 for i in range(len(rs)))
    return cov / var


class TestBetaLogConvention:
    def test_beta_fit_on_log_returns(self) -> None:
        """beta_252d must equal the OLS slope of LOG returns (not simple)."""
        as_of = _ts(2025, 6, 1)
        # 120 days of mildly trending, noisy prices so var(SPY)>0 and beta != 1.
        spy = [400.0]
        tic = [100.0]
        for i in range(1, 120):
            spy.append(spy[-1] * (1.0 + 0.002 * ((i % 5) - 2)))
            # ticker moves ~1.5x SPY plus idiosyncratic wiggle.
            tic.append(tic[-1] * (1.0 + 0.003 * ((i % 5) - 2) + 0.0005 * ((i % 3) - 1)))

        pit = _build_pit(tic, spy, as_of)
        beta = beta_252d(ticker_closes_ticker, as_of, pit)

        expected_log_beta = _ols_log_beta(tic, spy)
        assert abs(beta - expected_log_beta) < 1e-6
        # And it should NOT equal the simple-return beta (sanity that convention matters).
        # (They are close but not identical; we only require the log match above.)

    def test_imputes_when_thin(self) -> None:
        as_of = _ts(2025, 6, 1)
        tic = [100.0, 101.0, 102.0]
        spy = [400.0, 401.0, 402.0]
        pit = _build_pit(tic, spy, as_of)
        beta = beta_252d(ticker_closes_ticker, as_of, pit)
        assert beta == 1.0  # < 63 pairs → imputed
