"""Tests for arbiter/options/gate.py — options_expression_gate()."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from arbiter.config import Config
from arbiter.options.gate import options_expression_gate
from arbiter.options.types import OptionGateDecision, OptionSide


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides: object) -> Config:
    """Build a Config with all required fields; options-related fields default
    to sensible values that pass the gate so individual tests can override one
    thing at a time."""
    base: dict[str, object] = dict(
        live_trading=False,
        executor_backend="sim",
        db_path="data/arbiter.db",
        audit_path="data/audit.jsonl",
        metrics_path="data/metrics.jsonl",
        max_position_pct=0.05,
        max_sector_pct=0.20,
        max_gross_pct=0.80,
        max_open_positions=20,
        adv_cap_pct=0.02,
        alpaca_api_key="",
        alpaca_secret_key="",
        alpaca_paper_base_url="https://paper-api.alpaca.markets",
        alpaca_data_base_url="https://data.alpaca.markets",
        alpaca_timeout=20.0,
        edgar_user_agent="",
        kill_switch_url="",
        alert_webhook_url="",
        # Options gate defaults
        options_mode="shadow",
        option_conviction_mult=1.5,
        option_min_expiry_days=60,
        option_ivr_max=0.40,
        option_target_delta_low=0.70,
        option_target_delta_high=0.80,
        option_min_open_interest=100,
        option_min_volume=10,
    )
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def _make_fake_client() -> MagicMock:
    """Return a mock AlpacaOptionsClient that is not expected to be called by gate."""
    return MagicMock(name="AlpacaOptionsClient")


def _call_gate(
    *,
    conn: object = None,
    client: object = None,
    underlying: str = "AAPL",
    conviction: float = 0.75,
    horizon_days: float = 90.0,
    catalyst_tag: str | None = "13D",
    equity_entry_threshold: float = 0.40,
    underlying_price: float = 150.0,
    config: Config,
    as_of: str = "2026-06-25T00:00:00+00:00",
    iv_rank_return: float | None = 0.25,
    rvp_return: float | None = 0.22,
) -> OptionGateDecision:
    """Call gate with patched iv_history functions and sensible defaults."""
    import arbiter.options.gate as gate_module

    fake_conn = conn or MagicMock(name="sqlite3.Connection")
    fake_client = client or _make_fake_client()

    original_iv_rank = gate_module.iv_history.iv_rank
    original_rvp = gate_module.iv_history.realized_vol_proxy
    try:
        gate_module.iv_history.iv_rank = lambda *a, **kw: iv_rank_return  # type: ignore[assignment]
        gate_module.iv_history.realized_vol_proxy = lambda *a, **kw: rvp_return  # type: ignore[assignment]
        return options_expression_gate(
            fake_conn,
            fake_client,
            underlying=underlying,
            conviction=conviction,
            horizon_days=horizon_days,
            catalyst_tag=catalyst_tag,
            equity_entry_threshold=equity_entry_threshold,
            underlying_price=underlying_price,
            config=config,
            as_of=as_of,
        )
    finally:
        gate_module.iv_history.iv_rank = original_iv_rank  # type: ignore[assignment]
        gate_module.iv_history.realized_vol_proxy = original_rvp  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 1. OPTIONS_OFF
# ---------------------------------------------------------------------------

class TestOptionsOff:
    def test_off_mode_rejects_immediately(self) -> None:
        cfg = _make_config(options_mode="off")
        result = _call_gate(config=cfg, conviction=0.99)
        assert result.express is False
        assert result.reason == "OPTIONS_OFF"
        assert result.side is None

    def test_off_mode_populates_context_fields(self) -> None:
        cfg = _make_config(options_mode="off")
        result = _call_gate(
            config=cfg,
            conviction=0.99,
            horizon_days=120.0,
            equity_entry_threshold=0.40,
        )
        assert result.conviction == pytest.approx(0.99)
        assert result.horizon_days == pytest.approx(120.0)
        assert result.conviction_threshold_used == pytest.approx(0.40 * 1.5)


# ---------------------------------------------------------------------------
# 2. CONVICTION_TOO_LOW
# ---------------------------------------------------------------------------

class TestConvictionGate:
    def test_conviction_too_low_rejects(self) -> None:
        cfg = _make_config(options_mode="shadow", option_conviction_mult=1.5)
        # threshold = 0.40 * 1.5 = 0.60; conviction 0.50 < 0.60
        result = _call_gate(
            config=cfg,
            conviction=0.50,
            equity_entry_threshold=0.40,
        )
        assert result.express is False
        assert result.reason == "CONVICTION_TOO_LOW"

    def test_conviction_just_above_threshold_passes(self) -> None:
        cfg = _make_config(options_mode="shadow", option_conviction_mult=1.5)
        # threshold = 0.40 * 1.5 = 0.60; use 0.61 to safely clear it
        result = _call_gate(
            config=cfg,
            conviction=0.61,
            equity_entry_threshold=0.40,
            iv_rank_return=0.25,
        )
        # Should NOT reject on conviction
        assert result.reason != "CONVICTION_TOO_LOW"

    def test_negative_conviction_uses_abs_value(self) -> None:
        cfg = _make_config(options_mode="shadow", option_conviction_mult=1.5)
        # |conviction| = 0.50 < threshold 0.60 → should still reject
        result = _call_gate(
            config=cfg,
            conviction=-0.50,
            equity_entry_threshold=0.40,
        )
        assert result.express is False
        assert result.reason == "CONVICTION_TOO_LOW"

    def test_strong_negative_conviction_produces_put(self) -> None:
        cfg = _make_config(options_mode="shadow", option_conviction_mult=1.5)
        result = _call_gate(
            config=cfg,
            conviction=-0.80,
            equity_entry_threshold=0.40,
            iv_rank_return=0.20,
        )
        assert result.express is True
        assert result.side == OptionSide.PUT

    def test_positive_conviction_produces_call(self) -> None:
        cfg = _make_config(options_mode="shadow", option_conviction_mult=1.5)
        result = _call_gate(
            config=cfg,
            conviction=0.80,
            equity_entry_threshold=0.40,
            iv_rank_return=0.20,
        )
        assert result.express is True
        assert result.side == OptionSide.CALL


# ---------------------------------------------------------------------------
# 3. HORIZON_TOO_SHORT
# ---------------------------------------------------------------------------

class TestHorizonGate:
    def test_short_horizon_rejects(self) -> None:
        cfg = _make_config(options_mode="shadow", option_min_expiry_days=60)
        result = _call_gate(config=cfg, conviction=0.80, horizon_days=30.0)
        assert result.express is False
        assert result.reason == "HORIZON_TOO_SHORT"

    def test_exactly_min_expiry_passes(self) -> None:
        cfg = _make_config(options_mode="shadow", option_min_expiry_days=60)
        result = _call_gate(
            config=cfg,
            conviction=0.80,
            horizon_days=60.0,
            iv_rank_return=0.20,
        )
        assert result.reason != "HORIZON_TOO_SHORT"

    def test_longer_horizon_passes_horizon_check(self) -> None:
        cfg = _make_config(options_mode="shadow", option_min_expiry_days=60)
        result = _call_gate(
            config=cfg,
            conviction=0.80,
            horizon_days=90.0,
            iv_rank_return=0.20,
        )
        assert result.reason != "HORIZON_TOO_SHORT"


# ---------------------------------------------------------------------------
# 4. NO_CATALYST
# ---------------------------------------------------------------------------

class TestCatalystGate:
    def test_none_catalyst_rejects(self) -> None:
        cfg = _make_config(options_mode="shadow")
        result = _call_gate(config=cfg, conviction=0.80, catalyst_tag=None)
        assert result.express is False
        assert result.reason == "NO_CATALYST"

    def test_empty_string_catalyst_rejects(self) -> None:
        cfg = _make_config(options_mode="shadow")
        result = _call_gate(config=cfg, conviction=0.80, catalyst_tag="")
        assert result.express is False
        assert result.reason == "NO_CATALYST"

    def test_valid_catalyst_passes(self) -> None:
        cfg = _make_config(options_mode="shadow")
        result = _call_gate(
            config=cfg,
            conviction=0.80,
            catalyst_tag="13D",
            iv_rank_return=0.20,
        )
        assert result.reason != "NO_CATALYST"
        assert result.catalyst_tag == "13D"


# ---------------------------------------------------------------------------
# 5. IV_RANK_TOO_HIGH
# ---------------------------------------------------------------------------

class TestIVGate:
    def test_high_ivr_rejects(self) -> None:
        cfg = _make_config(options_mode="shadow", option_ivr_max=0.40)
        result = _call_gate(
            config=cfg,
            conviction=0.80,
            catalyst_tag="form4_cluster",
            iv_rank_return=0.45,  # > 0.40
        )
        assert result.express is False
        assert result.reason == "IV_RANK_TOO_HIGH"
        assert result.ivr_estimate == pytest.approx(0.45)

    def test_low_ivr_passes(self) -> None:
        cfg = _make_config(options_mode="shadow", option_ivr_max=0.40)
        result = _call_gate(
            config=cfg,
            conviction=0.80,
            catalyst_tag="fund_buy",
            iv_rank_return=0.25,
        )
        assert result.express is True
        assert result.ivr_estimate == pytest.approx(0.25)

    def test_ivr_at_max_boundary_passes(self) -> None:
        cfg = _make_config(options_mode="shadow", option_ivr_max=0.40)
        # exactly at 0.40 should pass (not strictly greater)
        result = _call_gate(
            config=cfg,
            conviction=0.80,
            catalyst_tag="13D",
            iv_rank_return=0.40,
        )
        assert result.express is True


# ---------------------------------------------------------------------------
# 6. Cold-start: iv_rank() returns None
# ---------------------------------------------------------------------------

class TestColdStart:
    def test_cold_start_no_rvp_passes_through(self) -> None:
        """When both IVR and realized-vol-proxy are None, gate passes (no data)."""
        cfg = _make_config(options_mode="shadow", option_ivr_max=0.40)
        result = _call_gate(
            config=cfg,
            conviction=0.80,
            catalyst_tag="13D",
            iv_rank_return=None,
            rvp_return=None,
        )
        assert result.express is True
        assert result.ivr_estimate is None
        assert result.realized_vol_proxy is None

    def test_cold_start_low_rvp_passes(self) -> None:
        """When IVR is None but realized vol is low, gate passes."""
        cfg = _make_config(options_mode="shadow", option_ivr_max=0.40)
        result = _call_gate(
            config=cfg,
            conviction=0.80,
            catalyst_tag="13D",
            iv_rank_return=None,
            rvp_return=0.25,  # below ivr_max 0.40 → pass
        )
        assert result.express is True
        assert result.ivr_estimate is None
        assert result.realized_vol_proxy == pytest.approx(0.25)

    def test_cold_start_high_rvp_rejects(self) -> None:
        """When IVR is None but realized vol proxy is very high, gate rejects."""
        cfg = _make_config(options_mode="shadow", option_ivr_max=0.40)
        result = _call_gate(
            config=cfg,
            conviction=0.80,
            catalyst_tag="13D",
            iv_rank_return=None,
            rvp_return=0.55,  # above ivr_max → reject
        )
        assert result.express is False
        assert result.reason == "IV_RANK_TOO_HIGH"
        assert result.ivr_estimate is None
        assert result.realized_vol_proxy == pytest.approx(0.55)

    def test_cold_start_rvp_recorded_in_decision(self) -> None:
        """Cold-start realized vol proxy is always recorded in the decision."""
        cfg = _make_config(options_mode="shadow", option_ivr_max=0.40)
        result = _call_gate(
            config=cfg,
            conviction=0.80,
            catalyst_tag="form4_cluster",
            iv_rank_return=None,
            rvp_return=0.30,
        )
        assert result.realized_vol_proxy == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# 7. Full pass — verify OptionGateDecision fields
# ---------------------------------------------------------------------------

class TestGatePassCase:
    def test_pass_populates_all_fields(self) -> None:
        cfg = _make_config(
            options_mode="shadow",
            option_conviction_mult=1.5,
            option_min_expiry_days=60,
            option_ivr_max=0.40,
            option_target_delta_low=0.70,
            option_target_delta_high=0.80,
        )
        result = _call_gate(
            config=cfg,
            underlying="MSFT",
            conviction=0.75,
            horizon_days=90.0,
            catalyst_tag="form4_cluster",
            equity_entry_threshold=0.40,
            iv_rank_return=0.25,
            rvp_return=0.22,
        )
        assert result.express is True
        assert result.reason == "OK"
        assert result.side == OptionSide.CALL
        assert result.target_delta_low == pytest.approx(0.70)
        assert result.target_delta_high == pytest.approx(0.80)
        assert result.min_expiry_days == 60
        assert result.catalyst_tag == "form4_cluster"
        assert result.conviction == pytest.approx(0.75)
        assert result.conviction_threshold_used == pytest.approx(0.40 * 1.5)
        assert result.horizon_days == pytest.approx(90.0)
        assert result.ivr_estimate == pytest.approx(0.25)
        assert result.realized_vol_proxy == pytest.approx(0.22)

    def test_gate_never_raises(self) -> None:
        """Gate must never propagate exceptions — graceful degradation only."""
        import arbiter.options.gate as gate_module

        cfg = _make_config(options_mode="shadow")

        def _bad_iv_rank(*a: object, **kw: object) -> float:
            raise RuntimeError("db exploded")

        def _bad_rvp(*a: object, **kw: object) -> float:
            raise RuntimeError("db exploded too")

        original_ivr = gate_module.iv_history.iv_rank
        original_rvp = gate_module.iv_history.realized_vol_proxy
        try:
            gate_module.iv_history.iv_rank = _bad_iv_rank  # type: ignore[assignment]
            gate_module.iv_history.realized_vol_proxy = _bad_rvp  # type: ignore[assignment]
            # Should not raise
            result = options_expression_gate(
                MagicMock(),
                MagicMock(),
                underlying="AAPL",
                conviction=0.80,
                horizon_days=90.0,
                catalyst_tag="13D",
                equity_entry_threshold=0.40,
                underlying_price=150.0,
                config=cfg,
                as_of="2026-06-25T00:00:00+00:00",
            )
            # When both iv calls fail we fall through (cold-start path with None)
            assert isinstance(result, OptionGateDecision)
        finally:
            gate_module.iv_history.iv_rank = original_ivr  # type: ignore[assignment]
            gate_module.iv_history.realized_vol_proxy = original_rvp  # type: ignore[assignment]
