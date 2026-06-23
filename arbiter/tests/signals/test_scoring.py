"""Tests for arbiter.signals.scoring — Lane 6.

Covers:
- Cold-start provider returns priors with 0 samples.
- gate_pass = False while sample count < threshold.
- gate_pass = True with sufficient empirical data.
- ScoreBundle produced by score_signal() combines axes correctly.
- Custom score_provider can be injected (Wave-C / Lane 14 hook).
- score_person returns per-person prior.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from arbiter.signals.detection import Signal, SignalType
from arbiter.signals.scoring import (
    ColdStartProvider,
    ScoreBundle,
    ScoreProvider,
    score_person,
    score_signal,
    score_signal_type,
    _GATE_MIN_SAMPLES,
    _GATE_MIN_ACCURACY,
    _PRIOR_ACCURACY,
    _PRIOR_PERSON_ACCURACY,
)


_UTC = timezone.utc
_AS_OF = datetime(2026, 6, 1, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(
    signal_type: SignalType = SignalType.CLUSTER_BUY,
    ticker: str = "AAPL",
    source: str = "form4",
    person_ids: tuple[str, ...] = ("P001", "P002"),
    filing_ids: tuple[str, ...] = ("F001", "F002"),
    conviction_score: float = 0.2,
) -> Signal:
    ts = datetime(2026, 5, 1, tzinfo=_UTC)
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


class EmpiricalProvider:
    """Stub empirical provider for testing Lane-14 wiring."""

    def __init__(
        self,
        signal_type_data: dict[str, tuple[float, int]],
        person_data: dict[str, tuple[float, int]],
    ) -> None:
        self._st = signal_type_data
        self._p = person_data

    def signal_type_score(self, signal_type, as_of, conn=None):
        return self._st.get(signal_type, (0.50, 0))

    def person_score(self, person_id, as_of, conn=None):
        return self._p.get(person_id, (0.50, 0))


# ---------------------------------------------------------------------------
# ColdStartProvider
# ---------------------------------------------------------------------------

class TestColdStartProvider:
    def test_cluster_buy_prior_returns_correct_accuracy(self):
        p = ColdStartProvider()
        acc, samples = p.signal_type_score(SignalType.CLUSTER_BUY.value, _AS_OF)
        assert acc == _PRIOR_ACCURACY[SignalType.CLUSTER_BUY.value]
        assert samples == 0

    def test_single_insider_prior(self):
        p = ColdStartProvider()
        acc, samples = p.signal_type_score(SignalType.SINGLE_INSIDER_BUY.value, _AS_OF)
        assert acc == _PRIOR_ACCURACY[SignalType.SINGLE_INSIDER_BUY.value]
        assert samples == 0

    def test_congress_prior(self):
        p = ColdStartProvider()
        acc, samples = p.signal_type_score(SignalType.CONGRESS_SECTOR.value, _AS_OF)
        assert acc == _PRIOR_ACCURACY[SignalType.CONGRESS_SECTOR.value]
        assert samples == 0

    def test_unknown_signal_type_returns_default_prior(self):
        p = ColdStartProvider()
        acc, samples = p.signal_type_score("some_future_signal", _AS_OF)
        assert 0.0 < acc <= 1.0
        assert samples == 0

    def test_person_prior_returns_near_coin_flip(self):
        p = ColdStartProvider()
        acc, samples = p.person_score("ANY_PERSON", _AS_OF)
        assert acc == _PRIOR_PERSON_ACCURACY
        assert samples == 0


# ---------------------------------------------------------------------------
# score_signal_type
# ---------------------------------------------------------------------------

class TestScoreSignalType:
    def test_cold_start_gate_fails(self):
        acc, samples, gate = score_signal_type(
            SignalType.CLUSTER_BUY.value, _AS_OF
        )
        assert samples == 0
        assert gate is False  # cold-start: 0 samples < min

    def test_gate_passes_with_empirical_data(self):
        provider = EmpiricalProvider(
            {SignalType.CLUSTER_BUY.value: (0.68, 20)}, {}
        )
        acc, samples, gate = score_signal_type(
            SignalType.CLUSTER_BUY.value, _AS_OF, score_provider=provider
        )
        assert gate is True
        assert samples == 20
        assert acc == 0.68

    def test_gate_fails_insufficient_samples(self):
        provider = EmpiricalProvider(
            {SignalType.CLUSTER_BUY.value: (0.80, 5)}, {}  # only 5 samples
        )
        _, samples, gate = score_signal_type(
            SignalType.CLUSTER_BUY.value, _AS_OF, score_provider=provider
        )
        assert gate is False

    def test_gate_fails_low_accuracy(self):
        provider = EmpiricalProvider(
            {SignalType.CLUSTER_BUY.value: (0.45, 50)}, {}  # accuracy too low
        )
        _, _, gate = score_signal_type(
            SignalType.CLUSTER_BUY.value, _AS_OF, score_provider=provider
        )
        assert gate is False


# ---------------------------------------------------------------------------
# score_person
# ---------------------------------------------------------------------------

class TestScorePerson:
    def test_cold_start_person_gate_fails(self):
        _, samples, gate = score_person("P001", _AS_OF)
        assert samples == 0
        assert gate is False

    def test_empirical_person_gate_passes(self):
        provider = EmpiricalProvider({}, {"P001": (0.70, 15)})
        acc, samples, gate = score_person("P001", _AS_OF, score_provider=provider)
        assert gate is True
        assert acc == 0.70

    def test_unknown_person_returns_prior(self):
        _, samples, gate = score_person("UNKNOWN_PERSON_XYZ", _AS_OF)
        assert samples == 0
        assert gate is False


# ---------------------------------------------------------------------------
# score_signal (combined)
# ---------------------------------------------------------------------------

class TestScoreSignal:
    def test_score_bundle_produced(self):
        sig = _make_signal()
        bundle = score_signal(sig, _AS_OF)
        assert isinstance(bundle, ScoreBundle)
        assert bundle.signal_type == SignalType.CLUSTER_BUY
        assert bundle.is_cold_start is True  # both sides cold

    def test_combined_score_is_weighted_blend(self):
        """combined = 0.6 * signal_type_acc + 0.4 * person_acc."""
        provider = EmpiricalProvider(
            {SignalType.CLUSTER_BUY.value: (0.70, 20)},
            {"P001": (0.60, 15), "P002": (0.60, 15)},
        )
        sig = _make_signal(person_ids=("P001", "P002"))
        bundle = score_signal(sig, _AS_OF, score_provider=provider)

        expected = round(0.60 * 0.70 + 0.40 * 0.60, 4)
        assert bundle.combined_score == pytest.approx(expected, abs=1e-4)

    def test_person_gate_fails_if_any_person_below_threshold(self):
        provider = EmpiricalProvider(
            {SignalType.CLUSTER_BUY.value: (0.70, 20)},
            {"P001": (0.80, 20), "P002": (0.50, 3)},  # P002 fails
        )
        sig = _make_signal(person_ids=("P001", "P002"))
        bundle = score_signal(sig, _AS_OF, score_provider=provider)
        assert bundle.person_gate_pass is False

    def test_both_gates_pass_when_data_sufficient(self):
        provider = EmpiricalProvider(
            {SignalType.CLUSTER_BUY.value: (0.65, 20)},
            {"P001": (0.65, 15), "P002": (0.65, 15)},
        )
        sig = _make_signal(person_ids=("P001", "P002"))
        bundle = score_signal(sig, _AS_OF, score_provider=provider)
        assert bundle.signal_type_gate_pass is True
        assert bundle.person_gate_pass is True
        assert bundle.is_cold_start is False

    def test_cold_start_flag_when_person_has_no_samples(self):
        provider = EmpiricalProvider(
            {SignalType.CLUSTER_BUY.value: (0.65, 20)},
            {},  # no person data
        )
        sig = _make_signal(person_ids=("P001",))
        bundle = score_signal(sig, _AS_OF, score_provider=provider)
        assert bundle.is_cold_start is True  # person side cold

    def test_score_provider_injected_correctly(self):
        """Verifies the score_provider hook is used (Wave-C / Lane 14 seam)."""
        called_with: list[str] = []

        class CapturingProvider:
            def signal_type_score(self, signal_type, as_of, conn=None):
                called_with.append(f"st:{signal_type}")
                return 0.62, 0

            def person_score(self, person_id, as_of, conn=None):
                called_with.append(f"p:{person_id}")
                return 0.55, 0

        sig = _make_signal(person_ids=("PX",))
        score_signal(sig, _AS_OF, score_provider=CapturingProvider())
        assert any(c.startswith("st:") for c in called_with)
        assert any(c.startswith("p:") for c in called_with)
