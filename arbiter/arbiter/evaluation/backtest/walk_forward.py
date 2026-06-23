"""Walk-forward evaluation — Lane 14b.

Splits a return series into N non-overlapping windows (or expanding windows)
and evaluates each on the out-of-sample period following the in-sample training
period.  Returns per-window and aggregate metrics.

Walk-forward prevents look-ahead by evaluating each window sequentially:
the model is "trained" on [t0, t1) and evaluated on [t1, t2).

Usage::

    from arbiter.evaluation.backtest.walk_forward import walk_forward_eval

    result = walk_forward_eval(
        returns=my_daily_returns,        # np.ndarray
        n_windows=10,
        min_train_periods=60,
        eval_periods=20,
    )
    print(result.aggregate_metrics)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from arbiter.evaluation.backtest.metrics import EvalMetrics, compute_eval_metrics

logger = logging.getLogger(__name__)

# Minimum number of windows the API accepts (spec: 5+)
_MIN_WINDOWS = 5


@dataclass(frozen=True)
class WindowResult:
    """Metrics for a single walk-forward window.

    Attributes
    ----------
    window_index:
        0-based window number.
    train_start:
        Inclusive start index of the training period.
    train_end:
        Exclusive end index of the training period.
    eval_start:
        Inclusive start index of the evaluation period.
    eval_end:
        Exclusive end index of the evaluation period.
    in_sample_metrics:
        Metrics computed on the training period.
    out_of_sample_metrics:
        Metrics computed on the evaluation (test) period.
    """

    window_index: int
    train_start: int
    train_end: int
    eval_start: int
    eval_end: int
    in_sample_metrics: EvalMetrics
    out_of_sample_metrics: EvalMetrics


@dataclass
class WalkForwardResult:
    """Aggregated walk-forward evaluation result.

    Attributes
    ----------
    windows:
        Per-window results (in time order).
    aggregate_metrics:
        Metrics computed across all out-of-sample return periods concatenated.
    n_windows:
        Number of windows evaluated.
    """

    windows: list[WindowResult] = field(default_factory=list)
    aggregate_metrics: EvalMetrics | None = None

    @property
    def n_windows(self) -> int:
        """Number of walk-forward windows."""
        return len(self.windows)

    @property
    def mean_oos_sharpe(self) -> float:
        """Mean out-of-sample Sharpe across all windows."""
        if not self.windows:
            return 0.0
        return float(np.mean([w.out_of_sample_metrics.sharpe for w in self.windows]))

    @property
    def mean_oos_hit_rate(self) -> float:
        """Mean out-of-sample hit rate across all windows."""
        if not self.windows:
            return 0.0
        return float(np.mean([w.out_of_sample_metrics.hit_rate for w in self.windows]))


def walk_forward_eval(
    returns: np.ndarray,
    *,
    n_windows: int = 5,
    min_train_periods: int = 60,
    eval_periods: int = 20,
    expanding: bool = False,
    periods_per_year: float = 252.0,
) -> WalkForwardResult:
    """Walk-forward evaluation over a return series.

    Splits the series into ``n_windows`` sequential windows.  Each window
    trains on an in-sample period and evaluates on the following
    ``eval_periods`` out-of-sample steps.

    Parameters
    ----------
    returns:
        1-D array of periodic returns (e.g. daily P&L / NAV).
    n_windows:
        Number of walk-forward windows.  Must be >= ``_MIN_WINDOWS`` (5).
        Raises ``ValueError`` otherwise.
    min_train_periods:
        Minimum number of return observations required in the training period
        of each window.  Raises ``ValueError`` if the series is too short.
    eval_periods:
        Number of periods per out-of-sample window.
    expanding:
        When True, each window's training period grows to include all
        preceding data (expanding window).  When False (default), windows
        use a fixed-width rolling training period.
    periods_per_year:
        Passed to ``compute_eval_metrics``.

    Returns
    -------
    WalkForwardResult
        Per-window and aggregate metrics.

    Raises
    ------
    ValueError
        If ``n_windows < 5`` or the series is too short.
    """
    returns = np.asarray(returns, dtype=float)
    n = len(returns)

    if n_windows < _MIN_WINDOWS:
        raise ValueError(
            f"walk_forward_eval requires n_windows >= {_MIN_WINDOWS}, got {n_windows}"
        )

    total_required = min_train_periods + n_windows * eval_periods
    if n < total_required:
        raise ValueError(
            f"Series too short: need at least {total_required} observations for "
            f"n_windows={n_windows}, min_train={min_train_periods}, "
            f"eval_periods={eval_periods}; got {n}"
        )

    result = WalkForwardResult()
    all_oos_returns: list[float] = []

    for i in range(n_windows):
        if expanding:
            # Expanding window: training always starts at 0 and grows.
            train_start = 0
            train_end = min_train_periods + i * eval_periods
        else:
            # Rolling window: each training window is the same width.
            train_end = min_train_periods + i * eval_periods
            train_start = train_end - min_train_periods

        eval_start = train_end
        eval_end = eval_start + eval_periods

        if eval_end > n:
            # Clamp last window to available data
            eval_end = n

        train_returns = returns[train_start:train_end]
        oos_returns = returns[eval_start:eval_end]

        if len(oos_returns) == 0:
            logger.warning("walk_forward_eval: window %d has empty OOS slice, skipping", i)
            continue

        is_metrics = compute_eval_metrics(
            train_returns, n_trials=n_windows, periods_per_year=periods_per_year
        )
        oos_metrics = compute_eval_metrics(
            oos_returns, n_trials=n_windows, periods_per_year=periods_per_year
        )

        result.windows.append(
            WindowResult(
                window_index=i,
                train_start=train_start,
                train_end=train_end,
                eval_start=eval_start,
                eval_end=eval_end,
                in_sample_metrics=is_metrics,
                out_of_sample_metrics=oos_metrics,
            )
        )

        all_oos_returns.extend(oos_returns.tolist())

    if all_oos_returns:
        result.aggregate_metrics = compute_eval_metrics(
            np.array(all_oos_returns),
            n_trials=n_windows,
            periods_per_year=periods_per_year,
        )

    logger.info(
        "walk_forward_eval: %d windows, aggregate OOS Sharpe=%.3f",
        result.n_windows,
        result.aggregate_metrics.sharpe if result.aggregate_metrics else float("nan"),
    )
    return result
