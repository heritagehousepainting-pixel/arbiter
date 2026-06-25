"""Tests for arbiter/options/manage.py — manage_option_positions().

Coverage
--------
- No-op when config.options_mode != "paper".
- A position whose premium-stop trigger fires:
    * sell-to-close order placed via client.close_position.
    * outcome recorded via record_option_outcome → option_outcomes has a row.
    * position no longer listed as open by list_open_positions.
- A position whose no trigger fires stays open.
- Fault isolation: one failing position does not prevent others from closing.
- current_conviction_for lookup used when provided; fallback to original_conviction
  when lookup returns None.
- Returns list of outcome ULIDs (one per closed position).
"""
from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, call

import pytest

from arbiter.config import Config
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.options.manage import manage_option_positions
from arbiter.options.positions import list_open_positions, record_open_position
from arbiter.options.types import OptionContract, OptionOrder, OptionSide

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_IDEA_ID_A = "01HZ0000000000000000000001"
_IDEA_ID_B = "01HZ0000000000000000000002"
_OCC_A = "AAPL260101C00150000"
_OCC_B = "TSLA260201P00200000"
_OPEN_TS = "2026-06-25T10:00:00+00:00"
_CLOCK = "2026-06-25T14:00:00+00:00"
_CREATED_AT = "2026-06-25T10:00:01+00:00"
# entry_premium = $500 total (2 contracts × $2.50 mid × 100)
_ENTRY_PREMIUM = 500.0
_CONTRACTS_QTY = 2
# mid above stop → no premium stop fires
_MID_HIGH = 10.0
# mid below 50% stop threshold: $500 × (1-0.50) = $250; mid × 2 × 100 must be ≤ $250 → mid ≤ 1.25
_MID_STOP = 1.00


# ---------------------------------------------------------------------------
# Config factory
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> Config:
    base: dict = dict(
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
        options_mode="paper",
        option_premium_stop_pct=0.50,
    )
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path: Path):
    db_path = str(tmp_path / "test_manage.db")
    c = get_connection(db_path)
    run_migrations(c, applied_at=_OPEN_TS)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Fake client builder
# ---------------------------------------------------------------------------

def _make_client(mid: float, occ_symbol: str = _OCC_A) -> MagicMock:
    """Fake client: snapshot returns mid; close_position returns a success dict."""
    client = MagicMock(spec=["snapshot", "close_position"])
    client.snapshot.return_value = {
        occ_symbol: {
            "bid": mid,
            "ask": mid,
            "iv": 0.28,
            "delta": 0.72,
        }
    }
    client.close_position.return_value = {
        "id": "broker-close-uuid-001",
        "status": "accepted",
        "symbol": occ_symbol,
        "qty": str(_CONTRACTS_QTY),
        "side": "sell",
    }
    return client


def _make_multi_client(mids: dict[str, float]) -> MagicMock:
    """Fake client with configurable per-OCC mids."""
    client = MagicMock(spec=["snapshot", "close_position"])

    def _snapshot(occ_list, **_kw):
        result = {}
        for occ in occ_list:
            if occ in mids:
                m = mids[occ]
                result[occ] = {"bid": m, "ask": m, "iv": 0.28}
        return result

    client.snapshot.side_effect = _snapshot
    client.close_position.return_value = {
        "id": "broker-close-uuid-multi",
        "status": "accepted",
        "qty": "1",
        "side": "sell",
    }
    return client


# ---------------------------------------------------------------------------
# Position helpers
# ---------------------------------------------------------------------------

def _make_call_contract(occ: str = _OCC_A) -> OptionContract:
    return OptionContract(
        occ_symbol=occ,
        underlying="AAPL",
        side=OptionSide.CALL,
        strike=150.0,
        expiry=datetime.date(2026, 1, 1),
        delta=0.75,
        iv=0.30,
        bid=2.45,
        ask=2.55,
        open_interest=500,
        volume=100,
    )


def _make_put_contract(occ: str = _OCC_B) -> OptionContract:
    return OptionContract(
        occ_symbol=occ,
        underlying="TSLA",
        side=OptionSide.PUT,
        strike=200.0,
        expiry=datetime.date(2026, 2, 1),
        delta=-0.75,
        iv=0.45,
        bid=3.00,
        ask=3.10,
        open_interest=300,
        volume=50,
    )


def _make_order(contract: OptionContract, contracts_qty: int = _CONTRACTS_QTY, est_premium: float = _ENTRY_PREMIUM) -> OptionOrder:
    return OptionOrder(
        contract=contract,
        contracts_qty=contracts_qty,
        est_premium=est_premium,
        delta_adjusted_notional=abs(contract.delta or 0) * 100 * 180.0 * contracts_qty,
        side=contract.side,
    )


def _open_call_position(
    conn,
    idea_id: str = _IDEA_ID_A,
    occ: str = _OCC_A,
    original_conviction: float = 0.80,
    horizon: datetime.date = datetime.date(2026, 12, 31),
    entry_premium: float = _ENTRY_PREMIUM,
) -> str:
    contract = _make_call_contract(occ)
    order = _make_order(contract, est_premium=entry_premium)
    return record_open_position(
        conn,
        idea_id=idea_id,
        shadow_id=None,
        contract=contract,
        order=order,
        broker_order_id="broker-open-uuid",
        underlying_open_price=180.0,
        thesis_horizon_date=horizon,
        original_conviction=original_conviction,
        open_ts=_OPEN_TS,
        created_at=_CREATED_AT,
    )


# ---------------------------------------------------------------------------
# Guard: no-op when mode != "paper"
# ---------------------------------------------------------------------------

class TestModeGuard:
    def test_off_mode_returns_empty_list(self, conn):
        _open_call_position(conn)
        cfg = _make_config(options_mode="off")
        client = MagicMock()
        result = manage_option_positions(conn, client, config=cfg, clock=_CLOCK)
        assert result == []
        client.snapshot.assert_not_called()
        client.close_position.assert_not_called()

    def test_shadow_mode_returns_empty_list(self, conn):
        _open_call_position(conn)
        cfg = _make_config(options_mode="shadow")
        client = MagicMock()
        result = manage_option_positions(conn, client, config=cfg, clock=_CLOCK)
        assert result == []
        client.snapshot.assert_not_called()

    def test_no_positions_returns_empty_list(self, conn):
        cfg = _make_config()
        client = MagicMock()
        result = manage_option_positions(conn, client, config=cfg, clock=_CLOCK)
        assert result == []


# ---------------------------------------------------------------------------
# Premium-stop trigger: sell-to-close placed + outcome recorded
# ---------------------------------------------------------------------------

class TestPremiumStopTrigger:
    def test_stop_position_removed_from_open_list(self, conn):
        _open_call_position(conn)
        assert len(list_open_positions(conn)) == 1

        cfg = _make_config(option_premium_stop_pct=0.50)
        client = _make_client(mid=_MID_STOP)
        manage_option_positions(conn, client, config=cfg, clock=_CLOCK)

        # Position should now be closed (outcome row inserted).
        assert list_open_positions(conn) == []

    def test_stop_returns_outcome_id_list(self, conn):
        _open_call_position(conn)
        cfg = _make_config(option_premium_stop_pct=0.50)
        client = _make_client(mid=_MID_STOP)
        result = manage_option_positions(conn, client, config=cfg, clock=_CLOCK)

        assert len(result) == 1
        assert isinstance(result[0], str)
        assert len(result[0]) == 26  # ULID

    def test_close_position_called_with_correct_args(self, conn):
        _open_call_position(conn)
        cfg = _make_config(option_premium_stop_pct=0.50)
        client = _make_client(mid=_MID_STOP)
        manage_option_positions(conn, client, config=cfg, clock=_CLOCK)

        client.close_position.assert_called_once()
        kwargs = client.close_position.call_args.kwargs
        assert kwargs["occ_symbol"] == _OCC_A
        assert kwargs["contracts_qty"] == _CONTRACTS_QTY
        assert abs(kwargs["limit_price"] - _MID_STOP) < 1e-9

    def test_outcome_row_inserted_in_db(self, conn):
        _open_call_position(conn)
        cfg = _make_config(option_premium_stop_pct=0.50)
        client = _make_client(mid=_MID_STOP)
        result = manage_option_positions(conn, client, config=cfg, clock=_CLOCK)

        # Outcome row is queryable by the returned id.
        row = conn.execute(
            "SELECT close_reason, idea_id, occ_symbol FROM option_outcomes WHERE id = ?",
            (result[0],),
        ).fetchone()
        assert row is not None
        assert row[0] == "premium_stop"
        assert row[1] == _IDEA_ID_A
        assert row[2] == _OCC_A

    def test_snapshot_called_before_close(self, conn):
        """Snapshot is called to get the mid price; close_position comes after."""
        _open_call_position(conn)
        cfg = _make_config(option_premium_stop_pct=0.50)
        client = _make_client(mid=_MID_STOP)
        manage_option_positions(conn, client, config=cfg, clock=_CLOCK)

        # Both called exactly once.
        assert client.snapshot.call_count >= 1
        client.close_position.assert_called_once()


# ---------------------------------------------------------------------------
# No-trigger: position stays open
# ---------------------------------------------------------------------------

class TestNoTrigger:
    def test_position_stays_open_when_no_trigger(self, conn):
        _open_call_position(conn)
        cfg = _make_config(option_premium_stop_pct=0.50)
        client = _make_client(mid=_MID_HIGH)
        result = manage_option_positions(conn, client, config=cfg, clock=_CLOCK)

        assert result == []
        assert len(list_open_positions(conn)) == 1

    def test_close_position_not_called_when_no_trigger(self, conn):
        _open_call_position(conn)
        cfg = _make_config(option_premium_stop_pct=0.50)
        client = _make_client(mid=_MID_HIGH)
        manage_option_positions(conn, client, config=cfg, clock=_CLOCK)
        client.close_position.assert_not_called()


# ---------------------------------------------------------------------------
# current_conviction_for lookup
# ---------------------------------------------------------------------------

class TestConvictionLookup:
    def test_uses_original_conviction_when_lookup_is_none(self, conn):
        """Without a conviction_for callable, original_conviction is used — no reversal."""
        _open_call_position(conn, original_conviction=0.80)
        cfg = _make_config()
        client = _make_client(mid=_MID_HIGH)
        # Pass no current_conviction_for → original_conviction used → no reversal
        result = manage_option_positions(conn, client, config=cfg, clock=_CLOCK)
        assert result == []

    def test_uses_original_when_lookup_returns_none(self, conn):
        """Lookup returning None falls back to original — no reversal exit."""
        _open_call_position(conn, original_conviction=0.80)
        cfg = _make_config()
        client = _make_client(mid=_MID_HIGH)

        def _lookup(idea_id: str) -> Optional[float]:
            return None  # absent from current cycle

        result = manage_option_positions(
            conn, client, config=cfg, clock=_CLOCK,
            current_conviction_for=_lookup,
        )
        assert result == []

    def test_reversal_fires_when_lookup_returns_opposite_sign(self, conn):
        """Lookup returning -0.80 (bearish) against original +0.80 (bullish) → reversal."""
        _open_call_position(conn, original_conviction=0.80)
        cfg = _make_config()
        client = _make_client(mid=_MID_HIGH)

        def _lookup(idea_id: str) -> Optional[float]:
            return -0.80  # conviction flipped

        result = manage_option_positions(
            conn, client, config=cfg, clock=_CLOCK,
            current_conviction_for=_lookup,
        )
        # Reversal triggered → position closed.
        assert len(result) == 1
        row = conn.execute(
            "SELECT close_reason FROM option_outcomes WHERE id = ?", (result[0],)
        ).fetchone()
        assert row[0] == "reversal"
        assert list_open_positions(conn) == []


# ---------------------------------------------------------------------------
# Fault isolation: one failing position doesn't block others
# ---------------------------------------------------------------------------

class TestFaultIsolation:
    def test_one_error_does_not_block_other_position(self, conn):
        """
        Position A's close_position call raises; position B should still close.
        Both positions have mids below stop threshold.
        """
        _open_call_position(conn, idea_id=_IDEA_ID_A, occ=_OCC_A)
        _open_call_position(conn, idea_id=_IDEA_ID_B, occ=_OCC_B)

        cfg = _make_config(option_premium_stop_pct=0.50)
        client = _make_multi_client({_OCC_A: _MID_STOP, _OCC_B: _MID_STOP})

        call_count = {"n": 0}

        def _close(**kwargs):
            call_count["n"] += 1
            if kwargs["occ_symbol"] == _OCC_A:
                raise RuntimeError("broker timeout")
            return {"id": "broker-ok", "status": "accepted", "qty": "2", "side": "sell"}

        client.close_position.side_effect = _close

        result = manage_option_positions(conn, client, config=cfg, clock=_CLOCK)

        # Only B closed successfully.
        assert len(result) == 1
        row = conn.execute(
            "SELECT occ_symbol FROM option_outcomes WHERE id = ?", (result[0],)
        ).fetchone()
        assert row[0] == _OCC_B

        # Position A is still open (close failed).
        open_pos = list_open_positions(conn)
        assert len(open_pos) == 1
        assert open_pos[0]["occ_symbol"] == _OCC_A


# ---------------------------------------------------------------------------
# Two positions: one closes, one stays open
# ---------------------------------------------------------------------------

class TestMixedPositions:
    def test_one_stop_one_no_trigger(self, conn):
        _open_call_position(conn, idea_id=_IDEA_ID_A, occ=_OCC_A, entry_premium=500.0)
        _open_call_position(conn, idea_id=_IDEA_ID_B, occ=_OCC_B, entry_premium=500.0)

        cfg = _make_config(option_premium_stop_pct=0.50)
        # OCC_A mid below stop; OCC_B mid well above stop.
        client = _make_multi_client({_OCC_A: _MID_STOP, _OCC_B: _MID_HIGH})

        result = manage_option_positions(conn, client, config=cfg, clock=_CLOCK)

        # One outcome created (for A).
        assert len(result) == 1

        # B still open.
        open_pos = list_open_positions(conn)
        assert len(open_pos) == 1
        assert open_pos[0]["occ_symbol"] == _OCC_B
