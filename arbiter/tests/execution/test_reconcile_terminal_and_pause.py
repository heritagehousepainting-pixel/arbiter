"""C2 (terminal order reconcile) + C4 (durable pause) tests — OFFLINE."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from arbiter.config import load_config
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.connection import get_connection
from arbiter.db.helpers import generate_ulid, insert_row
from arbiter.db.migrate import run_migrations
from arbiter.execution.alpaca_adapter import AlpacaAdapter
from arbiter.types import HorizonBucket, OrderSide

from tests.execution._fake_alpaca import FakeAlpaca

_UTC = timezone.utc
_AS_OF = datetime(2025, 3, 17, 14, 0, 0, tzinfo=_UTC)


def _pit():
    fx = FixtureSource()
    pit = PITGateway()
    for f in ("price_close", "price_open", "spread", "beta_252d", "adv_20d"):
        pit.register_source(f, fx)
    return pit


def _build(tmp_path, monkeypatch, fake):
    db_path = str(tmp_path / "e.db")
    config = dataclasses.replace(
        load_config(), live_trading=False, executor_backend="alpaca_paper",
        alpaca_api_key="key", alpaca_secret_key="secret", db_path=db_path,
        audit_path=str(tmp_path / "a.jsonl"), metrics_path=str(tmp_path / "m.jsonl"),
    )
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    adapter = AlpacaAdapter(config=config, http_post=fake.http_post,
                            http_get=fake.http_get, http_delete=fake.http_delete)
    monkeypatch.setattr("arbiter.engine.build_executor", lambda cfg: adapter)
    from arbiter.engine import build_engine
    eng = build_engine(config, conn=conn, pit=_pit(), clock=BacktestClock(_AS_OF))
    return eng, conn


def _seed_pending_order(conn, *, order_id, ticker, side):
    insert_row(conn, "orders", {
        "order_id": order_id, "dedup_hash": generate_ulid(),
        "ticker": ticker, "side": side.value, "qty": 10.0,
        "horizon_bucket": HorizonBucket.MEDIUM.value,
        "entry_date": str(_AS_OF.date() - timedelta(days=5)),
        "advisor_signature": "A1.insider:sig",
        "exits_json": json.dumps({}), "status": "pending",
        "created_at": _AS_OF.isoformat(),
    })


@pytest.mark.parametrize("broker_status,expected_local", [
    ("expired", "expired"),
    ("canceled", "expired"),
    ("rejected", "rejected"),
])
class TestC2TerminalReconcile:
    def test_terminal_status_mapped_and_not_requeried(
        self, tmp_path, monkeypatch, broker_status, expected_local,
    ):
        fake = FakeAlpaca(fill_mode="pending")
        eng, conn = _build(tmp_path, monkeypatch, fake)
        oid = "ORD-1"
        _seed_pending_order(conn, order_id=oid, ticker="AAPL", side=OrderSide.BUY)
        # Register the order at the broker with a terminal status.
        fake.orders[oid] = {
            "id": oid, "symbol": "AAPL", "qty": "10",
            "filled_qty": "0", "filled_avg_price": None, "status": broker_status,
        }

        eng._reconcile_pending_orders(_AS_OF)

        row = conn.execute("SELECT status FROM orders WHERE order_id=?", (oid,)).fetchone()
        assert row["status"] == expected_local
        # No longer selected as pending → a second reconcile is a no-op.
        pending = conn.execute("SELECT COUNT(*) c FROM orders WHERE status='pending'").fetchone()["c"]
        assert pending == 0


class TestC4DurablePause:
    def test_persisted_pause_restored_on_rebuild(self, tmp_path, monkeypatch):
        fake = FakeAlpaca(fill_mode="pending")
        eng, conn = _build(tmp_path, monkeypatch, fake)
        # Simulate an auto-pause that persists the flag.
        eng._persist_paused(True, reason="broker-fatal SELL rejection", now=_AS_OF)
        assert eng.paused is False  # in-memory flag not yet set by helper

        # Rebuild the engine against the SAME db → pause must be restored.
        from arbiter.engine import build_engine
        config = eng.config
        conn2 = get_connection(config.db_path)
        adapter = AlpacaAdapter(config=config, http_post=fake.http_post,
                                http_get=fake.http_get, http_delete=fake.http_delete)
        monkeypatch.setattr("arbiter.engine.build_executor", lambda cfg: adapter)
        eng2 = build_engine(config, conn=conn2, pit=_pit(), clock=BacktestClock(_AS_OF))
        assert eng2.paused is True

        # A fast iteration on the restored engine does not trade (gate short-circuits).
        result = eng2.run_fast_iteration(_AS_OF)
        assert getattr(result, "paused_by_alert", False) is True

    def test_resume_clears_persisted_pause(self, tmp_path, monkeypatch):
        fake = FakeAlpaca(fill_mode="pending")
        eng, conn = _build(tmp_path, monkeypatch, fake)
        eng.paused = True
        eng._persist_paused(True, reason="x", now=_AS_OF)
        eng.resume()
        row = conn.execute("SELECT paused FROM engine_state WHERE id=1").fetchone()
        assert row["paused"] == 0
