"""Tests for arbiter/options/shadow_log.py — log_shadow_option()."""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.options.shadow_log import log_shadow_option
from arbiter.options.types import (
    OptionContract,
    OptionGateDecision,
    OptionOrder,
    OptionSide,
)

# ---------------------------------------------------------------------------
# Fixed timestamps used across all tests
# ---------------------------------------------------------------------------

_AS_OF = "2026-06-25T10:00:00+00:00"
_CREATED_AT = "2026-06-25T10:00:01+00:00"
_IDEA_ID = "01HZ0000000000000000000001"


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------

def _make_gate_decision(
    *,
    express: bool = True,
    reason: str = "OK",
    side: OptionSide | None = OptionSide.CALL,
    conviction: float = 0.85,
    ivr_estimate: float | None = 0.42,
) -> OptionGateDecision:
    return OptionGateDecision(
        express=express,
        reason=reason,
        side=side,
        target_delta_low=0.70,
        target_delta_high=0.80,
        min_expiry_days=30,
        catalyst_tag="form4_cluster",
        conviction=conviction,
        conviction_threshold_used=0.70,
        horizon_days=45.0,
        ivr_estimate=ivr_estimate,
        realized_vol_proxy=0.28,
    )


def _make_contract() -> OptionContract:
    return OptionContract(
        occ_symbol="AAPL240119C00150000",
        underlying="AAPL",
        side=OptionSide.CALL,
        strike=150.0,
        expiry=datetime.date(2024, 1, 19),
        delta=0.75,
        iv=0.30,
        bid=3.00,
        ask=3.20,
        open_interest=500,
        volume=50,
    )


def _make_order(contract: OptionContract) -> OptionOrder:
    return OptionOrder(
        contract=contract,
        contracts_qty=5,
        est_premium=1_550.0,
        delta_adjusted_notional=56_250.0,
        side=OptionSide.CALL,
    )


@pytest.fixture()
def migrated_conn(tmp_path: Path):
    """Return a real SQLite connection with all migrations applied."""
    db_path = str(tmp_path / "test_shadow.db")
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Core behaviour: gate fired (express=True) with contract + order
# ---------------------------------------------------------------------------

class TestLogShadowOptionExpressed:
    def test_returns_string_ulid(self, migrated_conn):
        gate = _make_gate_decision()
        contract = _make_contract()
        order = _make_order(contract)

        row_id = log_shadow_option(
            migrated_conn,
            idea_id=_IDEA_ID,
            gate_decision=gate,
            contract=contract,
            order=order,
            as_of=_AS_OF,
            created_at=_CREATED_AT,
        )
        assert isinstance(row_id, str)
        assert len(row_id) == 26  # ULID is 26 chars (Crockford base32)

    def test_row_persisted_in_db(self, migrated_conn):
        gate = _make_gate_decision()
        contract = _make_contract()
        order = _make_order(contract)

        row_id = log_shadow_option(
            migrated_conn,
            idea_id=_IDEA_ID,
            gate_decision=gate,
            contract=contract,
            order=order,
            as_of=_AS_OF,
            created_at=_CREATED_AT,
        )

        cursor = migrated_conn.execute(
            "SELECT * FROM option_shadow_log WHERE id = ?", (row_id,)
        )
        row = cursor.fetchone()
        assert row is not None

    def test_gate_express_stored_as_1(self, migrated_conn):
        gate = _make_gate_decision(express=True)
        contract = _make_contract()
        order = _make_order(contract)

        row_id = log_shadow_option(
            migrated_conn,
            idea_id=_IDEA_ID,
            gate_decision=gate,
            contract=contract,
            order=order,
            as_of=_AS_OF,
            created_at=_CREATED_AT,
        )

        row = migrated_conn.execute(
            "SELECT gate_express FROM option_shadow_log WHERE id = ?", (row_id,)
        ).fetchone()
        assert row["gate_express"] == 1

    def test_idea_id_stored(self, migrated_conn):
        gate = _make_gate_decision()
        contract = _make_contract()
        order = _make_order(contract)

        row_id = log_shadow_option(
            migrated_conn,
            idea_id=_IDEA_ID,
            gate_decision=gate,
            contract=contract,
            order=order,
            as_of=_AS_OF,
            created_at=_CREATED_AT,
        )

        row = migrated_conn.execute(
            "SELECT idea_id FROM option_shadow_log WHERE id = ?", (row_id,)
        ).fetchone()
        assert row["idea_id"] == _IDEA_ID

    def test_underlying_from_contract(self, migrated_conn):
        gate = _make_gate_decision()
        contract = _make_contract()
        order = _make_order(contract)

        row_id = log_shadow_option(
            migrated_conn,
            idea_id=_IDEA_ID,
            gate_decision=gate,
            contract=contract,
            order=order,
            as_of=_AS_OF,
            created_at=_CREATED_AT,
        )

        row = migrated_conn.execute(
            "SELECT underlying FROM option_shadow_log WHERE id = ?", (row_id,)
        ).fetchone()
        assert row["underlying"] == "AAPL"

    def test_contract_fields_stored(self, migrated_conn):
        gate = _make_gate_decision()
        contract = _make_contract()
        order = _make_order(contract)

        row_id = log_shadow_option(
            migrated_conn,
            idea_id=_IDEA_ID,
            gate_decision=gate,
            contract=contract,
            order=order,
            as_of=_AS_OF,
            created_at=_CREATED_AT,
        )

        row = migrated_conn.execute(
            "SELECT occ_symbol, strike, expiry, delta, iv, bid, ask, "
            "open_interest, volume, side "
            "FROM option_shadow_log WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["occ_symbol"] == "AAPL240119C00150000"
        assert abs(row["strike"] - 150.0) < 1e-9
        assert row["expiry"] == "2024-01-19"
        assert abs(row["delta"] - 0.75) < 1e-9
        assert abs(row["iv"] - 0.30) < 1e-9
        assert abs(row["bid"] - 3.00) < 1e-9
        assert abs(row["ask"] - 3.20) < 1e-9
        assert row["open_interest"] == 500
        assert row["volume"] == 50
        assert row["side"] == "call"

    def test_order_fields_stored(self, migrated_conn):
        gate = _make_gate_decision()
        contract = _make_contract()
        order = _make_order(contract)

        row_id = log_shadow_option(
            migrated_conn,
            idea_id=_IDEA_ID,
            gate_decision=gate,
            contract=contract,
            order=order,
            as_of=_AS_OF,
            created_at=_CREATED_AT,
        )

        row = migrated_conn.execute(
            "SELECT contracts_qty, est_premium, delta_adjusted_notional "
            "FROM option_shadow_log WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["contracts_qty"] == 5
        assert abs(row["est_premium"] - 1_550.0) < 1e-6
        assert abs(row["delta_adjusted_notional"] - 56_250.0) < 1e-6

    def test_gate_decision_fields_stored(self, migrated_conn):
        gate = _make_gate_decision(
            reason="OK",
            conviction=0.85,
            ivr_estimate=0.42,
        )
        contract = _make_contract()
        order = _make_order(contract)

        row_id = log_shadow_option(
            migrated_conn,
            idea_id=_IDEA_ID,
            gate_decision=gate,
            contract=contract,
            order=order,
            as_of=_AS_OF,
            created_at=_CREATED_AT,
        )

        row = migrated_conn.execute(
            "SELECT gate_reason, conviction, horizon_days, catalyst_tag, "
            "ivr_estimate, realized_vol_proxy "
            "FROM option_shadow_log WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["gate_reason"] == "OK"
        assert abs(row["conviction"] - 0.85) < 1e-9
        assert abs(row["horizon_days"] - 45.0) < 1e-9
        assert row["catalyst_tag"] == "form4_cluster"
        assert abs(row["ivr_estimate"] - 0.42) < 1e-9
        assert abs(row["realized_vol_proxy"] - 0.28) < 1e-9

    def test_timestamps_stored(self, migrated_conn):
        gate = _make_gate_decision()
        contract = _make_contract()
        order = _make_order(contract)

        row_id = log_shadow_option(
            migrated_conn,
            idea_id=_IDEA_ID,
            gate_decision=gate,
            contract=contract,
            order=order,
            as_of=_AS_OF,
            created_at=_CREATED_AT,
        )

        row = migrated_conn.execute(
            "SELECT as_of, created_at FROM option_shadow_log WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["as_of"] == _AS_OF
        assert row["created_at"] == _CREATED_AT


# ---------------------------------------------------------------------------
# Gate rejected (express=False): contract and order are None
# ---------------------------------------------------------------------------

class TestLogShadowOptionRejected:
    def test_rejected_row_has_gate_express_0(self, migrated_conn):
        gate = _make_gate_decision(
            express=False,
            reason="CONVICTION_TOO_LOW",
            side=None,
        )

        row_id = log_shadow_option(
            migrated_conn,
            idea_id=_IDEA_ID,
            gate_decision=gate,
            contract=None,
            order=None,
            as_of=_AS_OF,
            created_at=_CREATED_AT,
        )

        row = migrated_conn.execute(
            "SELECT gate_express, gate_reason, occ_symbol, contracts_qty, "
            "est_premium, delta_adjusted_notional, side "
            "FROM option_shadow_log WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["gate_express"] == 0
        assert row["gate_reason"] == "CONVICTION_TOO_LOW"
        assert row["occ_symbol"] is None
        assert row["contracts_qty"] is None
        assert row["est_premium"] is None
        assert row["delta_adjusted_notional"] is None
        assert row["side"] is None

    def test_rejected_row_still_has_conviction(self, migrated_conn):
        gate = _make_gate_decision(
            express=False,
            reason="IV_RANK_TOO_HIGH",
            side=None,
            conviction=0.55,
        )

        row_id = log_shadow_option(
            migrated_conn,
            idea_id=_IDEA_ID,
            gate_decision=gate,
            contract=None,
            order=None,
            as_of=_AS_OF,
            created_at=_CREATED_AT,
        )

        row = migrated_conn.execute(
            "SELECT conviction FROM option_shadow_log WHERE id = ?", (row_id,)
        ).fetchone()
        assert abs(row["conviction"] - 0.55) < 1e-9


# ---------------------------------------------------------------------------
# Gate fired but no contract found: order=None, contract=None
# ---------------------------------------------------------------------------

class TestLogShadowOptionNoContract:
    def test_gate_fired_no_contract_nulls_order_fields(self, migrated_conn):
        gate = _make_gate_decision(express=True, reason="OK")

        row_id = log_shadow_option(
            migrated_conn,
            idea_id=_IDEA_ID,
            gate_decision=gate,
            contract=None,
            order=None,
            as_of=_AS_OF,
            created_at=_CREATED_AT,
        )

        row = migrated_conn.execute(
            "SELECT gate_express, occ_symbol, contracts_qty, est_premium "
            "FROM option_shadow_log WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["gate_express"] == 1
        assert row["occ_symbol"] is None
        assert row["contracts_qty"] is None
        assert row["est_premium"] is None


# ---------------------------------------------------------------------------
# Gate fired with contract but no order (sizing returned None)
# ---------------------------------------------------------------------------

class TestLogShadowOptionNoOrder:
    def test_contract_present_order_absent(self, migrated_conn):
        gate = _make_gate_decision(express=True, reason="OK")
        contract = _make_contract()

        row_id = log_shadow_option(
            migrated_conn,
            idea_id=_IDEA_ID,
            gate_decision=gate,
            contract=contract,
            order=None,
            as_of=_AS_OF,
            created_at=_CREATED_AT,
        )

        row = migrated_conn.execute(
            "SELECT occ_symbol, contracts_qty, est_premium "
            "FROM option_shadow_log WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["occ_symbol"] == "AAPL240119C00150000"
        assert row["contracts_qty"] is None
        assert row["est_premium"] is None


# ---------------------------------------------------------------------------
# Multiple rows can be written independently (no PK collision)
# ---------------------------------------------------------------------------

class TestMultipleRows:
    def test_two_rows_have_distinct_ids(self, migrated_conn):
        gate = _make_gate_decision()
        contract = _make_contract()
        order = _make_order(contract)

        id1 = log_shadow_option(
            migrated_conn,
            idea_id=_IDEA_ID,
            gate_decision=gate,
            contract=contract,
            order=order,
            as_of=_AS_OF,
            created_at=_CREATED_AT,
        )
        id2 = log_shadow_option(
            migrated_conn,
            idea_id=_IDEA_ID,
            gate_decision=gate,
            contract=contract,
            order=order,
            as_of=_AS_OF,
            created_at=_CREATED_AT,
        )

        assert id1 != id2

    def test_two_rows_both_queryable(self, migrated_conn):
        gate = _make_gate_decision()
        contract = _make_contract()
        order = _make_order(contract)

        log_shadow_option(
            migrated_conn,
            idea_id=_IDEA_ID,
            gate_decision=gate,
            contract=contract,
            order=order,
            as_of=_AS_OF,
            created_at=_CREATED_AT,
        )
        log_shadow_option(
            migrated_conn,
            idea_id=_IDEA_ID,
            gate_decision=gate,
            contract=contract,
            order=order,
            as_of=_AS_OF,
            created_at=_CREATED_AT,
        )

        count = migrated_conn.execute(
            "SELECT COUNT(*) FROM option_shadow_log"
        ).fetchone()[0]
        assert count == 2
