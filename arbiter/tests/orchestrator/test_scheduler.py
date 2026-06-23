"""Tests for arbiter.orchestrator.scheduler — fault-isolated advisor runner (Lane 13).

Covers:
- A crashing advisor does not abort the cycle (other advisors still run)
- A timed-out advisor yields None; others complete normally
- All nulls when all advisors crash
- Named advisor map returns keyed results
- Empty input returns empty output
- Successful opinions pass through
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from arbiter.contract.opinion import Opinion
from arbiter.orchestrator.scheduler import (
    run_advisors_parallel,
    run_named_advisors_parallel,
)
from arbiter.types import ConfidenceSource

_UTC = timezone.utc


def _make_opinion(advisor_id: str = "A1.test") -> Opinion:
    """Build a minimal valid Opinion."""
    return Opinion(
        advisor_id=advisor_id,
        ticker="AAPL",
        stance_score=0.5,
        confidence=0.8,
        confidence_source=ConfidenceSource.SELF_REPORTED,
        horizon_days=10,
        as_of=datetime(2024, 1, 1, tzinfo=_UTC),
        rationale="test",
        source_fingerprint="fp_test",
        run_group_id="rg_test",
    )


# ---------------------------------------------------------------------------
# Fault isolation — crash does not abort the cycle
# ---------------------------------------------------------------------------

class TestFaultIsolation:
    def test_crashing_advisor_yields_none(self):
        def crashing():
            raise RuntimeError("advisor exploded")

        results = run_advisors_parallel([crashing])
        assert results == [None]

    def test_crashing_advisor_does_not_abort_cycle(self):
        """Other advisors must still complete when one crashes."""
        expected_opinion = _make_opinion("A1.good")

        def crashing():
            raise RuntimeError("A2 crashed")

        def good():
            return expected_opinion

        # Order: crashing first, good second
        results = run_advisors_parallel([crashing, good])

        assert results[0] is None  # crashing → null
        assert results[1] is expected_opinion  # good → opinion

    def test_multiple_crashes_still_returns_per_slot(self):
        def crash1():
            raise ValueError("v1")

        def crash2():
            raise RuntimeError("r2")

        def ok():
            return _make_opinion("A1.ok")

        results = run_advisors_parallel([crash1, ok, crash2])
        assert results[0] is None
        assert results[1] is not None
        assert results[2] is None

    def test_all_crash_returns_all_none(self):
        def crash():
            raise Exception("boom")

        results = run_advisors_parallel([crash, crash, crash])
        assert all(r is None for r in results)
        assert len(results) == 3


# ---------------------------------------------------------------------------
# Timeout isolation
# ---------------------------------------------------------------------------

class TestTimeoutIsolation:
    def test_timed_out_advisor_yields_none(self):
        def slow():
            time.sleep(5)  # will be killed
            return _make_opinion()

        results = run_advisors_parallel([slow], timeout_seconds=0.1)
        assert results == [None]

    def test_timed_out_advisor_does_not_block_others(self):
        """Fast advisor must complete even if another is slow."""
        fast_opinion = _make_opinion("A1.fast")

        def slow():
            time.sleep(5)
            return _make_opinion("A2.slow")

        def fast():
            return fast_opinion

        results = run_advisors_parallel([slow, fast], timeout_seconds=0.2)
        assert results[0] is None     # slow timed out
        assert results[1] is fast_opinion  # fast completed


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_successful_opinions_pass_through(self):
        op1 = _make_opinion("A1.op1")
        op2 = _make_opinion("A1.op2")

        results = run_advisors_parallel([lambda: op1, lambda: op2])
        assert results[0] is op1
        assert results[1] is op2

    def test_abstaining_advisor_returns_none(self):
        """An advisor that returns None (abstain) is not a fault."""
        results = run_advisors_parallel([lambda: None])
        assert results == [None]

    def test_empty_advisor_list_returns_empty(self):
        results = run_advisors_parallel([])
        assert results == []

    def test_order_preserved(self):
        opinions = [_make_opinion(f"A1.{i}") for i in range(5)]
        callables = [lambda op=op: op for op in opinions]
        results = run_advisors_parallel(callables)
        assert results == opinions


# ---------------------------------------------------------------------------
# Named advisor map
# ---------------------------------------------------------------------------

class TestNamedAdvisors:
    def test_run_named_returns_keyed_results(self):
        op = _make_opinion("A1.insider")

        advisor_map = {
            "A1.insider": lambda: op,
            "A2.crash": lambda: (_ for _ in ()).throw(RuntimeError("kaboom")),
        }

        results = run_named_advisors_parallel(advisor_map, timeout_seconds=2.0)
        assert results["A1.insider"] is op
        assert results["A2.crash"] is None

    def test_run_named_empty_returns_empty(self):
        results = run_named_advisors_parallel({})
        assert results == {}

    def test_run_named_all_keys_present(self):
        advisor_map = {
            "A1.a": lambda: _make_opinion("A1.a"),
            "A1.b": lambda: None,
            "A1.c": lambda: (_ for _ in ()).throw(ValueError("err")),
        }
        results = run_named_advisors_parallel(advisor_map)
        assert set(results.keys()) == {"A1.a", "A1.b", "A1.c"}
        assert results["A1.b"] is None
        assert results["A1.c"] is None
