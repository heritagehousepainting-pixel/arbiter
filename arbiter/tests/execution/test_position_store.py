"""Tests for arbiter.execution.position_store — WP-B (Phase-2 persistence).

Covers:
  - migration 022 creates sim_positions + sim_account tables
  - SimExecutor.export_state / restore_state round-trip
  - snapshot_executor then load_account_state round-trip
  - seed_executor restores cash + positions into a fresh executor
  - seed_executor on an empty DB is a no-op
  - open_position_count is correct (only counts shares > 0)
  - snapshot is idempotent (re-snapshot replaces, does not duplicate)

All tests are OFFLINE and fast (in-memory / tmp_path SQLite, no network).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.execution.position_store import (
    load_account_state,
    open_position_count,
    seed_executor,
    snapshot_executor,
)
from arbiter.shared.executor import OrderIntent
from arbiter.shared.sim_executor import SimExecutor
from arbiter.types import OrderSide

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Migrated SQLite connection (fresh per test)."""
    db_path = str(tmp_path / "test_positions.db")
    c = get_connection(db_path)
    run_migrations(c)
    return c


def _ts(year: int = 2026, month: int = 6, day: int = 19) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def _buy(executor: SimExecutor, ticker: str, qty: float, price: float) -> None:
    executor.place(
        OrderIntent(
            order_id=f"ord-{ticker}-{qty}-{price}",
            ticker=ticker,
            side=OrderSide.BUY,
            qty=qty,
            limit_price=price,
        )
    )


# ---------------------------------------------------------------------------
# 1. Migration 022 creates the tables
# ---------------------------------------------------------------------------

class TestMigration022:
    def test_creates_sim_positions_table(self, conn):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sim_positions'"
        ).fetchone()
        assert row is not None

    def test_creates_sim_account_table(self, conn):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sim_account'"
        ).fetchone()
        assert row is not None

    def test_migration_022_applied(self, conn):
        applied = {
            r[0]
            for r in conn.execute("SELECT filename FROM schema_migrations").fetchall()
        }
        assert "022_positions.sql" in applied

    def test_sim_positions_columns(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sim_positions)").fetchall()}
        assert {"ticker", "shares", "avg_price", "updated_at"} <= cols

    def test_sim_account_columns(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sim_account)").fetchall()}
        assert {"id", "cash", "realized_pl", "updated_at"} <= cols


# ---------------------------------------------------------------------------
# 2. SimExecutor export_state / restore_state round-trip
# ---------------------------------------------------------------------------

class TestExportRestoreState:
    def test_export_shape(self):
        ex = SimExecutor(starting_cash=50_000.0)
        _buy(ex, "AAPL", 10, 100.0)
        state = ex.export_state()
        assert set(state.keys()) == {"cash", "realized_pl", "positions"}
        assert isinstance(state["positions"], list)
        assert state["positions"][0].keys() >= {"ticker", "shares", "avg_price"}

    def test_export_values(self):
        ex = SimExecutor(starting_cash=50_000.0)
        _buy(ex, "AAPL", 10, 100.0)  # cost 1000
        state = ex.export_state()
        assert state["cash"] == pytest.approx(49_000.0)
        assert state["realized_pl"] == pytest.approx(0.0)
        pos = {p["ticker"]: p for p in state["positions"]}
        assert pos["AAPL"]["shares"] == pytest.approx(10)
        assert pos["AAPL"]["avg_price"] == pytest.approx(100.0)

    def test_round_trip(self):
        ex = SimExecutor(starting_cash=100_000.0)
        _buy(ex, "AAPL", 10, 100.0)
        _buy(ex, "MSFT", 5, 200.0)
        # realize some P&L on a sell
        ex.place(
            OrderIntent(
                order_id="sell1", ticker="AAPL", side=OrderSide.SELL,
                qty=4, limit_price=120.0,
            )
        )
        state = ex.export_state()

        fresh = SimExecutor(starting_cash=1.0)
        fresh.restore_state(state)

        assert fresh.export_state() == state
        assert fresh.get_account().cash == pytest.approx(ex.get_account().cash)
        assert fresh.get_account().realized_pl == pytest.approx(ex.get_account().realized_pl)
        # positions restored and usable through the normal interface
        restored_pos = fresh.get_positions()
        assert set(restored_pos.keys()) == set(ex.get_positions().keys())

    def test_restore_replaces_prior_state(self):
        ex = SimExecutor(starting_cash=100_000.0)
        _buy(ex, "AAPL", 10, 100.0)
        ex.restore_state({"cash": 5.0, "realized_pl": 2.0, "positions": []})
        assert ex.get_account().cash == pytest.approx(5.0)
        assert ex.get_account().realized_pl == pytest.approx(2.0)
        assert ex.get_positions() == {}


# ---------------------------------------------------------------------------
# 3. snapshot_executor -> load_account_state round-trip
# ---------------------------------------------------------------------------

class TestSnapshotLoad:
    def test_load_empty_is_none(self, conn):
        assert load_account_state(conn) is None

    def test_snapshot_then_load(self, conn):
        ex = SimExecutor(starting_cash=100_000.0)
        _buy(ex, "AAPL", 10, 100.0)
        _buy(ex, "MSFT", 5, 200.0)
        snapshot_executor(conn, ex, as_of=_ts())

        loaded = load_account_state(conn)
        assert loaded is not None
        assert loaded["cash"] == pytest.approx(ex.export_state()["cash"])
        assert loaded["realized_pl"] == pytest.approx(ex.export_state()["realized_pl"])
        loaded_pos = {p["ticker"]: p for p in loaded["positions"]}
        assert set(loaded_pos) == {"AAPL", "MSFT"}
        assert loaded_pos["AAPL"]["shares"] == pytest.approx(10)
        assert loaded_pos["AAPL"]["avg_price"] == pytest.approx(100.0)

    def test_loaded_state_feeds_restore(self, conn):
        ex = SimExecutor(starting_cash=100_000.0)
        _buy(ex, "TSLA", 3, 250.0)
        snapshot_executor(conn, ex, as_of=_ts())

        loaded = load_account_state(conn)
        fresh = SimExecutor(starting_cash=1.0)
        fresh.restore_state(loaded)
        assert fresh.export_state() == ex.export_state()


# ---------------------------------------------------------------------------
# 4. snapshot idempotency (replace, not duplicate)
# ---------------------------------------------------------------------------

class TestSnapshotIdempotent:
    def test_re_snapshot_replaces_positions(self, conn):
        ex = SimExecutor(starting_cash=100_000.0)
        _buy(ex, "AAPL", 10, 100.0)
        _buy(ex, "MSFT", 5, 200.0)
        snapshot_executor(conn, ex, as_of=_ts())

        # mutate: sell all MSFT, buy GOOG, then re-snapshot
        ex.place(
            OrderIntent(
                order_id="sell-msft", ticker="MSFT", side=OrderSide.SELL,
                qty=5, limit_price=210.0,
            )
        )
        _buy(ex, "GOOG", 2, 300.0)
        snapshot_executor(conn, ex, as_of=_ts(day=20))

        rows = conn.execute("SELECT ticker FROM sim_positions").fetchall()
        tickers = {r["ticker"] for r in rows}
        assert tickers == {"AAPL", "GOOG"}  # MSFT gone, no dupes

    def test_re_snapshot_keeps_single_account_row(self, conn):
        ex = SimExecutor(starting_cash=100_000.0)
        _buy(ex, "AAPL", 1, 100.0)
        snapshot_executor(conn, ex, as_of=_ts())
        snapshot_executor(conn, ex, as_of=_ts(day=20))
        count = conn.execute("SELECT count(*) FROM sim_account").fetchone()[0]
        assert count == 1
        row = conn.execute("SELECT id FROM sim_account").fetchone()
        assert row["id"] == 1

    def test_snapshot_within_open_transaction(self, conn):
        """snapshot_executor must succeed when a parent transaction is already
        open (proves the SAVEPOINT-based wrapper, not a bare ``BEGIN``).

        A bare ``conn.execute("BEGIN")`` would raise OperationalError
        ("cannot start a transaction within a transaction") here, the engine's
        broad except would swallow it, and the snapshot would be silently
        skipped -> position continuity lost.
        """
        ex = SimExecutor(starting_cash=100_000.0)
        _buy(ex, "AAPL", 10, 100.0)
        _buy(ex, "MSFT", 5, 200.0)

        # Open an implicit transaction with an uncommitted write on another table.
        conn.execute(
            "INSERT INTO sim_account (id, cash, realized_pl, updated_at) "
            "VALUES (1, ?, ?, ?)",
            (12345.0, 0.0, _ts().isoformat()),
        )
        assert conn.in_transaction  # a transaction is genuinely open

        # Must not raise even though a transaction is already open.
        snapshot_executor(conn, ex, as_of=_ts())

        # And it must have persisted correctly (positions + account overwritten).
        loaded = load_account_state(conn)
        assert loaded is not None
        assert loaded["cash"] == pytest.approx(ex.export_state()["cash"])
        loaded_pos = {p["ticker"]: p for p in loaded["positions"]}
        assert set(loaded_pos) == {"AAPL", "MSFT"}


# ---------------------------------------------------------------------------
# 5. open_position_count
# ---------------------------------------------------------------------------

class TestOpenPositionCount:
    def test_empty(self, conn):
        assert open_position_count(conn) == 0

    def test_counts_positive_shares(self, conn):
        ex = SimExecutor(starting_cash=100_000.0)
        _buy(ex, "AAPL", 10, 100.0)
        _buy(ex, "MSFT", 5, 200.0)
        snapshot_executor(conn, ex, as_of=_ts())
        assert open_position_count(conn) == 2

    def test_ignores_zero_share_rows(self, conn):
        # Directly insert a zero-share row to prove the WHERE shares>0 guard.
        conn.execute(
            "INSERT INTO sim_positions (ticker, shares, avg_price, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("ZERO", 0.0, 0.0, _ts().isoformat()),
        )
        conn.execute(
            "INSERT INTO sim_positions (ticker, shares, avg_price, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("REAL", 5.0, 10.0, _ts().isoformat()),
        )
        conn.commit()
        assert open_position_count(conn) == 1


# ---------------------------------------------------------------------------
# 6. seed_executor
# ---------------------------------------------------------------------------

class TestSeedExecutor:
    def test_seed_restores_cash_and_positions(self, conn):
        src = SimExecutor(starting_cash=100_000.0)
        _buy(src, "AAPL", 10, 100.0)
        _buy(src, "MSFT", 5, 200.0)
        snapshot_executor(conn, src, as_of=_ts())

        fresh = SimExecutor(starting_cash=100_000.0)
        seed_executor(conn, fresh)

        assert fresh.export_state() == src.export_state()
        assert fresh.get_account().cash == pytest.approx(src.get_account().cash)
        assert set(fresh.get_positions()) == {"AAPL", "MSFT"}

    def test_seed_empty_db_is_noop(self, conn):
        fresh = SimExecutor(starting_cash=100_000.0)
        before = fresh.export_state()
        seed_executor(conn, fresh)  # nothing snapshotted yet
        assert fresh.export_state() == before
        assert fresh.get_account().cash == pytest.approx(100_000.0)
        assert fresh.get_positions() == {}

    def test_export_state_excludes_zero_share_position(self):
        """export_state must filter out lingering 0-share positions so a
        restored executor matches get_positions / open_position_count.
        """
        ex = SimExecutor(starting_cash=100_000.0)
        # Inject a 0-share position alongside a real one via restore_state.
        ex.restore_state(
            {
                "cash": 50_000.0,
                "realized_pl": 0.0,
                "positions": [
                    {"ticker": "ZERO", "shares": 0.0, "avg_price": 0.0},
                    {"ticker": "REAL", "shares": 5.0, "avg_price": 10.0},
                ],
            }
        )
        state = ex.export_state()
        tickers = {p["ticker"] for p in state["positions"]}
        assert tickers == {"REAL"}  # ZERO excluded, matches get_positions

    def test_seed_preserves_realized_pl(self, conn):
        src = SimExecutor(starting_cash=100_000.0)
        _buy(src, "AAPL", 10, 100.0)
        src.place(
            OrderIntent(
                order_id="sell-a", ticker="AAPL", side=OrderSide.SELL,
                qty=4, limit_price=130.0,
            )
        )
        snapshot_executor(conn, src, as_of=_ts())

        fresh = SimExecutor(starting_cash=1.0)
        seed_executor(conn, fresh)
        assert fresh.get_account().realized_pl == pytest.approx(
            src.get_account().realized_pl
        )
