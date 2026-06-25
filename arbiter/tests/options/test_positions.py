"""Tests for arbiter/options/positions.py.

Guarantees
----------
- record_open_position inserts a row into option_positions with the correct fields.
- list_open_positions returns open rows (no matching outcome) and excludes closed ones.
- Openness is derived entirely by the ABSENCE of an option_outcomes row on
  (idea_id, occ_symbol) — no status column, no UPDATE ever issued.
- A position with an outcome row (closed) is excluded from list_open_positions.
- Multiple positions with different idea_id/occ_symbol combos are tracked
  independently.
"""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.options.outcomes import record_option_outcome
from arbiter.options.positions import list_open_positions, record_open_position
from arbiter.options.types import OptionContract, OptionOrder, OptionSide

# ---------------------------------------------------------------------------
# Fixed test constants
# ---------------------------------------------------------------------------

_IDEA_ID_A = "01HZ0000000000000000000001"
_IDEA_ID_B = "01HZ0000000000000000000002"
_OCC_A = "AAPL260101C00150000"
_OCC_B = "TSLA260201P00200000"
_OPEN_TS = "2026-06-25T10:00:00+00:00"
_CLOSE_TS = "2026-07-01T15:30:00+00:00"
_CREATED_AT = "2026-06-25T10:00:01+00:00"
_BROKER_ORDER_ID = "broker-uuid-0001"
_THESIS_HORIZON = datetime.date(2026, 9, 1)
_UNDERLYING_OPEN = 180.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path: Path):
    """Real SQLite connection with all migrations applied (incl. 031)."""
    db_path = str(tmp_path / "test_positions.db")
    c = get_connection(db_path)
    run_migrations(c, applied_at=_OPEN_TS)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Helpers: build typed objects for testing
# ---------------------------------------------------------------------------

def _make_call_contract(
    occ_symbol: str = _OCC_A,
    underlying: str = "AAPL",
    strike: float = 150.0,
    delta: float = 0.75,
    iv: float = 0.30,
    ask: float = 2.55,
    bid: float = 2.45,
) -> OptionContract:
    return OptionContract(
        occ_symbol=occ_symbol,
        underlying=underlying,
        side=OptionSide.CALL,
        strike=strike,
        expiry=datetime.date(2026, 1, 1),
        delta=delta,
        iv=iv,
        bid=bid,
        ask=ask,
        open_interest=500,
        volume=100,
    )


def _make_put_contract(
    occ_symbol: str = _OCC_B,
    underlying: str = "TSLA",
    strike: float = 200.0,
) -> OptionContract:
    return OptionContract(
        occ_symbol=occ_symbol,
        underlying=underlying,
        side=OptionSide.PUT,
        strike=strike,
        expiry=datetime.date(2026, 2, 1),
        delta=-0.75,
        iv=0.45,
        bid=3.00,
        ask=3.10,
        open_interest=300,
        volume=50,
    )


def _make_order(
    contract: OptionContract,
    contracts_qty: int = 2,
    est_premium: float = 500.0,
) -> OptionOrder:
    return OptionOrder(
        contract=contract,
        contracts_qty=contracts_qty,
        est_premium=est_premium,
        delta_adjusted_notional=abs(contract.delta or 0) * 100 * 180.0 * contracts_qty,
        side=contract.side,
    )


def _record_call_position(conn, idea_id: str = _IDEA_ID_A, occ: str = _OCC_A) -> str:
    contract = _make_call_contract(occ_symbol=occ)
    order = _make_order(contract)
    return record_open_position(
        conn,
        idea_id=idea_id,
        shadow_id=None,
        contract=contract,
        order=order,
        broker_order_id=_BROKER_ORDER_ID,
        underlying_open_price=_UNDERLYING_OPEN,
        thesis_horizon_date=_THESIS_HORIZON,
        original_conviction=0.80,
        open_ts=_OPEN_TS,
        created_at=_CREATED_AT,
    )


def _record_put_position(conn, idea_id: str = _IDEA_ID_B, occ: str = _OCC_B) -> str:
    contract = _make_put_contract(occ_symbol=occ)
    order = _make_order(contract, contracts_qty=1, est_premium=310.0)
    return record_open_position(
        conn,
        idea_id=idea_id,
        shadow_id="01HZ0000000000000000000099",
        contract=contract,
        order=order,
        broker_order_id="broker-uuid-0002",
        underlying_open_price=220.0,
        thesis_horizon_date=datetime.date(2026, 10, 1),
        original_conviction=-0.70,
        open_ts=_OPEN_TS,
        created_at=_CREATED_AT,
    )


def _close_position(conn, idea_id: str, occ_symbol: str) -> str:
    """Insert a minimal outcome row to mark the position as closed."""
    return record_option_outcome(
        conn,
        shadow_id=None,
        idea_id=idea_id,
        underlying="AAPL",
        occ_symbol=occ_symbol,
        side="call",
        open_ts=_OPEN_TS,
        close_ts=_CLOSE_TS,
        close_reason="premium_stop",
        entry_premium=500.0,
        exit_premium=200.0,
        underlying_open_price=180.0,
        underlying_close_price=175.0,
        delta_at_open=0.75,
        iv_at_open=0.30,
        iv_at_close=0.35,
        contracts_qty=2,
        created_at=_CLOSE_TS,
    )


# ---------------------------------------------------------------------------
# record_open_position: row inserted with correct fields
# ---------------------------------------------------------------------------

class TestRecordOpenPosition:
    def test_returns_ulid_string(self, conn):
        pos_id = _record_call_position(conn)
        assert isinstance(pos_id, str)
        assert len(pos_id) == 26

    def test_row_queryable_by_id(self, conn):
        pos_id = _record_call_position(conn)
        row = conn.execute(
            "SELECT * FROM option_positions WHERE id = ?", (pos_id,)
        ).fetchone()
        assert row is not None

    def test_scalar_fields_stored(self, conn):
        pos_id = _record_call_position(conn)
        conn.row_factory = __import__("sqlite3").Row
        row = conn.execute(
            "SELECT idea_id, shadow_id, underlying, occ_symbol, side, strike, "
            "expiry, contracts_qty, entry_premium, delta_at_open, iv_at_open, "
            "underlying_open_price, thesis_horizon_date, original_conviction, "
            "broker_order_id, open_ts, created_at "
            "FROM option_positions WHERE id = ?",
            (pos_id,),
        ).fetchone()
        assert row["idea_id"] == _IDEA_ID_A
        assert row["shadow_id"] is None
        assert row["underlying"] == "AAPL"
        assert row["occ_symbol"] == _OCC_A
        assert row["side"] == "call"
        assert abs(row["strike"] - 150.0) < 1e-9
        assert row["expiry"] == "2026-01-01"
        assert row["contracts_qty"] == 2
        assert abs(row["entry_premium"] - 500.0) < 1e-9
        assert abs(row["delta_at_open"] - 0.75) < 1e-9
        assert abs(row["iv_at_open"] - 0.30) < 1e-9
        assert abs(row["underlying_open_price"] - 180.0) < 1e-9
        assert row["thesis_horizon_date"] == "2026-09-01"
        assert abs(row["original_conviction"] - 0.80) < 1e-9
        assert row["broker_order_id"] == _BROKER_ORDER_ID
        assert row["open_ts"] == _OPEN_TS
        assert row["created_at"] == _CREATED_AT

    def test_shadow_id_stored_when_provided(self, conn):
        pos_id = _record_put_position(conn)
        row = conn.execute(
            "SELECT shadow_id FROM option_positions WHERE id = ?", (pos_id,)
        ).fetchone()
        assert row[0] == "01HZ0000000000000000000099"

    def test_entry_limit_price_derived(self, conn):
        # est_premium=500, contracts_qty=2, qty_shares=200 → limit_price=2.50
        pos_id = _record_call_position(conn)
        row = conn.execute(
            "SELECT entry_limit_price FROM option_positions WHERE id = ?", (pos_id,)
        ).fetchone()
        assert abs(row[0] - 2.50) < 1e-9

    def test_put_side_stored(self, conn):
        pos_id = _record_put_position(conn)
        row = conn.execute(
            "SELECT side FROM option_positions WHERE id = ?", (pos_id,)
        ).fetchone()
        assert row[0] == "put"


# ---------------------------------------------------------------------------
# list_open_positions: openness derived by absence of outcome row
# ---------------------------------------------------------------------------

class TestListOpenPositions:
    def test_empty_when_no_positions(self, conn):
        assert list_open_positions(conn) == []

    def test_returns_one_open_position(self, conn):
        _record_call_position(conn)
        open_pos = list_open_positions(conn)
        assert len(open_pos) == 1
        assert open_pos[0]["occ_symbol"] == _OCC_A

    def test_returns_two_independent_positions(self, conn):
        _record_call_position(conn)
        _record_put_position(conn)
        open_pos = list_open_positions(conn)
        assert len(open_pos) == 2
        occs = {p["occ_symbol"] for p in open_pos}
        assert occs == {_OCC_A, _OCC_B}

    def test_closed_position_excluded(self, conn):
        """After inserting an outcome row, the position should disappear from open list."""
        _record_call_position(conn)
        assert len(list_open_positions(conn)) == 1

        # Close via outcome row.
        _close_position(conn, _IDEA_ID_A, _OCC_A)

        assert list_open_positions(conn) == []

    def test_only_closed_position_excluded_not_the_other(self, conn):
        """Closing one position must not affect the other."""
        _record_call_position(conn)
        _record_put_position(conn)
        assert len(list_open_positions(conn)) == 2

        # Close only the CALL position.
        _close_position(conn, _IDEA_ID_A, _OCC_A)

        remaining = list_open_positions(conn)
        assert len(remaining) == 1
        assert remaining[0]["occ_symbol"] == _OCC_B

    def test_returns_dicts_not_sqlite_rows(self, conn):
        _record_call_position(conn)
        open_pos = list_open_positions(conn)
        assert isinstance(open_pos[0], dict)

    def test_open_position_dict_has_all_expected_keys(self, conn):
        _record_call_position(conn)
        row = list_open_positions(conn)[0]
        expected_keys = {
            "id", "idea_id", "shadow_id", "underlying", "occ_symbol", "side",
            "strike", "expiry", "contracts_qty", "entry_premium",
            "entry_limit_price", "delta_at_open", "iv_at_open",
            "underlying_open_price", "thesis_horizon_date",
            "original_conviction", "broker_order_id", "open_ts", "created_at",
        }
        assert expected_keys.issubset(set(row.keys()))

    def test_same_occ_different_idea_both_open(self, conn):
        """Two positions with the same OCC but different idea_ids are independent."""
        contract = _make_call_contract()
        order = _make_order(contract)
        record_open_position(
            conn,
            idea_id=_IDEA_ID_A,
            shadow_id=None,
            contract=contract,
            order=order,
            broker_order_id="broker-a",
            underlying_open_price=180.0,
            thesis_horizon_date=_THESIS_HORIZON,
            original_conviction=0.80,
            open_ts=_OPEN_TS,
            created_at=_CREATED_AT,
        )
        record_open_position(
            conn,
            idea_id=_IDEA_ID_B,
            shadow_id=None,
            contract=contract,
            order=order,
            broker_order_id="broker-b",
            underlying_open_price=180.0,
            thesis_horizon_date=_THESIS_HORIZON,
            original_conviction=0.70,
            open_ts=_OPEN_TS,
            created_at=_CREATED_AT,
        )
        assert len(list_open_positions(conn)) == 2
