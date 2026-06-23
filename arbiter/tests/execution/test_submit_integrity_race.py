"""IntegrityError dedup-race test for ``submit_order``.

W-TESTHARDEN seam #3.  ``submit_order`` does a TOCTOU dance: it calls
``ensure_not_duplicate`` (the check), then ``executor.place`` (a slow broker
round-trip), then INSERTs the order row guarded by ``UNIQUE(dedup_hash)``.

If a CONCURRENT writer inserts the same ``dedup_hash`` during the broker
round-trip, our INSERT raises ``sqlite3.IntegrityError``.  The code at
``submit.py`` lines ~351-366 must catch that and return a clean
``DUPLICATE_SKIP`` (``duplicate=True``) result — NOT crash and NOT double-place.

The existing ``test_submit.py::test_dedup_hash_unique_constraint_in_db`` only
exercises the *pre-insert* ``ensure_not_duplicate`` branch (the row is visible
at check time).  This file exercises the genuine *insert-time* race branch: the
row becomes visible only AFTER the duplicate check passed, by inserting it from
inside a wrapped ``place()`` (which runs between the check and the INSERT).

OFFLINE: in-memory SQLite, no network.
"""
from __future__ import annotations

import sqlite3

from arbiter.execution.idempotency import dedup_hash
from arbiter.execution.submit import _SKIP_SENTINEL, submit_order
from arbiter.shared.executor import ExecutionReport
from arbiter.shared.sim_executor import SimExecutor
from arbiter.types import OrderSide

from tests.execution.conftest import make_paper_order


def _insert_colliding_row(conn: sqlite3.Connection, dh: str) -> None:
    """Simulate a concurrent writer inserting the same dedup_hash."""
    conn.execute(
        """
        INSERT INTO orders
            (order_id, dedup_hash, ticker, side, qty, horizon_bucket,
             entry_date, advisor_signature, exits_json, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "concurrent-writer-id",
            dh,
            "AAPL",
            OrderSide.BUY.value,
            5.0,
            "short",
            "2024-01-15",
            "A1.insider",
            "{}",
            "filled",
            "2024-01-15T12:00:00+00:00",
        ),
    )
    conn.commit()


class _RacingExecutor:
    """Executor whose ``place`` injects a colliding row mid-flight.

    This reproduces the real race: the duplicate check already passed, then the
    broker round-trip (``place``) takes time, during which another process
    commits the same ``dedup_hash``.  Our subsequent INSERT then collides.
    """

    name = "racing"

    def __init__(self, conn: sqlite3.Connection, dh: str) -> None:
        self._conn = conn
        self._dh = dh
        self.place_calls = 0

    def place(self, intent) -> ExecutionReport:
        self.place_calls += 1
        # The concurrent writer commits the duplicate during our broker call.
        _insert_colliding_row(self._conn, self._dh)
        return ExecutionReport(
            order_id=intent.order_id,
            ticker=intent.ticker,
            side=intent.side,
            status="filled",
            filled_qty=intent.qty,
            avg_fill_price=intent.limit_price,
            gross_notional=intent.qty * (intent.limit_price or 0.0),
            realized_pl=None,
            reject_reason="",
            executor=self.name,
            paper_only=True,
        )

    def get_positions(self):
        return {}


class TestIntegrityRace:
    def test_insert_time_collision_returns_duplicate_skip(
        self, mem_conn, fixed_clock, tmp_audit
    ):
        """A UNIQUE(dedup_hash) collision at INSERT time → DUPLICATE_SKIP, no crash."""
        order = make_paper_order(qty=5_000.0)
        dh = dedup_hash(order)
        executor = _RacingExecutor(mem_conn, dh)

        result = submit_order(
            order,
            executor,
            fixed_clock,
            conn=mem_conn,
            spread=0.05,
            raw_price=100.0,
            audit_path=str(tmp_audit),
        )

        # The check passed (so place WAS called), but the insert raced.
        assert executor.place_calls == 1, "duplicate check passed → place must run"
        assert result.status == _SKIP_SENTINEL
        assert result.duplicate is True
        assert result.order_id is None

    def test_race_does_not_persist_our_order_id(
        self, mem_conn, fixed_clock, tmp_audit
    ):
        """After the race, only the concurrent writer's row exists for the hash."""
        order = make_paper_order(qty=5_000.0)
        dh = dedup_hash(order)
        executor = _RacingExecutor(mem_conn, dh)

        submit_order(
            order,
            executor,
            fixed_clock,
            conn=mem_conn,
            spread=0.05,
            raw_price=100.0,
            audit_path=str(tmp_audit),
        )

        rows = mem_conn.execute(
            "SELECT order_id FROM orders WHERE dedup_hash = ?", (dh,)
        ).fetchall()
        assert len(rows) == 1, "UNIQUE(dedup_hash) must hold exactly one row"
        assert rows[0]["order_id"] == "concurrent-writer-id"
        # Our order_id must NOT have been persisted.
        ours = mem_conn.execute(
            "SELECT 1 FROM orders WHERE order_id = ?", (order.order_id,)
        ).fetchone()
        assert ours is None

    def test_race_writes_race_skip_audit(
        self, mem_conn, fixed_clock, tmp_path
    ):
        """The race path emits an ``order.race_skip`` audit event."""
        import json

        audit_path = str(tmp_path / "audit.jsonl")
        order = make_paper_order(qty=5_000.0)
        dh = dedup_hash(order)
        executor = _RacingExecutor(mem_conn, dh)

        submit_order(
            order,
            executor,
            fixed_clock,
            conn=mem_conn,
            spread=0.05,
            raw_price=100.0,
            audit_path=audit_path,
        )

        events = [
            json.loads(line)["event"]
            for line in open(audit_path).readlines()
            if line.strip()
        ]
        assert "order.race_skip" in events
        # The successful-submit event must NOT have been written.
        assert "order.submitted" not in events
