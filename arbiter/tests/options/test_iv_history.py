"""Tests for iv_history module.

All HTTP is mocked — no real network calls.
Uses in-memory SQLite with the option_iv_history schema.
"""
from __future__ import annotations

import datetime
import sqlite3
from unittest.mock import MagicMock

import pytest

from arbiter.options import iv_history as ivh


# ---------------------------------------------------------------------------
# DB fixture (in-memory SQLite with option_iv_history schema)
# ---------------------------------------------------------------------------

_IV_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS option_iv_history (
    id          TEXT PRIMARY KEY,
    underlying  TEXT NOT NULL,
    as_of       TEXT NOT NULL,
    atm_iv      REAL NOT NULL,
    occ_symbol  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(_IV_HISTORY_DDL)
    conn.commit()
    return conn


def _insert_iv_row(
    conn: sqlite3.Connection,
    underlying: str,
    as_of: str,
    atm_iv: float,
    occ_symbol: str = "AAPL240119C00150000",
) -> None:
    from arbiter.db.helpers import generate_ulid

    now = "2026-06-25T12:00:00+00:00"
    conn.execute(
        "INSERT INTO option_iv_history (id, underlying, as_of, atm_iv, occ_symbol, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (generate_ulid(), underlying, as_of, atm_iv, occ_symbol, now),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# iv_rank()
# ---------------------------------------------------------------------------

class TestIVRank:
    def test_returns_none_when_no_history(self):
        conn = _make_conn()
        result = ivh.iv_rank(conn, "AAPL")
        assert result is None

    def test_returns_none_below_min_history_days(self):
        conn = _make_conn()
        # Insert 5 rows (< default min_history_days=30).
        for i in range(5):
            _insert_iv_row(conn, "AAPL", f"2026-01-{str(i+1).zfill(2)}T12:00:00+00:00", 0.30)
        result = ivh.iv_rank(conn, "AAPL")
        assert result is None

    def test_returns_rank_when_sufficient_history(self):
        conn = _make_conn()
        # Insert 35 rows with IV ranging from 0.20 to 0.54.
        base_date = datetime.date(2025, 7, 1)
        for i in range(35):
            date = base_date + datetime.timedelta(days=i)
            iv = 0.20 + 0.01 * i  # 0.20 to 0.54
            _insert_iv_row(conn, "AAPL", f"{date.isoformat()}T12:00:00+00:00", iv)

        as_of = base_date + datetime.timedelta(days=34)
        result = ivh.iv_rank(conn, "AAPL", as_of=as_of)
        # current_iv = 0.54, min = 0.20, max = 0.54 → rank = 1.0
        assert result is not None
        assert result == pytest.approx(1.0, abs=0.01)

    def test_rank_at_minimum_iv_is_zero(self):
        conn = _make_conn()
        base_date = datetime.date(2025, 7, 1)
        ivs = [0.50] * 34 + [0.20]  # newest (index 34) inserted last → as_of is its date
        for i, iv in enumerate(ivs):
            date = base_date + datetime.timedelta(days=i)
            _insert_iv_row(conn, "AAPL", f"{date.isoformat()}T12:00:00+00:00", iv)

        as_of = base_date + datetime.timedelta(days=34)
        result = ivh.iv_rank(conn, "AAPL", as_of=as_of)
        # Newest row is the one with IV=0.20 (minimum). Rank should be 0.
        assert result is not None
        assert result == pytest.approx(0.0, abs=0.01)

    def test_rank_clamps_to_unit_interval(self):
        conn = _make_conn()
        base_date = datetime.date(2025, 7, 1)
        for i in range(35):
            date = base_date + datetime.timedelta(days=i)
            _insert_iv_row(conn, "AAPL", f"{date.isoformat()}T12:00:00+00:00", 0.30)

        # All IVs identical → returns 0.5 (neutral by convention).
        as_of = base_date + datetime.timedelta(days=34)
        result = ivh.iv_rank(conn, "AAPL", as_of=as_of)
        assert result == pytest.approx(0.5)

    def test_rank_obeys_lookback_window(self):
        conn = _make_conn()
        # Insert 35 rows far in the past (> 252 days ago).
        base_date = datetime.date(2024, 1, 1)
        for i in range(35):
            date = base_date + datetime.timedelta(days=i)
            _insert_iv_row(conn, "AAPL", f"{date.isoformat()}T12:00:00+00:00", 0.30)

        # as_of far in the future — all rows fall outside lookback.
        result = ivh.iv_rank(conn, "AAPL", as_of=datetime.date(2025, 12, 31))
        assert result is None

    def test_rank_uses_custom_min_history_days(self):
        conn = _make_conn()
        # Insert exactly 10 rows — below default 30 but above custom 5.
        base_date = datetime.date(2025, 7, 1)
        for i in range(10):
            date = base_date + datetime.timedelta(days=i)
            _insert_iv_row(conn, "AAPL", f"{date.isoformat()}T12:00:00+00:00", 0.30)

        as_of = base_date + datetime.timedelta(days=9)
        result = ivh.iv_rank(conn, "AAPL", as_of=as_of, min_history_days=5)
        assert result is not None

    def test_rank_none_safety_on_bad_table(self):
        """iv_rank returns None gracefully when the table doesn't exist."""
        conn = sqlite3.connect(":memory:")  # no DDL applied
        result = ivh.iv_rank(conn, "AAPL")
        assert result is None


# ---------------------------------------------------------------------------
# realized_vol_proxy()
# ---------------------------------------------------------------------------

class TestRealizedVolProxy:
    def test_returns_none_when_no_data(self):
        conn = _make_conn()
        result = ivh.realized_vol_proxy(conn, "AAPL")
        assert result is None

    def test_returns_none_on_insufficient_data(self):
        conn = _make_conn()
        # Insert 3 IV rows — below _MIN_BARS_FOR_RVOL (5).
        base_date = datetime.date(2026, 5, 1)
        for i in range(3):
            date = base_date + datetime.timedelta(days=i)
            _insert_iv_row(conn, "AAPL", f"{date.isoformat()}T12:00:00+00:00", 0.30)
        result = ivh.realized_vol_proxy(conn, "AAPL", as_of=base_date + datetime.timedelta(days=5))
        assert result is None

    def test_returns_float_on_sufficient_data(self):
        conn = _make_conn()
        base_date = datetime.date(2026, 5, 1)
        # Insert 20 rows with a random-walk IV to give non-trivial stdev.
        ivs = [0.30, 0.32, 0.31, 0.29, 0.33, 0.35, 0.34, 0.36, 0.38, 0.37,
               0.36, 0.34, 0.32, 0.31, 0.30, 0.29, 0.28, 0.27, 0.29, 0.31]
        for i, iv in enumerate(ivs):
            date = base_date + datetime.timedelta(days=i)
            _insert_iv_row(conn, "AAPL", f"{date.isoformat()}T12:00:00+00:00", iv)

        result = ivh.realized_vol_proxy(
            conn, "AAPL",
            as_of=base_date + datetime.timedelta(days=len(ivs)),
        )
        assert result is not None
        assert isinstance(result, float)
        assert result > 0.0

    def test_returns_none_on_empty_table(self):
        conn = _make_conn()
        result = ivh.realized_vol_proxy(conn, "AAPL")
        assert result is None

    def test_returns_none_gracefully_on_bad_table(self):
        """realized_vol_proxy never raises — returns None when tables absent."""
        conn = sqlite3.connect(":memory:")  # no DDL
        result = ivh.realized_vol_proxy(conn, "AAPL")
        assert result is None

    def test_result_is_annualised(self):
        """A known constant series (zero variance) produces 0.0 vol, not 0.38."""
        conn = _make_conn()
        base_date = datetime.date(2026, 5, 1)
        # Constant IV series → zero variance → 0.0 rvol.
        for i in range(20):
            date = base_date + datetime.timedelta(days=i)
            _insert_iv_row(conn, "AAPL", f"{date.isoformat()}T12:00:00+00:00", 0.30)

        result = ivh.realized_vol_proxy(
            conn, "AAPL",
            as_of=base_date + datetime.timedelta(days=20),
        )
        # Constant values → all log returns = log(1) = 0 → stdev = 0.
        assert result == pytest.approx(0.0, abs=1e-10)


# ---------------------------------------------------------------------------
# record_iv_snapshot()
# ---------------------------------------------------------------------------

class TestRecordIVSnapshot:
    def _make_mock_client(self, contracts=None, snapshot_data=None):
        """Build a mock AlpacaOptionsClient."""
        from arbiter.options.types import OptionContract, OptionSide

        mock_client = MagicMock()

        if contracts is None:
            expiry = datetime.date(2026, 12, 19)
            contracts = [
                OptionContract(
                    occ_symbol="AAPL261219C00180000",
                    underlying="AAPL",
                    side=OptionSide.CALL,
                    strike=180.0,
                    expiry=expiry,
                    delta=0.52,
                    iv=0.34,
                    bid=5.0,
                    ask=5.5,
                    open_interest=1000,
                    volume=100,
                )
            ]

        mock_client.fetch_chain.return_value = contracts
        return mock_client

    def test_returns_ulid_on_success(self):
        conn = _make_conn()
        mock_client = self._make_mock_client()

        row_id = ivh.record_iv_snapshot(
            conn, mock_client, "AAPL", "2026-06-25T12:00:00+00:00"
        )
        assert row_id is not None
        assert isinstance(row_id, str)
        assert len(row_id) > 0

    def test_persists_iv_row_to_db(self):
        conn = _make_conn()
        mock_client = self._make_mock_client()

        ivh.record_iv_snapshot(conn, mock_client, "AAPL", "2026-06-25T12:00:00+00:00")

        rows = conn.execute(
            "SELECT underlying, atm_iv, occ_symbol FROM option_iv_history WHERE underlying = 'AAPL'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "AAPL"
        assert rows[0][1] == pytest.approx(0.34)
        assert rows[0][2] == "AAPL261219C00180000"

    def test_returns_none_when_no_contracts(self):
        conn = _make_conn()
        mock_client = self._make_mock_client(contracts=[])

        row_id = ivh.record_iv_snapshot(
            conn, mock_client, "AAPL", "2026-06-25T12:00:00+00:00"
        )
        assert row_id is None

    def test_returns_none_when_all_iv_null(self):
        from arbiter.options.types import OptionContract, OptionSide

        conn = _make_conn()
        # Contract with iv=None.
        contracts = [
            OptionContract(
                occ_symbol="AAPL261219C00180000",
                underlying="AAPL",
                side=OptionSide.CALL,
                strike=180.0,
                expiry=datetime.date(2026, 12, 19),
                delta=None,
                iv=None,
                bid=None,
                ask=None,
                open_interest=None,
                volume=None,
            )
        ]
        mock_client = self._make_mock_client(contracts=contracts)

        row_id = ivh.record_iv_snapshot(
            conn, mock_client, "AAPL", "2026-06-25T12:00:00+00:00"
        )
        assert row_id is None

    def test_returns_none_on_fetch_chain_exception(self):
        conn = _make_conn()
        mock_client = MagicMock()
        mock_client.fetch_chain.side_effect = RuntimeError("network error")

        row_id = ivh.record_iv_snapshot(
            conn, mock_client, "AAPL", "2026-06-25T12:00:00+00:00"
        )
        assert row_id is None

    def test_selects_nearest_atm_contract(self):
        """When multiple contracts have valid IV, the one nearest ATM (delta≈0.5) is selected."""
        from arbiter.options.types import OptionContract, OptionSide

        conn = _make_conn()
        expiry = datetime.date(2026, 12, 19)

        contracts = [
            OptionContract(
                occ_symbol="AAPL261219C00150000",
                underlying="AAPL",
                side=OptionSide.CALL,
                strike=150.0,
                expiry=expiry,
                delta=0.80,  # deep ITM
                iv=0.25,
                bid=30.0,
                ask=31.0,
                open_interest=500,
                volume=50,
            ),
            OptionContract(
                occ_symbol="AAPL261219C00180000",
                underlying="AAPL",
                side=OptionSide.CALL,
                strike=180.0,
                expiry=expiry,
                delta=0.51,  # nearest ATM
                iv=0.34,
                bid=5.0,
                ask=5.5,
                open_interest=1000,
                volume=100,
            ),
            OptionContract(
                occ_symbol="AAPL261219C00200000",
                underlying="AAPL",
                side=OptionSide.CALL,
                strike=200.0,
                expiry=expiry,
                delta=0.20,  # OTM
                iv=0.42,
                bid=1.0,
                ask=1.5,
                open_interest=200,
                volume=20,
            ),
        ]
        mock_client = self._make_mock_client(contracts=contracts)

        ivh.record_iv_snapshot(conn, mock_client, "AAPL", "2026-06-25T12:00:00+00:00")

        rows = conn.execute(
            "SELECT atm_iv, occ_symbol FROM option_iv_history WHERE underlying = 'AAPL'"
        ).fetchall()
        assert len(rows) == 1
        # delta 0.51 is closest to 0.5 → should pick AAPL261219C00180000 with iv=0.34
        assert rows[0][1] == "AAPL261219C00180000"
        assert rows[0][0] == pytest.approx(0.34)

    def test_returns_none_on_bad_as_of(self):
        conn = _make_conn()
        mock_client = self._make_mock_client()

        row_id = ivh.record_iv_snapshot(
            conn, mock_client, "AAPL", "not-a-date"
        )
        assert row_id is None
