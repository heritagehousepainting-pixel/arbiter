"""Tests for arbiter.policy.sizing — Lane 12a.

Covered cases:
- Quarter-Kelly math
- Per-name cap binds
- ADV cap is applied LAST and caps size
- Missing ADV → size 0
- Gate HALTED → size 0
- Gate DEGRADED (0.25×) reduces size proportionally
- Cold start multiplier halves size
- Sector headroom cap binds
- Gross headroom cap binds
- Open-position count cap triggers zero
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from arbiter.policy.sizing import compute_size, _COLD_START_MULTIPLIER
from tests.policy.conftest import (
    adv_always,
    adv_missing,
    make_fusion,
    _make_gate,
)
from arbiter.types import DegradationLevel, HorizonBucket
from arbiter.contract.seams import TradingDecision


PORTFOLIO = 100_000.0  # $100k


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normal_decision() -> TradingDecision:
    return TradingDecision(
        allowed=True,
        size_multiplier=1.0,
        level=DegradationLevel.NORMAL,
        reasons=[],
    )


def _halted_decision() -> TradingDecision:
    return TradingDecision(
        allowed=False,
        size_multiplier=0.0,
        level=DegradationLevel.HALTED,
        reasons=["kill switch"],
    )


def _degraded_decision() -> TradingDecision:
    return TradingDecision(
        allowed=True,
        size_multiplier=0.25,
        level=DegradationLevel.DEGRADED,
        reasons=["only 1 advisor"],
    )


def _as_of() -> datetime:
    return datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Quarter-Kelly math
# ---------------------------------------------------------------------------

class TestQuarterKellyMath:
    """Verify raw quarter-Kelly computation before caps."""

    def test_full_conviction_quarter_kelly(self, cfg):
        """conviction=1.0 → 25% of equity before caps."""
        fusion = make_fusion(conviction=1.0, cold_start=False)
        # Use huge ADV so ADV cap doesn't bite
        size = compute_size(
            fusion=fusion,
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_always(10_000_000.0),
            ticker="AAPL",
            as_of=_as_of(),
        )
        # Quarter-Kelly = 0.25 * 1.0 * 100k = 25k, but per-name cap = 5% = 5k
        assert size == pytest.approx(5_000.0)  # per-name cap binds first

    def test_low_conviction_quarter_kelly(self, cfg):
        """conviction=0.2 → 5% of equity = $5k, capped by name cap of 5k."""
        fusion = make_fusion(conviction=0.2, cold_start=False)
        size = compute_size(
            fusion=fusion,
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_always(10_000_000.0),
            ticker="AAPL",
            as_of=_as_of(),
        )
        # Quarter-Kelly = 0.25 * 0.2 * 100k = 5k; name cap = 5k → both equal
        assert size == pytest.approx(5_000.0)

    def test_very_low_conviction_smaller_than_cap(self, cfg):
        """conviction=0.1 → 2.5% of equity = $2.5k, below all caps."""
        fusion = make_fusion(conviction=0.1, cold_start=False)
        size = compute_size(
            fusion=fusion,
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_always(10_000_000.0),
            ticker="AAPL",
            as_of=_as_of(),
        )
        # Quarter-Kelly = 0.25 * 0.1 * 100k = 2.5k < 5k cap
        assert size == pytest.approx(2_500.0)

    def test_zero_conviction_returns_zero(self, cfg):
        """conviction=0.0 → size 0 immediately."""
        fusion = make_fusion(conviction=0.0, cold_start=False)
        size = compute_size(
            fusion=fusion,
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_always(10_000_000.0),
            ticker="AAPL",
            as_of=_as_of(),
        )
        assert size == 0.0


# ---------------------------------------------------------------------------
# Per-name cap
# ---------------------------------------------------------------------------

class TestPerNameCap:
    """Per-name cap binds at max_position_pct * equity."""

    def test_per_name_cap_binds(self, cfg):
        """High conviction still capped at 5% of portfolio."""
        fusion = make_fusion(conviction=0.9, cold_start=False)
        size = compute_size(
            fusion=fusion,
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_always(10_000_000.0),
            ticker="TSLA",
            as_of=_as_of(),
        )
        assert size == pytest.approx(cfg.max_position_pct * PORTFOLIO)  # $5k

    def test_per_name_cap_with_large_portfolio(self, cfg):
        """Cap scales with portfolio equity."""
        large_equity = 1_000_000.0
        fusion = make_fusion(conviction=1.0, cold_start=False)
        size = compute_size(
            fusion=fusion,
            portfolio_equity=large_equity,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_always(100_000_000.0),
            ticker="AAPL",
            as_of=_as_of(),
        )
        assert size == pytest.approx(cfg.max_position_pct * large_equity)  # $50k


# ---------------------------------------------------------------------------
# ADV cap is applied LAST
# ---------------------------------------------------------------------------

class TestAdvCap:
    """ADV cap is the last transform; it can override earlier caps."""

    def test_adv_cap_applied_last(self, cfg):
        """Small ADV forces size below per-name cap (ADV cap is last)."""
        # ADV = $50k → ADV cap = 2% * 50k = $1k
        # Per-name cap = 5% * 100k = $5k
        # Quarter-Kelly (conviction=0.5) = 0.25 * 0.5 * 100k = $12.5k → capped to $5k by name
        # Then ADV cap = $1k → $1k is final
        fusion = make_fusion(conviction=0.5, cold_start=False)
        size = compute_size(
            fusion=fusion,
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_always(50_000.0),
            ticker="TINY",
            as_of=_as_of(),
        )
        assert size == pytest.approx(0.02 * 50_000.0)  # $1k

    def test_adv_cap_does_not_inflate_size(self, cfg):
        """Huge ADV cap doesn't inflate size beyond Kelly/name caps."""
        fusion = make_fusion(conviction=0.1, cold_start=False)
        size = compute_size(
            fusion=fusion,
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_always(100_000_000.0),  # huge ADV
            ticker="AAPL",
            as_of=_as_of(),
        )
        # Quarter-Kelly = 0.25 * 0.1 * 100k = $2.5k; ADV cap = 2m, doesn't bind
        assert size == pytest.approx(2_500.0)

    def test_adv_cap_exactly_at_name_cap(self, cfg):
        """ADV cap exactly equal to name cap → still returns that value."""
        # ADV cap = 2% of ADV; name cap = 5% of 100k = $5k
        # Set ADV = 250k → ADV cap = $5k (same as name cap)
        fusion = make_fusion(conviction=1.0, cold_start=False)
        size = compute_size(
            fusion=fusion,
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_always(250_000.0),
            ticker="MID",
            as_of=_as_of(),
        )
        assert size == pytest.approx(5_000.0)


# ---------------------------------------------------------------------------
# Missing ADV → size 0
# ---------------------------------------------------------------------------

class TestMissingAdv:
    """Fail-closed: missing ADV returns size 0."""

    def test_missing_adv_returns_zero(self, cfg):
        fusion = make_fusion(conviction=0.8, cold_start=False)
        size = compute_size(
            fusion=fusion,
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_missing(),
            ticker="GHOST",
            as_of=_as_of(),
        )
        assert size == 0.0

    def test_missing_adv_even_with_high_conviction(self, cfg):
        """Even conviction=1.0 cannot override missing ADV."""
        fusion = make_fusion(conviction=1.0, cold_start=False)
        size = compute_size(
            fusion=fusion,
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_missing(),
            ticker="GHOST",
            as_of=_as_of(),
        )
        assert size == 0.0


# ---------------------------------------------------------------------------
# Gate HALTED → size 0
# ---------------------------------------------------------------------------

class TestGateHalted:
    """Gate disallows → size 0 regardless of conviction."""

    def test_halted_gate_returns_zero(self, cfg):
        fusion = make_fusion(conviction=0.9, cold_start=False)
        size = compute_size(
            fusion=fusion,
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_halted_decision(),
            adv_provider=adv_always(10_000_000.0),
            ticker="AAPL",
            as_of=_as_of(),
        )
        assert size == 0.0

    def test_zero_multiplier_also_returns_zero(self, cfg):
        """size_multiplier=0.0 with allowed=True also returns 0."""
        decision = TradingDecision(
            allowed=True,
            size_multiplier=0.0,
            level=DegradationLevel.RESTRICTED,
            reasons=["restricted"],
        )
        fusion = make_fusion(conviction=0.9, cold_start=False)
        size = compute_size(
            fusion=fusion,
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=decision,
            adv_provider=adv_always(10_000_000.0),
            ticker="AAPL",
            as_of=_as_of(),
        )
        assert size == 0.0


# ---------------------------------------------------------------------------
# Gate DEGRADED → size reduced by 0.25×
# ---------------------------------------------------------------------------

class TestGateDegraded:
    """DEGRADED gate (0.25×) reduces position size proportionally."""

    def test_degraded_halves_ish_vs_normal(self, cfg):
        """DEGRADED (0.25×) vs NORMAL (1.0×) → ratio ~0.25."""
        fusion = make_fusion(conviction=0.1, cold_start=False)  # below name cap
        huge_adv = adv_always(10_000_000.0)

        normal_size = compute_size(
            fusion=fusion, portfolio_equity=PORTFOLIO, config=cfg,
            gate_decision=_normal_decision(), adv_provider=huge_adv,
            ticker="AAPL", as_of=_as_of(),
        )
        degraded_size = compute_size(
            fusion=fusion, portfolio_equity=PORTFOLIO, config=cfg,
            gate_decision=_degraded_decision(), adv_provider=huge_adv,
            ticker="AAPL", as_of=_as_of(),
        )
        assert degraded_size == pytest.approx(normal_size * 0.25)

    def test_degraded_size_still_positive(self, cfg):
        """DEGRADED gate does not zero out positions — just reduces them."""
        fusion = make_fusion(conviction=0.5, cold_start=False)
        size = compute_size(
            fusion=fusion, portfolio_equity=PORTFOLIO, config=cfg,
            gate_decision=_degraded_decision(),
            adv_provider=adv_always(10_000_000.0),
            ticker="AAPL", as_of=_as_of(),
        )
        assert size > 0.0


# ---------------------------------------------------------------------------
# Cold start multiplier
# ---------------------------------------------------------------------------

class TestColdStart:
    """cold_start=True applies 0.5× calibration multiplier."""

    def test_cold_start_halves_size(self, cfg):
        fusion_warm = make_fusion(conviction=0.1, cold_start=False)
        fusion_cold = make_fusion(conviction=0.1, cold_start=True)
        huge_adv = adv_always(10_000_000.0)
        as_of = _as_of()

        warm_size = compute_size(
            fusion=fusion_warm, portfolio_equity=PORTFOLIO, config=cfg,
            gate_decision=_normal_decision(), adv_provider=huge_adv,
            ticker="AAPL", as_of=as_of,
        )
        cold_size = compute_size(
            fusion=fusion_cold, portfolio_equity=PORTFOLIO, config=cfg,
            gate_decision=_normal_decision(), adv_provider=huge_adv,
            ticker="AAPL", as_of=as_of,
        )
        assert cold_size == pytest.approx(warm_size * _COLD_START_MULTIPLIER)


# ---------------------------------------------------------------------------
# Sector and gross cap headroom
# ---------------------------------------------------------------------------

class TestHeadroomCaps:
    """Sector and gross caps limit size based on existing exposure."""

    def test_sector_headroom_caps_size(self, cfg):
        """If sector is nearly full, new position is capped at remaining headroom."""
        fusion = make_fusion(conviction=1.0, cold_start=False)
        # Sector max = 20% of 100k = $20k; already have $19.5k in sector
        size = compute_size(
            fusion=fusion, portfolio_equity=PORTFOLIO, config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_always(10_000_000.0),
            ticker="NVDA", as_of=_as_of(),
            current_sector_exposure=19_500.0,
        )
        # Headroom = 20k - 19.5k = 500
        assert size == pytest.approx(500.0)

    def test_gross_headroom_caps_size(self, cfg):
        """If gross exposure is nearly at 80%, new position is tiny."""
        fusion = make_fusion(conviction=1.0, cold_start=False)
        # Gross max = 80% of 100k = $80k; already have $79.8k gross
        size = compute_size(
            fusion=fusion, portfolio_equity=PORTFOLIO, config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_always(10_000_000.0),
            ticker="MSFT", as_of=_as_of(),
            current_gross_exposure=79_800.0,
        )
        assert size == pytest.approx(200.0)

    def test_open_positions_at_cap_returns_zero(self, cfg):
        """At max_open_positions capacity, new positions return 0."""
        fusion = make_fusion(conviction=0.9, cold_start=False)
        size = compute_size(
            fusion=fusion, portfolio_equity=PORTFOLIO, config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_always(10_000_000.0),
            ticker="GOOG", as_of=_as_of(),
            current_open_positions=20,  # at cap
        )
        assert size == 0.0

    def test_open_positions_below_cap_allowed(self, cfg):
        """Below max positions, trading is allowed."""
        fusion = make_fusion(conviction=0.1, cold_start=False)
        size = compute_size(
            fusion=fusion, portfolio_equity=PORTFOLIO, config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_always(10_000_000.0),
            ticker="GOOG", as_of=_as_of(),
            current_open_positions=19,  # one slot remaining
        )
        assert size > 0.0


# ---------------------------------------------------------------------------
# NaN ADV → size 0 (Finding 9)
# ---------------------------------------------------------------------------

class TestNaNAdv:
    """NaN ADV must return size 0 (not bypass via min(x, nan)==x)."""

    def test_nan_adv_returns_zero(self, cfg):
        """math.isnan guard: NaN ADV must produce size 0, not bypass the cap."""
        import math

        def nan_provider(ticker: str, as_of: datetime) -> float | None:
            return float("nan")

        fusion = make_fusion(conviction=0.8, cold_start=False)
        size = compute_size(
            fusion=fusion,
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=nan_provider,
            ticker="NANTEST",
            as_of=_as_of(),
        )
        assert size == 0.0, (
            f"Expected 0.0 for NaN ADV but got {size} — "
            "min(x, nan)==x bypassed the cap (Finding 9)"
        )

    def test_nan_adv_with_max_conviction(self, cfg):
        """NaN ADV blocks even conviction=1.0 (fail-closed for bad data)."""
        import math

        def nan_provider(ticker: str, as_of: datetime) -> float | None:
            return float("nan")

        fusion = make_fusion(conviction=1.0, cold_start=False)
        size = compute_size(
            fusion=fusion,
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=nan_provider,
            ticker="NANMAX",
            as_of=_as_of(),
        )
        assert size == 0.0


# ---------------------------------------------------------------------------
# Trace callback (unfreeze Stage 1 — decision tracing)
# ---------------------------------------------------------------------------

def _collect():
    """Return (events, trace) — trace appends (event, payload) tuples."""
    events: list[tuple[str, dict]] = []

    def trace(event: str, payload: dict) -> None:
        events.append((event, payload))

    return events, trace


class TestSizingTrace:
    """compute_size(trace=...) reports WHY a size came back 0."""

    def test_adv_missing_traced(self, cfg):
        events, trace = _collect()
        size = compute_size(
            fusion=make_fusion(conviction=0.5),
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_missing(),
            ticker="NOADV",
            as_of=_as_of(),
            trace=trace,
        )
        assert size == 0.0
        assert ("size", {"reason": "adv_missing", "ticker": "NOADV"}) in events

    def test_position_count_full_traced(self, cfg):
        events, trace = _collect()
        size = compute_size(
            fusion=make_fusion(conviction=0.5),
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_always(1e9),
            ticker="FULL",
            as_of=_as_of(),
            current_open_positions=cfg.max_open_positions,
            trace=trace,
        )
        assert size == 0.0
        assert ("size", {"reason": "position_count_full", "ticker": "FULL"}) in events

    def test_caps_exhausted_traced(self, cfg):
        """Zero gross headroom clamps size to 0 → caps_exhausted."""
        events, trace = _collect()
        size = compute_size(
            fusion=make_fusion(conviction=0.5),
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_always(1e9),
            ticker="NOROOM",
            as_of=_as_of(),
            current_gross_exposure=cfg.max_gross_pct * PORTFOLIO,
            trace=trace,
        )
        assert size == 0.0
        assert ("size", {"reason": "caps_exhausted", "ticker": "NOROOM"}) in events

    def test_gate_blocked_traced(self, cfg):
        events, trace = _collect()
        size = compute_size(
            fusion=make_fusion(conviction=0.5),
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_halted_decision(),
            adv_provider=adv_always(1e9),
            ticker="HALT",
            as_of=_as_of(),
            trace=trace,
        )
        assert size == 0.0
        assert ("size", {"reason": "gate_blocked", "ticker": "HALT"}) in events

    def test_no_trace_kwarg_unchanged(self, cfg):
        """Default trace=None keeps legacy behavior (no error, same size)."""
        size = compute_size(
            fusion=make_fusion(conviction=0.5, cold_start=False),
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_always(1e9),
            ticker="PLAIN",
            as_of=_as_of(),
        )
        assert size > 0.0

    def test_raising_trace_never_breaks_sizing(self, cfg):
        """A broken trace callback must not abort sizing (fail-safe)."""
        def bad_trace(event: str, payload: dict) -> None:
            raise RuntimeError("boom")

        size = compute_size(
            fusion=make_fusion(conviction=0.5),
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_missing(),
            ticker="BOOM",
            as_of=_as_of(),
            trace=bad_trace,
        )
        assert size == 0.0


# ---------------------------------------------------------------------------
# Minimum position size floor (unfreeze Stage 4 — deployment pressure)
# ---------------------------------------------------------------------------

class TestMinPositionFloor:
    """Conviction-qualified floor: a trade that clears the bar is worth at
    least min_position_pct × equity — but the floor never breaches a cap."""

    def _cfg_with_floor(self, cfg, pct):
        import dataclasses
        return dataclasses.replace(cfg, min_position_pct=pct)

    def test_floor_raises_small_size(self, cfg):
        """conviction 0.05 → raw $1250 on $100k; floor 2% → $2000."""
        size = compute_size(
            fusion=make_fusion(conviction=0.05, cold_start=False),
            portfolio_equity=PORTFOLIO,
            config=self._cfg_with_floor(cfg, 0.02),
            gate_decision=_normal_decision(),
            adv_provider=adv_always(1e9),
            ticker="FLOOR",
            as_of=_as_of(),
        )
        assert size == pytest.approx(0.02 * PORTFOLIO)

    def test_floor_clamped_by_name_headroom(self, cfg):
        """Floor never breaches the per-name cap headroom."""
        headroom_target = 1_500.0  # name cap 5% of 100k = 5000; held 3500
        size = compute_size(
            fusion=make_fusion(conviction=0.05, cold_start=False),
            portfolio_equity=PORTFOLIO,
            config=self._cfg_with_floor(cfg, 0.02),
            gate_decision=_normal_decision(),
            adv_provider=adv_always(1e9),
            ticker="CLAMP",
            as_of=_as_of(),
            current_name_exposure=cfg.max_position_pct * PORTFOLIO - headroom_target,
        )
        assert size == pytest.approx(headroom_target)

    def test_zero_floor_is_legacy_behavior(self, cfg):
        """min_position_pct=0 (the bare-Config default) → exact legacy size."""
        size = compute_size(
            fusion=make_fusion(conviction=0.05, cold_start=False),
            portfolio_equity=PORTFOLIO,
            config=cfg,
            gate_decision=_normal_decision(),
            adv_provider=adv_always(1e9),
            ticker="LEGACY",
            as_of=_as_of(),
        )
        assert size == pytest.approx(0.25 * 0.05 * PORTFOLIO)  # $1250

    def test_adv_cap_still_binds_after_floor(self, cfg):
        """ADV cap is the LAST transform — floor cannot bypass it."""
        adv = 10_000.0  # adv cap = 2% × 10k = $200 < $2000 floor
        size = compute_size(
            fusion=make_fusion(conviction=0.05, cold_start=False),
            portfolio_equity=PORTFOLIO,
            config=self._cfg_with_floor(cfg, 0.02),
            gate_decision=_normal_decision(),
            adv_provider=adv_always(adv),
            ticker="ADVCAP",
            as_of=_as_of(),
        )
        assert size == pytest.approx(0.02 * adv)

    def test_floor_does_not_resurrect_zero_size(self, cfg):
        """A size already clamped to 0 by caps stays 0 — the floor only lifts
        LIVE (positive) sizes, it never creates a trade the caps rejected."""
        size = compute_size(
            fusion=make_fusion(conviction=0.05, cold_start=False),
            portfolio_equity=PORTFOLIO,
            config=self._cfg_with_floor(cfg, 0.02),
            gate_decision=_normal_decision(),
            adv_provider=adv_always(1e9),
            ticker="DEAD",
            as_of=_as_of(),
            current_gross_exposure=cfg.max_gross_pct * PORTFOLIO,
        )
        assert size == 0.0
