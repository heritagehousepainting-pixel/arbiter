"""Tests for arbiter/options/outcomes.py — record_option_outcome().

Guarantees
----------
- A row lands in ``option_outcomes`` with the correct computed fields.
- ``option_pl_pct`` is computed from premiums (display-only).
- ``underlying_alpha_bps`` is signed by side: CALL → +move = positive alpha,
  PUT → -move = positive alpha.
- Nothing is written to the equity ``outcomes`` table.
- Zero-division guards for entry_premium=0 and underlying_open_price=0.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.options.outcomes import record_option_outcome

# ---------------------------------------------------------------------------
# Fixed test data
# ---------------------------------------------------------------------------

_IDEA_ID = "01HZ0000000000000000000001"
_SHADOW_ID = "01HZ0000000000000000000002"
_OPEN_TS = "2026-06-01T10:00:00+00:00"
_CLOSE_TS = "2026-07-01T15:30:00+00:00"
_CREATED_AT = "2026-07-01T15:30:01+00:00"


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def migrated_conn(tmp_path: Path):
    """Real SQLite connection with all migrations applied."""
    db_path = str(tmp_path / "test_outcomes.db")
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_OPEN_TS)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _record_call(conn, *, entry_premium=500.0, exit_premium=750.0,
                 underlying_open=100.0, underlying_close=110.0):
    """Write a CALL outcome row with sensible defaults."""
    return record_option_outcome(
        conn,
        shadow_id=_SHADOW_ID,
        idea_id=_IDEA_ID,
        underlying="AAPL",
        occ_symbol="AAPL260101C00150000",
        side="call",
        open_ts=_OPEN_TS,
        close_ts=_CLOSE_TS,
        close_reason="horizon",
        entry_premium=entry_premium,
        exit_premium=exit_premium,
        underlying_open_price=underlying_open,
        underlying_close_price=underlying_close,
        delta_at_open=0.75,
        iv_at_open=0.30,
        iv_at_close=0.25,
        contracts_qty=2,
        created_at=_CREATED_AT,
    )


def _record_put(conn, *, entry_premium=400.0, exit_premium=600.0,
                underlying_open=100.0, underlying_close=90.0):
    """Write a PUT outcome row — underlying moves DOWN."""
    return record_option_outcome(
        conn,
        shadow_id=None,
        idea_id=_IDEA_ID,
        underlying="TSLA",
        occ_symbol="TSLA260101P00200000",
        side="put",
        open_ts=_OPEN_TS,
        close_ts=_CLOSE_TS,
        close_reason="premium_stop",
        entry_premium=entry_premium,
        exit_premium=exit_premium,
        underlying_open_price=underlying_open,
        underlying_close_price=underlying_close,
        delta_at_open=-0.75,
        iv_at_open=0.45,
        iv_at_close=0.40,
        contracts_qty=1,
        created_at=_CREATED_AT,
    )


# ---------------------------------------------------------------------------
# Core: row lands in option_outcomes
# ---------------------------------------------------------------------------

class TestRowPersisted:
    def test_returns_ulid_string(self, migrated_conn):
        row_id = _record_call(migrated_conn)
        assert isinstance(row_id, str)
        assert len(row_id) == 26

    def test_row_queryable_by_id(self, migrated_conn):
        row_id = _record_call(migrated_conn)
        row = migrated_conn.execute(
            "SELECT * FROM option_outcomes WHERE id = ?", (row_id,)
        ).fetchone()
        assert row is not None

    def test_scalar_fields_stored(self, migrated_conn):
        row_id = _record_call(migrated_conn)
        row = migrated_conn.execute(
            "SELECT idea_id, underlying, occ_symbol, side, close_reason, "
            "entry_premium, exit_premium, contracts_qty, open_ts, close_ts, "
            "created_at, shadow_id, delta_at_open, iv_at_open, iv_at_close "
            "FROM option_outcomes WHERE id = ?",
            (row_id,),
        ).fetchone()
        assert row["idea_id"] == _IDEA_ID
        assert row["underlying"] == "AAPL"
        assert row["occ_symbol"] == "AAPL260101C00150000"
        assert row["side"] == "call"
        assert row["close_reason"] == "horizon"
        assert abs(row["entry_premium"] - 500.0) < 1e-9
        assert abs(row["exit_premium"] - 750.0) < 1e-9
        assert row["contracts_qty"] == 2
        assert row["open_ts"] == _OPEN_TS
        assert row["close_ts"] == _CLOSE_TS
        assert row["created_at"] == _CREATED_AT
        assert row["shadow_id"] == _SHADOW_ID
        assert abs(row["delta_at_open"] - 0.75) < 1e-9
        assert abs(row["iv_at_open"] - 0.30) < 1e-9
        assert abs(row["iv_at_close"] - 0.25) < 1e-9

    def test_null_shadow_id_stored(self, migrated_conn):
        row_id = _record_put(migrated_conn)
        row = migrated_conn.execute(
            "SELECT shadow_id FROM option_outcomes WHERE id = ?", (row_id,)
        ).fetchone()
        assert row["shadow_id"] is None


# ---------------------------------------------------------------------------
# option_pl_pct computation
# ---------------------------------------------------------------------------

class TestOptionPlPct:
    def test_call_profit(self, migrated_conn):
        # exit 750, entry 500 → pl_pct = (750-500)/500 = 0.50
        row_id = _record_call(migrated_conn, entry_premium=500.0, exit_premium=750.0)
        row = migrated_conn.execute(
            "SELECT option_pl_pct FROM option_outcomes WHERE id = ?", (row_id,)
        ).fetchone()
        assert abs(row["option_pl_pct"] - 0.50) < 1e-9

    def test_put_loss(self, migrated_conn):
        # exit 200, entry 400 → pl_pct = (200-400)/400 = -0.50
        row_id = _record_put(migrated_conn, entry_premium=400.0, exit_premium=200.0)
        row = migrated_conn.execute(
            "SELECT option_pl_pct FROM option_outcomes WHERE id = ?", (row_id,)
        ).fetchone()
        assert abs(row["option_pl_pct"] - (-0.50)) < 1e-9

    def test_breakeven(self, migrated_conn):
        row_id = _record_call(migrated_conn, entry_premium=500.0, exit_premium=500.0)
        row = migrated_conn.execute(
            "SELECT option_pl_pct FROM option_outcomes WHERE id = ?", (row_id,)
        ).fetchone()
        assert abs(row["option_pl_pct"]) < 1e-9

    def test_zero_entry_premium_guard(self, migrated_conn):
        # entry_premium=0 must not raise; result must be 0.0
        row_id = _record_call(migrated_conn, entry_premium=0.0, exit_premium=100.0)
        row = migrated_conn.execute(
            "SELECT option_pl_pct FROM option_outcomes WHERE id = ?", (row_id,)
        ).fetchone()
        assert row["option_pl_pct"] == 0.0


# ---------------------------------------------------------------------------
# underlying_alpha_bps sign convention
# ---------------------------------------------------------------------------

class TestUnderlyingAlphaBps:
    def test_call_up_move_positive_alpha(self, migrated_conn):
        # CALL: underlying 100→110 (+10%), alpha = +10% × 10000 = +1000 bps
        row_id = _record_call(
            migrated_conn, underlying_open=100.0, underlying_close=110.0
        )
        row = migrated_conn.execute(
            "SELECT underlying_alpha_bps FROM option_outcomes WHERE id = ?", (row_id,)
        ).fetchone()
        assert abs(row["underlying_alpha_bps"] - 1000.0) < 1e-6

    def test_call_down_move_negative_alpha(self, migrated_conn):
        # CALL: underlying 100→90 (-10%), alpha = -10% × 10000 = -1000 bps
        row_id = _record_call(
            migrated_conn, underlying_open=100.0, underlying_close=90.0
        )
        row = migrated_conn.execute(
            "SELECT underlying_alpha_bps FROM option_outcomes WHERE id = ?", (row_id,)
        ).fetchone()
        assert abs(row["underlying_alpha_bps"] - (-1000.0)) < 1e-6

    def test_put_down_move_positive_alpha(self, migrated_conn):
        # PUT: underlying 100→90 (-10%), direction_mult=-1 → alpha = +1000 bps
        row_id = _record_put(
            migrated_conn, underlying_open=100.0, underlying_close=90.0
        )
        row = migrated_conn.execute(
            "SELECT underlying_alpha_bps FROM option_outcomes WHERE id = ?", (row_id,)
        ).fetchone()
        assert abs(row["underlying_alpha_bps"] - 1000.0) < 1e-6

    def test_put_up_move_negative_alpha(self, migrated_conn):
        # PUT: underlying 100→110 (+10%), direction_mult=-1 → alpha = -1000 bps
        row_id = _record_put(
            migrated_conn, underlying_open=100.0, underlying_close=110.0
        )
        row = migrated_conn.execute(
            "SELECT underlying_alpha_bps FROM option_outcomes WHERE id = ?", (row_id,)
        ).fetchone()
        assert abs(row["underlying_alpha_bps"] - (-1000.0)) < 1e-6

    def test_zero_underlying_open_guard(self, migrated_conn):
        # underlying_open_price=0 must not raise; alpha must be 0.0
        row_id = _record_call(
            migrated_conn, underlying_open=0.0, underlying_close=100.0
        )
        row = migrated_conn.execute(
            "SELECT underlying_alpha_bps FROM option_outcomes WHERE id = ?", (row_id,)
        ).fetchone()
        assert row["underlying_alpha_bps"] == 0.0


# ---------------------------------------------------------------------------
# Isolation: equity outcomes table must never be touched
# ---------------------------------------------------------------------------

class TestIsolation:
    def test_equity_outcomes_empty_after_call_record(self, migrated_conn):
        _record_call(migrated_conn)
        count = migrated_conn.execute(
            "SELECT COUNT(*) FROM outcomes"
        ).fetchone()[0]
        assert count == 0

    def test_equity_outcomes_empty_after_put_record(self, migrated_conn):
        _record_put(migrated_conn)
        count = migrated_conn.execute(
            "SELECT COUNT(*) FROM outcomes"
        ).fetchone()[0]
        assert count == 0

    def test_two_records_both_land_in_option_outcomes(self, migrated_conn):
        _record_call(migrated_conn)
        _record_put(migrated_conn)
        count = migrated_conn.execute(
            "SELECT COUNT(*) FROM option_outcomes"
        ).fetchone()[0]
        assert count == 2
        eq_count = migrated_conn.execute(
            "SELECT COUNT(*) FROM outcomes"
        ).fetchone()[0]
        assert eq_count == 0
