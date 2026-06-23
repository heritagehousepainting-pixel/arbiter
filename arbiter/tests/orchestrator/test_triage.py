"""Tests for arbiter.orchestrator.triage — MiroFish invoke/skip matrix (Lane 13).

Covers all rules:
    INVOKE always: SHORT (SWING), LONG
    INVOKE conditional: INTRADAY (DAY) with > 30 min to entry
    SKIP always: INTRADAY < 5 min, INTRADAY unknown/≤30 min, MEDIUM, NEWS signals
"""
from __future__ import annotations

import pytest

from arbiter.orchestrator.triage import (
    TriageAction,
    TriageResult,
    maybe_invoke_mirofish,
    triage_mirofish,
)
from arbiter.types import HorizonBucket


# ---------------------------------------------------------------------------
# Always INVOKE: SHORT (SWING) and LONG
# ---------------------------------------------------------------------------

class TestAlwaysInvoke:
    def test_short_bucket_invoke(self):
        result = triage_mirofish(HorizonBucket.SHORT)
        assert result.action == TriageAction.INVOKE
        assert result.should_invoke is True

    def test_short_bucket_invoke_with_no_minutes(self):
        """SHORT bucket ignores minutes_to_entry."""
        result = triage_mirofish(HorizonBucket.SHORT, minutes_to_entry=None)
        assert result.should_invoke is True

    def test_short_bucket_invoke_regardless_of_time(self):
        """SHORT always invokes even with very short time to entry."""
        result = triage_mirofish(HorizonBucket.SHORT, minutes_to_entry=1.0)
        assert result.should_invoke is True

    def test_long_bucket_invoke(self):
        result = triage_mirofish(HorizonBucket.LONG)
        assert result.action == TriageAction.INVOKE
        assert result.should_invoke is True

    def test_long_bucket_invoke_with_no_minutes(self):
        result = triage_mirofish(HorizonBucket.LONG, minutes_to_entry=None)
        assert result.should_invoke is True


# ---------------------------------------------------------------------------
# Always SKIP: MEDIUM
# ---------------------------------------------------------------------------

class TestMediumBucketSkip:
    def test_medium_always_skip(self):
        result = triage_mirofish(HorizonBucket.MEDIUM)
        assert result.action == TriageAction.SKIP
        assert result.should_invoke is False

    def test_medium_skip_regardless_of_time(self):
        result = triage_mirofish(HorizonBucket.MEDIUM, minutes_to_entry=120.0)
        assert result.should_invoke is False


# ---------------------------------------------------------------------------
# INTRADAY (DAY) — conditional on minutes_to_entry
# ---------------------------------------------------------------------------

class TestIntradayConditional:
    def test_intraday_invoke_above_30_min(self):
        result = triage_mirofish(HorizonBucket.INTRADAY, minutes_to_entry=31.0)
        assert result.action == TriageAction.INVOKE

    def test_intraday_invoke_exactly_31_min(self):
        result = triage_mirofish(HorizonBucket.INTRADAY, minutes_to_entry=31.0)
        assert result.should_invoke is True

    def test_intraday_invoke_90_min(self):
        result = triage_mirofish(HorizonBucket.INTRADAY, minutes_to_entry=90.0)
        assert result.should_invoke is True

    def test_intraday_skip_exactly_30_min(self):
        """30 min is NOT > 30, so skip."""
        result = triage_mirofish(HorizonBucket.INTRADAY, minutes_to_entry=30.0)
        assert result.action == TriageAction.SKIP

    def test_intraday_skip_below_30_min(self):
        result = triage_mirofish(HorizonBucket.INTRADAY, minutes_to_entry=20.0)
        assert result.should_invoke is False

    def test_intraday_skip_below_5_min(self):
        """< 5 min is the hardest skip — always skip regardless."""
        result = triage_mirofish(HorizonBucket.INTRADAY, minutes_to_entry=4.0)
        assert result.should_invoke is False

    def test_intraday_skip_at_1_min(self):
        result = triage_mirofish(HorizonBucket.INTRADAY, minutes_to_entry=1.0)
        assert result.should_invoke is False

    def test_intraday_skip_when_minutes_unknown(self):
        """Unknown minutes_to_entry → skip for intraday (safe default)."""
        result = triage_mirofish(HorizonBucket.INTRADAY, minutes_to_entry=None)
        assert result.should_invoke is False


# ---------------------------------------------------------------------------
# NEWS signals — always skip regardless of bucket
# ---------------------------------------------------------------------------

class TestNewsSignalSkip:
    def test_news_skip_on_short(self):
        result = triage_mirofish(HorizonBucket.SHORT, signal_kind="NEWS")
        assert result.action == TriageAction.SKIP

    def test_news_skip_on_long(self):
        result = triage_mirofish(HorizonBucket.LONG, signal_kind="NEWS")
        assert result.should_invoke is False

    def test_news_skip_on_intraday_even_with_time(self):
        result = triage_mirofish(
            HorizonBucket.INTRADAY, minutes_to_entry=60.0, signal_kind="NEWS"
        )
        assert result.should_invoke is False

    def test_news_signal_case_insensitive(self):
        """news (lowercase) should also skip."""
        result = triage_mirofish(HorizonBucket.SHORT, signal_kind="news")
        assert result.should_invoke is False

    def test_non_news_signal_does_not_skip(self):
        """Other signal kinds don't trigger the NEWS rule."""
        result = triage_mirofish(HorizonBucket.SHORT, signal_kind="FILING")
        assert result.should_invoke is True


# ---------------------------------------------------------------------------
# TriageResult attributes
# ---------------------------------------------------------------------------

class TestTriageResult:
    def test_result_has_reason(self):
        result = triage_mirofish(HorizonBucket.SHORT)
        assert isinstance(result.reason, str)
        assert len(result.reason) > 0

    def test_invoke_result_has_action_string(self):
        result = triage_mirofish(HorizonBucket.LONG)
        assert result.action == "invoke"

    def test_skip_result_has_action_string(self):
        result = triage_mirofish(HorizonBucket.MEDIUM)
        assert result.action == "skip"


# ---------------------------------------------------------------------------
# maybe_invoke_mirofish — integration of triage + callable
# ---------------------------------------------------------------------------

class TestMaybeInvokeMiroFish:
    def _make_mirofish(self, opinions: list):
        """Return a fake mirofish callable that returns the given opinions."""
        calls = []

        def mirofish(ticker: str, **kwargs):
            calls.append(ticker)
            return opinions

        mirofish.calls = calls
        return mirofish

    def test_invokes_mirofish_on_short_bucket(self):
        fake_opinions = ["op1", "op2"]
        mirofish = self._make_mirofish(fake_opinions)

        result, opinions = maybe_invoke_mirofish(
            mirofish, "AAPL", HorizonBucket.SHORT
        )
        assert result.should_invoke is True
        assert opinions == fake_opinions
        assert "AAPL" in mirofish.calls

    def test_skips_mirofish_on_medium_bucket(self):
        mirofish = self._make_mirofish(["op1"])
        result, opinions = maybe_invoke_mirofish(
            mirofish, "AAPL", HorizonBucket.MEDIUM
        )
        assert result.should_invoke is False
        assert opinions == []
        assert len(mirofish.calls) == 0  # not called

    def test_skips_mirofish_on_news_signal(self):
        mirofish = self._make_mirofish(["op1"])
        result, opinions = maybe_invoke_mirofish(
            mirofish, "AAPL", HorizonBucket.LONG, signal_kind="NEWS"
        )
        assert result.should_invoke is False
        assert len(mirofish.calls) == 0
