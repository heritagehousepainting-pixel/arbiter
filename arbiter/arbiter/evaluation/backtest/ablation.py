"""Leave-one-out advisor ablation — Lane 14b.

For each advisor in the pool, drops that advisor from the ensemble and
recomputes evaluation metrics on the resulting reduced set.  An advisor
earns its place when removing it *degrades* performance (lower Sharpe,
lower hit-rate, etc.).

The ablation accepts a ``labeler`` callable and a ``run_cycle`` callable as
parameters — it does NOT import either directly (per lane boundary rules).

Usage::

    from arbiter.evaluation.backtest.ablation import leave_one_out_ablation

    report = leave_one_out_ablation(
        advisor_ids=["A1.insider", "A1.congress", "A2.mirofish"],
        outcome_records=list_of_resolved_outcomes,
        compute_returns=my_returns_fn,   # (outcomes, excluded_id) -> np.ndarray
    )
    for item in report.items:
        print(item.excluded_advisor, item.delta_sharpe)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from arbiter.contract.seams import ResolvedOutcome
from arbiter.evaluation.backtest.metrics import EvalMetrics, compute_eval_metrics

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AblationItem:
    """Single leave-one-out ablation result.

    Attributes
    ----------
    excluded_advisor:
        The advisor_id that was removed for this ablation trial.
    full_metrics:
        Metrics with all advisors included.
    ablated_metrics:
        Metrics with the excluded advisor removed.
    delta_sharpe:
        ``full_metrics.sharpe - ablated_metrics.sharpe``.
        Positive → removing this advisor hurt (it earns its place).
        Negative → removing this advisor helped (dead weight or noise).
    earns_place:
        True when ``delta_sharpe >= min_delta_sharpe``.
    """

    excluded_advisor: str
    full_metrics: EvalMetrics
    ablated_metrics: EvalMetrics
    delta_sharpe: float
    earns_place: bool


@dataclass
class AblationReport:
    """Full leave-one-out ablation report.

    Attributes
    ----------
    items:
        Per-advisor ablation results.
    full_metrics:
        Baseline metrics with all advisors.
    min_delta_sharpe:
        The threshold used to determine ``earns_place``.
    advisors_earning_place:
        Subset of advisor_ids that cleared the threshold.
    advisors_not_earning_place:
        Subset of advisor_ids that did NOT clear the threshold.
    """

    items: list[AblationItem] = field(default_factory=list)
    full_metrics: EvalMetrics | None = None
    min_delta_sharpe: float = 0.0

    @property
    def advisors_earning_place(self) -> list[str]:
        """Advisors whose removal degrades Sharpe by >= min_delta_sharpe."""
        return [it.excluded_advisor for it in self.items if it.earns_place]

    @property
    def advisors_not_earning_place(self) -> list[str]:
        """Advisors whose removal does NOT degrade Sharpe (surplus or harmful)."""
        return [it.excluded_advisor for it in self.items if not it.earns_place]


def leave_one_out_ablation(
    advisor_ids: list[str],
    outcomes: list[ResolvedOutcome],
    compute_returns: Callable[[list[ResolvedOutcome], str | None], np.ndarray],
    *,
    min_delta_sharpe: float = 0.0,
    periods_per_year: float = 252.0,
) -> AblationReport:
    """Leave-one-out advisor ablation.

    Evaluates the ensemble with all advisors, then re-evaluates once per
    advisor after dropping that advisor's outcomes.

    Parameters
    ----------
    advisor_ids:
        All advisor IDs in the ensemble.
    outcomes:
        Resolved outcome records for ALL advisors over the backtest period.
        These are passed unchanged to ``compute_returns``.
    compute_returns:
        Callable with signature ``(outcomes, excluded_advisor_id | None) -> np.ndarray``.
        When ``excluded_advisor_id`` is ``None``, returns the full-ensemble returns.
        When a string, returns returns computed excluding that advisor's outcomes.
        This callable is INJECTED — do not import it here.
    min_delta_sharpe:
        Minimum Sharpe degradation from removing an advisor for it to be
        considered earning its place.  Default 0.0 (any positive contribution).
    periods_per_year:
        Passed to ``compute_eval_metrics``.

    Returns
    -------
    AblationReport
    """
    if not advisor_ids:
        raise ValueError("advisor_ids must be non-empty")

    report = AblationReport(min_delta_sharpe=min_delta_sharpe)

    # Full-ensemble baseline
    full_returns = compute_returns(outcomes, None)
    full_metrics = compute_eval_metrics(
        full_returns, n_trials=len(advisor_ids), periods_per_year=periods_per_year
    )
    report.full_metrics = full_metrics

    logger.info(
        "ablation: full-ensemble Sharpe=%.3f (n_advisors=%d)",
        full_metrics.sharpe,
        len(advisor_ids),
    )

    for advisor_id in advisor_ids:
        ablated_returns = compute_returns(outcomes, advisor_id)
        ablated_metrics = compute_eval_metrics(
            ablated_returns,
            n_trials=len(advisor_ids),
            periods_per_year=periods_per_year,
        )
        delta = full_metrics.sharpe - ablated_metrics.sharpe
        earns = delta >= min_delta_sharpe

        item = AblationItem(
            excluded_advisor=advisor_id,
            full_metrics=full_metrics,
            ablated_metrics=ablated_metrics,
            delta_sharpe=delta,
            earns_place=earns,
        )
        report.items.append(item)

        logger.info(
            "ablation: drop %s → Sharpe %.3f (delta=%.3f, earns_place=%s)",
            advisor_id,
            ablated_metrics.sharpe,
            delta,
            earns,
        )

    return report
