"""Tests for arbiter.orchestrator.loop_runner — scheduled-loop runner.

All tests inject fake ingest_fn / cycle_fn.  No real engine, no network.

Covers:
- Normal run: ingest called then cycle called with as_of from clock
- Ingest raising: cycle STILL runs; RunReport.ingest_ok=False, ingest_error set
- run_ingest_first=False: ingest skipped entirely, cycle still runs
- RunReport fields populated correctly in all branches
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from arbiter.orchestrator.loop_runner import RunReport, run_once

_UTC = timezone.utc
_NOW = datetime(2026, 1, 15, 9, 0, 0, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Minimal fake clock
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, as_of: datetime = _NOW) -> None:
        self._as_of = as_of

    def now(self) -> datetime:
        return self._as_of


# ---------------------------------------------------------------------------
# Happy path — normal run
# ---------------------------------------------------------------------------


class TestNormalRun:
    def test_ingest_and_cycle_both_called(self) -> None:
        """Both ingest_fn and cycle_fn must be invoked in a normal run."""
        calls: list[str] = []
        cycle_return = object()

        def _ingest() -> None:
            calls.append("ingest")

        def _cycle(as_of: datetime) -> object:
            calls.append("cycle")
            return cycle_return

        report = run_once(
            ingest_fn=_ingest,
            cycle_fn=_cycle,
            clock=_FakeClock(),
        )

        assert calls == ["ingest", "cycle"], "ingest must be called before cycle"
        assert report.ingest_ok is True
        assert report.ingest_error is None
        assert report.cycle_result is cycle_return

    def test_as_of_comes_from_clock(self) -> None:
        """RunReport.as_of must equal clock.now()."""
        ts = datetime(2025, 6, 1, 8, 30, tzinfo=_UTC)

        received: list[datetime] = []

        def _cycle(as_of: datetime) -> None:
            received.append(as_of)

        report = run_once(
            ingest_fn=lambda: None,
            cycle_fn=_cycle,
            clock=_FakeClock(ts),
        )

        assert report.as_of == ts
        assert received == [ts], "cycle must receive as_of from clock"

    def test_cycle_result_stored(self) -> None:
        """cycle_fn return value must appear in RunReport.cycle_result."""
        sentinel = {"ideas_processed": 3, "orders_submitted": 1}

        report = run_once(
            ingest_fn=lambda: None,
            cycle_fn=lambda _as_of: sentinel,
            clock=_FakeClock(),
        )

        assert report.cycle_result is sentinel


# ---------------------------------------------------------------------------
# Fault isolation — ingest raises but cycle still runs
# ---------------------------------------------------------------------------


class TestIngestFaultIsolation:
    def test_ingest_error_does_not_abort_cycle(self) -> None:
        """When ingest raises, the cycle MUST still be called."""
        cycle_called: list[bool] = []

        def _bad_ingest() -> None:
            raise RuntimeError("EDGAR is down")

        def _cycle(as_of: datetime) -> str:
            cycle_called.append(True)
            return "cycle_ran"

        report = run_once(
            ingest_fn=_bad_ingest,
            cycle_fn=_cycle,
            clock=_FakeClock(),
        )

        assert cycle_called == [True], "cycle must run even after ingest failure"
        assert report.ingest_ok is False
        assert isinstance(report.ingest_error, RuntimeError)
        assert "EDGAR is down" in str(report.ingest_error)
        assert report.cycle_result == "cycle_ran"

    def test_ingest_error_recorded_in_report(self) -> None:
        """ingest_error field must hold the actual exception instance."""
        err = ValueError("bad data")

        def _bad_ingest() -> None:
            raise err

        report = run_once(
            ingest_fn=_bad_ingest,
            cycle_fn=lambda _: None,
            clock=_FakeClock(),
        )

        assert report.ingest_ok is False
        assert report.ingest_error is err

    def test_ingest_ok_false_when_ingest_raises(self) -> None:
        report = run_once(
            ingest_fn=lambda: (_ for _ in ()).throw(ConnectionError("timeout")),
            cycle_fn=lambda _: "ok",
            clock=_FakeClock(),
        )

        assert report.ingest_ok is False
        assert isinstance(report.ingest_error, ConnectionError)

    def test_ingest_ok_true_when_ingest_succeeds(self) -> None:
        report = run_once(
            ingest_fn=lambda: None,
            cycle_fn=lambda _: None,
            clock=_FakeClock(),
        )

        assert report.ingest_ok is True
        assert report.ingest_error is None


# ---------------------------------------------------------------------------
# run_ingest_first=False — skip ingest
# ---------------------------------------------------------------------------


class TestSkipIngest:
    def test_ingest_not_called_when_flag_false(self) -> None:
        """When run_ingest_first=False, ingest_fn must not be invoked."""
        calls: list[str] = []

        def _ingest() -> None:
            calls.append("ingest")

        def _cycle(as_of: datetime) -> str:
            calls.append("cycle")
            return "done"

        report = run_once(
            ingest_fn=_ingest,
            cycle_fn=_cycle,
            clock=_FakeClock(),
            run_ingest_first=False,
        )

        assert calls == ["cycle"], "ingest must be skipped when run_ingest_first=False"
        # ingest_ok is True when ingest was not attempted (no failure occurred)
        assert report.ingest_ok is True
        assert report.ingest_error is None
        assert report.cycle_result == "done"

    def test_cycle_still_called_when_ingest_skipped(self) -> None:
        """Cycle must run even with ingest disabled."""
        ran: list[bool] = []

        report = run_once(
            ingest_fn=lambda: None,
            cycle_fn=lambda _: ran.append(True) or "result",
            clock=_FakeClock(),
            run_ingest_first=False,
        )

        assert ran == [True]
        assert report.cycle_result == "result"


# ---------------------------------------------------------------------------
# Cycle exceptions propagate (not swallowed)
# ---------------------------------------------------------------------------


class TestCycleExceptionPropagates:
    def test_cycle_exception_propagates(self) -> None:
        """Exceptions from cycle_fn must NOT be swallowed."""

        def _bad_cycle(as_of: datetime) -> None:
            raise RuntimeError("engine exploded")

        with pytest.raises(RuntimeError, match="engine exploded"):
            run_once(
                ingest_fn=lambda: None,
                cycle_fn=_bad_cycle,
                clock=_FakeClock(),
            )


# ---------------------------------------------------------------------------
# RunReport is a proper dataclass
# ---------------------------------------------------------------------------


class TestRunReport:
    def test_run_report_fields(self) -> None:
        """Spot-check that RunReport is a dataclass with expected fields."""
        ts = _NOW
        sentinel = object()
        err = ValueError("x")

        report = RunReport(
            as_of=ts,
            ingest_ok=False,
            ingest_error=err,
            cycle_result=sentinel,
        )

        assert report.as_of is ts
        assert report.ingest_ok is False
        assert report.ingest_error is err
        assert report.cycle_result is sentinel

    def test_run_report_defaults(self) -> None:
        report = RunReport(as_of=_NOW, ingest_ok=True)
        assert report.ingest_error is None
        assert report.cycle_result is None
