"""Engine partial-SELL guard tests via the reconcile path (FakeAlpaca partial).

W-TESTHARDEN seam #4.  A partial SELL fill must leave the idea/ledger CONSISTENT:

  - the ledger ``orders.qty`` is updated to the ACTUALLY-FILLED share count
    (not the requested qty) — otherwise ledger-summed exposure/P&L over-states
    the position by the unfilled remainder (D3 P2 / D2 P1);
  - the order row stays ``partial`` so it is re-selected next cycle to reconcile
    the residual — it is NOT marked terminal;
  - the idea stays ``MONITORED`` and NO outcome is labeled (a partial SELL is
    not a close, B4);
  - and the residual is handled: the next cycle's full reconcile closes it.

Driven through the REAL ``engine._reconcile_pending_orders`` (via
``engine.run_cycle``) with a FakeAlpaca returning a partial.  OFFLINE: temp DB,
no network, BacktestClock.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone

from arbiter.config import load_config
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.connection import get_connection
from arbiter.db.helpers import generate_ulid, insert_row
from arbiter.db.migrate import run_migrations
from arbiter.execution.alpaca_adapter import AlpacaAdapter
from arbiter.contract.seams import Idea
from arbiter.orchestrator import idea_store
from arbiter.policy.exits import compute_exits
from arbiter.types import HorizonBucket, IdeaState, OrderSide

from tests.execution._fake_alpaca import FakeAlpaca

_UTC = timezone.utc
_AS_OF = datetime(2025, 3, 15, 12, 0, 0, tzinfo=_UTC)


def _pit(ticker, *, close):
    fx = FixtureSource()
    pit = PITGateway()
    for f in ("price_close", "price_open", "spread", "beta_252d", "adv_20d"):
        pit.register_source(f, fx)
    early = _AS_OF - timedelta(days=400)
    for t in (ticker, "SPY"):
        fx.add("price_open", t, early, 100.0)
        fx.add("price_close", t, early, 100.0)
        fx.add("spread", t, early, 0.01)
        fx.add("beta_252d", t, early, 1.0)
        fx.add("adv_20d", t, early, 10_000_000.0)
    ts = _AS_OF - timedelta(days=1)
    fx.add("price_close", ticker, ts, close)
    fx.add("price_open", ticker, ts, close)
    fx.add("spread", ticker, ts, 0.01)
    fx.add("price_close", "SPY", ts, 100.0)
    fx.add("price_open", "SPY", ts, 100.0)
    return pit


def _adapter(fake):
    cfg = dataclasses.replace(
        load_config(), live_trading=False, executor_backend="alpaca_paper",
        alpaca_api_key="key", alpaca_secret_key="secret",
    )
    return AlpacaAdapter(config=cfg, http_post=fake.http_post,
                         http_get=fake.http_get, http_delete=fake.http_delete)


def _build_engine(tmp_path, monkeypatch, executor, pit):
    db_path = str(tmp_path / "e.db")
    config = dataclasses.replace(
        load_config(), live_trading=False, executor_backend="alpaca_paper",
        alpaca_api_key="key", alpaca_secret_key="secret",
        db_path=db_path, audit_path=str(tmp_path / "a.jsonl"),
        metrics_path=str(tmp_path / "m.jsonl"), kill_switch_url="",
    )
    clock = BacktestClock(_AS_OF)
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    monkeypatch.setattr("arbiter.engine.build_executor", lambda cfg: executor)
    from arbiter.engine import build_engine
    eng = build_engine(config, conn=conn, pit=pit, clock=clock)
    return eng, conn


def _seed_monitored(conn, *, ticker, shares, bucket, entry_date, horizon_days):
    idea_id = generate_ulid()
    idea = Idea(idea_id=idea_id, ticker=ticker, thesis="t", horizon_days=horizon_days,
                state=IdeaState.NASCENT, as_of=_AS_OF - timedelta(days=horizon_days),
                dedupe_key=(ticker, bucket.value))
    idea_store.persist_new_idea(conn, idea, created_at=_AS_OF)
    conn.execute("UPDATE ideas SET state=? WHERE idea_id=?",
                 (IdeaState.MONITORED.value, idea_id))
    conn.commit()
    exits = compute_exits(bucket=bucket, side=OrderSide.BUY, entry_price=100.0,
                          entry_date=entry_date)
    insert_row(conn, "orders", {
        "order_id": generate_ulid(), "dedup_hash": generate_ulid(),
        "ticker": ticker, "side": OrderSide.BUY.value, "qty": float(shares),
        "horizon_bucket": bucket.value, "entry_date": str(entry_date),
        "advisor_signature": "A1.insider:sig",
        "exits_json": json.dumps(exits, default=str),
        "status": "filled", "created_at": _AS_OF.isoformat(), "idea_id": idea_id,
    })
    return idea_id


class TestEnginePartialSellGuard:
    def test_partial_sell_reconcile_persists_filled_qty_and_holds_idea(
        self, tmp_path, monkeypatch
    ):
        """Reconcile of a PARTIAL SELL: ledger qty = filled (5), idea stays MONITORED."""
        fake = FakeAlpaca(fill_mode="pending", cash=1_000_000.0,
                          equity=1_000_000.0, last_equity=1_000_000.0)
        fake.positions["AAPL"] = {"symbol": "AAPL", "qty": "10", "avg_entry_price": "300.0"}
        ex = _adapter(fake)
        pit = _pit("AAPL", close=280.0)  # avg 300 → stop breached → SELL.
        eng, conn = _build_engine(tmp_path, monkeypatch, ex, pit)
        idea_id = _seed_monitored(
            conn, ticker="AAPL", shares=10, bucket=HorizonBucket.MEDIUM,
            entry_date=_AS_OF.date() - timedelta(days=10), horizon_days=75,
        )

        # Cycle 1: the stop-loss SELL is placed pending (10 requested shares).
        eng.run_cycle(as_of=_AS_OF)
        sell = conn.execute(
            "SELECT order_id, status, qty FROM orders WHERE side='SELL'"
        ).fetchone()
        assert sell["status"] == "pending"
        assert sell["qty"] == 10.0  # requested whole position

        # Broker PARTIALLY fills the SELL: 4 of 10 shares.  Reduce the broker
        # position to the residual 6.  fill_mode stays "pending" so the monitor's
        # residual re-sell also stays pending — isolating the reconcile-of-partial
        # so the idea must remain MONITORED with no outcome yet.
        order = fake.orders[sell["order_id"]]
        order["filled_qty"] = "4"
        order["filled_avg_price"] = "279.0"
        order["qty"] = "10"
        order["status"] = "partially_filled"
        fake.positions["AAPL"] = {"symbol": "AAPL", "qty": "6", "avg_entry_price": "300.0"}

        # Cycle 2: reconcile sees PARTIAL on the original SELL.
        eng.run_cycle(as_of=_AS_OF + timedelta(days=1))

        row = conn.execute(
            "SELECT status, qty FROM orders WHERE order_id=?", (sell["order_id"],)
        ).fetchone()
        # filled_qty (4) persisted — NOT the requested 10 (no exposure overstatement).
        assert row["qty"] == 4.0, "partial must persist filled_qty, not requested qty"
        # Row stays 'partial' so it is re-reconciled for the residual next cycle.
        assert row["status"] == "partial"
        # Idea stays MONITORED — a partial SELL is NOT a close.
        st = conn.execute(
            "SELECT state FROM ideas WHERE idea_id=?", (idea_id,)
        ).fetchone()["state"]
        assert st == IdeaState.MONITORED.value
        # No outcome labeled on a partial.
        assert conn.execute(
            "SELECT COUNT(*) c FROM outcomes WHERE idea_id=?", (idea_id,)
        ).fetchone()["c"] == 0

    def test_residual_fill_next_cycle_closes_idea(self, tmp_path, monkeypatch):
        """The partial row is re-reconciled: when it fully fills, the idea CLOSES."""
        fake = FakeAlpaca(fill_mode="pending", cash=1_000_000.0,
                          equity=1_000_000.0, last_equity=1_000_000.0)
        fake.positions["AAPL"] = {"symbol": "AAPL", "qty": "10", "avg_entry_price": "300.0"}
        ex = _adapter(fake)
        pit = _pit("AAPL", close=280.0)
        eng, conn = _build_engine(tmp_path, monkeypatch, ex, pit)
        idea_id = _seed_monitored(
            conn, ticker="AAPL", shares=10, bucket=HorizonBucket.MEDIUM,
            entry_date=_AS_OF.date() - timedelta(days=10), horizon_days=75,
        )

        eng.run_cycle(as_of=_AS_OF)
        sell = conn.execute(
            "SELECT order_id FROM orders WHERE side='SELL'"
        ).fetchone()

        # Partial fill of the pending SELL (5 of 10); position drops to residual 5.
        order = fake.orders[sell["order_id"]]
        order["filled_qty"] = "5"
        order["filled_avg_price"] = "279.0"
        order["qty"] = "10"
        order["status"] = "partially_filled"
        fake.positions["AAPL"] = {"symbol": "AAPL", "qty": "5", "avg_entry_price": "300.0"}

        # Next cycle: reconcile persists partial; monitor re-sells the residual 5
        # (fresh dedup nonce, B4) which the 'filled' fake fills immediately.
        fake.fill_mode = "filled"
        eng.run_cycle(as_of=_AS_OF + timedelta(days=1))

        st = conn.execute(
            "SELECT state FROM ideas WHERE idea_id=?", (idea_id,)
        ).fetchone()["state"]
        assert st == IdeaState.CLOSED.value
        # Two SELL rows: original partial + residual full fill.
        statuses = sorted(
            r["status"] for r in
            conn.execute("SELECT status FROM orders WHERE side='SELL'").fetchall()
        )
        assert statuses == ["filled", "partial"]
        # Exactly one outcome, from the residual close-out (no double-label).
        assert conn.execute(
            "SELECT COUNT(*) c FROM outcomes WHERE idea_id=?", (idea_id,)
        ).fetchone()["c"] == 1
