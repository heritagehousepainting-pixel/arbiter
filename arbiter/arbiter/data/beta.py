"""252-day rolling beta computation — Lane 3 core.

Implements INTERFACES.md §3::

    def beta_252d(ticker, as_of, pit) -> float

Beta = slope of OLS regression of ticker daily returns on SPY daily returns
over 252 trading days ending as_of−1.

- Uses PIT price bars fetched via ``pit.get("price_close", ...)`` to avoid
  look-ahead (no ``datetime.now()`` calls).
- Imputes 1.0 and logs a flag when fewer than 63 usable return pairs are
  available (design spec §4.2 / INTERFACES §6).

See design spec §4.1 for the outcome-label formula that uses this value:
    alpha_i = R_i(t0,t1) − beta_i × R_SPY(t0,t1)
    beta_i = 252-day rolling beta as of t0−1 (impute 1.0 + flag if unavailable)
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

from arbiter.data.pit import PITGateway

_logger = logging.getLogger(__name__)

# Minimum number of usable return pairs before we impute.
_MIN_PAIRS = 63


def beta_252d(ticker: str, as_of: datetime, pit: PITGateway) -> float:
    """Compute 252-day rolling beta vs SPY ending as_of−1.

    The window ends one day before ``as_of`` to prevent look-ahead:
        window = [as_of − 253 calendar days, as_of − 1 day]

    We fetch daily close prices for both the ticker and SPY via the PIT
    gateway, compute daily log-returns, and run OLS.

    Parameters
    ----------
    ticker:
        Exchange ticker symbol to compute beta for.
    as_of:
        Information cutoff date.  Beta window ends one day before this.
    pit:
        PITGateway instance — all price reads go through here.

    Returns
    -------
    float
        Beta coefficient.  Returns 1.0 (imputed) if insufficient data.
    """
    # Window ends as_of − 1 day (no look-ahead per spec).
    end_exclusive = as_of
    # We need 252 returns → 253 price points; add buffer for weekends/holidays.
    start = as_of - timedelta(days=400)

    ticker_prices = _fetch_closes(ticker, start, end_exclusive, pit)
    spy_prices = _fetch_closes("SPY", start, end_exclusive, pit)

    if not ticker_prices or not spy_prices:
        _logger.warning(
            "beta_252d: no price data for %s or SPY as of %s — imputing 1.0",
            ticker,
            as_of,
        )
        return 1.0

    # Align by date (use only dates present in both series, up to 252 bars).
    aligned = _align_returns(ticker_prices, spy_prices)

    if len(aligned) < _MIN_PAIRS:
        _logger.warning(
            "beta_252d: only %d usable return pairs for %s as of %s "
            "(need %d) — imputing 1.0",
            len(aligned),
            ticker,
            as_of,
            _MIN_PAIRS,
        )
        return 1.0

    # OLS beta = cov(r_ticker, r_spy) / var(r_spy)
    ticker_rets = [r[0] for r in aligned]
    spy_rets = [r[1] for r in aligned]

    return _ols_beta(ticker_rets, spy_rets)


def _fetch_closes(
    ticker: str,
    start: datetime,
    end_exclusive: datetime,
    pit: PITGateway,
) -> list[tuple[datetime, float]]:
    """Fetch PIT close prices as a sorted list of (timestamp, close) pairs.

    Uses sequential daily probing via the PIT gateway since we don't have
    a bars() call at this layer (Wave-B clients supply bars; in unit tests
    FixtureSource supplies close prices via the "price_close" field).

    In practice, Wave-B clients will register a source for "price_close"
    that returns a list of Bar objects indexed by timestamp.  We probe
    the gateway once per day in the window.

    Note: This approach is intentionally simple for Phase 1 / test use.
    Wave-B lane may optimise by returning bar lists directly.
    """
    results: list[tuple[datetime, float]] = []

    # Walk day-by-day collecting the close for each calendar day.
    # PITGateway returns None when data is unavailable for that as_of.
    cursor = start
    one_day = timedelta(days=1)

    # The loop condition ``cursor < end_exclusive`` (where end_exclusive = as_of)
    # means the LAST day probed is as_of − 1 day, never as_of itself.
    # This is intentional: the beta window must end one day before as_of to
    # prevent look-ahead (spec §4.2, INTERFACES.md §3 — "beta_i = 252-day
    # rolling as of t0−1").  Do NOT change this to ``cursor <= end_exclusive``
    # without updating the caller to pass ``as_of − 1`` explicitly.
    while cursor < end_exclusive:
        value = pit.get("price_close", ticker, cursor)
        if value is not None:
            try:
                close = float(value)  # type: ignore[arg-type]
                results.append((cursor, close))
            except (TypeError, ValueError):
                pass
        cursor = cursor + one_day

    return results


def _align_returns(
    ticker_prices: list[tuple[datetime, float]],
    spy_prices: list[tuple[datetime, float]],
) -> list[tuple[float, float]]:
    """Compute aligned log-returns for ticker and SPY, up to 252 pairs.

    Returns list of (ticker_log_return, spy_log_return) pairs.
    """
    # Build price dicts keyed by date (normalize to date only)
    ticker_map = {ts.date(): price for ts, price in ticker_prices}
    spy_map = {ts.date(): price for ts, price in spy_prices}

    # Common dates, sorted
    common_dates = sorted(set(ticker_map.keys()) & set(spy_map.keys()))

    # Need at least 2 dates to compute returns
    if len(common_dates) < 2:
        return []

    # Limit to last 253 dates (to get up to 252 returns)
    common_dates = common_dates[-253:]

    returns = []
    for i in range(1, len(common_dates)):
        d_prev = common_dates[i - 1]
        d_curr = common_dates[i]

        t_prev = ticker_map[d_prev]
        t_curr = ticker_map[d_curr]
        s_prev = spy_map[d_prev]
        s_curr = spy_map[d_curr]

        # Guard against zero or negative prices
        if t_prev <= 0 or t_curr <= 0 or s_prev <= 0 or s_curr <= 0:
            continue

        r_ticker = math.log(t_curr / t_prev)
        r_spy = math.log(s_curr / s_prev)
        returns.append((r_ticker, r_spy))

    return returns


def _ols_beta(ticker_rets: list[float], spy_rets: list[float]) -> float:
    """Compute OLS beta = cov(ticker, spy) / var(spy)."""
    n = len(ticker_rets)
    if n < 2:
        return 1.0

    mean_t = sum(ticker_rets) / n
    mean_s = sum(spy_rets) / n

    cov = sum(
        (ticker_rets[i] - mean_t) * (spy_rets[i] - mean_s)
        for i in range(n)
    )
    var_s = sum((spy_rets[i] - mean_s) ** 2 for i in range(n))

    if var_s == 0.0:
        _logger.warning("beta_252d: SPY variance is zero — imputing 1.0")
        return 1.0

    return cov / var_s
