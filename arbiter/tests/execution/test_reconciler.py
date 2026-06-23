"""Tests for arbiter.execution.reconciler."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from arbiter.execution.reconciler import (
    Divergence,
    ReconcileResult,
    reconcile,
    _local_positions,
)
from arbiter.shared.executor import OrderIntent, PositionSnapshot
from arbiter.shared.sim_executor import SimExecutor
from arbiter.types import OrderSide

from tests.execution.conftest import make_paper_order


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AS_OF = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _fill_sim(executor: SimExecutor, ticker: str, qty: float, price: float) -> None:
    """Plant a position in a SimExecutor by placing a buy order."""
    executor.place(OrderIntent(
        order_id="seed-" + ticker,
        ticker=ticker,
        side=OrderSide.BUY,
        qty=qty,
        limit_price=price,
    ))


def _insert_filled_order(conn, ticker: str, qty: float, side: str = "BUY") -> None:
    """Insert a filled order row directly into the local ledger."""
    import hashlib
    from arbiter.db.helpers import generate_ulid
    dh = hashlib.sha256(f"{ticker}{side}".encode()).hexdigest()
    conn.execute(
        """
        INSERT OR IGNORE INTO orders
            (order_id, dedup_hash, ticker, side, qty, horizon_bucket,
             entry_date, advisor_signature, exits_json, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            generate_ulid(),
            dh,
            ticker,
            side,
            qty,
            "SHORT",
            "2024-01-15",
            "A1.test",
            "{}",
            "filled",
            "2024-01-15T12:00:00+00:00",
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Test _local_positions
# ---------------------------------------------------------------------------

class TestLocalPositions:
    def test_empty_returns_empty(self, mem_conn):
        assert _local_positions(mem_conn) == {}

    def test_single_buy(self, mem_conn):
        _insert_filled_order(mem_conn, "AAPL", 10.0, "BUY")
        positions = _local_positions(mem_conn)
        assert positions["AAPL"] == pytest.approx(10.0)

    def test_buy_minus_sell(self, mem_conn):
        _insert_filled_order(mem_conn, "AAPL", 10.0, "BUY")
        # Insert sell with different hash
        import hashlib
        from arbiter.db.helpers import generate_ulid
        dh2 = hashlib.sha256(b"AAPLSELL2").hexdigest()
        mem_conn.execute(
            """
            INSERT INTO orders
                (order_id, dedup_hash, ticker, side, qty, horizon_bucket,
                 entry_date, advisor_signature, exits_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generate_ulid(), dh2, "AAPL", "SELL", 4.0,
                "SHORT", "2024-01-16", "A1.test", "{}", "filled",
                "2024-01-16T12:00:00+00:00",
            ),
        )
        mem_conn.commit()
        positions = _local_positions(mem_conn)
        assert positions["AAPL"] == pytest.approx(6.0)

    def test_fully_closed_not_in_positions(self, mem_conn):
        """A ticker with net 0 shares does not appear in positions."""
        import hashlib
        from arbiter.db.helpers import generate_ulid
        for i, (side, qty) in enumerate([("BUY", 10.0), ("SELL", 10.0)]):
            dh = hashlib.sha256(f"AAPL{side}{i}".encode()).hexdigest()
            mem_conn.execute(
                """
                INSERT INTO orders
                    (order_id, dedup_hash, ticker, side, qty, horizon_bucket,
                     entry_date, advisor_signature, exits_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generate_ulid(), dh, "AAPL", side, qty,
                    "SHORT", "2024-01-15", "A1.test", "{}", "filled",
                    "2024-01-15T12:00:00+00:00",
                ),
            )
        mem_conn.commit()
        positions = _local_positions(mem_conn)
        assert "AAPL" not in positions


# ---------------------------------------------------------------------------
# Test reconcile() — clean case
# ---------------------------------------------------------------------------

class TestReconcileClean:
    def test_both_empty_is_clean(self, mem_conn, tmp_audit):
        """No local positions + no broker positions → clean."""
        executor = SimExecutor()
        result = reconcile(conn=mem_conn, executor=executor, as_of=AS_OF, audit_path=str(tmp_audit))
        assert result.clean
        assert result.divergences == []

    def test_matching_positions_is_clean(self, mem_conn, tmp_audit):
        """Same ticker + same qty in both local and broker → clean."""
        executor = SimExecutor(starting_cash=1_000_000.0)
        _fill_sim(executor, "AAPL", 10.0, 150.0)
        _insert_filled_order(mem_conn, "AAPL", 10.0, "BUY")

        result = reconcile(conn=mem_conn, executor=executor, as_of=AS_OF, audit_path=str(tmp_audit))
        assert result.clean


# ---------------------------------------------------------------------------
# Test reconcile() — divergence detection
# ---------------------------------------------------------------------------

class TestReconcileDivergences:
    def test_local_only_detected(self, mem_conn, tmp_audit):
        """Ticker in local ledger but not at broker → LOCAL_ONLY divergence."""
        executor = SimExecutor()
        _insert_filled_order(mem_conn, "AAPL", 10.0, "BUY")

        result = reconcile(conn=mem_conn, executor=executor, as_of=AS_OF, audit_path=str(tmp_audit))
        assert not result.clean
        kinds = {d.kind for d in result.divergences}
        assert "LOCAL_ONLY" in kinds
        tickers = {d.ticker for d in result.divergences}
        assert "AAPL" in tickers

    def test_broker_only_detected(self, mem_conn, tmp_audit):
        """Ticker at broker but not in local ledger → BROKER_ONLY divergence."""
        executor = SimExecutor(starting_cash=1_000_000.0)
        _fill_sim(executor, "MSFT", 5.0, 300.0)
        # No local order for MSFT

        result = reconcile(conn=mem_conn, executor=executor, as_of=AS_OF, audit_path=str(tmp_audit))
        assert not result.clean
        kinds = {d.kind for d in result.divergences}
        assert "BROKER_ONLY" in kinds

    def test_qty_mismatch_detected(self, mem_conn, tmp_audit):
        """Same ticker but different qty → QTY_MISMATCH divergence."""
        executor = SimExecutor(starting_cash=1_000_000.0)
        _fill_sim(executor, "AAPL", 15.0, 150.0)   # broker: 15 shares
        _insert_filled_order(mem_conn, "AAPL", 10.0, "BUY")  # local: 10 shares

        result = reconcile(conn=mem_conn, executor=executor, as_of=AS_OF, audit_path=str(tmp_audit))
        assert not result.clean
        kinds = {d.kind for d in result.divergences}
        assert "QTY_MISMATCH" in kinds

    def test_planted_divergence_detected(self, mem_conn, tmp_audit):
        """Plant a divergence (AAPL local only) and verify it's caught."""
        executor = SimExecutor(starting_cash=1_000_000.0)
        # Local has AAPL, broker has MSFT — two divergences
        _insert_filled_order(mem_conn, "AAPL", 10.0, "BUY")
        _fill_sim(executor, "MSFT", 5.0, 300.0)

        result = reconcile(conn=mem_conn, executor=executor, as_of=AS_OF, audit_path=str(tmp_audit))
        assert not result.clean
        assert len(result.divergences) == 2
        kinds = {d.kind for d in result.divergences}
        assert "LOCAL_ONLY" in kinds
        assert "BROKER_ONLY" in kinds

    def test_result_metadata(self, mem_conn, tmp_audit):
        """ReconcileResult contains correct metadata."""
        executor = SimExecutor(starting_cash=1_000_000.0)
        _fill_sim(executor, "AAPL", 10.0, 150.0)
        _insert_filled_order(mem_conn, "AAPL", 10.0, "BUY")

        result = reconcile(conn=mem_conn, executor=executor, as_of=AS_OF, audit_path=str(tmp_audit))
        assert result.as_of == AS_OF
        assert "AAPL" in result.local_tickers
        assert "AAPL" in result.broker_tickers

    def test_audit_written(self, mem_conn, tmp_path):
        """reconcile() writes an audit entry."""
        import json
        audit_path = str(tmp_path / "audit.jsonl")
        executor = SimExecutor()

        reconcile(conn=mem_conn, executor=executor, as_of=AS_OF, audit_path=audit_path)

        lines = open(audit_path).readlines()
        events = [json.loads(l)["event"] for l in lines if l.strip()]
        assert "reconciler.pass" in events
