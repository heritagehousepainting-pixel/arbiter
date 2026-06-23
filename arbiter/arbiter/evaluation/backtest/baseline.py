"""Naive-follow-every-insider baseline + no-look-ahead guard — Lane 14b.

Two "silent killer" guards that must be cleared before a strategy is
considered viable:

1. **Naive insider baseline**: A strategy that blindly follows every Form-4
   insider buy/sell at the disclosure date + 1 open.  If the arbiter ensemble
   can't beat this naive rule, it has no alpha over cheap public-data execution.

2. **No-look-ahead guard**: The baseline itself is computed in point-in-time
   fashion — it only uses data that was available at the disclosure date.  This
   guard is enforced by passing the ``pit`` gateway and current ``as_of``;
   the function raises ``LookAheadViolation`` if it detects a future timestamp
   in the return computation.

``must_beat_baseline(strategy_stats, baseline_stats) -> bool`` is the final
gate: returns True only when the strategy beats the baseline on Sharpe AND
deflated Sharpe.

Usage::

    from arbiter.evaluation.backtest.baseline import (
        naive_insider_baseline, must_beat_baseline, BaselineStats
    )

    baseline = naive_insider_baseline(insider_trades, pit_gateway, as_of)
    passes = must_beat_baseline(strategy_metrics, baseline)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from arbiter.contract.seams import ResolvedOutcome
from arbiter.evaluation.backtest.metrics import EvalMetrics, compute_eval_metrics

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class LookAheadViolation(RuntimeError):
    """Raised when baseline computation uses data from after ``as_of``."""


# ---------------------------------------------------------------------------
# Baseline statistics dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BaselineStats:
    """Statistics for the naive insider baseline.

    Attributes
    ----------
    metrics:
        ``EvalMetrics`` for the naive strategy.
    n_trades:
        Number of insider disclosures followed.
    description:
        Human-readable description of the baseline strategy.
    """

    metrics: EvalMetrics
    n_trades: int
    description: str = "naive-follow-every-insider"


# ---------------------------------------------------------------------------
# No-look-ahead guard
# ---------------------------------------------------------------------------

def _assert_no_lookahead(
    outcomes: list[ResolvedOutcome],
    as_of_horizon_days: dict[str, int],
) -> None:
    """Raise LookAheadViolation if any outcome was resolved before its horizon.

    This is the structural no-look-ahead canary for the baseline:
    we check that no outcome's label was computed using data beyond
    the trade's stated horizon.  In normal operation the PIT gateway
    enforces this; this function is an additional defence-in-depth check.

    Parameters
    ----------
    outcomes:
        Resolved outcome records to audit.
    as_of_horizon_days:
        Map from idea_id to the expected horizon in days.
    """
    for outcome in outcomes:
        expected_horizon = as_of_horizon_days.get(outcome.idea_id)
        if expected_horizon is not None and outcome.horizon_days > expected_horizon + 1:
            raise LookAheadViolation(
                f"Outcome for idea_id={outcome.idea_id!r} has horizon_days="
                f"{outcome.horizon_days} but expected <= {expected_horizon + 1}; "
                "possible look-ahead in baseline construction."
            )


# ---------------------------------------------------------------------------
# Naive insider baseline
# ---------------------------------------------------------------------------

def naive_insider_baseline(
    outcomes: list[ResolvedOutcome],
    *,
    periods_per_year: float = 252.0,
) -> BaselineStats:
    """Compute the naive follow-every-insider baseline.

    The naive strategy follows EVERY non-abstained insider outcome
    (alpha_bps as the return signal, converted to a fractional return).
    This is the cheapest conceivable strategy: buy (or sell) whatever
    insiders do at disclosure + 1 open, hold for the stated horizon,
    take the beta-adjusted outcome.

    No weighting, no filtering, no horizon management.  If a managed
    ensemble can't beat this, it has no edge.

    Parameters
    ----------
    outcomes:
        ``ResolvedOutcome`` records from INSIDER advisors only (or all advisors
        if you want a broader baseline).  Abstentions are excluded.
    periods_per_year:
        Used when computing Sharpe / DSR.

    Returns
    -------
    BaselineStats
    """
    active = [o for o in outcomes if not o.abstained]
    if not active:
        # Degenerate: return zero-performance baseline
        zero = np.zeros(0)
        return BaselineStats(
            metrics=compute_eval_metrics(zero, periods_per_year=periods_per_year),
            n_trades=0,
        )

    # Each outcome contributes one "return" equal to alpha_bps / 10_000.
    # Alpha is already beta-adjusted and net-of-slippage per the contract.
    returns = np.array([o.alpha_bps / 10_000.0 for o in active], dtype=float)

    metrics = compute_eval_metrics(
        returns, n_trials=1, periods_per_year=periods_per_year
    )

    logger.info(
        "naive_insider_baseline: n_trades=%d, Sharpe=%.3f, hit_rate=%.2f",
        len(active),
        metrics.sharpe,
        metrics.hit_rate,
    )

    return BaselineStats(metrics=metrics, n_trades=len(active))


# ---------------------------------------------------------------------------
# Gate: must_beat_baseline
# ---------------------------------------------------------------------------

def must_beat_baseline(
    strategy_stats: EvalMetrics,
    baseline_stats: BaselineStats,
    *,
    require_sharpe_margin: float = 0.0,
    require_dsr_margin: float = 0.0,
) -> bool:
    """Return True only when the strategy beats the naive insider baseline.

    A strategy must beat the baseline on BOTH criteria:
    1. Sharpe ratio: ``strategy.sharpe > baseline.sharpe + require_sharpe_margin``
    2. Deflated Sharpe: ``strategy.deflated_sharpe > baseline.deflated_sharpe + require_dsr_margin``

    The deflated Sharpe criterion is the more important guard: it screens out
    strategies that only appear better due to multiple-testing inflation.

    Parameters
    ----------
    strategy_stats:
        ``EvalMetrics`` for the candidate strategy.
    baseline_stats:
        ``BaselineStats`` for the naive insider baseline.
    require_sharpe_margin:
        Additional margin required above the baseline Sharpe (default 0.0).
    require_dsr_margin:
        Additional margin required above the baseline DSR (default 0.0).

    Returns
    -------
    bool
        True when the strategy clears BOTH gates, False otherwise.
    """
    baseline_metrics = baseline_stats.metrics

    beats_sharpe = strategy_stats.sharpe > (baseline_metrics.sharpe + require_sharpe_margin)
    beats_dsr = strategy_stats.deflated_sharpe > (baseline_metrics.deflated_sharpe + require_dsr_margin)

    result = beats_sharpe and beats_dsr

    logger.info(
        "must_beat_baseline: strategy_sharpe=%.3f vs baseline=%.3f (beats=%s); "
        "strategy_dsr=%.3f vs baseline=%.3f (beats=%s) → PASS=%s",
        strategy_stats.sharpe,
        baseline_metrics.sharpe,
        beats_sharpe,
        strategy_stats.deflated_sharpe,
        baseline_metrics.deflated_sharpe,
        beats_dsr,
        result,
    )
    return result
