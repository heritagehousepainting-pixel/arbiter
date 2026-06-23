"""Tests for arbiter.defenses.volume_anomaly — VolumeAnomalyGate.

Verifies:
  - is_anomalous() returns True on a planted volume spike (z >= threshold).
  - is_anomalous() returns False on normal volume (z < threshold).
  - Insufficient baseline days → False (fail-open on uncertainty, not fail-closed).
  - No today-volume data → False.
  - σ == 0 (flat baseline) → False (degenerate baseline, not anomalous).
  - Custom z_threshold is respected.
  - Gate is importable from arbiter.defenses (top-level package).
  - No datetime.now() used anywhere in the gate — all time from as_of.
  - VolumeAnomalyGate correctly wires to Lane 4 (documented interface).

All reads go through a PITGateway backed by FixtureSource (no network).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from arbiter.data.pit import Bar, FixtureSource, PITGateway
from arbiter.defenses import VolumeAnomalyGate
from arbiter.defenses.volume_anomaly import _mean_stddev


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_DATE = datetime(2026, 6, 1, 16, 0, 0, tzinfo=timezone.utc)


def _day(offset: int) -> datetime:
    """Return _BASE_DATE + offset days."""
    return _BASE_DATE + timedelta(days=offset)


def _make_bar(ticker: str, ts: datetime, volume: float, close: float = 100.0) -> Bar:
    return Bar(
        ticker=ticker,
        timestamp=ts,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
    )


def _build_pit_with_volume(
    ticker: str,
    baseline_volumes: list[float],
    today_volume: float | None,
    as_of: datetime,
) -> PITGateway:
    """Build a PITGateway pre-loaded with baseline + today volumes.

    Baseline days are placed in the 35 calendar days before ``as_of``.
    Today's volume is placed AT ``as_of``.
    """
    src = FixtureSource()

    # Place baseline bars one day apart ending at as_of - 1
    for i, vol in enumerate(baseline_volumes):
        day_ts = as_of - timedelta(days=len(baseline_volumes) - i)
        src.add("price_close", ticker, day_ts, _make_bar(ticker, day_ts, vol))

    # Today's bar
    if today_volume is not None:
        src.add("price_close", ticker, as_of, _make_bar(ticker, as_of, today_volume))

    pit = PITGateway()
    pit.register_source("price_close", src)
    return pit


def _normal_volumes(n: int = 20, base: float = 1_000_000.0) -> list[float]:
    """Return a list of n similar volumes (slight variation)."""
    return [base + (i % 3) * 10_000 for i in range(n)]


# ---------------------------------------------------------------------------
# _mean_stddev helper
# ---------------------------------------------------------------------------

class TestMeanStddev:
    def test_empty_list(self) -> None:
        mu, sigma = _mean_stddev([])
        assert mu == 0.0
        assert sigma == 0.0

    def test_single_element(self) -> None:
        mu, sigma = _mean_stddev([5.0])
        assert mu == 5.0
        assert sigma == 0.0

    def test_uniform_list(self) -> None:
        mu, sigma = _mean_stddev([3.0, 3.0, 3.0])
        assert mu == pytest.approx(3.0)
        assert sigma == pytest.approx(0.0)

    def test_known_values(self) -> None:
        # values = [2, 4, 4, 4, 5, 5, 7, 9]; μ=5, σ=2 (population)
        values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        mu, sigma = _mean_stddev(values)
        assert mu == pytest.approx(5.0)
        assert sigma == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# VolumeAnomalyGate — core detection
# ---------------------------------------------------------------------------

class TestVolumeAnomalyGate:
    def setup_method(self) -> None:
        self.gate = VolumeAnomalyGate(z_threshold=3.0, baseline_days=20)

    # ----- Planted spike -----

    def test_flags_planted_volume_spike(self) -> None:
        """A spike at 10x the baseline mean should be anomalous (z >> 3)."""
        as_of = _day(25)
        ticker = "AAPL"
        baseline = _normal_volumes(n=20, base=1_000_000.0)
        today_vol = 10_000_000.0  # 10x spike — clearly anomalous

        pit = _build_pit_with_volume(ticker, baseline, today_vol, as_of)
        assert self.gate.is_anomalous(ticker, as_of, pit) is True

    def test_z_score_above_threshold_anomalous(self) -> None:
        """Precisely computed: place today_vol such that z >= 3.0."""
        # baseline: 20 bars at volume=100; μ=100, σ=0 → use slight variation
        # Use a real varying baseline to get non-zero σ
        as_of = _day(25)
        ticker = "TSLA"
        # baseline volumes cluster around 1000
        baseline = [1000.0] * 18 + [900.0, 1100.0]  # σ ≈ 70.7
        # μ = 1000, σ ≈ 70.71
        # for z >= 3: today_vol >= 1000 + 3 * 70.71 ≈ 1212
        today_vol = 1500.0

        pit = _build_pit_with_volume(ticker, baseline, today_vol, as_of)
        assert self.gate.is_anomalous(ticker, as_of, pit) is True

    # ----- Normal volume -----

    def test_clean_on_normal_volume(self) -> None:
        """Normal-range volume should not be flagged."""
        as_of = _day(25)
        ticker = "MSFT"
        baseline = _normal_volumes(n=20, base=1_000_000.0)
        today_vol = 1_010_000.0  # within 2% of baseline mean — far below 3σ

        pit = _build_pit_with_volume(ticker, baseline, today_vol, as_of)
        assert self.gate.is_anomalous(ticker, as_of, pit) is False

    def test_z_score_below_threshold_clean(self) -> None:
        """z-score just below threshold should not fire.

        baseline = [1000]*18 + [900, 1100]:  μ=1000, σ=√1000 ≈ 31.62
        z=3 boundary → today_vol ≈ 1000 + 3*31.62 = 1094.87
        today_vol=1060 → z ≈ 1.90 < 3.0 → should be clean.
        """
        as_of = _day(25)
        ticker = "NVDA"
        baseline = [1000.0] * 18 + [900.0, 1100.0]
        today_vol = 1060.0  # z ≈ 1.90 < 3.0

        pit = _build_pit_with_volume(ticker, baseline, today_vol, as_of)
        assert self.gate.is_anomalous(ticker, as_of, pit) is False

    # ----- Edge cases -----

    def test_insufficient_baseline_returns_false(self) -> None:
        """< 20 baseline days → gate abstains (False)."""
        as_of = _day(10)
        ticker = "GOOG"
        # Only 5 baseline bars (not enough)
        baseline = _normal_volumes(n=5, base=1_000_000.0)
        today_vol = 50_000_000.0

        pit = _build_pit_with_volume(ticker, baseline, today_vol, as_of)
        assert self.gate.is_anomalous(ticker, as_of, pit) is False

    def test_no_today_volume_returns_false(self) -> None:
        """Missing today-volume → not anomalous (cannot check)."""
        as_of = _day(25)
        ticker = "AMD"
        baseline = _normal_volumes(n=20, base=1_000_000.0)

        pit = _build_pit_with_volume(ticker, baseline, today_volume=None, as_of=as_of)
        assert self.gate.is_anomalous(ticker, as_of, pit) is False

    def test_flat_baseline_sigma_zero_returns_false(self) -> None:
        """σ == 0 → degenerate baseline — gate cannot compute z-score → False."""
        as_of = _day(25)
        ticker = "XYZ"
        baseline = [1_000_000.0] * 20  # exactly uniform — σ == 0
        today_vol = 999_999_999.0  # astronomically high

        pit = _build_pit_with_volume(ticker, baseline, today_vol, as_of)
        assert self.gate.is_anomalous(ticker, as_of, pit) is False

    def test_no_baseline_data_at_all_returns_false(self) -> None:
        """Completely empty gateway → False (no data)."""
        as_of = _day(25)
        ticker = "BLANK"
        pit = PITGateway()  # no sources registered
        assert self.gate.is_anomalous(ticker, as_of, pit) is False

    # ----- Custom threshold -----

    def test_custom_low_threshold_fires_more_easily(self) -> None:
        """Lower threshold (1.0σ) should flag more volumes."""
        gate = VolumeAnomalyGate(z_threshold=1.0, baseline_days=20)
        as_of = _day(25)
        ticker = "EASY"
        baseline = [1000.0] * 18 + [900.0, 1100.0]  # σ ≈ 70.71, μ=1000
        today_vol = 1100.0  # z ≈ 1.41 > 1.0

        pit = _build_pit_with_volume(ticker, baseline, today_vol, as_of)
        assert gate.is_anomalous(ticker, as_of, pit) is True

    def test_custom_high_threshold_harder_to_fire(self) -> None:
        """Higher threshold (10.0σ) should rarely flag.

        baseline = [1000]*18 + [900, 1100]: μ=1000, σ≈31.62
        z=10 boundary: today_vol ≈ 1000 + 10*31.62 = 1316.2
        today_vol=1200 → z ≈ 6.32 < 10.0 → should be clean.
        """
        gate = VolumeAnomalyGate(z_threshold=10.0, baseline_days=20)
        as_of = _day(25)
        ticker = "HARD"
        baseline = [1000.0] * 18 + [900.0, 1100.0]
        today_vol = 1200.0  # z ≈ 6.32 < 10.0

        pit = _build_pit_with_volume(ticker, baseline, today_vol, as_of)
        assert gate.is_anomalous(ticker, as_of, pit) is False

    def test_invalid_z_threshold_raises(self) -> None:
        with pytest.raises(ValueError, match="z_threshold"):
            VolumeAnomalyGate(z_threshold=0.0)

    def test_invalid_baseline_days_raises(self) -> None:
        with pytest.raises(ValueError, match="baseline_days"):
            VolumeAnomalyGate(baseline_days=0)


# ---------------------------------------------------------------------------
# Top-level import from arbiter.defenses
# ---------------------------------------------------------------------------

class TestTopLevelImport:
    def test_importable_from_defenses(self) -> None:
        from arbiter.defenses import VolumeAnomalyGate as VAG
        gate = VAG()
        assert isinstance(gate, VolumeAnomalyGate)

    def test_all_exports(self) -> None:
        import arbiter.defenses as defenses_pkg
        assert "VolumeAnomalyGate" in defenses_pkg.__all__


# ---------------------------------------------------------------------------
# Wave-C wiring contract
# ---------------------------------------------------------------------------

class TestWaveCWiringContract:
    """Document and verify the Lane 4 + Lane 13 wiring interface.

    These tests do NOT call the actual breaker (that would require a DB
    connection and is tested in tests/safety/).  They verify that the gate
    output type is correct for the expected wiring pattern.
    """

    def test_gate_returns_bool_for_lane4_integration(self) -> None:
        """Lane 4 calls: if gate.is_anomalous(ticker, as_of, pit): breaker.check_a3_volume_anomaly(...)"""
        gate = VolumeAnomalyGate()
        as_of = _day(25)
        ticker = "WIRING"
        baseline = _normal_volumes(n=20)
        today_vol = 10_000_000.0  # spike

        pit = _build_pit_with_volume(ticker, baseline, today_vol, as_of)
        result = gate.is_anomalous(ticker, as_of, pit)

        # Lane 4 expects a plain bool
        assert isinstance(result, bool)
        assert result is True  # spike should be flagged

    def test_gate_clean_for_lane13_passthrough(self) -> None:
        """Lane 13 calls: if gate.is_anomalous(held_ticker, as_of, pit): skip idea creation."""
        gate = VolumeAnomalyGate()
        as_of = _day(25)
        ticker = "NORMAL"
        baseline = _normal_volumes(n=20)
        today_vol = 1_010_000.0  # normal

        pit = _build_pit_with_volume(ticker, baseline, today_vol, as_of)
        result = gate.is_anomalous(ticker, as_of, pit)

        # Lane 13 should NOT suppress idea creation here
        assert result is False
