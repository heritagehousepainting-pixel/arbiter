"""Tests for arbiter/options/contract_selector.py — select_contract()."""
from __future__ import annotations

import datetime
from typing import Optional
from unittest.mock import MagicMock


from arbiter.config import Config
from arbiter.options.contract_selector import select_contract
from arbiter.options.types import OptionContract, OptionGateDecision, OptionSide


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AS_OF = datetime.date(2026, 6, 25)

_REQUIRED_CONFIG_FIELDS: dict[str, object] = dict(
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
)


def _make_config(**overrides: object) -> Config:
    base = dict(_REQUIRED_CONFIG_FIELDS)
    # Options defaults
    base.update(
        options_mode="shadow",
        option_conviction_mult=1.5,
        option_min_expiry_days=60,
        option_horizon_buffer_days=30,
        option_max_expiry_buffer_days=180,
        option_target_delta_low=0.70,
        option_target_delta_high=0.80,
        option_min_open_interest=100,
        option_min_volume=10,
        option_ivr_max=0.40,
        option_breakeven_buffer_pct=0.05,
    )
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def _make_gate_decision(
    *,
    express: bool = True,
    side: OptionSide = OptionSide.CALL,
    target_delta_low: float = 0.70,
    target_delta_high: float = 0.80,
) -> OptionGateDecision:
    return OptionGateDecision(
        express=express,
        reason="OK" if express else "CONVICTION_TOO_LOW",
        side=side if express else None,
        target_delta_low=target_delta_low,
        target_delta_high=target_delta_high,
        min_expiry_days=60,
        catalyst_tag="13D",
        conviction=0.80,
        conviction_threshold_used=0.60,
        horizon_days=90.0,
        ivr_estimate=0.25,
        realized_vol_proxy=0.22,
    )


def _make_contract(
    *,
    occ_symbol: str = "AAPL261218C00150000",
    underlying: str = "AAPL",
    side: OptionSide = OptionSide.CALL,
    strike: float = 150.0,
    expiry: datetime.date = datetime.date(2026, 12, 18),
    delta: Optional[float] = 0.75,
    iv: Optional[float] = 0.30,
    bid: Optional[float] = 10.0,
    ask: Optional[float] = 10.20,
    open_interest: Optional[int] = 500,
    volume: Optional[int] = 50,
) -> OptionContract:
    return OptionContract(
        occ_symbol=occ_symbol,
        underlying=underlying,
        side=side,
        strike=strike,
        expiry=expiry,
        delta=delta,
        iv=iv,
        bid=bid,
        ask=ask,
        open_interest=open_interest,
        volume=volume,
    )


def _make_client(chain: list[OptionContract]) -> MagicMock:
    """Return a fake AlpacaOptionsClient whose fetch_chain returns ``chain``."""
    client = MagicMock(name="AlpacaOptionsClient")
    client.fetch_chain.return_value = chain
    return client


# ---------------------------------------------------------------------------
# 1. Gate not expressed → None
# ---------------------------------------------------------------------------

class TestGateNotExpressed:
    def test_returns_none_when_gate_rejected(self) -> None:
        gate = _make_gate_decision(express=False)
        cfg = _make_config()
        client = _make_client([])
        result = select_contract(
            client,
            gate,
            underlying="AAPL",
            horizon_days=90.0,
            config=cfg,
            as_of=AS_OF,
        )
        assert result is None
        # fetch_chain should not have been called
        client.fetch_chain.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Empty chain → None
# ---------------------------------------------------------------------------

class TestEmptyChain:
    def test_empty_chain_returns_none(self) -> None:
        gate = _make_gate_decision(express=True, side=OptionSide.CALL)
        cfg = _make_config()
        client = _make_client([])
        result = select_contract(
            client,
            gate,
            underlying="AAPL",
            horizon_days=90.0,
            config=cfg,
            as_of=AS_OF,
        )
        assert result is None


# ---------------------------------------------------------------------------
# 3. Open-interest / volume filtering
# ---------------------------------------------------------------------------

class TestLiquidityFilters:
    def test_contract_below_min_oi_excluded(self) -> None:
        gate = _make_gate_decision(express=True)
        cfg = _make_config(option_min_open_interest=100, option_min_volume=10)
        low_oi = _make_contract(open_interest=50, volume=20, delta=0.75)
        client = _make_client([low_oi])
        result = select_contract(
            client, gate, underlying="AAPL", horizon_days=90.0, config=cfg, as_of=AS_OF
        )
        assert result is None

    def test_contract_below_min_volume_excluded(self) -> None:
        gate = _make_gate_decision(express=True)
        cfg = _make_config(option_min_open_interest=100, option_min_volume=10)
        low_vol = _make_contract(open_interest=200, volume=5, delta=0.75)
        client = _make_client([low_vol])
        result = select_contract(
            client, gate, underlying="AAPL", horizon_days=90.0, config=cfg, as_of=AS_OF
        )
        assert result is None

    def test_contract_at_min_oi_and_vol_threshold_included(self) -> None:
        gate = _make_gate_decision(express=True)
        cfg = _make_config(option_min_open_interest=100, option_min_volume=10)
        exact = _make_contract(open_interest=100, volume=10, delta=0.75)
        client = _make_client([exact])
        result = select_contract(
            client, gate, underlying="AAPL", horizon_days=90.0, config=cfg, as_of=AS_OF
        )
        assert result is exact

    def test_contract_with_none_oi_excluded(self) -> None:
        gate = _make_gate_decision(express=True)
        cfg = _make_config()
        no_oi = _make_contract(open_interest=None, volume=50, delta=0.75)
        client = _make_client([no_oi])
        result = select_contract(
            client, gate, underlying="AAPL", horizon_days=90.0, config=cfg, as_of=AS_OF
        )
        assert result is None

    def test_contract_with_none_volume_kept_when_oi_strong(self) -> None:
        # Alpaca's contracts endpoint frequently returns volume=None and LEAPS
        # trade thinly; a missing volume must NOT veto a deep-OI contract (else
        # the layer is inert — caught in live shadow testing). OI is binding.
        gate = _make_gate_decision(express=True)
        cfg = _make_config()
        no_vol = _make_contract(open_interest=200, volume=None, delta=0.75)
        client = _make_client([no_vol])
        result = select_contract(
            client, gate, underlying="AAPL", horizon_days=90.0, config=cfg, as_of=AS_OF
        )
        assert result is no_vol

    def test_present_volume_below_floor_excluded(self) -> None:
        gate = _make_gate_decision(express=True)
        cfg = _make_config()
        low_vol = _make_contract(open_interest=200, volume=3, delta=0.75)
        client = _make_client([low_vol])
        result = select_contract(
            client, gate, underlying="AAPL", horizon_days=90.0, config=cfg, as_of=AS_OF
        )
        assert result is None


# ---------------------------------------------------------------------------
# 4. Delta band filtering
# ---------------------------------------------------------------------------

class TestDeltaBandFiltering:
    def test_delta_below_band_excluded(self) -> None:
        gate = _make_gate_decision(express=True, target_delta_low=0.70, target_delta_high=0.80)
        cfg = _make_config(option_target_delta_low=0.70, option_target_delta_high=0.80)
        below = _make_contract(delta=0.55)  # below 0.70
        client = _make_client([below])
        result = select_contract(
            client, gate, underlying="AAPL", horizon_days=90.0, config=cfg, as_of=AS_OF
        )
        assert result is None

    def test_delta_above_band_excluded(self) -> None:
        gate = _make_gate_decision(express=True, target_delta_low=0.70, target_delta_high=0.80)
        cfg = _make_config(option_target_delta_low=0.70, option_target_delta_high=0.80)
        above = _make_contract(delta=0.92)  # above 0.80
        client = _make_client([above])
        result = select_contract(
            client, gate, underlying="AAPL", horizon_days=90.0, config=cfg, as_of=AS_OF
        )
        assert result is None

    def test_delta_in_band_included(self) -> None:
        gate = _make_gate_decision(express=True, target_delta_low=0.70, target_delta_high=0.80)
        cfg = _make_config(option_target_delta_low=0.70, option_target_delta_high=0.80)
        good = _make_contract(delta=0.75)
        client = _make_client([good])
        result = select_contract(
            client, gate, underlying="AAPL", horizon_days=90.0, config=cfg, as_of=AS_OF
        )
        assert result is good

    def test_put_uses_abs_delta(self) -> None:
        """Alpaca signs put delta negative; selector must use |delta| for band test."""
        gate = _make_gate_decision(
            express=True, side=OptionSide.PUT, target_delta_low=0.70, target_delta_high=0.80
        )
        cfg = _make_config(option_target_delta_low=0.70, option_target_delta_high=0.80)
        put_contract = _make_contract(
            side=OptionSide.PUT,
            delta=-0.75,  # Alpaca negative for puts
            occ_symbol="AAPL261218P00150000",
        )
        client = _make_client([put_contract])
        result = select_contract(
            client, gate, underlying="AAPL", horizon_days=90.0, config=cfg, as_of=AS_OF
        )
        assert result is put_contract

    def test_contract_with_none_delta_excluded(self) -> None:
        gate = _make_gate_decision(express=True)
        cfg = _make_config()
        no_delta = _make_contract(delta=None)
        client = _make_client([no_delta])
        result = select_contract(
            client, gate, underlying="AAPL", horizon_days=90.0, config=cfg, as_of=AS_OF
        )
        assert result is None


# ---------------------------------------------------------------------------
# 5. Closest-to-midpoint (0.75) selection
# ---------------------------------------------------------------------------

class TestDeltaClosestToMidpoint:
    def test_selects_closest_to_0_75(self) -> None:
        gate = _make_gate_decision(express=True, target_delta_low=0.70, target_delta_high=0.80)
        cfg = _make_config(option_target_delta_low=0.70, option_target_delta_high=0.80)
        c1 = _make_contract(occ_symbol="SYM1", delta=0.71)  # distance 0.04 from 0.75
        c2 = _make_contract(occ_symbol="SYM2", delta=0.76)  # distance 0.01 from 0.75
        c3 = _make_contract(occ_symbol="SYM3", delta=0.79)  # distance 0.04 from 0.75
        client = _make_client([c1, c3, c2])  # shuffled
        result = select_contract(
            client, gate, underlying="AAPL", horizon_days=90.0, config=cfg, as_of=AS_OF
        )
        assert result is not None
        assert result.occ_symbol == "SYM2"  # closest to 0.75

    def test_tie_break_by_tightest_spread_pct(self) -> None:
        """When two contracts are equidistant from 0.75, pick the tighter spread."""
        gate = _make_gate_decision(express=True, target_delta_low=0.70, target_delta_high=0.80)
        cfg = _make_config(option_target_delta_low=0.70, option_target_delta_high=0.80)
        # Both have delta 0.73 → equidistant from 0.75 (distance 0.02)
        wide_spread = _make_contract(
            occ_symbol="WIDE",
            delta=0.73,
            bid=10.0,
            ask=10.50,  # spread_pct = 0.5/10.25 ≈ 0.049
        )
        tight_spread = _make_contract(
            occ_symbol="TIGHT",
            delta=0.73,
            bid=10.0,
            ask=10.10,  # spread_pct = 0.1/10.05 ≈ 0.010
        )
        client = _make_client([wide_spread, tight_spread])
        result = select_contract(
            client, gate, underlying="AAPL", horizon_days=90.0, config=cfg, as_of=AS_OF
        )
        assert result is not None
        assert result.occ_symbol == "TIGHT"


# ---------------------------------------------------------------------------
# 6. Expiry window passed to fetch_chain
# ---------------------------------------------------------------------------

class TestExpiryWindow:
    def test_expiry_window_uses_max_of_min_expiry_and_horizon_plus_buffer(self) -> None:
        """With horizon=90 + buffer=30 = 120 > min_expiry_days=60 → min_dte=120."""
        gate = _make_gate_decision(express=True)
        cfg = _make_config(
            option_min_expiry_days=60,
            option_horizon_buffer_days=30,
            option_max_expiry_buffer_days=180,
        )
        client = _make_client([])
        select_contract(
            client, gate, underlying="AAPL", horizon_days=90.0, config=cfg, as_of=AS_OF
        )
        call_kwargs = client.fetch_chain.call_args
        min_exp = call_kwargs.kwargs["min_expiry"]
        max_exp = call_kwargs.kwargs["max_expiry"]
        expected_min = AS_OF + datetime.timedelta(days=120)  # max(60, 90+30)
        expected_max = AS_OF + datetime.timedelta(days=90 + 180)
        assert min_exp == expected_min
        assert max_exp == expected_max

    def test_expiry_window_uses_min_expiry_days_when_horizon_is_short(self) -> None:
        """With horizon=20 + buffer=30 = 50 < min_expiry_days=60 → min_dte=60."""
        gate = _make_gate_decision(express=True)
        cfg = _make_config(
            option_min_expiry_days=60,
            option_horizon_buffer_days=30,
            option_max_expiry_buffer_days=180,
        )
        client = _make_client([])
        select_contract(
            client, gate, underlying="AAPL", horizon_days=20.0, config=cfg, as_of=AS_OF
        )
        call_kwargs = client.fetch_chain.call_args
        min_exp = call_kwargs.kwargs["min_expiry"]
        expected_min = AS_OF + datetime.timedelta(days=60)  # max(60, 20+30=50) → 60
        assert min_exp == expected_min

    def test_side_passed_to_fetch_chain(self) -> None:
        gate = _make_gate_decision(express=True, side=OptionSide.PUT)
        cfg = _make_config()
        client = _make_client([])
        select_contract(
            client, gate, underlying="AAPL", horizon_days=90.0, config=cfg, as_of=AS_OF
        )
        call_kwargs = client.fetch_chain.call_args
        assert call_kwargs.kwargs["side"] == OptionSide.PUT


# ---------------------------------------------------------------------------
# 7. Fetch-chain exception → None (never raise)
# ---------------------------------------------------------------------------

class TestNeverRaises:
    def test_fetch_chain_exception_returns_none(self) -> None:
        gate = _make_gate_decision(express=True)
        cfg = _make_config()
        client = MagicMock(name="AlpacaOptionsClient")
        client.fetch_chain.side_effect = RuntimeError("network error")
        result = select_contract(
            client, gate, underlying="AAPL", horizon_days=90.0, config=cfg, as_of=AS_OF
        )
        assert result is None


# ---------------------------------------------------------------------------
# 8. Multiple qualifiers — returns the best one
# ---------------------------------------------------------------------------

class TestMultipleQualifiers:
    def test_selects_best_from_mixed_quality_chain(self) -> None:
        gate = _make_gate_decision(express=True, target_delta_low=0.70, target_delta_high=0.80)
        cfg = _make_config(
            option_target_delta_low=0.70,
            option_target_delta_high=0.80,
            option_min_open_interest=100,
            option_min_volume=10,
        )
        # This one fails OI
        bad_oi = _make_contract(occ_symbol="BAD_OI", delta=0.74, open_interest=5)
        # This one fails volume
        bad_vol = _make_contract(occ_symbol="BAD_VOL", delta=0.74, volume=2)
        # This one fails delta band
        bad_delta = _make_contract(occ_symbol="BAD_DELTA", delta=0.60)
        # This one is good but far from 0.75
        ok_far = _make_contract(occ_symbol="OK_FAR", delta=0.71)
        # This one is closest to 0.75
        ok_best = _make_contract(occ_symbol="OK_BEST", delta=0.75)
        chain = [bad_oi, bad_vol, bad_delta, ok_far, ok_best]
        client = _make_client(chain)
        result = select_contract(
            client, gate, underlying="AAPL", horizon_days=90.0, config=cfg, as_of=AS_OF
        )
        assert result is not None
        assert result.occ_symbol == "OK_BEST"
