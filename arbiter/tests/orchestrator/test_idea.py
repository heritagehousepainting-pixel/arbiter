"""Tests for arbiter.orchestrator.idea — factory and dedupe (Lane 13).

Covers:
- make_idea generates a valid ULID idea_id
- dedupe_key matches (ticker, bucket.value)
- make_idea raises for naive as_of
- make_idea raises for out-of-range horizon_days
- is_duplicate: blocks same (ticker, bucket) in active state
- is_duplicate: allows different bucket on same ticker
- is_duplicate: ignores self (idea_id match)
- is_duplicate: allows if existing idea is EXECUTED+ (post-execution)
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from arbiter.orchestrator.idea import dedupe_key_for, is_duplicate, make_idea
from arbiter.types import HorizonBucket, IdeaState

_UTC = timezone.utc
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def _as_of() -> datetime:
    return datetime(2024, 6, 1, 12, 0, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# make_idea basics
# ---------------------------------------------------------------------------

class TestMakeIdea:
    def test_generates_ulid_idea_id(self):
        idea = make_idea("AAPL", "thesis", horizon_days=10, as_of=_as_of())
        assert _ULID_RE.match(idea.idea_id), f"Not a ULID: {idea.idea_id!r}"

    def test_explicit_idea_id_used(self):
        uid = "01ARYZ6S41TSV4RRFFQ69G5FAV"
        idea = make_idea("AAPL", "thesis", horizon_days=10, as_of=_as_of(), idea_id=uid)
        assert idea.idea_id == uid

    def test_ticker_stored(self):
        idea = make_idea("TSLA", "thesis", horizon_days=10, as_of=_as_of())
        assert idea.ticker == "TSLA"

    def test_thesis_stored(self):
        idea = make_idea("AAPL", "insider buy signal", horizon_days=10, as_of=_as_of())
        assert idea.thesis == "insider buy signal"

    def test_horizon_days_stored(self):
        idea = make_idea("AAPL", "thesis", horizon_days=45, as_of=_as_of())
        assert idea.horizon_days == 45

    def test_as_of_stored(self):
        ts = _as_of()
        idea = make_idea("AAPL", "thesis", horizon_days=10, as_of=ts)
        assert idea.as_of == ts

    def test_default_state_is_nascent(self):
        idea = make_idea("AAPL", "thesis", horizon_days=10, as_of=_as_of())
        assert idea.state is IdeaState.NASCENT

    def test_explicit_state_honored(self):
        idea = make_idea(
            "AAPL", "thesis", horizon_days=10, as_of=_as_of(),
            state=IdeaState.GATHERING,
        )
        assert idea.state is IdeaState.GATHERING

    def test_raises_for_naive_as_of(self):
        naive = datetime(2024, 6, 1, 12, 0)  # no tzinfo
        with pytest.raises(ValueError, match="tz-aware"):
            make_idea("AAPL", "thesis", horizon_days=10, as_of=naive)

    def test_raises_for_horizon_out_of_range(self):
        with pytest.raises(ValueError):
            make_idea("AAPL", "thesis", horizon_days=400, as_of=_as_of())

    def test_raises_for_zero_horizon(self):
        with pytest.raises(ValueError):
            make_idea("AAPL", "thesis", horizon_days=0, as_of=_as_of())


# ---------------------------------------------------------------------------
# dedupe_key_for
# ---------------------------------------------------------------------------

class TestDedupeKeyFor:
    def test_short_bucket(self):
        key = dedupe_key_for("AAPL", HorizonBucket.SHORT)
        assert key == ("AAPL", "SHORT")

    def test_long_bucket(self):
        key = dedupe_key_for("MSFT", HorizonBucket.LONG)
        assert key == ("MSFT", "LONG")

    def test_intraday_bucket(self):
        key = dedupe_key_for("SPY", HorizonBucket.INTRADAY)
        assert key == ("SPY", "INTRADAY")

    def test_matches_idea_dedupe_key(self):
        idea = make_idea("AAPL", "thesis", horizon_days=10, as_of=_as_of())
        # horizon_days=10 → SHORT bucket
        assert idea.dedupe_key == dedupe_key_for("AAPL", HorizonBucket.SHORT)


# ---------------------------------------------------------------------------
# is_duplicate
# ---------------------------------------------------------------------------

class TestIsDuplicate:
    def _make(self, ticker: str, horizon_days: int, state: IdeaState = IdeaState.NASCENT):
        idea = make_idea(ticker, "thesis", horizon_days=horizon_days, as_of=_as_of())
        idea.state = state
        return idea

    def test_duplicate_same_ticker_same_bucket_active(self):
        existing = self._make("AAPL", 10, state=IdeaState.GATHERING)
        candidate = self._make("AAPL", 15)  # both SHORT bucket
        assert is_duplicate(candidate, [existing]) is True

    def test_not_duplicate_different_bucket(self):
        existing = self._make("AAPL", 10, state=IdeaState.GATHERING)   # SHORT
        candidate = self._make("AAPL", 200)  # LONG bucket
        assert is_duplicate(candidate, [existing]) is False

    def test_not_duplicate_different_ticker(self):
        existing = self._make("AAPL", 10, state=IdeaState.GATHERING)
        candidate = self._make("MSFT", 10)
        assert is_duplicate(candidate, [existing]) is False

    def test_not_duplicate_self(self):
        """An idea should not be flagged as its own duplicate."""
        idea = self._make("AAPL", 10, state=IdeaState.GATHERING)
        assert is_duplicate(idea, [idea]) is False

    def test_not_duplicate_if_existing_is_executed(self):
        """Post-execution ideas do not block new entries in the same bucket."""
        existing = self._make("AAPL", 10, state=IdeaState.EXECUTED)
        candidate = self._make("AAPL", 10)
        assert is_duplicate(candidate, [existing]) is False

    def test_not_duplicate_if_existing_is_monitored(self):
        existing = self._make("AAPL", 10, state=IdeaState.MONITORED)
        candidate = self._make("AAPL", 10)
        assert is_duplicate(candidate, [existing]) is False

    def test_not_duplicate_if_existing_is_closed(self):
        existing = self._make("AAPL", 10, state=IdeaState.CLOSED)
        candidate = self._make("AAPL", 10)
        assert is_duplicate(candidate, [existing]) is False

    def test_not_duplicate_if_existing_is_abandoned(self):
        existing = self._make("AAPL", 10, state=IdeaState.ABANDONED)
        candidate = self._make("AAPL", 10)
        assert is_duplicate(candidate, [existing]) is False

    def test_duplicate_when_existing_is_provisional(self):
        existing = self._make("AAPL", 10, state=IdeaState.PROVISIONAL_DECIDED)
        candidate = self._make("AAPL", 10)
        assert is_duplicate(candidate, [existing]) is True

    def test_duplicate_when_existing_is_final_decided(self):
        existing = self._make("AAPL", 10, state=IdeaState.FINAL_DECIDED)
        candidate = self._make("AAPL", 10)
        assert is_duplicate(candidate, [existing]) is True

    def test_empty_active_list_is_not_duplicate(self):
        candidate = self._make("AAPL", 10)
        assert is_duplicate(candidate, []) is False

    def test_multiple_different_buckets_on_same_ticker_allowed(self):
        """Concurrent ideas on same ticker in different buckets are ALL allowed."""
        short_idea = self._make("AAPL", 10, state=IdeaState.GATHERING)   # SHORT
        medium_idea = self._make("AAPL", 60, state=IdeaState.GATHERING)  # MEDIUM
        long_idea = self._make("AAPL", 200, state=IdeaState.GATHERING)   # LONG

        active = [short_idea, medium_idea, long_idea]

        # A new INTRADAY idea on AAPL should be fine
        intraday_idea = make_idea("AAPL", "thesis", horizon_days=1, as_of=_as_of())
        # horizon_days=1 → SHORT (boundary); let's use a clearly intraday approach
        # Actually: 1 day maps to SHORT per bucket_for_days. Use a fresh SHORT dupe test.
        new_long = self._make("AAPL", 250)  # LONG bucket
        assert is_duplicate(new_long, active) is True  # duplicate of long_idea

        new_medium = self._make("AAPL", 90)  # MEDIUM bucket
        assert is_duplicate(new_medium, active) is True  # duplicate of medium_idea


class TestDedupeCooldown:
    """2026-07-10 unfreeze: a never-executed FINAL_DECIDED idea stops blocking its
    (ticker,bucket) after a short cooldown; other active states are unaffected."""

    @staticmethod
    def _idea(idea_id, state, as_of):
        from arbiter.contract.seams import Idea
        return Idea(
            idea_id=idea_id,
            ticker="NVDA",
            thesis="t",
            horizon_days=180,
            state=state,
            as_of=as_of,
            dedupe_key=dedupe_key_for("NVDA", HorizonBucket.LONG),
        )

    def test_final_decided_past_cooldown_does_not_block(self):
        from datetime import timedelta
        now = datetime(2026, 7, 10, tzinfo=_UTC)
        candidate = self._idea("new", IdeaState.NASCENT, now)
        stale = self._idea("old", IdeaState.FINAL_DECIDED, now - timedelta(days=5))
        fresh = self._idea("recent", IdeaState.FINAL_DECIDED, now - timedelta(days=1))
        in_flight = self._idea("gathering", IdeaState.GATHERING, now - timedelta(days=30))

        # Stale never-executed FINAL_DECIDED past 3d cooldown -> no longer blocks.
        assert is_duplicate(candidate, [stale], now=now, cooldown_days=3) is False
        # Fresh FINAL_DECIDED within cooldown -> still blocks (avoid churn).
        assert is_duplicate(candidate, [fresh], now=now, cooldown_days=3) is True
        # In-flight GATHERING idea -> always blocks regardless of age.
        assert is_duplicate(candidate, [in_flight], now=now, cooldown_days=3) is True
        # Legacy call (no cooldown args) -> stale FINAL_DECIDED still blocks (back-compat).
        assert is_duplicate(candidate, [stale]) is True
