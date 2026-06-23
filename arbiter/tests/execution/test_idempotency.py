"""Tests for arbiter.execution.idempotency."""
from __future__ import annotations

import hashlib
from datetime import date

import pytest

from arbiter.execution.idempotency import (
    DuplicateOrderError,
    dedup_hash,
    ensure_not_duplicate,
)
from arbiter.types import HorizonBucket, OrderSide

from tests.execution.conftest import make_paper_order


# ---------------------------------------------------------------------------
# dedup_hash determinism
# ---------------------------------------------------------------------------

class TestDedupHash:
    """dedup_hash must be deterministic and match the expected formula."""

    def test_hash_deterministic(self, paper_order):
        """Same order always produces the same hash."""
        h1 = dedup_hash(paper_order)
        h2 = dedup_hash(paper_order)
        assert h1 == h2

    def test_hash_matches_formula(self, paper_order):
        """sha256(ticker|side|horizon|entry_date|advisor_signature)."""
        raw = "|".join([
            paper_order.ticker,
            paper_order.side.value,
            paper_order.horizon_bucket.value,
            str(paper_order.entry_date),
            paper_order.advisor_signature,
        ])
        expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        assert dedup_hash(paper_order) == expected

    def test_hash_differs_on_ticker(self, paper_order):
        """Changing the ticker changes the hash."""
        other = make_paper_order(ticker="MSFT")
        assert dedup_hash(paper_order) != dedup_hash(other)

    def test_hash_differs_on_side(self, paper_order):
        """Changing the side changes the hash."""
        sell = make_paper_order(side=OrderSide.SELL)
        assert dedup_hash(paper_order) != dedup_hash(sell)

    def test_hash_differs_on_horizon(self, paper_order):
        """Changing the horizon bucket changes the hash."""
        long_order = make_paper_order(horizon=HorizonBucket.LONG)
        assert dedup_hash(paper_order) != dedup_hash(long_order)

    def test_hash_differs_on_entry_date(self, paper_order):
        """Changing the entry date changes the hash."""
        later = make_paper_order(entry_date=date(2024, 2, 1))
        assert dedup_hash(paper_order) != dedup_hash(later)

    def test_hash_differs_on_advisor_sig(self, paper_order):
        """Changing the advisor signature changes the hash."""
        other_sig = make_paper_order(advisor_sig="A2.mirofish")
        assert dedup_hash(paper_order) != dedup_hash(other_sig)

    def test_hash_is_64_char_hex(self, paper_order):
        """sha256 hex digest is always 64 lowercase hex characters."""
        h = dedup_hash(paper_order)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_stable_across_entry_date_types(self):
        """D1 P3: date / datetime / str entry_date all hash identically.

        entry_date is normalized to a canonical ISO date string, so a
        datetime's time component must NOT leak into the digest and a
        pre-formatted ISO string must round-trip to the same hash as a date.
        """
        from datetime import datetime

        as_date = make_paper_order(entry_date=date(2024, 1, 15))
        # datetime with a non-midnight time component on the same calendar day
        as_datetime = make_paper_order(entry_date=datetime(2024, 1, 15, 9, 30, 0))
        as_iso_str = make_paper_order(entry_date="2024-01-15")
        as_iso_datetime_str = make_paper_order(entry_date="2024-01-15T09:30:00")

        h = dedup_hash(as_date)
        assert dedup_hash(as_datetime) == h
        assert dedup_hash(as_iso_str) == h
        assert dedup_hash(as_iso_datetime_str) == h


# ---------------------------------------------------------------------------
# ensure_not_duplicate
# ---------------------------------------------------------------------------

class TestEnsureNotDuplicate:
    """Pre-submit duplicate checks against local ledger and broker."""

    def test_passes_for_new_order(self, mem_conn, paper_order, sim_executor):
        """A brand-new order passes the duplicate check (no exception)."""
        ensure_not_duplicate(paper_order, mem_conn, sim_executor)

    def test_raises_on_local_ledger_duplicate(self, mem_conn, paper_order, sim_executor):
        """An order already in the local ledger raises DuplicateOrderError."""
        # Insert the order into the ledger first
        dh = dedup_hash(paper_order)
        mem_conn.execute(
            """
            INSERT INTO orders
                (order_id, dedup_hash, ticker, side, qty, horizon_bucket,
                 entry_date, advisor_signature, exits_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper_order.order_id,
                dh,
                paper_order.ticker,
                paper_order.side.value,
                paper_order.qty,
                paper_order.horizon_bucket.value,
                str(paper_order.entry_date),
                paper_order.advisor_signature,
                "{}",
                "filled",
                "2024-01-15T12:00:00+00:00",
            ),
        )
        mem_conn.commit()

        with pytest.raises(DuplicateOrderError, match="local ledger"):
            ensure_not_duplicate(paper_order, mem_conn, sim_executor)

    def test_raises_on_broker_duplicate(self, mem_conn, paper_order):
        """An order with an existing broker position raises DuplicateOrderError."""
        from arbiter.shared.sim_executor import SimExecutor
        from arbiter.shared.executor import OrderIntent
        from arbiter.types import OrderSide

        executor = SimExecutor(starting_cash=1_000_000.0)
        # Plant a position by filling a buy order
        intent = OrderIntent(
            order_id="test-id",
            ticker=paper_order.ticker,
            side=OrderSide.BUY,
            qty=10.0,
            limit_price=150.0,
        )
        executor.place(intent)
        assert paper_order.ticker in executor.get_positions()

        with pytest.raises(DuplicateOrderError, match="broker"):
            ensure_not_duplicate(paper_order, mem_conn, executor)

    def test_precomputed_hash_is_used(self, mem_conn, paper_order, sim_executor):
        """Passing dh= avoids recomputing; the supplied hash is used."""
        # With a wrong hash (not in DB), it should pass
        ensure_not_duplicate(paper_order, mem_conn, sim_executor, dh="a" * 64)

    def test_broker_check_exception_raises_duplicate_error(self, mem_conn, paper_order):
        """Finding 6 fix: broker check exception is now fail-closed → DuplicateOrderError.

        Previously, a get_positions() exception was swallowed and returned False
        (fail-open: "not duplicate, proceed").  Now it raises DuplicateOrderError
        to block the submission (fail-closed).
        """
        from arbiter.shared.executor import Executor, ExecutionReport, OrderIntent

        class BrokenExecutor(Executor):
            """Executor whose get_positions() always raises."""
            name = "broken"

            def place(self, intent: OrderIntent) -> ExecutionReport:
                raise NotImplementedError

            def cancel(self, order_id: str) -> ExecutionReport:
                raise NotImplementedError

            def get_positions(self):
                raise ConnectionError("broker unreachable")

            def get_account(self):
                raise NotImplementedError

        executor = BrokenExecutor()

        with pytest.raises(DuplicateOrderError, match="fail-closed"):
            ensure_not_duplicate(paper_order, mem_conn, executor)
