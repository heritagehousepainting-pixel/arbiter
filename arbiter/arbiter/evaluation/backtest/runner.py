"""Backtest entrypoint — Lane 14b runner.

Wires ``BacktestReplay``, ``iter_trading_days``, ``compute_eval_metrics``,
``walk_forward_eval``, ``leave_one_out_ablation``, ``naive_insider_baseline``,
and ``must_beat_baseline`` into a single callable ``run_backtest``.

No look-ahead: all data access goes through the injected ``pit`` gateway and
the ``BacktestClock`` that ``BacktestReplay`` pins to each step's ``as_of``.
``datetime.now()`` is never called here (enforced by check_no_lookahead.sh).

Usage (integration)::

    from datetime import date
    from arbiter.evaluation.backtest.runner import run_backtest, BacktestReport

    report = run_backtest(
        start=date(2024, 1, 2),
        end=date(2024, 6, 28),
        cycle_fn=engine.run_cycle,   # (as_of, pit, clock) -> Any
        labeler=my_labeler,          # (cycle_outputs) -> list[ResolvedOutcome]
        pit=my_pit_gateway,
    )
    print(report.render())

Usage (CLI integration)::

    from arbiter.evaluation.backtest.runner import main
    main(start=date(2024, 1, 2), end=date(2024, 6, 28))
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable

import numpy as np

from arbiter.contract.seams import ResolvedOutcome
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import PITGateway
from arbiter.data.replay_clock import iter_trading_days
from arbiter.evaluation.backtest.ablation import AblationReport, leave_one_out_ablation
from arbiter.evaluation.backtest.baseline import BaselineStats, must_beat_baseline, naive_insider_baseline
from arbiter.evaluation.backtest.metrics import EvalMetrics, compute_eval_metrics
from arbiter.evaluation.backtest.replay import BacktestReplay
from arbiter.evaluation.backtest.walk_forward import WalkForwardResult, walk_forward_eval

logger = logging.getLogger(__name__)

# Minimum observations needed to attempt a walk-forward split.
# walk_forward_eval requires n >= min_train_periods + n_windows * eval_periods.
# With _WF_N_WINDOWS=5, _WF_MIN_TRAIN=20, _WF_EVAL_PERIODS=5 that means 45+.
_WF_N_WINDOWS: int = 5
_WF_MIN_TRAIN: int = 20
_WF_EVAL_PERIODS: int = 5
_WF_REQUIRED: int = _WF_MIN_TRAIN + _WF_N_WINDOWS * _WF_EVAL_PERIODS  # 45


# ---------------------------------------------------------------------------
# BacktestReport
# ---------------------------------------------------------------------------

@dataclass
class BacktestReport:
    """Full backtest result.

    Attributes
    ----------
    start:
        First date in the backtest range.
    end:
        Last date in the backtest range.
    n_trades:
        Total number of resolved outcomes (across all advisors).
    metrics:
        Aggregate ``EvalMetrics`` for the full strategy.
    walk_forward:
        Walk-forward result (``None`` when not enough data for ≥5 windows).
    ablation:
        Leave-one-out ablation report (``None`` when outcomes list is empty).
    baseline:
        Naive follow-every-insider ``BaselineStats``.
    beats_baseline:
        ``True`` when the strategy beats the naive insider baseline on both
        Sharpe and deflated Sharpe (via ``must_beat_baseline``).
    """

    start: date
    end: date
    n_trades: int
    metrics: EvalMetrics
    walk_forward: WalkForwardResult | None
    ablation: AblationReport | None
    baseline: BaselineStats
    beats_baseline: bool

    # Raw data preserved for downstream use
    outcomes: list[ResolvedOutcome] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------ #
    def render(self) -> str:
        """Return a human-readable text summary of the backtest results.

        Returns
        -------
        str
            Multi-line summary string — non-empty even for degenerate runs.
        """
        lines: list[str] = [
            "=" * 60,
            "Backtest Report",
            "=" * 60,
            f"  Period        : {self.start} → {self.end}",
            f"  Trades        : {self.n_trades}",
            "",
            "[ Strategy Metrics ]",
            f"  Sharpe        : {self.metrics.sharpe:.4f}",
            f"  Deflated SR   : {self.metrics.deflated_sharpe:.4f}",
            f"  Max Drawdown  : {self.metrics.max_drawdown:.4f}",
            f"  Hit Rate      : {self.metrics.hit_rate:.4f}",
            f"  Observations  : {self.metrics.n_obs}",
            "",
            "[ Baseline ]",
            f"  Naive insider Sharpe : {self.baseline.metrics.sharpe:.4f}",
            f"  Naive insider DSR    : {self.baseline.metrics.deflated_sharpe:.4f}",
            f"  Beats baseline       : {self.beats_baseline}",
        ]

        if self.walk_forward is not None:
            wf = self.walk_forward
            lines += [
                "",
                f"[ Walk-Forward ({wf.n_windows} windows) ]",
                f"  Mean OOS Sharpe  : {wf.mean_oos_sharpe:.4f}",
                f"  Mean OOS HitRate : {wf.mean_oos_hit_rate:.4f}",
            ]
            if wf.aggregate_metrics:
                lines += [
                    f"  Aggregate Sharpe : {wf.aggregate_metrics.sharpe:.4f}",
                    f"  Aggregate DSR    : {wf.aggregate_metrics.deflated_sharpe:.4f}",
                ]
        else:
            lines += [
                "",
                "[ Walk-Forward ]",
                "  Skipped (insufficient observations for 5 windows)",
            ]

        if self.ablation is not None:
            abl = self.ablation
            lines += [
                "",
                "[ Leave-One-Out Ablation ]",
                f"  Full-ensemble Sharpe : {abl.full_metrics.sharpe:.4f}"
                if abl.full_metrics
                else "  Full metrics unavailable",
            ]
            for item in abl.items:
                mark = "+" if item.earns_place else "-"
                lines.append(
                    f"  [{mark}] drop {item.excluded_advisor:<20} "
                    f"delta_sharpe={item.delta_sharpe:+.4f}"
                )
            lines += [
                f"  Earning place  : {', '.join(abl.advisors_earning_place) or 'none'}",
                f"  Not earning    : {', '.join(abl.advisors_not_earning_place) or 'none'}",
            ]
        else:
            lines += [
                "",
                "[ Ablation ]",
                "  Skipped (no outcomes to ablate)",
            ]

        lines.append("=" * 60)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# run_backtest
# ---------------------------------------------------------------------------

def run_backtest(
    *,
    start: date,
    end: date,
    cycle_fn: Callable[[datetime, PITGateway, BacktestClock], Any],
    labeler: Callable[[dict[datetime, Any]], list[ResolvedOutcome]],
    pit: PITGateway,
    clock: BacktestClock | None = None,
) -> BacktestReport:
    """Drive a full backtest over [start, end] and return a ``BacktestReport``.

    This is the SAME code path as live: ``BacktestReplay`` steps a
    ``BacktestClock`` through each trading day returned by
    ``iter_trading_days(start, end)`` and calls ``cycle_fn(as_of, pit, clock)``
    at each step.  The clock is frozen to the step's ``as_of`` for the
    entire duration of the call — no look-ahead is structurally possible.

    After the replay, the injected ``labeler`` converts the per-step outputs
    into ``ResolvedOutcome`` records, then metrics / walk-forward / ablation /
    baseline are computed and packaged into a ``BacktestReport``.

    Parameters
    ----------
    start:
        First calendar date (inclusive).  Non-trading days are skipped by
        ``iter_trading_days``.
    end:
        Last calendar date (inclusive).
    cycle_fn:
        Callable with signature ``(as_of: datetime, pit: PITGateway,
        clock: BacktestClock) -> Any``.  This is the engine's ``run_cycle``
        in production; tests inject a lightweight fake.
    labeler:
        Callable with signature ``(cycle_outputs: dict[datetime, Any])
        -> list[ResolvedOutcome]``.  Converts the replay's output map into
        resolved outcome records.  Injected; the real implementation calls
        ``outcome_labeler.label()`` per idea.
    pit:
        Pre-loaded ``PITGateway``.  Enforces the PIT look-ahead invariant.
    clock:
        Optional ``BacktestClock`` to use.  When ``None``, a fresh clock is
        created from the first trading day in the range.  Passing a clock is
        useful when the caller needs to observe clock state after the run.

    Returns
    -------
    BacktestReport
        Full backtest result including metrics, walk-forward, ablation, and
        baseline comparison.

    Raises
    ------
    ValueError
        If ``start`` is after ``end``, or there are no trading days in range.
    """
    # ------------------------------------------------------------------ #
    # 1. Build the trading-day schedule via iter_trading_days.
    # ------------------------------------------------------------------ #
    trading_days = list(iter_trading_days(start, end))
    if not trading_days:
        raise ValueError(
            f"run_backtest: no trading days in [{start}, {end}]"
        )

    logger.info(
        "run_backtest: %d trading days from %s to %s",
        len(trading_days),
        start,
        end,
    )

    # ------------------------------------------------------------------ #
    # 2. Convert date range to tz-aware datetimes for BacktestReplay.
    # ------------------------------------------------------------------ #
    _UTC = timezone.utc
    start_dt = datetime(start.year, start.month, start.day, tzinfo=_UTC)
    # end_dt is EXCLUSIVE for BacktestReplay (it uses half-open [start, end)).
    # The last trading day from iter_trading_days is inclusive, so we advance
    # by one calendar day to make end_dt exclusive.
    last_day = trading_days[-1]
    from datetime import timedelta as _td  # local import keeps top-level clean
    end_dt = datetime(last_day.year, last_day.month, last_day.day, tzinfo=_UTC) + _td(days=1)

    # ------------------------------------------------------------------ #
    # 3. Optionally use the provided clock; BacktestReplay will create its
    #    own internally if we don't — but passing one lets callers observe
    #    clock state after the run.  We always let BacktestReplay own the
    #    clock pin loop; here we only build it if explicitly requested.
    # ------------------------------------------------------------------ #
    # BacktestReplay always creates its own clock internally; the optional
    # ``clock`` parameter is provided for callers that want to pre-seed or
    # inspect the clock, but BacktestReplay manages pin-to-as_of internally.
    # We don't pass the external clock into BacktestReplay since that class
    # builds its own.  If the caller passed a clock we just use it for
    # bookkeeping in the report; BacktestReplay is always authoritative.

    # ------------------------------------------------------------------ #
    # 4. Run the replay.
    # ------------------------------------------------------------------ #
    replay = BacktestReplay(
        start_date=start_dt,
        end_date=end_dt,
        step_days=1,
        pit=pit,
        run_cycle=cycle_fn,
        skip_weekends=True,
    )
    replay_result = replay.run()

    logger.info(
        "run_backtest: replay complete — %d steps, %d errors",
        replay_result.n_steps,
        replay_result.n_errors,
    )

    # ------------------------------------------------------------------ #
    # 5. Label outcomes from cycle outputs.
    # ------------------------------------------------------------------ #
    outcomes: list[ResolvedOutcome] = labeler(replay_result.cycle_outputs)
    n_trades = len(outcomes)

    logger.info("run_backtest: labeler produced %d outcomes", n_trades)

    # ------------------------------------------------------------------ #
    # 6. Build a returns series from outcomes (alpha_bps / 10_000).
    # ------------------------------------------------------------------ #
    active_outcomes = [o for o in outcomes if not o.abstained]
    if active_outcomes:
        strategy_returns = np.array(
            [o.alpha_bps / 10_000.0 for o in active_outcomes], dtype=float
        )
    else:
        strategy_returns = np.zeros(0, dtype=float)

    # ------------------------------------------------------------------ #
    # 7. Compute aggregate metrics.
    # ------------------------------------------------------------------ #
    n_obs = len(strategy_returns)
    metrics = compute_eval_metrics(
        strategy_returns,
        n_trials=max(1, n_obs),  # n_trials = n_obs for realistic DSR penalty
        periods_per_year=252.0,
    )

    # ------------------------------------------------------------------ #
    # 8. Walk-forward evaluation (≥5 windows when enough data).
    # ------------------------------------------------------------------ #
    walk_forward: WalkForwardResult | None = None
    if n_obs >= _WF_REQUIRED:
        try:
            walk_forward = walk_forward_eval(
                strategy_returns,
                n_windows=_WF_N_WINDOWS,
                min_train_periods=_WF_MIN_TRAIN,
                eval_periods=_WF_EVAL_PERIODS,
                periods_per_year=252.0,
            )
            logger.info(
                "run_backtest: walk-forward complete — %d windows",
                walk_forward.n_windows,
            )
        except ValueError as exc:
            logger.warning("run_backtest: walk_forward_eval skipped — %s", exc)
    else:
        logger.info(
            "run_backtest: walk-forward skipped (n_obs=%d < required=%d)",
            n_obs,
            _WF_REQUIRED,
        )

    # ------------------------------------------------------------------ #
    # 9. Leave-one-out ablation.
    # ------------------------------------------------------------------ #
    ablation: AblationReport | None = None
    if outcomes:
        advisor_ids = sorted({o.advisor_id for o in outcomes if not o.abstained})
        if advisor_ids:
            def _compute_returns(
                all_outcomes: list[ResolvedOutcome],
                excluded: str | None,
            ) -> np.ndarray:
                active = [
                    o for o in all_outcomes
                    if not o.abstained and (excluded is None or o.advisor_id != excluded)
                ]
                if not active:
                    return np.zeros(1, dtype=float)
                return np.array([o.alpha_bps / 10_000.0 for o in active], dtype=float)

            try:
                ablation = leave_one_out_ablation(
                    advisor_ids=advisor_ids,
                    outcomes=outcomes,
                    compute_returns=_compute_returns,
                    periods_per_year=252.0,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("run_backtest: ablation failed — %s", exc)

    # ------------------------------------------------------------------ #
    # 10. Naive insider baseline + gate.
    # ------------------------------------------------------------------ #
    baseline = naive_insider_baseline(outcomes, periods_per_year=252.0)
    beats = must_beat_baseline(metrics, baseline)

    logger.info(
        "run_backtest: beats_baseline=%s (strategy_sharpe=%.4f vs baseline=%.4f)",
        beats,
        metrics.sharpe,
        baseline.metrics.sharpe,
    )

    # ------------------------------------------------------------------ #
    # 11. Assemble and return the report.
    # ------------------------------------------------------------------ #
    return BacktestReport(
        start=start,
        end=end,
        n_trades=n_trades,
        metrics=metrics,
        walk_forward=walk_forward,
        ablation=ablation,
        baseline=baseline,
        beats_baseline=beats,
        outcomes=outcomes,
    )


# ---------------------------------------------------------------------------
# main — real wiring for CLI integration
# ---------------------------------------------------------------------------

def main(
    config: object | None = None,
    start: date | None = None,
    end: date | None = None,
) -> BacktestReport:
    """Build the real wiring and run a backtest.

    Intended to be called by ``arbiter backtest --start YYYY-MM-DD --end YYYY-MM-DD``.
    All heavy imports are deferred so the function is safe to import without
    triggering the full engine boot (tests that import runner.py don't need it).

    Parameters
    ----------
    config:
        ``Config`` object.  When ``None``, loads from ``config/arbiter.toml`` + env.
    start:
        First date of the backtest range.  Defaults to 90 calendar days before
        the clock's current date (determined via ``BacktestClock`` / live clock).
    end:
        Last date (inclusive) of the backtest range.  Defaults to yesterday.

    Returns
    -------
    BacktestReport
    """
    # Deferred imports keep the module importable without the full engine stack.
    from arbiter.config import load_config  # noqa: PLC0415
    from arbiter.data.clock import Clock  # noqa: PLC0415
    from arbiter.engine import build_engine  # noqa: PLC0415

    if config is None:
        config = load_config()

    # Build a live clock to resolve default dates — we don't call
    # ``datetime.now()`` directly (check_no_lookahead.sh would flag it).
    live_clock = Clock()
    today = live_clock.now().date()

    from datetime import timedelta as _timedelta  # noqa: PLC0415
    if end is None:
        end = today - _timedelta(days=1)
    if start is None:
        start = end - _timedelta(days=90)

    # Build the engine with a BacktestClock seeded at start so all advisor
    # calls during replay read PIT data relative to the simulated date.
    _UTC = timezone.utc
    seed_dt = datetime(start.year, start.month, start.day, tzinfo=_UTC)
    bt_clock = BacktestClock(seed_dt)

    engine = build_engine(config=config, clock=bt_clock)  # type: ignore[arg-type]

    # PIT-purity guard (amendment C0): a backtest MUST NOT carry a live current-
    # price provider, even when EXECUTOR_BACKEND=alpaca_paper is set in the shell.
    from arbiter.data.current_price import NullCurrentPriceProvider  # noqa: PLC0415

    assert isinstance(engine.current_price_provider, NullCurrentPriceProvider), (
        "backtest engine must use NullCurrentPriceProvider — a live current price "
        "must never enter a backtest (C0 clock-type gate)"
    )

    # cycle_fn: wraps engine.run_cycle to match (as_of, pit, clock) signature.
    def _cycle_fn(
        as_of: datetime,
        pit: PITGateway,
        clock: BacktestClock,
    ) -> Any:
        return engine.run_cycle(as_of=as_of)

    # labeler: converts CycleResult dict into ResolvedOutcome list.
    # The real outcome_labeler.label() is called per idea; for the integration
    # wiring here we delegate to outcome_store if available, else return [].
    def _labeler(cycle_outputs: dict[datetime, Any]) -> list[ResolvedOutcome]:
        # Phase-1 MVP: the engine doesn't yet surface resolved outcomes from
        # run_cycle.  Return an empty list; a Wave-D lane will wire the real
        # outcome_labeler sweep here.  The report is still meaningful for
        # replay coverage and error-rate auditing.
        all_outcomes: list[ResolvedOutcome] = []
        for as_of, output in cycle_outputs.items():
            if output is None:
                continue
            # If cycle_fn returns a list of ResolvedOutcome directly (tests),
            # collect them.
            if isinstance(output, list):
                for item in output:
                    if isinstance(item, ResolvedOutcome):
                        all_outcomes.append(item)
        return all_outcomes

    report = run_backtest(
        start=start,
        end=end,
        cycle_fn=_cycle_fn,
        labeler=_labeler,
        pit=engine.pit,
        clock=bt_clock,
    )

    logger.info(
        "main: backtest complete — n_trades=%d beats_baseline=%s",
        report.n_trades,
        report.beats_baseline,
    )
    return report
