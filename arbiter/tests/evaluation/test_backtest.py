"""Tests for Lane 14b: backtest replay, metrics, ablation, and baseline.

Naming convention: test_backtest_* to avoid collisions with the outcome-labeler
agent's test_outcome_* files (same tests/evaluation/ directory).

Test groups
-----------
TestBacktestReplay      — replay determinism, step count, error handling
TestNoLookAheadCanary   — structural proof that the replay cannot see future data
TestMetrics             — Sharpe, deflated Sharpe, drawdown, hit-rate
TestDeflatedSharpeLessThanPlain — DSR < plain Sharpe on a noisy series
TestWalkForward         — 5-window split, OOS metrics
TestAblation            — leave-one-out, delta_sharpe, earns_place
TestBaseline            — naive insider baseline, must_beat_baseline gate
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from arbiter.contract.seams import ResolvedOutcome
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.evaluation.backtest.ablation import AblationReport, leave_one_out_ablation
from arbiter.evaluation.backtest.baseline import (
    BaselineStats,
    LookAheadViolation,
    must_beat_baseline,
    naive_insider_baseline,
)
from arbiter.evaluation.backtest.metrics import (
    EvalMetrics,
    compute_eval_metrics,
    deflated_sharpe_ratio,
    hit_rate,
    max_drawdown,
    sharpe_ratio,
)
from arbiter.evaluation.backtest.replay import BacktestReplay, ReplayResult
from arbiter.evaluation.backtest.walk_forward import walk_forward_eval

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = timezone.utc


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=_UTC)


def _make_outcome(
    *,
    idea_id: str = "idea-1",
    advisor_id: str = "A1.insider",
    ticker: str = "AAPL",
    alpha_bps: float = 50.0,
    binary: int = 1,
    advisor_confidence: float = 0.7,
    abstained: bool = False,
    horizon_days: int = 30,
    label_kind: str = "normal",
) -> ResolvedOutcome:
    return ResolvedOutcome(
        idea_id=idea_id,
        advisor_id=advisor_id,
        ticker=ticker,
        alpha_bps=alpha_bps,
        binary=binary,
        advisor_confidence=advisor_confidence,
        stance_score=float(binary),
        abstained=abstained,
        horizon_days=horizon_days,
        label_kind=label_kind,
    )


def _make_positive_returns(n: int = 250, seed: int = 42) -> np.ndarray:
    """Returns series with clearly positive expected value (Sharpe ~1.5)."""
    rng = np.random.default_rng(seed)
    return rng.normal(loc=0.001, scale=0.01, size=n)


def _make_noisy_returns(n: int = 500, seed: int = 99) -> np.ndarray:
    """Pure noise — zero expected Sharpe, used for DSR test."""
    rng = np.random.default_rng(seed)
    return rng.normal(loc=0.0, scale=0.01, size=n)


def _make_pit_with_canary(future_as_of: datetime) -> tuple[PITGateway, dict[str, list[datetime]]]:
    """Return a PITGateway with a sentinel future value and a log of all reads."""
    read_log: dict[str, list[datetime]] = {"reads": []}

    class LoggingFixtureSource:
        """FixtureSource that records every PIT read timestamp."""

        def __init__(self) -> None:
            self._src = FixtureSource()

        def add(self, field: str, ticker: str, ts: datetime, value: object) -> None:
            self._src.add(field, ticker, ts, value)

        def get_pit(self, field: str, ticker: str, as_of: datetime) -> object | None:
            read_log["reads"].append(as_of)
            return self._src.get_pit(field, ticker, as_of)

    src = LoggingFixtureSource()
    # Past value — always visible
    src.add("price_close", "SPY", _dt(2024, 1, 1), 400.0)
    # FUTURE sentinel — must NEVER be returned during replay
    src.add("price_close", "SPY", future_as_of, 999_999.0)

    pit = PITGateway()
    pit.register_source("price_close", src)
    return pit, read_log


# ===========================================================================
# TestBacktestReplay
# ===========================================================================

class TestBacktestReplay:
    """Replay engine: basic functionality."""

    def test_step_count_weekdays_only(self) -> None:
        """Replay should produce one step per weekday in [start, end)."""
        # 2024-01-01 (Mon) to 2024-01-08 (Mon) — 5 weekdays
        start = _dt(2024, 1, 1)
        end = _dt(2024, 1, 8)
        pit = PITGateway()

        calls: list[datetime] = []

        def run_cycle(as_of: datetime, pit: PITGateway, clock: BacktestClock) -> dict:
            calls.append(as_of)
            return {"as_of": as_of}

        replay = BacktestReplay(
            start_date=start,
            end_date=end,
            step_days=1,
            pit=pit,
            run_cycle=run_cycle,
            skip_weekends=True,
        )
        result = replay.run()

        assert result.n_steps == 5
        assert result.n_errors == 0
        assert result.success_rate == 1.0
        # Steps must be in ascending order
        assert result.steps == sorted(result.steps)

    def test_replay_is_deterministic(self) -> None:
        """Running the same replay twice produces identical results."""
        start = _dt(2024, 2, 5)   # Monday
        end = _dt(2024, 2, 12)    # Monday (5 weekdays)
        pit = PITGateway()

        def run_cycle(as_of: datetime, pit: PITGateway, clock: BacktestClock) -> int:
            return hash(as_of.isoformat())

        replay = BacktestReplay(
            start_date=start, end_date=end, step_days=1, pit=pit, run_cycle=run_cycle
        )
        result_a = replay.run()
        result_b = replay.run()

        assert result_a.steps == result_b.steps
        assert result_a.cycle_outputs == result_b.cycle_outputs

    def test_cycle_outputs_keyed_by_as_of(self) -> None:
        """cycle_outputs maps as_of datetime to run_cycle return value."""
        start = _dt(2024, 3, 4)   # Monday
        end = _dt(2024, 3, 6)     # Wednesday
        pit = PITGateway()

        def run_cycle(as_of: datetime, pit: PITGateway, clock: BacktestClock) -> str:
            return f"output_{as_of.date()}"

        replay = BacktestReplay(
            start_date=start, end_date=end, step_days=1, pit=pit, run_cycle=run_cycle
        )
        result = replay.run()

        assert result.cycle_outputs[_dt(2024, 3, 4)] == "output_2024-03-04"
        assert result.cycle_outputs[_dt(2024, 3, 5)] == "output_2024-03-05"

    def test_errors_are_captured_replay_continues(self) -> None:
        """A run_cycle exception is recorded but replay continues to next step."""
        start = _dt(2024, 3, 4)
        end = _dt(2024, 3, 7)  # 3 weekdays: Mon, Tue, Wed
        pit = PITGateway()
        boom_day = _dt(2024, 3, 5)

        def run_cycle(as_of: datetime, pit: PITGateway, clock: BacktestClock) -> str:
            if as_of == boom_day:
                raise ValueError("deliberate failure")
            return "ok"

        replay = BacktestReplay(
            start_date=start, end_date=end, step_days=1, pit=pit, run_cycle=run_cycle
        )
        result = replay.run()

        assert result.n_steps == 3
        assert result.n_errors == 1
        assert boom_day in result.errors
        assert isinstance(result.errors[boom_day], ValueError)
        assert result.success_rate == pytest.approx(2 / 3)

    def test_clock_frozen_to_as_of_during_cycle(self) -> None:
        """The BacktestClock passed to run_cycle must return the step's as_of."""
        start = _dt(2024, 4, 1)
        end = _dt(2024, 4, 3)
        pit = PITGateway()
        observed_nows: list[datetime] = []

        def run_cycle(as_of: datetime, pit: PITGateway, clock: BacktestClock) -> None:
            # clock.now() MUST equal as_of — not wall-clock time
            observed_nows.append(clock.now())

        replay = BacktestReplay(
            start_date=start, end_date=end, step_days=1, pit=pit, run_cycle=run_cycle
        )
        result = replay.run()

        assert result.n_steps == 2  # Mon, Tue (2024-04-01 is Mon)
        for i, as_of in enumerate(result.steps):
            assert observed_nows[i] == as_of, (
                f"clock.now() returned {observed_nows[i]} but as_of={as_of}"
            )

    def test_raises_on_naive_start_date(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            BacktestReplay(
                start_date=datetime(2024, 1, 1),   # naive — no tzinfo
                end_date=_dt(2024, 1, 8),
                pit=PITGateway(),
                run_cycle=lambda *_: None,
            )

    def test_raises_when_end_before_start(self) -> None:
        with pytest.raises(ValueError, match="after start_date"):
            BacktestReplay(
                start_date=_dt(2024, 2, 1),
                end_date=_dt(2024, 1, 1),
                pit=PITGateway(),
                run_cycle=lambda *_: None,
            )

    def test_include_weekends_flag(self) -> None:
        """When skip_weekends=False, Saturday and Sunday steps are included."""
        start = _dt(2024, 1, 1)   # Monday
        end = _dt(2024, 1, 8)     # Monday (7 calendar days)
        pit = PITGateway()

        replay = BacktestReplay(
            start_date=start,
            end_date=end,
            step_days=1,
            pit=pit,
            run_cycle=lambda *_: None,
            skip_weekends=False,
        )
        result = replay.run()
        # 7 calendar days (1–7 Jan); end is exclusive so we get Mon–Sun = 7 days
        assert result.n_steps == 7


# ===========================================================================
# TestNoLookAheadCanary
# ===========================================================================

class TestNoLookAheadCanary:
    """Structural proof: replay CANNOT see data with timestamp > as_of.

    This is the "canary" test — it MUST fail loudly if the PIT look-ahead guard
    is ever broken.
    """

    def test_canary_future_price_never_returned(self) -> None:
        """A price registered with a future timestamp must never be returned.

        We replay 2024-01-01 → 2024-01-04 (3 weekdays).
        The 'future' sentinel is registered with timestamp 2024-06-01 (far future).
        If the replay returns 999_999.0 at ANY step, the no-look-ahead guard failed.
        """
        future_ts = _dt(2024, 6, 1)
        pit, read_log = _make_pit_with_canary(future_ts)

        prices_seen: list[float | None] = []

        def run_cycle(as_of: datetime, pit: PITGateway, clock: BacktestClock) -> float | None:
            # Assert clock.now() == as_of (belt-and-suspenders check)
            assert clock.now() == as_of, "Clock is not frozen to as_of!"
            price = pit.get("price_close", "SPY", as_of)
            prices_seen.append(price)
            return price

        replay = BacktestReplay(
            start_date=_dt(2024, 1, 1),
            end_date=_dt(2024, 1, 4),
            step_days=1,
            pit=pit,
            run_cycle=run_cycle,
        )
        result = replay.run()

        assert result.n_steps == 3
        # The future sentinel (999_999.0) must NEVER have been returned
        for price in prices_seen:
            assert price != 999_999.0, (
                "LOOK-AHEAD VIOLATION: future price 999_999.0 was returned during replay! "
                "The PIT gateway no-look-ahead guard is broken."
            )

    def test_canary_fails_if_pit_guard_bypassed(self) -> None:
        """Demonstrate that a naive dict-based data store WOULD leak future data.

        This documents WHY PITGateway is necessary — a naive ``dict`` lookup
        would return the future value (and thus this test uses a deliberately
        broken source to prove the canary catches the violation).
        """
        future_ts = _dt(2025, 1, 1)

        class LeakySource:
            """Broken source that ignores as_of — always returns the latest value."""

            def get_pit(self, field: str, ticker: str, as_of: datetime) -> float:
                # BUG: ignores as_of — this simulates a broken implementation
                return 999_999.0

        pit = PITGateway()
        pit.register_source("price_close", LeakySource())

        prices_seen: list[float | None] = []

        def run_cycle(as_of: datetime, pit: PITGateway, clock: BacktestClock) -> float | None:
            price = pit.get("price_close", "SPY", as_of)
            prices_seen.append(price)
            return price

        replay = BacktestReplay(
            start_date=_dt(2024, 1, 1),
            end_date=_dt(2024, 1, 4),
            step_days=1,
            pit=pit,
            run_cycle=run_cycle,
        )
        replay.run()

        # The leaky source returns future data — confirm the canary detects it
        assert any(p == 999_999.0 for p in prices_seen), (
            "Expected the leaky source to return future data, but it didn't — "
            "the canary validation logic is wrong."
        )

    def test_fixture_source_pit_invariant(self) -> None:
        """FixtureSource must not return values whose timestamp > as_of."""
        src = FixtureSource()
        t0 = _dt(2024, 1, 1)
        t_future = _dt(2024, 6, 1)

        src.add("price_close", "AAPL", t0, 150.0)
        src.add("price_close", "AAPL", t_future, 999_999.0)

        # Before the future timestamp
        assert src.get_pit("price_close", "AAPL", _dt(2024, 1, 15)) == 150.0
        # Exactly at future timestamp — should return future value
        assert src.get_pit("price_close", "AAPL", t_future) == 999_999.0
        # Just before future timestamp — must NOT return future value
        assert src.get_pit("price_close", "AAPL", _dt(2024, 5, 31)) == 150.0


# ===========================================================================
# TestMetrics
# ===========================================================================

class TestMetrics:
    """Basic metric correctness."""

    def test_sharpe_positive_returns(self) -> None:
        """Series with positive mean return should have positive Sharpe."""
        returns = _make_positive_returns(250)
        sr = sharpe_ratio(returns)
        assert sr > 0.0

    def test_sharpe_zero_std(self) -> None:
        """Constant returns → std = 0 → Sharpe = 0.0 (no division by zero)."""
        returns = np.full(100, 0.001)
        sr = sharpe_ratio(returns)
        assert sr == 0.0

    def test_sharpe_empty(self) -> None:
        assert sharpe_ratio(np.array([])) == 0.0

    def test_sharpe_negative_mean(self) -> None:
        """Negative mean returns → negative Sharpe (when variance is nonzero)."""
        # Constant series → near-zero std → clamped to 0.0
        returns_constant = np.full(100, -0.001)
        assert sharpe_ratio(returns_constant) == 0.0

        # Linearly declining series has nonzero std; mean is negative → SR < 0
        returns_noisy = np.array([-0.001 - 0.0001 * i for i in range(100)])
        sr_noisy = sharpe_ratio(returns_noisy)
        assert sr_noisy < 0

    def test_max_drawdown_known_series(self) -> None:
        """Manual test: 10% up, 20% down → drawdown = -20%/(1.10) ≈ -18.2%."""
        # wealth path: 1.0 → 1.1 → 0.88
        # peak at 1.1, trough 0.88 → drawdown = 0.88/1.1 - 1 = -0.2
        returns = np.array([0.10, -0.20])
        dd = max_drawdown(returns)
        assert dd == pytest.approx(-0.20, abs=1e-9)

    def test_max_drawdown_always_non_positive(self) -> None:
        returns = _make_positive_returns(200)
        dd = max_drawdown(returns)
        assert dd <= 0.0

    def test_max_drawdown_empty(self) -> None:
        assert max_drawdown(np.array([])) == 0.0

    def test_hit_rate_all_positive(self) -> None:
        returns = np.ones(50) * 0.01
        assert hit_rate(returns) == 1.0

    def test_hit_rate_all_negative(self) -> None:
        returns = np.ones(50) * -0.01
        assert hit_rate(returns) == 0.0

    def test_hit_rate_mixed(self) -> None:
        returns = np.array([0.01, -0.01, 0.01, -0.01])
        assert hit_rate(returns) == pytest.approx(0.5)

    def test_hit_rate_empty(self) -> None:
        assert hit_rate(np.array([])) == 0.0

    def test_compute_eval_metrics_returns_all_fields(self) -> None:
        returns = _make_positive_returns(100)
        m = compute_eval_metrics(returns)
        assert isinstance(m, EvalMetrics)
        assert m.n_obs == 100
        assert m.sharpe != 0.0
        assert 0.0 <= m.deflated_sharpe <= 1.0
        assert m.max_drawdown <= 0.0
        assert 0.0 <= m.hit_rate <= 1.0


# ===========================================================================
# TestDeflatedSharpeLessThanPlain
# ===========================================================================

class TestDeflatedSharpeLessThanPlain:
    """DSR must be strictly less than plain Sharpe on a noisy multi-trial series.

    DSR is a probability in (0,1); plain Sharpe is annualised and can be >> 1.
    So on ANY non-trivial series, DSR < plain_sharpe by construction.
    """

    def test_dsr_less_than_plain_sharpe_noisy(self) -> None:
        """On a noise series, plain Sharpe may be marginally positive but DSR
        will be lower (and closer to 0.5 under the null)."""
        rng = np.random.default_rng(0)
        # Slightly positive drift so plain Sharpe > 0
        returns = rng.normal(loc=0.0005, scale=0.01, size=500)

        plain = sharpe_ratio(returns)
        dsr = deflated_sharpe_ratio(returns, n_trials=50)

        # DSR is always in (0,1); plain Sharpe is annualised (can be > 1)
        assert 0.0 < dsr < 1.0
        # With n_trials=50 the DSR penalty drags DSR well below plain Sharpe
        assert dsr < plain

    def test_dsr_decreases_with_more_trials(self) -> None:
        """More multiple-testing trials → lower DSR on the same returns."""
        returns = _make_positive_returns(300)

        dsr_1 = deflated_sharpe_ratio(returns, n_trials=1)
        dsr_10 = deflated_sharpe_ratio(returns, n_trials=10)
        dsr_100 = deflated_sharpe_ratio(returns, n_trials=100)

        assert dsr_1 >= dsr_10 >= dsr_100, (
            f"DSR should decrease as n_trials increases: "
            f"dsr_1={dsr_1:.3f}, dsr_10={dsr_10:.3f}, dsr_100={dsr_100:.3f}"
        )

    def test_dsr_in_unit_interval(self) -> None:
        """DSR must always be in (0, 1)."""
        for seed in range(5):
            rng = np.random.default_rng(seed)
            returns = rng.normal(0, 0.01, 250)
            dsr = deflated_sharpe_ratio(returns, n_trials=5)
            assert 0.0 <= dsr <= 1.0, f"DSR={dsr} out of [0,1] for seed={seed}"

    def test_dsr_degenerate_inputs(self) -> None:
        """DSR should return 0.0 for degenerate / too-short series."""
        assert deflated_sharpe_ratio(np.array([]), n_trials=1) == 0.0
        assert deflated_sharpe_ratio(np.array([0.01]), n_trials=1) == 0.0


# ===========================================================================
# TestWalkForward
# ===========================================================================

class TestWalkForward:
    """Walk-forward evaluation."""

    def _long_returns(self, n: int = 800, seed: int = 7) -> np.ndarray:
        rng = np.random.default_rng(seed)
        return rng.normal(0.0008, 0.01, n)

    def test_five_windows_produced(self) -> None:
        result = walk_forward_eval(
            self._long_returns(),
            n_windows=5,
            min_train_periods=60,
            eval_periods=20,
        )
        assert result.n_windows == 5

    def test_ten_windows_produced(self) -> None:
        result = walk_forward_eval(
            self._long_returns(1000),
            n_windows=10,
            min_train_periods=60,
            eval_periods=20,
        )
        assert result.n_windows == 10

    def test_aggregate_metrics_present(self) -> None:
        result = walk_forward_eval(self._long_returns(), n_windows=5)
        assert result.aggregate_metrics is not None
        assert isinstance(result.aggregate_metrics, EvalMetrics)

    def test_oos_periods_non_overlapping(self) -> None:
        """Out-of-sample evaluation periods must not overlap."""
        result = walk_forward_eval(
            self._long_returns(600), n_windows=5, min_train_periods=60, eval_periods=20
        )
        windows = result.windows
        for i in range(len(windows) - 1):
            # Window i's eval_end must be <= window i+1's eval_start
            assert windows[i].eval_end <= windows[i + 1].eval_start, (
                f"OOS windows {i} and {i+1} overlap: "
                f"{windows[i].eval_start}:{windows[i].eval_end} vs "
                f"{windows[i+1].eval_start}:{windows[i+1].eval_end}"
            )

    def test_raises_below_min_windows(self) -> None:
        with pytest.raises(ValueError, match="n_windows >= 5"):
            walk_forward_eval(self._long_returns(), n_windows=4)

    def test_raises_when_series_too_short(self) -> None:
        short = np.zeros(10)
        with pytest.raises(ValueError, match="too short"):
            walk_forward_eval(short, n_windows=5, min_train_periods=60, eval_periods=20)

    def test_mean_oos_sharpe_property(self) -> None:
        result = walk_forward_eval(self._long_returns(), n_windows=5)
        # Mean OOS Sharpe should be approximately the average of per-window Sharpes
        expected = np.mean([w.out_of_sample_metrics.sharpe for w in result.windows])
        assert result.mean_oos_sharpe == pytest.approx(expected)

    def test_expanding_window_mode(self) -> None:
        """In expanding mode each window's training period starts at 0."""
        result = walk_forward_eval(
            self._long_returns(600), n_windows=5, expanding=True
        )
        for w in result.windows:
            assert w.train_start == 0, (
                f"Window {w.window_index}: train_start={w.train_start}, expected 0"
            )


# ===========================================================================
# TestAblation
# ===========================================================================

class TestAblation:
    """Leave-one-out advisor ablation."""

    def _make_outcomes(self) -> list[ResolvedOutcome]:
        """Three advisors, 4 outcomes each."""
        outcomes = []
        for advisor, bps in [("A1.insider", 40.0), ("A1.congress", 30.0), ("A2.mirofish", 10.0)]:
            for i in range(4):
                outcomes.append(
                    _make_outcome(
                        idea_id=f"{advisor}-{i}",
                        advisor_id=advisor,
                        alpha_bps=bps,
                    )
                )
        return outcomes

    def _compute_returns(
        self, outcomes: list[ResolvedOutcome], excluded: str | None
    ) -> np.ndarray:
        """Simple compute_returns: mean alpha_bps of non-excluded outcomes."""
        active = [o for o in outcomes if excluded is None or o.advisor_id != excluded]
        if not active:
            return np.zeros(1)
        # Use alpha_bps series directly as our "returns"
        return np.array([o.alpha_bps / 10_000.0 for o in active])

    def test_ablation_runs_all_advisors(self) -> None:
        outcomes = self._make_outcomes()
        report = leave_one_out_ablation(
            advisor_ids=["A1.insider", "A1.congress", "A2.mirofish"],
            outcomes=outcomes,
            compute_returns=self._compute_returns,
        )
        assert len(report.items) == 3

    def test_ablation_full_metrics_set(self) -> None:
        outcomes = self._make_outcomes()
        report = leave_one_out_ablation(
            advisor_ids=["A1.insider", "A1.congress", "A2.mirofish"],
            outcomes=outcomes,
            compute_returns=self._compute_returns,
        )
        assert report.full_metrics is not None

    def test_ablation_dropping_best_advisor_degrades(self) -> None:
        """Dropping A1.insider (highest alpha) should lower Sharpe most."""
        outcomes = self._make_outcomes()
        report = leave_one_out_ablation(
            advisor_ids=["A1.insider", "A1.congress", "A2.mirofish"],
            outcomes=outcomes,
            compute_returns=self._compute_returns,
        )
        item_insider = next(i for i in report.items if i.excluded_advisor == "A1.insider")
        item_mirofish = next(i for i in report.items if i.excluded_advisor == "A2.mirofish")
        # Dropping the high-alpha insider should hurt more than dropping mirofish
        assert item_insider.delta_sharpe >= item_mirofish.delta_sharpe

    def test_ablation_earns_place_threshold(self) -> None:
        """Advisors with positive delta_sharpe earn their place."""
        outcomes = self._make_outcomes()
        report = leave_one_out_ablation(
            advisor_ids=["A1.insider", "A1.congress", "A2.mirofish"],
            outcomes=outcomes,
            compute_returns=self._compute_returns,
            min_delta_sharpe=0.0,
        )
        for item in report.items:
            expected = item.delta_sharpe >= 0.0
            assert item.earns_place == expected, (
                f"{item.excluded_advisor}: delta_sharpe={item.delta_sharpe:.3f}, "
                f"earns_place={item.earns_place}, expected={expected}"
            )

    def test_ablation_report_helpers(self) -> None:
        outcomes = self._make_outcomes()
        report = leave_one_out_ablation(
            advisor_ids=["A1.insider", "A1.congress", "A2.mirofish"],
            outcomes=outcomes,
            compute_returns=self._compute_returns,
        )
        all_ids = {i.excluded_advisor for i in report.items}
        earning = set(report.advisors_earning_place)
        not_earning = set(report.advisors_not_earning_place)
        assert earning | not_earning == all_ids
        assert earning & not_earning == set()

    def test_ablation_raises_on_empty_advisor_list(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            leave_one_out_ablation(
                advisor_ids=[], outcomes=[], compute_returns=lambda o, e: np.zeros(0)
            )


# ===========================================================================
# TestBaseline
# ===========================================================================

class TestBaseline:
    """Naive insider baseline and must_beat_baseline gate."""

    def _make_positive_outcomes(self, n: int = 20) -> list[ResolvedOutcome]:
        # Use varying alpha_bps (40..59) so std > 0 and Sharpe is well-defined.
        return [
            _make_outcome(
                idea_id=f"idea-{i}",
                advisor_id="A1.insider",
                alpha_bps=40.0 + float(i),  # 40, 41, …, 59 — all positive
                abstained=False,
            )
            for i in range(n)
        ]

    def _make_negative_outcomes(self, n: int = 20) -> list[ResolvedOutcome]:
        # All negative with variance so Sharpe is well-defined and negative.
        return [
            _make_outcome(
                idea_id=f"idea-neg-{i}",
                advisor_id="A1.insider",
                alpha_bps=-30.0 - float(i),  # -30, -31, …, -49 — all negative
                abstained=False,
            )
            for i in range(n)
        ]

    def test_baseline_positive_outcomes(self) -> None:
        outcomes = self._make_positive_outcomes()
        stats = naive_insider_baseline(outcomes)
        assert stats.n_trades == 20
        assert stats.metrics.sharpe > 0.0
        assert stats.metrics.hit_rate == 1.0

    def test_baseline_negative_outcomes(self) -> None:
        outcomes = self._make_negative_outcomes()
        stats = naive_insider_baseline(outcomes)
        assert stats.n_trades == 20
        assert stats.metrics.sharpe < 0.0

    def test_baseline_excludes_abstentions(self) -> None:
        abstained = [
            _make_outcome(idea_id=f"abs-{i}", abstained=True) for i in range(5)
        ]
        active = self._make_positive_outcomes(10)
        stats = naive_insider_baseline(abstained + active)
        assert stats.n_trades == 10

    def test_baseline_empty_returns_zero_performance(self) -> None:
        stats = naive_insider_baseline([])
        assert stats.n_trades == 0
        assert stats.metrics.n_obs == 0

    def test_baseline_all_abstained(self) -> None:
        outcomes = [_make_outcome(abstained=True) for _ in range(5)]
        stats = naive_insider_baseline(outcomes)
        assert stats.n_trades == 0

    def test_must_beat_baseline_returns_true_when_strategy_wins(self) -> None:
        """Strategy with higher Sharpe and DSR passes the gate."""
        good_strategy = EvalMetrics(
            sharpe=2.0,
            deflated_sharpe=0.90,
            max_drawdown=-0.05,
            hit_rate=0.65,
            n_obs=252,
        )
        # Baseline with mediocre metrics
        baseline_outcomes = self._make_positive_outcomes(50)
        baseline_stats = naive_insider_baseline(baseline_outcomes)

        # Force a weak baseline for comparison
        weak_baseline = BaselineStats(
            metrics=EvalMetrics(
                sharpe=0.5,
                deflated_sharpe=0.60,
                max_drawdown=-0.15,
                hit_rate=0.52,
                n_obs=50,
            ),
            n_trades=50,
        )
        assert must_beat_baseline(good_strategy, weak_baseline) is True

    def test_must_beat_baseline_returns_false_when_strategy_loses(self) -> None:
        """Strategy with lower Sharpe than naive baseline fails the gate."""
        weak_strategy = EvalMetrics(
            sharpe=0.3,
            deflated_sharpe=0.55,
            max_drawdown=-0.25,
            hit_rate=0.48,
            n_obs=100,
        )
        strong_baseline = BaselineStats(
            metrics=EvalMetrics(
                sharpe=1.2,
                deflated_sharpe=0.80,
                max_drawdown=-0.10,
                hit_rate=0.60,
                n_obs=200,
            ),
            n_trades=200,
        )
        assert must_beat_baseline(weak_strategy, strong_baseline) is False

    def test_must_beat_baseline_requires_both_criteria(self) -> None:
        """Beats Sharpe but not DSR → still fails (both criteria required)."""
        strategy_beats_sharpe_only = EvalMetrics(
            sharpe=2.0,          # beats baseline Sharpe
            deflated_sharpe=0.50,  # does NOT beat baseline DSR
            max_drawdown=-0.10,
            hit_rate=0.60,
            n_obs=100,
        )
        baseline = BaselineStats(
            metrics=EvalMetrics(
                sharpe=1.0,
                deflated_sharpe=0.75,   # higher DSR than strategy
                max_drawdown=-0.15,
                hit_rate=0.55,
                n_obs=100,
            ),
            n_trades=100,
        )
        assert must_beat_baseline(strategy_beats_sharpe_only, baseline) is False

    def test_must_beat_baseline_with_margin(self) -> None:
        """require_sharpe_margin adds extra hurdle."""
        strategy = EvalMetrics(
            sharpe=1.1,
            deflated_sharpe=0.80,
            max_drawdown=-0.10,
            hit_rate=0.58,
            n_obs=100,
        )
        baseline = BaselineStats(
            metrics=EvalMetrics(
                sharpe=1.0,
                deflated_sharpe=0.70,
                max_drawdown=-0.12,
                hit_rate=0.55,
                n_obs=100,
            ),
            n_trades=100,
        )
        # Without margin: 1.1 > 1.0 → True
        assert must_beat_baseline(strategy, baseline) is True
        # With margin 0.2: 1.1 > 1.0 + 0.2 = 1.2 → False
        assert must_beat_baseline(strategy, baseline, require_sharpe_margin=0.2) is False


# ===========================================================================
# TestReplayIntegration
# ===========================================================================

class TestReplayIntegration:
    """End-to-end: replay drives a simple strategy, metrics are computed."""

    def test_replay_then_metrics(self) -> None:
        """Replay 10 weekdays, collect 'returns', compute metrics."""
        start = _dt(2024, 3, 4)    # Monday
        end = _dt(2024, 3, 18)     # Monday (10 weekdays)
        pit = PITGateway()

        # Seed a source with daily "alpha" values
        src = FixtureSource()
        rng = np.random.default_rng(42)
        current = start
        for _ in range(14):  # 14 calendar days covers 10 weekdays
            if current.weekday() < 5:
                src.add("price_close", "AAPL", current, float(rng.normal(0.001, 0.01)))
            current += timedelta(days=1)
        pit.register_source("price_close", src)

        collected_returns: list[float] = []

        def run_cycle(as_of: datetime, pit: PITGateway, clock: BacktestClock) -> float | None:
            val = pit.get("price_close", "AAPL", as_of)
            if val is not None:
                collected_returns.append(float(val))
            return val

        replay = BacktestReplay(
            start_date=start, end_date=end, step_days=1, pit=pit, run_cycle=run_cycle
        )
        result = replay.run()

        assert result.n_steps == 10
        # Compute metrics on whatever returns were collected
        if collected_returns:
            metrics = compute_eval_metrics(np.array(collected_returns))
            assert isinstance(metrics, EvalMetrics)
            assert metrics.n_obs <= 10
