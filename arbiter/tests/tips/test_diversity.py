"""Tests for arbiter.tips.diversity — source-diversity gate.

Verifies:
  - A single tip (one source) yields corroborated=False / None (abstain).
  - Two tips from the SAME source_id yield corroborated=False (counts as one voice).
  - Two tips from DIFFERENT source_ids yield corroborated=True.
  - Three-source corroboration works.
  - Ticker mismatch: tips for wrong ticker are excluded.
  - Empty tip list → not corroborated.
  - corroborate() convenience function returns frozenset on success, None on fail.
  - DiversityGate.evaluate() returns correct CorroborationResult fields.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from arbiter.tips.diversity import CorroborationResult, DiversityGate, corroborate
from arbiter.tips.source import UnverifiedTip


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts() -> datetime:
    return datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _tip(
    ticker: str = "AAPL",
    source_id: str = "twitter.v2",
    account: str = "@default",
    url: str | None = None,
) -> UnverifiedTip:
    return UnverifiedTip(
        ticker=ticker,
        claim="Hot tip",
        account=account,
        ts=_ts(),
        url=url or f"https://{source_id}/1/{account}",
        source_id=source_id,
    )


# ---------------------------------------------------------------------------
# DiversityGate.evaluate()
# ---------------------------------------------------------------------------

class TestDiversityGate:
    def setup_method(self) -> None:
        self.gate = DiversityGate()

    def test_empty_tips_not_corroborated(self) -> None:
        result = self.gate.evaluate("AAPL", [])
        assert result.corroborated is False
        assert result.n_sources == 0
        assert result.tip_count == 0

    def test_single_tip_not_corroborated(self) -> None:
        tips = [_tip("AAPL", "twitter.v2")]
        result = self.gate.evaluate("AAPL", tips)
        assert result.corroborated is False
        assert result.n_sources == 1

    def test_two_tips_same_source_not_corroborated(self) -> None:
        """Two different accounts on the same platform count as ONE voice."""
        tips = [
            _tip("AAPL", "twitter.v2", "@guru1"),
            _tip("AAPL", "twitter.v2", "@guru2"),
        ]
        result = self.gate.evaluate("AAPL", tips)
        assert result.corroborated is False
        assert result.n_sources == 1
        assert "twitter.v2" in result.independent_sources

    def test_two_tips_different_sources_corroborated(self) -> None:
        tips = [
            _tip("AAPL", "twitter.v2"),
            _tip("AAPL", "reddit.wsb"),
        ]
        result = self.gate.evaluate("AAPL", tips)
        assert result.corroborated is True
        assert result.n_sources == 2
        assert result.independent_sources == frozenset({"twitter.v2", "reddit.wsb"})

    def test_three_sources_corroborated(self) -> None:
        tips = [
            _tip("TSLA", "twitter.v2"),
            _tip("TSLA", "reddit.wsb"),
            _tip("TSLA", "stocktwits.api"),
        ]
        result = self.gate.evaluate("TSLA", tips)
        assert result.corroborated is True
        assert result.n_sources == 3

    def test_ticker_mismatch_excluded(self) -> None:
        """Tips for a different ticker are excluded from the count."""
        tips = [
            _tip("AAPL", "twitter.v2"),    # correct ticker
            _tip("MSFT", "reddit.wsb"),     # wrong ticker — should not count
        ]
        result = self.gate.evaluate("AAPL", tips)
        assert result.corroborated is False
        assert result.n_sources == 1
        assert result.tip_count == 1  # only the AAPL tip counts

    def test_result_tip_count_includes_duplicates(self) -> None:
        """tip_count reflects all matching tips, even from same source."""
        tips = [
            _tip("AAPL", "twitter.v2", "@a"),
            _tip("AAPL", "twitter.v2", "@b"),
            _tip("AAPL", "reddit.wsb", "@c"),
        ]
        result = self.gate.evaluate("AAPL", tips)
        assert result.tip_count == 3
        assert result.corroborated is True

    def test_result_corroboration_result_dataclass(self) -> None:
        tips = [
            _tip("NVDA", "twitter.v2"),
            _tip("NVDA", "fintwit.scrape"),
        ]
        result = self.gate.evaluate("NVDA", tips)
        assert isinstance(result, CorroborationResult)
        assert result.ticker == "NVDA"

    def test_many_accounts_same_source_still_one_voice(self) -> None:
        """Even 100 accounts from one source count as one independent source."""
        tips = [
            _tip("GME", "twitter.v2", f"@pump_account_{i}", f"https://x.com/{i}")
            for i in range(100)
        ]
        result = self.gate.evaluate("GME", tips)
        assert result.corroborated is False
        assert result.n_sources == 1
        # This is the key anti-manipulation property: coordinated pump across
        # many accounts on one platform is still treated as a single source.


# ---------------------------------------------------------------------------
# corroborate() convenience function
# ---------------------------------------------------------------------------

class TestCorroborate:
    def test_no_tips_returns_none(self) -> None:
        result = corroborate("AAPL", [])
        assert result is None

    def test_single_source_returns_none(self) -> None:
        tips = [_tip("AAPL", "twitter.v2")]
        result = corroborate("AAPL", tips)
        assert result is None

    def test_two_sources_returns_frozenset(self) -> None:
        tips = [
            _tip("AAPL", "twitter.v2"),
            _tip("AAPL", "reddit.wsb"),
        ]
        result = corroborate("AAPL", tips)
        assert result is not None
        assert isinstance(result, frozenset)
        assert "twitter.v2" in result
        assert "reddit.wsb" in result

    def test_wrong_ticker_returns_none(self) -> None:
        tips = [
            _tip("MSFT", "twitter.v2"),
            _tip("MSFT", "reddit.wsb"),
        ]
        result = corroborate("AAPL", tips)  # asking about AAPL
        assert result is None

    def test_abstain_is_none_not_zero(self) -> None:
        """INTERFACES.md §11: abstain MUST be None, never 0.0 or False."""
        result = corroborate("AAPL", [])
        # Explicitly check it is None, not just falsy
        assert result is None
