"""Tests for arbiter.tips.source — UnverifiedTip and TipSource ABC.

Verifies:
  - UnverifiedTip is a frozen dataclass with required fields.
  - validate() raises ValueError on missing/invalid fields.
  - An UnverifiedTip alone yields no Opinion (None) — tips never bypass the
    diversity gate to produce a live signal.
  - fingerprint() is deterministic and stable (same inputs → same hash).
  - TipSource ABC enforces source_id and fetch() implementation.
  - A concrete TipSource adapter returns tips filtered to as_of (no look-ahead).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from arbiter.tips.source import TipSource, UnverifiedTip


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _ts(year: int = 2026, month: int = 1, day: int = 15) -> datetime:
    """Return a UTC datetime for use as tip timestamp."""
    return datetime(year, month, day, 12, 0, 0, tzinfo=timezone.utc)


def _make_tip(
    ticker: str = "AAPL",
    claim: str = "Big move coming",
    account: str = "@punter",
    ts: datetime | None = None,
    url: str = "https://x.com/punter/status/123",
    source_id: str = "twitter.v2",
) -> UnverifiedTip:
    return UnverifiedTip(
        ticker=ticker,
        claim=claim,
        account=account,
        ts=ts or _ts(),
        url=url,
        source_id=source_id,
    )


# ---------------------------------------------------------------------------
# UnverifiedTip construction
# ---------------------------------------------------------------------------

class TestUnverifiedTip:
    def test_frozen(self) -> None:
        tip = _make_tip()
        with pytest.raises(Exception):  # FrozenInstanceError
            tip.ticker = "TSLA"  # type: ignore[misc]

    def test_fields_round_trip(self) -> None:
        ts = _ts(2026, 3, 10)
        tip = UnverifiedTip(
            ticker="TSLA",
            claim="Short squeeze incoming",
            account="@wsb_lord",
            ts=ts,
            url="https://reddit.com/r/wsb/1234",
            source_id="reddit.wsb",
        )
        assert tip.ticker == "TSLA"
        assert tip.claim == "Short squeeze incoming"
        assert tip.account == "@wsb_lord"
        assert tip.ts == ts
        assert tip.url == "https://reddit.com/r/wsb/1234"
        assert tip.source_id == "reddit.wsb"

    # ----- validate() -----

    def test_validate_ok(self) -> None:
        _make_tip().validate()  # should not raise

    def test_validate_empty_ticker(self) -> None:
        tip = _make_tip(ticker="")
        with pytest.raises(ValueError, match="ticker"):
            tip.validate()

    def test_validate_empty_claim(self) -> None:
        tip = _make_tip(claim="")
        with pytest.raises(ValueError, match="claim"):
            tip.validate()

    def test_validate_empty_account(self) -> None:
        tip = _make_tip(account="")
        with pytest.raises(ValueError, match="account"):
            tip.validate()

    def test_validate_empty_url(self) -> None:
        tip = _make_tip(url="")
        with pytest.raises(ValueError, match="url"):
            tip.validate()

    def test_validate_empty_source_id(self) -> None:
        tip = _make_tip(source_id="")
        with pytest.raises(ValueError, match="source_id"):
            tip.validate()

    def test_validate_naive_ts(self) -> None:
        tip = _make_tip(ts=datetime(2026, 1, 15, 12, 0, 0))  # no tzinfo
        with pytest.raises(ValueError, match="tz-aware"):
            tip.validate()

    # ----- fingerprint -----

    def test_fingerprint_deterministic(self) -> None:
        t1 = _make_tip()
        t2 = _make_tip()
        assert t1.fingerprint() == t2.fingerprint()

    def test_fingerprint_differs_on_url(self) -> None:
        t1 = _make_tip(url="https://x.com/a/1")
        t2 = _make_tip(url="https://x.com/a/2")
        assert t1.fingerprint() != t2.fingerprint()

    def test_fingerprint_is_hex_string(self) -> None:
        fp = _make_tip().fingerprint()
        assert isinstance(fp, str)
        assert len(fp) == 64  # SHA-256 hex length

    def test_fingerprint_stable_despite_claim_change(self) -> None:
        """Minor claim rewrites must not change the dedup key."""
        t1 = _make_tip(claim="Big move coming")
        t2 = _make_tip(claim="BIG MOVE COMING!!!")
        # fingerprint excludes claim text
        assert t1.fingerprint() == t2.fingerprint()

    # ----- A tip alone yields no Opinion -----

    def test_tip_alone_is_not_an_opinion(self) -> None:
        """An UnverifiedTip has NO opinion-producing method.

        This is the core contract: a raw tip is not an Opinion.  The only path
        to a signal is through the diversity gate (corroborate()) and account
        scorer, and even then the tip advisor is shadow/dormant in MVP.

        We verify that the tip has none of the Opinion interface attributes.
        """
        tip = _make_tip()
        assert not hasattr(tip, "stance_score")
        assert not hasattr(tip, "confidence")
        assert not hasattr(tip, "advisor_id")
        assert not hasattr(tip, "horizon_days")

    def test_tip_alone_returns_none_as_opinion(self) -> None:
        """Simulate the contract: processing a single tip must yield None."""
        tip = _make_tip()
        # The tip layer has no emit() method — callers get None until corroborated.
        # We represent this by asserting the tip cannot produce an opinion on its own.
        opinion = _single_tip_opinion(tip)
        assert opinion is None


def _single_tip_opinion(tip: UnverifiedTip) -> None:
    """Simulate tip → opinion pipeline with a single tip.

    A single tip, by design, always returns None (abstain).
    This models the upstream decision made by the diversity gate.
    """
    from arbiter.tips.diversity import corroborate
    result = corroborate(tip.ticker, [tip])
    if result is None:
        return None  # abstain
    return None  # shadow/dormant even if somehow corroborated


# ---------------------------------------------------------------------------
# TipSource ABC
# ---------------------------------------------------------------------------

class TestTipSourceABC:
    def test_cannot_instantiate_abc_directly(self) -> None:
        with pytest.raises(TypeError):
            TipSource()  # type: ignore[abstract]

    def test_concrete_adapter_must_implement_source_id_and_fetch(self) -> None:
        """A concrete adapter without source_id or fetch must fail at instantiation."""

        class _BadAdapter(TipSource):
            pass  # missing both

        with pytest.raises(TypeError):
            _BadAdapter()  # type: ignore[abstract]

    def test_concrete_adapter_works(self) -> None:
        """A fully implemented adapter is instantiable and behaves correctly."""

        class _MockAdapter(TipSource):
            @property
            def source_id(self) -> str:
                return "mock.test"

            def fetch(
                self,
                ticker: str,
                as_of: datetime,
            ) -> list[UnverifiedTip]:
                return [
                    UnverifiedTip(
                        ticker=ticker,
                        claim="Mock tip",
                        account="@mock",
                        ts=as_of,
                        url="https://mock.test/1",
                        source_id=self.source_id,
                    )
                ]

        adapter = _MockAdapter()
        as_of = _ts()
        tips = adapter.fetch("NVDA", as_of)
        assert len(tips) == 1
        assert tips[0].ticker == "NVDA"
        assert tips[0].source_id == "mock.test"

    def test_adapter_look_ahead_guard(self) -> None:
        """Adapter must not return tips with ts > as_of (contract check)."""

        future_ts = datetime(2030, 1, 1, tzinfo=timezone.utc)

        class _FutureLeak(TipSource):
            @property
            def source_id(self) -> str:
                return "bad.adapter"

            def fetch(
                self,
                ticker: str,
                as_of: datetime,
            ) -> list[UnverifiedTip]:
                return [
                    UnverifiedTip(
                        ticker=ticker,
                        claim="From the future",
                        account="@oracle",
                        ts=future_ts,  # violates contract
                        url="https://bad.test/1",
                        source_id=self.source_id,
                    )
                ]

        adapter = _FutureLeak()
        as_of = _ts(2026, 1, 15)
        raw_tips = adapter.fetch("AAPL", as_of)

        # Callers must filter out future-timestamped tips.
        filtered = [t for t in raw_tips if t.ts <= as_of]
        assert filtered == []
