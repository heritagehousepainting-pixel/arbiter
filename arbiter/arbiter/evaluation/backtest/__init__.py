"""Backtest sub-package — replay, walk-forward, metrics, ablation, baseline.

Lane 14b.  Import surface::

    from arbiter.evaluation.backtest.replay import BacktestReplay, ReplayResult
    from arbiter.evaluation.backtest.walk_forward import walk_forward_eval, WalkForwardResult
    from arbiter.evaluation.backtest.metrics import (
        sharpe_ratio, deflated_sharpe_ratio, max_drawdown, hit_rate, EvalMetrics
    )
    from arbiter.evaluation.backtest.ablation import leave_one_out_ablation, AblationReport
    from arbiter.evaluation.backtest.baseline import (
        naive_insider_baseline, must_beat_baseline, BaselineStats
    )
"""
from __future__ import annotations
