"""Engine-level exit-monitor wiring tests (sub-project #2) — OFFLINE.

Covers run_cycle ordering, the B2 sweep guard, paused-engine-does-not-sell,
and the alpaca_paper pending-SELL → reconcile close-out path (incl. the None
filled_avg_price guard and a partial sell).
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arbiter.config import load_config
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.connection import get_connection
from arbiter.db.helpers import generate_ulid, insert_row
from arbiter.db.migrate import run_migrations
from arbiter.execution.alpaca_adapter import AlpacaAdapter
from arbiter.contract.seams import Idea
from arbiter.orchestrator import idea_store
from arbiter.shared.executor import OrderIntent
from arbiter.shared.sim_executor import SimExecutor
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


def _seed(conn, *, ticker, shares, avg_price, bucket, entry_date, horizon_days,
          order_status="filled", idea_id=None):
    idea_id = idea_id or generate_ulid()
    idea = Idea(idea_id=idea_id, ticker=ticker, thesis="t", horizon_days=horizon_days,
                state=IdeaState.NASCENT, as_of=_AS_OF - timedelta(days=horizon_days),
                dedupe_key=(ticker, bucket.value))
    idea_store.persist_new_idea(conn, idea, created_at=_AS_OF)
    conn.execute("UPDATE ideas SET state=? WHERE idea_id=?",
                 (IdeaState.MONITORED.value, idea_id))
    conn.commit()
    from arbiter.policy.exits import compute_exits
    exits = compute_exits(bucket=bucket, side=OrderSide.BUY, entry_price=100.0,
                          entry_date=entry_date)
    insert_row(conn, "orders", {
        "order_id": generate_ulid(), "dedup_hash": generate_ulid(),
        "ticker": ticker, "side": OrderSide.BUY.value, "qty": float(shares),
        "horizon_bucket": bucket.value, "entry_date": str(entry_date),
        "advisor_signature": "A1.insider:sig",
        "exits_json": json.dumps(exits, default=str),
        "status": order_status, "created_at": _AS_OF.isoformat(),
        "idea_id": idea_id,
    })
    return idea_id


def _build_engine(tmp_path, monkeypatch, executor, pit, *, kill_switch_url=""):
    db_path = str(tmp_path / "e.db")
    config = dataclasses.replace(
        load_config(), live_trading=False,
        executor_backend="alpaca_paper" if isinstance(executor, AlpacaAdapter) else "sim",
        alpaca_api_key="key", alpaca_secret_key="secret",
        db_path=db_path, audit_path=str(tmp_path / "a.jsonl"),
        metrics_path=str(tmp_path / "m.jsonl"), kill_switch_url=kill_switch_url,
    )
    clock = BacktestClock(_AS_OF)
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    monkeypatch.setattr("arbiter.engine.build_executor", lambda cfg: executor)
    from arbiter.engine import build_engine
    eng = build_engine(config, conn=conn, pit=pit, clock=clock)
    return eng, conn, config


class TestEngineWiringSim:
    def test_stop_loss_sell_in_run_cycle_closes_idea(self, tmp_path, monkeypatch):
        ex = SimExecutor(starting_cash=1_000_000.0)
        ex.place(OrderIntent(generate_ulid(), "AAPL", OrderSide.BUY, qty=10.0, limit_price=300.0))
        pit = _pit("AAPL", close=280.0)
        eng, conn, _ = _build_engine(tmp_path, monkeypatch, ex, pit)
        idea_id = _seed(conn, ticker="AAPL", shares=10, avg_price=300.0,
                        bucket=HorizonBucket.MEDIUM,
                        entry_date=_AS_OF.date() - timedelta(days=10), horizon_days=75)
        eng.run_cycle(as_of=_AS_OF)
        assert "AAPL" not in ex.get_positions()
        state = conn.execute("SELECT state FROM ideas WHERE idea_id=?", (idea_id,)).fetchone()["state"]
        assert state == IdeaState.CLOSED.value
        outs = conn.execute("SELECT label_kind FROM outcomes WHERE idea_id=?", (idea_id,)).fetchall()
        assert len(outs) == 1 and outs[0]["label_kind"] == "early_exit"

    def test_sweep_does_not_double_label_after_monitor(self, tmp_path, monkeypatch):
        """Monitor closes a horizon-expired held idea; the sweep must not relabel."""
        ex = SimExecutor(starting_cash=1_000_000.0)
        ex.place(OrderIntent(generate_ulid(), "MSFT", OrderSide.BUY, qty=5.0, limit_price=200.0))
        pit = _pit("MSFT", close=210.0)
        eng, conn, _ = _build_engine(tmp_path, monkeypatch, ex, pit)
        idea_id = _seed(conn, ticker="MSFT", shares=5, avg_price=200.0,
                        bucket=HorizonBucket.MEDIUM,
                        entry_date=_AS_OF.date() - timedelta(days=80), horizon_days=75)
        eng.run_cycle(as_of=_AS_OF)
        outs = conn.execute("SELECT * FROM outcomes WHERE idea_id=?", (idea_id,)).fetchall()
        # Exactly ONE outcome, labeled by the monitor (normal horizon w/ real fill),
        # NOT a second one from the sweep.
        assert len(outs) == 1

    def test_paused_engine_does_not_sell(self, tmp_path, monkeypatch):
        ex = SimExecutor(starting_cash=1_000_000.0)
        ex.place(OrderIntent(generate_ulid(), "AAPL", OrderSide.BUY, qty=10.0, limit_price=300.0))
        pit = _pit("AAPL", close=280.0)
        eng, conn, _ = _build_engine(tmp_path, monkeypatch, ex, pit)
        _seed(conn, ticker="AAPL", shares=10, avg_price=300.0, bucket=HorizonBucket.MEDIUM,
              entry_date=_AS_OF.date() - timedelta(days=10), horizon_days=75)
        eng.paused = True
        eng.run_cycle(as_of=_AS_OF)
        # No SELL placed while paused.
        assert "AAPL" in ex.get_positions()
        assert conn.execute("SELECT COUNT(*) c FROM orders WHERE side='SELL'").fetchone()["c"] == 0

    def test_kill_switch_halted_does_not_sell(self, tmp_path, monkeypatch):
        """Spec §5: a kill-switched engine must NOT run the exit monitor, even
        for a held position that WOULD otherwise hit its stop."""
        ex = SimExecutor(starting_cash=1_000_000.0)
        ex.place(OrderIntent(generate_ulid(), "AAPL", OrderSide.BUY, qty=10.0, limit_price=300.0))
        pit = _pit("AAPL", close=280.0)  # avg 300 → stop 285; 280 breaches.
        # A configured kill_switch_url makes the gate active on the paper path.
        eng, conn, _ = _build_engine(
            tmp_path, monkeypatch, ex, pit,
            kill_switch_url="http://kill.invalid/status",
        )
        _seed(conn, ticker="AAPL", shares=10, avg_price=300.0, bucket=HorizonBucket.MEDIUM,
              entry_date=_AS_OF.date() - timedelta(days=10), horizon_days=75)
        # Force the kill switch to report halted (no real network).
        monkeypatch.setattr(eng.kill_switch, "is_halted", lambda *, as_of: True)
        eng.run_cycle(as_of=_AS_OF)
        # Halted → no exit monitor → position still held, ZERO SELL rows.
        assert "AAPL" in ex.get_positions()
        assert conn.execute("SELECT COUNT(*) c FROM orders WHERE side='SELL'").fetchone()["c"] == 0

    def test_breaker_tripped_does_not_sell(self, tmp_path, monkeypatch):
        """Spec §5: a circuit-breaker-tripped engine must NOT run the exit
        monitor, even for a held position that WOULD otherwise hit its stop."""
        ex = SimExecutor(starting_cash=1_000_000.0)
        ex.place(OrderIntent(generate_ulid(), "AAPL", OrderSide.BUY, qty=10.0, limit_price=300.0))
        pit = _pit("AAPL", close=280.0)  # avg 300 → stop 285; 280 breaches.
        eng, conn, _ = _build_engine(tmp_path, monkeypatch, ex, pit)
        _seed(conn, ticker="AAPL", shares=10, avg_price=300.0, bucket=HorizonBucket.MEDIUM,
              entry_date=_AS_OF.date() - timedelta(days=10), horizon_days=75)
        # Latch a circuit breaker before the cycle.
        eng.breaker.trip("daily_loss", "test trip", conn)
        eng.run_cycle(as_of=_AS_OF)
        # Tripped → no exit monitor → position still held, ZERO SELL rows.
        assert "AAPL" in ex.get_positions()
        assert conn.execute("SELECT COUNT(*) c FROM orders WHERE side='SELL'").fetchone()["c"] == 0


class TestEngineWiringAdapter:
    def _adapter(self, fake):
        cfg = dataclasses.replace(load_config(), live_trading=False,
                                  executor_backend="alpaca_paper",
                                  alpaca_api_key="key", alpaca_secret_key="secret")
        return AlpacaAdapter(config=cfg, http_post=fake.http_post,
                             http_get=fake.http_get, http_delete=fake.http_delete)

    def test_pending_sell_then_reconcile_closes(self, tmp_path, monkeypatch):
        """A pending SELL leaves the idea MONITORED; next cycle reconcile closes it."""
        fake = FakeAlpaca(fill_mode="pending", cash=1_000_000.0,
                          equity=1_000_000.0, last_equity=1_000_000.0)
        # Seed a held broker position so the monitor sees it.
        fake.positions["AAPL"] = {"symbol": "AAPL", "qty": "10", "avg_entry_price": "300.0"}
        ex = self._adapter(fake)
        pit = _pit("AAPL", close=280.0)
        eng, conn, _ = _build_engine(tmp_path, monkeypatch, ex, pit)
        idea_id = _seed(conn, ticker="AAPL", shares=10, avg_price=300.0,
                        bucket=HorizonBucket.MEDIUM,
                        entry_date=_AS_OF.date() - timedelta(days=10), horizon_days=75)
        eng.run_cycle(as_of=_AS_OF)
        # SELL placed pending — idea still MONITORED, no outcome yet.
        sell = conn.execute("SELECT order_id, status FROM orders WHERE side='SELL'").fetchone()
        assert sell is not None and sell["status"] == "pending"
        st = conn.execute("SELECT state FROM ideas WHERE idea_id=?", (idea_id,)).fetchone()["state"]
        assert st == IdeaState.MONITORED.value
        assert conn.execute("SELECT COUNT(*) c FROM outcomes").fetchone()["c"] == 0

        # Broker fills the SELL; next cycle reconcile closes the idea + labels.
        fake.fill_order(sell["order_id"], avg_price=279.0)
        # Remove the position so the monitor doesn't try to re-sell.
        fake.positions.pop("AAPL", None)
        eng.run_cycle(as_of=_AS_OF + timedelta(days=1))
        st2 = conn.execute("SELECT state FROM ideas WHERE idea_id=?", (idea_id,)).fetchone()["state"]
        assert st2 == IdeaState.CLOSED.value
        outs = conn.execute("SELECT label_kind FROM outcomes WHERE idea_id=?", (idea_id,)).fetchall()
        assert len(outs) == 1 and outs[0]["label_kind"] == "early_exit"

    def test_none_filled_avg_price_falls_back_to_pit_close(self, tmp_path, monkeypatch):
        """A filled SELL with no avg fill price falls back to the PIT close."""
        from arbiter.execution import exit_monitor
        fake = FakeAlpaca(cash=1_000_000.0)
        ex = self._adapter(fake)
        pit = _pit("AAPL", close=280.0)
        eng, conn, _ = _build_engine(tmp_path, monkeypatch, ex, pit)
        idea_id = _seed(conn, ticker="AAPL", shares=10, avg_price=300.0,
                        bucket=HorizonBucket.MEDIUM,
                        entry_date=_AS_OF.date() - timedelta(days=10), horizon_days=75)
        order_row = conn.execute("SELECT * FROM orders WHERE side='BUY'").fetchone()
        # exit_price=None → labeler should fall back to PIT close (280) not crash.
        oid = exit_monitor.close_idea_on_sell_fill(
            conn, order_row=order_row, exit_price=None, exit_as_of=_AS_OF,
            label_kind="early_exit", pit=pit,
            advisor_id_for=lambda i: "A1.insider",
            audit_path=None,
        )
        assert oid is not None
        st = conn.execute("SELECT state FROM ideas WHERE idea_id=?", (idea_id,)).fetchone()["state"]
        assert st == IdeaState.CLOSED.value

    def test_partial_sell_reconcile_keeps_monitored_then_resells_residual(self, tmp_path, monkeypatch):
        """A pending SELL that reconciles as PARTIAL leaves the idea MONITORED;
        the next cycle re-sells the residual with a fresh dedup nonce (B4)."""
        fake = FakeAlpaca(fill_mode="pending", cash=1_000_000.0,
                          equity=1_000_000.0, last_equity=1_000_000.0)
        fake.positions["AAPL"] = {"symbol": "AAPL", "qty": "10", "avg_entry_price": "300.0"}
        ex = self._adapter(fake)
        pit = _pit("AAPL", close=280.0)
        eng, conn, _ = _build_engine(tmp_path, monkeypatch, ex, pit)
        idea_id = _seed(conn, ticker="AAPL", shares=10, avg_price=300.0,
                        bucket=HorizonBucket.MEDIUM,
                        entry_date=_AS_OF.date() - timedelta(days=10), horizon_days=75)
        eng.run_cycle(as_of=_AS_OF)
        sell = conn.execute("SELECT order_id, status FROM orders WHERE side='SELL'").fetchone()
        assert sell["status"] == "pending"

        # Broker partially fills the SELL (5 of 10).  Mark the order partial and
        # reduce the broker position to the residual 5 shares.
        order = fake.orders[sell["order_id"]]
        order["filled_qty"] = "5"
        order["filled_avg_price"] = "279.0"
        order["status"] = "partially_filled"
        fake.positions["AAPL"] = {"symbol": "AAPL", "qty": "5", "avg_entry_price": "300.0"}

        # Next cycle: reconcile sees partial → idea stays MONITORED, no outcome.
        # The monitor then re-sells the residual 5 shares with a fresh nonce.
        fake.fill_mode = "filled"
        eng.run_cycle(as_of=_AS_OF + timedelta(days=1))

        st = conn.execute("SELECT state FROM ideas WHERE idea_id=?", (idea_id,)).fetchone()["state"]
        # After the residual full-fill the idea is CLOSED.
        assert st == IdeaState.CLOSED.value
        # Two distinct SELL rows: the original partial + the residual.
        sells = conn.execute("SELECT status FROM orders WHERE side='SELL'").fetchall()
        assert len(sells) == 2
        statuses = sorted(s["status"] for s in sells)
        assert statuses == ["filled", "partial"]
        # Exactly one outcome (from the residual full-fill close-out).
        assert conn.execute("SELECT COUNT(*) c FROM outcomes WHERE idea_id=?", (idea_id,)).fetchone()["c"] == 1
