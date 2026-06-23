"""Evaluation metrics — Lane 14b.

Implements:
- ``sharpe_ratio``        — classic annualised Sharpe
- ``deflated_sharpe_ratio`` — Bailey & López de Prado (2014) deflated Sharpe
                              that corrects for multiple-testing inflation
- ``max_drawdown``        — peak-to-trough drawdown on a returns series
- ``hit_rate``            — fraction of periods with positive return
- ``EvalMetrics``         — dataclass collecting all four metrics

Deflated Sharpe background
--------------------------
When a strategy is selected from N candidate strategies (or N parameter sets),
the best observed Sharpe overestimates true skill.  Bailey & López de Prado
(2014) derive a deflated Sharpe Ratio (DSR) that is the probability that the
true Sharpe exceeds zero after accounting for:
  1. The number of independent trials (strategies / folds) examined
  2. The skewness and excess kurtosis of the returns distribution

DSR ∈ (0, 1).  A value < 0.95 indicates the observed Sharpe is probably
luck rather than skill.

Reference: Bailey, D. H., & López de Prado, M. (2014).
           "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest
           Overfitting and Non-Normality." Journal of Portfolio Management, 40(5).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import stats


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvalMetrics:
    """Aggregate evaluation metrics for a strategy or window.

    Attributes
    ----------
    sharpe:
        Annualised Sharpe ratio (Rf = 0).
    deflated_sharpe:
        DSR ∈ (0, 1) — probability the true Sharpe > 0 after multiple-testing.
        Requires ``n_trials >= 1``.
    max_drawdown:
        Maximum peak-to-trough drawdown (negative, e.g. -0.15 = 15% loss).
    hit_rate:
        Fraction of periods with positive return.
    n_obs:
        Number of return observations used.
    """

    sharpe: float
    deflated_sharpe: float
    max_drawdown: float
    hit_rate: float
    n_obs: int


# ---------------------------------------------------------------------------
# Sharpe ratio
# ---------------------------------------------------------------------------

def sharpe_ratio(
    returns: np.ndarray,
    *,
    periods_per_year: float = 252.0,
) -> float:
    """Annualised Sharpe ratio (Rf = 0).

    Parameters
    ----------
    returns:
        1-D array of periodic returns (e.g. daily P&L / portfolio value).
    periods_per_year:
        Trading periods per year (252 for daily, 52 for weekly, 12 for monthly).

    Returns
    -------
    float
        Annualised Sharpe.  Returns 0.0 when std dev is zero.
    """
    returns = np.asarray(returns, dtype=float)
    if len(returns) == 0:
        return 0.0
    std = float(np.std(returns, ddof=1))
    # Guard against numerical near-zero std (e.g. constant series) to avoid
    # divide-by-near-zero producing astronomically large values.
    if std < 1e-12:
        return 0.0
    mean = float(np.mean(returns))
    return mean / std * math.sqrt(periods_per_year)


# ---------------------------------------------------------------------------
# Deflated Sharpe Ratio  (Bailey & López de Prado 2014)
# ---------------------------------------------------------------------------

def deflated_sharpe_ratio(
    returns: np.ndarray,
    *,
    n_trials: int = 1,
    periods_per_year: float = 252.0,
    benchmark_sharpe: float | None = None,
) -> float:
    """Deflated Sharpe Ratio (DSR).

    Probability that the true Sharpe exceeds zero, corrected for
    selection bias from multiple-testing over ``n_trials`` independent
    strategies / parameter sets.

    Algorithm (Bailey & López de Prado 2014, equations 8–11):
    1. Compute the observed annualised Sharpe ``SR_hat``.
    2. Compute the expected maximum Sharpe under the null (all N strategies
       have SR = 0) via the Euler–Mascheroni correction:
           E[max SR] ≈ ((1 - γ) Φ⁻¹(1 - 1/N) + γ Φ⁻¹(1 - 1/(N·e))) / √(T-1)
       where γ ≈ 0.5772 (Euler–Mascheroni constant) and T = len(returns).
    3. Adjust for non-normality using skewness (μ₃) and excess kurtosis (μ₄):
           SR_hat* = SR_hat · √(1 - skew·SR_hat + (kurt-1)/4 · SR_hat²)
           (de-annualised first, then re-annualised after correction)
    4. DSR = Φ((SR_hat* - E[max SR]) · √(T-1))

    Parameters
    ----------
    returns:
        1-D array of periodic returns.
    n_trials:
        Number of independent strategies / parameter-set evaluations compared.
        Must be >= 1.  Larger values → smaller DSR (more penalisation).
    periods_per_year:
        Used to annualise the final DSR threshold.
    benchmark_sharpe:
        When provided, test against this benchmark rather than zero.
        Defaults to 0.0.

    Returns
    -------
    float
        DSR ∈ (0, 1).  Returns 0.0 when the series is degenerate.
    """
    returns = np.asarray(returns, dtype=float)
    T = len(returns)
    if T < 2:
        return 0.0

    sr_hat = sharpe_ratio(returns, periods_per_year=periods_per_year)
    # De-annualise for per-period calculation
    sr_hat_per_period = sr_hat / math.sqrt(periods_per_year)

    std = float(np.std(returns, ddof=1))
    if std == 0.0:
        return 0.0

    # Skewness and excess kurtosis of returns
    skew = float(stats.skew(returns))
    kurt_excess = float(stats.kurtosis(returns))  # Fisher: excess kurtosis (normal = 0)

    # Non-normality adjustment (applied to per-period SR)
    # Bailey & LdP eq. 9:  SR* = SR * sqrt(1 - gamma3*SR + (gamma4-1)/4 * SR^2)
    # where gamma3 = skewness, gamma4 = excess kurtosis + 3 (but using excess form it's (kurt_excess+2)/4)
    # Using the simplified form from the paper (with excess kurtosis):
    correction_factor = 1.0 - skew * sr_hat_per_period + ((kurt_excess + 2.0) / 4.0) * (sr_hat_per_period ** 2)
    if correction_factor <= 0.0:
        # Guard: degenerate distribution
        return 0.0
    sr_star = sr_hat_per_period * math.sqrt(correction_factor)

    # Expected maximum SR under the null (all n_trials strategies have SR=0)
    # Bailey & LdP eq. 8 using Euler-Mascheroni γ ≈ 0.5772156649
    EULER_MASCHERONI = 0.5772156649015328
    if n_trials == 1:
        # No multiple-testing penalty when only one strategy is evaluated
        e_max_sr = 0.0
    else:
        e_max_sr = (
            (1.0 - EULER_MASCHERONI) * stats.norm.ppf(1.0 - 1.0 / n_trials)
            + EULER_MASCHERONI * stats.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
        ) / math.sqrt(T - 1)

    bench = (benchmark_sharpe / math.sqrt(periods_per_year)) if benchmark_sharpe is not None else 0.0

    # DSR = Φ( (SR* − E[max SR] − bench) × √(T−1) )
    dsr_arg = (sr_star - e_max_sr - bench) * math.sqrt(T - 1)
    return float(stats.norm.cdf(dsr_arg))


# ---------------------------------------------------------------------------
# Max drawdown
# ---------------------------------------------------------------------------

def max_drawdown(returns: np.ndarray) -> float:
    """Maximum peak-to-trough drawdown on a returns series.

    Parameters
    ----------
    returns:
        1-D array of periodic returns.

    Returns
    -------
    float
        Maximum drawdown expressed as a negative fraction (e.g. -0.20 = 20%
        peak-to-trough loss).  Returns 0.0 when ``returns`` is empty.
    """
    returns = np.asarray(returns, dtype=float)
    if len(returns) == 0:
        return 0.0

    # Cumulative wealth index (starting at 1.0)
    wealth = np.cumprod(1.0 + returns)
    peak = np.maximum.accumulate(wealth)
    drawdowns = wealth / peak - 1.0
    return float(np.min(drawdowns))


# ---------------------------------------------------------------------------
# Hit rate
# ---------------------------------------------------------------------------

def hit_rate(returns: np.ndarray) -> float:
    """Fraction of return periods that are strictly positive.

    Parameters
    ----------
    returns:
        1-D array of periodic returns.

    Returns
    -------
    float
        Hit rate ∈ [0, 1].  Returns 0.0 for empty input.
    """
    returns = np.asarray(returns, dtype=float)
    if len(returns) == 0:
        return 0.0
    return float(np.mean(returns > 0.0))


# ---------------------------------------------------------------------------
# Composite builder
# ---------------------------------------------------------------------------

def compute_eval_metrics(
    returns: np.ndarray,
    *,
    n_trials: int = 1,
    periods_per_year: float = 252.0,
) -> EvalMetrics:
    """Compute all four metrics and return an ``EvalMetrics`` bundle.

    Parameters
    ----------
    returns:
        1-D array of periodic returns.
    n_trials:
        Passed to ``deflated_sharpe_ratio``.
    periods_per_year:
        Used for both Sharpe and DSR.

    Returns
    -------
    EvalMetrics
    """
    returns = np.asarray(returns, dtype=float)
    return EvalMetrics(
        sharpe=sharpe_ratio(returns, periods_per_year=periods_per_year),
        deflated_sharpe=deflated_sharpe_ratio(
            returns, n_trials=n_trials, periods_per_year=periods_per_year
        ),
        max_drawdown=max_drawdown(returns),
        hit_rate=hit_rate(returns),
        n_obs=len(returns),
    )
