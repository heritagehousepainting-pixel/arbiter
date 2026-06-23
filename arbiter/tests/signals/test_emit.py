"""Tests for arbiter.signals.emit — Lane 6.

Covers:
- Emitted Opinion is valid + passes validate_opinion.
- Form 4 → LONG bucket (horizon_days=180).
- Congress → MEDIUM bucket (horizon_days=90).
- 10b5-1 signals abstain (return None) — via zero-conviction guard.
- Weak signal (low combined_score) abstains.
- Zero-conviction signal abstains.
- No filing IDs abstains.
- source_fingerprint is deterministic hash of filing IDs.
- advisor_id is correct per source.
- as_of must be tz-aware (raises ValueError otherwise).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from arbiter.contract.opinion import Opinion, validate_opinion
from arbiter.signals.detection import Signal, SignalType
import arbiter.signals.emit as _emit_module
from arbiter.signals.emit import (
    _HORIZON_DAYS_CONGRESS,
    _HORIZON_DAYS_FORM4,
    _MIN_COMBINED_SCORE,
    _ADVISOR_ID_CONGRESS,
    _ADVISOR_ID_FORM4,
    emit_opinion,
)
from arbiter.signals.scoring import ScoreBundle
from arbiter.types import HorizonBucket


_UTC = timezone.utc
_AS_OF = datetime(2026, 6, 1, 12, 0, 0, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _signal(
    signal_type: SignalType = SignalType.CLUSTER_BUY,
    ticker: str = "AAPL",
    source: str = "form4",
    person_ids: tuple[str, ...] = ("P001", "P002"),
    filing_ids: tuple[str, ...] = ("F001", "F002"),
    conviction_score: float = 0.3,
) -> Signal:
    ts = datetime(2026, 5, 15, tzinfo=_UTC)
    return Signal(
        signal_type=signal_type,
        ticker=ticker,
        source=source,
        person_ids=person_ids,
        filing_ids=filing_ids,
        window_start=ts,
        window_end=ts,
        conviction_score=conviction_score,
        as_of=_AS_OF,
    )


def _cold_bundle(
    signal_type: SignalType = SignalType.CLUSTER_BUY,
    combined_score: float = 0.62,
) -> ScoreBundle:
    return ScoreBundle(
        signal_type=signal_type,
        signal_type_accuracy=0.62,
        signal_type_samples=0,
        signal_type_gate_pass=False,
        person_ids=("P001", "P002"),
        person_accuracy_avg=0.52,
        person_min_samples=0,
        person_gate_pass=False,
        combined_score=combined_score,
        is_cold_start=True,
    )


# ---------------------------------------------------------------------------
# Horizon mapping
# ---------------------------------------------------------------------------

class TestHorizonMapping:
    def test_form4_maps_to_long_bucket(self):
        sig = _signal(source="form4")
        op = emit_opinion(sig, _AS_OF)
        assert op is not None
        assert op.horizon_days == _HORIZON_DAYS_FORM4
        assert op.horizon_bucket == HorizonBucket.LONG

    def test_congress_maps_to_medium_bucket(self):
        sig = _signal(source="congress", signal_type=SignalType.CONGRESS_SECTOR)
        op = emit_opinion(sig, _AS_OF)
        assert op is not None
        assert op.horizon_days == _HORIZON_DAYS_CONGRESS
        assert op.horizon_bucket == HorizonBucket.MEDIUM


# ---------------------------------------------------------------------------
# Advisor ID
# ---------------------------------------------------------------------------

class TestAdvisorId:
    def test_form4_advisor_id(self):
        sig = _signal(source="form4")
        op = emit_opinion(sig, _AS_OF)
        assert op is not None
        assert op.advisor_id == _ADVISOR_ID_FORM4

    def test_congress_advisor_id(self):
        sig = _signal(source="congress")
        op = emit_opinion(sig, _AS_OF)
        assert op is not None
        assert op.advisor_id == _ADVISOR_ID_CONGRESS


# ---------------------------------------------------------------------------
# Opinion contract
# ---------------------------------------------------------------------------

class TestOpinionContract:
    def test_emitted_opinion_passes_validate_opinion(self):
        sig = _signal()
        op = emit_opinion(sig, _AS_OF)
        assert op is not None
        validate_opinion(op)  # must not raise

    def test_stance_is_positive_for_buy_signal(self):
        sig = _signal()
        op = emit_opinion(sig, _AS_OF)
        assert op is not None
        assert op.stance_score > 0.0

    def test_stance_never_zero(self):
        sig = _signal(conviction_score=0.001)
        op = emit_opinion(sig, _AS_OF)
        # either abstain or positive stance; never 0.0
        if op is not None:
            assert op.stance_score != 0.0

    def test_run_group_id_is_non_empty(self):
        sig = _signal()
        op1 = emit_opinion(sig, _AS_OF)
        op2 = emit_opinion(sig, _AS_OF)
        assert op1 is not None and op2 is not None
        assert op1.run_group_id != ""
        assert op2.run_group_id != ""
        # Each call should produce a FRESH ULID (different).
        assert op1.run_group_id != op2.run_group_id

    def test_source_fingerprint_is_deterministic(self):
        sig = _signal(filing_ids=("F001", "F002"))
        op1 = emit_opinion(sig, _AS_OF)
        op2 = emit_opinion(sig, _AS_OF)
        assert op1 is not None and op2 is not None
        assert op1.source_fingerprint == op2.source_fingerprint

    def test_source_fingerprint_is_sha256_of_sorted_filing_ids(self):
        filing_ids = ("F002", "F001")  # unsorted deliberately
        sig = _signal(filing_ids=filing_ids)
        op = emit_opinion(sig, _AS_OF)
        assert op is not None
        expected = hashlib.sha256(":".join(sorted(filing_ids)).encode()).hexdigest()
        assert op.source_fingerprint == expected

    def test_as_of_is_tz_aware(self):
        sig = _signal()
        op = emit_opinion(sig, _AS_OF)
        assert op is not None
        assert op.as_of.tzinfo is not None

    def test_naive_as_of_raises(self):
        sig = _signal()
        naive = datetime(2026, 6, 1, 12, 0, 0)  # no tzinfo
        with pytest.raises(ValueError):
            emit_opinion(sig, naive)


# ---------------------------------------------------------------------------
# Abstention cases
# ---------------------------------------------------------------------------

class TestAbstention:
    def test_no_filing_ids_abstains(self):
        sig = _signal(filing_ids=())
        op = emit_opinion(sig, _AS_OF)
        assert op is None

    def test_zero_conviction_abstains(self):
        sig = _signal(conviction_score=0.0)
        op = emit_opinion(sig, _AS_OF)
        assert op is None

    def test_weak_combined_score_abstains(self):
        """Score bundle with combined_score below threshold → abstain."""
        sig = _signal(conviction_score=0.3)
        weak_bundle = _cold_bundle(combined_score=_MIN_COMBINED_SCORE - 0.01)
        op = emit_opinion(sig, _AS_OF, score_bundle=weak_bundle)
        assert op is None

    def test_abstain_returns_none_not_zero_stance(self):
        """Abstain must be None, NEVER stance_score=0.0."""
        sig = _signal(filing_ids=())
        op = emit_opinion(sig, _AS_OF)
        assert op is None  # not Opinion(stance_score=0.0, ...)

    def test_strong_bundle_does_not_abstain(self):
        """A sufficient bundle should produce a valid opinion."""
        sig = _signal(conviction_score=0.3)
        strong_bundle = _cold_bundle(combined_score=_MIN_COMBINED_SCORE + 0.05)
        op = emit_opinion(sig, _AS_OF, score_bundle=strong_bundle)
        assert op is not None
        validate_opinion(op)

    def test_no_score_bundle_uses_conviction_as_confidence(self):
        """Without a bundle, conviction_score drives confidence."""
        sig = _signal(conviction_score=0.4)
        op = emit_opinion(sig, _AS_OF, score_bundle=None)
        assert op is not None
        # Confidence should be ~conviction (clamped to (0, 1])
        assert op.confidence > 0.0

    def test_10b5_1_weak_conviction_abstains(self):
        """A 10b5-1 style filing typically has zero conviction → abstains."""
        # Detection layer already filters 10b5-1; emit layer abstains on 0 conviction.
        sig = _signal(conviction_score=0.0)
        op = emit_opinion(sig, _AS_OF)
        assert op is None


# ---------------------------------------------------------------------------
# Module-level import hygiene
# ---------------------------------------------------------------------------

class TestModuleLevelImports:
    def test_confidence_source_is_module_level_import(self):
        """ConfidenceSource must be importable from the module namespace
        (not buried inside a function body)."""
        from arbiter.types import ConfidenceSource
        # Verify the emit module has ConfidenceSource at module level.
        assert hasattr(_emit_module, "ConfidenceSource")
        assert _emit_module.ConfidenceSource is ConfidenceSource

    def test_emit_module_imports_do_not_require_function_call(self):
        """Importing arbiter.signals.emit must not raise; all deps resolve at import."""
        import importlib
        mod = importlib.import_module("arbiter.signals.emit")
        assert callable(mod.emit_opinion)


# ---------------------------------------------------------------------------
# Activist (Schedule 13D/13G, source="form13d") emit branch — Wave 2
# ---------------------------------------------------------------------------

def _activist_signal(*, txn_type: str = "P", conviction: float = 0.70) -> Signal:
    ts = datetime(2026, 5, 15, tzinfo=_UTC)
    return Signal(
        signal_type=SignalType.ACTIVIST_STAKE,
        ticker="AAPL",
        source="form13d",
        person_ids=("ACT001",),
        filing_ids=("S001",),
        window_start=ts,
        window_end=ts,
        conviction_score=conviction,
        meta={"schedule": "13D", "percent_of_class": 8.5,
              "is_activist": True, "txn_type": txn_type},
        as_of=_AS_OF,
    )


class TestActivistEmit:
    def test_emit_activist_long_opinion(self):
        from arbiter.signals.emit import _ADVISOR_ID_ACTIVIST, _HORIZON_DAYS_ACTIVIST
        from arbiter.types import ConfidenceSource
        op = emit_opinion(_activist_signal(txn_type="P"), _AS_OF)
        assert op is not None
        assert op.advisor_id == _ADVISOR_ID_ACTIVIST == "A1.activist"
        assert op.horizon_days == _HORIZON_DAYS_ACTIVIST == 180
        assert op.horizon_bucket == HorizonBucket.LONG
        assert op.stance_score > 0
        assert op.confidence_source == ConfidenceSource.MODELED
        validate_opinion(op)  # must not raise

    def test_emit_activist_exit_negative_stance(self):
        op = emit_opinion(_activist_signal(txn_type="S"), _AS_OF)
        assert op is not None
        assert op.stance_score < 0
        validate_opinion(op)  # negative stance is in-range [-1, 1]

    def test_emit_activist_abstains_zero_conviction(self):
        # conviction below _MIN_CONVICTION abstains
        op = emit_opinion(_activist_signal(conviction=0.0), _AS_OF)
        assert op is None
