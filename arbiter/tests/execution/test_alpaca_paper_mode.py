"""Engine + adapter tests for the real Alpaca PAPER execution path (spec §5).

OFFLINE: the broker is the in-memory ``FakeAlpaca`` wired into ``AlpacaAdapter``
through its injectable HTTP callables.  No network is touched.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arbiter.config import Config, load_config
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.execution.alpaca_adapter import AlpacaAdapter
from arbiter.shared.executor import OrderIntent
from arbiter.shared.sim_executor import SimExecutor
from arbiter.types import IdeaState, OrderSide

from tests.execution._fake_alpaca import FakeAlpaca

# Reuse the end-to-end seeding/PIT helpers (same package-relative import style).
from tests.integration.test_end_to_end import (
    _AS_OF,
    _seed_cluster_buy,
    _build_pit_with_price,
)


# ---------------------------------------------------------------------------
# Adapter-level unit tests (FakeAlpaca via injected http callables)
# ---------------------------------------------------------------------------

def _adapter_cfg() -> Config:
    base = load_config()
    return dataclasses.replace(
        base,
        live_trading=False,
        executor_backend="alpaca_paper",
        alpaca_api_key="key",
        alpaca_secret_key="secret",
    )


def _make_adapter(fake: FakeAlpaca) -> AlpacaAdapter:
    return AlpacaAdapter(
        config=_adapter_cfg(),
        http_post=fake.http_post,
        http_get=fake.http_get,
        http_delete=fake.http_delete,
    )


@pytest.mark.parametrize(
    "price,expected",
    [
        (240.27988726012836, "240.28"),   # >=$1 -> penny (the live 422 bug)
        (398.2011253841544, "398.20"),
        (1.0, "1.00"),
        (0.46637500000000004, "0.4664"),   # <$1 -> 4 decimals
        (0.999, "0.9990"),
    ],
)
def test_alpaca_limit_str_quantizes_to_tick(price, expected):
    """Alpaca rejects sub-penny limit prices (422) — must quantize at the boundary."""
    from arbiter.execution.alpaca_adapter import _alpaca_limit_str
    assert _alpaca_limit_str(price) == expected


def test_place_limit_price_is_penny_rounded_in_body():
    """The order body sent to Alpaca carries a tick-valid limit_price, not a raw float."""
    fake = FakeAlpaca()
    adapter = _make_adapter(fake)
    adapter.place(OrderIntent(
        order_id="OID1", ticker="AMZN", qty=1.0, side=OrderSide.BUY,
        limit_price=240.27988726012836,
    ))
    assert fake.last_order_body["limit_price"] == "240.28"


class TestAdapterPlaceAndGetOrder:
    def test_place_sends_client_order_id_and_paper_only(self):
        fake = FakeAlpaca()
        adapter = _make_adapter(fake)
        intent = OrderIntent("ULID-1", "AAPL", OrderSide.BUY, qty=10.0, limit_price=100.0)

        report = adapter.place(intent)

        assert report.status == "filled"
        assert report.paper_only is True
        # client_order_id == the order ULID was recorded at the broker.
        assert "ULID-1" in fake._client_order_ids
        assert fake.orders["ULID-1"]["client_order_id"] == "ULID-1"

    def test_duplicate_client_order_id_rejected(self):
        """A retried POST with the same client_order_id does not create a 2nd order."""
        fake = FakeAlpaca()
        adapter = _make_adapter(fake)
        intent = OrderIntent("ULID-DUP", "AAPL", OrderSide.BUY, qty=10.0, limit_price=100.0)

        adapter.place(intent)
        # Second place with the same order_id → broker rejects → adapter reports rejected.
        report2 = adapter.place(intent)

        assert report2.status == "rejected"
        # Only ONE order exists at the broker.
        assert len(fake.orders) == 1

    def test_get_order_reports_fill(self):
        fake = FakeAlpaca(fill_mode="pending")
        adapter = _make_adapter(fake)
        intent = OrderIntent("ULID-2", "MSFT", OrderSide.BUY, qty=4.0, limit_price=50.0)
        adapter.place(intent)

        pending = adapter.get_order("ULID-2")
        assert pending.status == "pending"

        fake.fill_order("ULID-2")
        filled = adapter.get_order("ULID-2")
        assert filled.status == "filled"
        assert filled.filled_qty == 4.0

    def test_get_account_fail_closed_returns_zero_equity(self):
        """On a /v2/account exception the adapter returns equity 0 (A2 input)."""
        fake = FakeAlpaca()

        def boom_get(url, headers):
            if url.endswith("/v2/account"):
                raise RuntimeError("network down")
            return fake.http_get(url, headers)

        adapter = AlpacaAdapter(
            config=_adapter_cfg(),
            http_post=fake.http_post,
            http_get=boom_get,
            http_delete=fake.http_delete,
        )
        acct = adapter.get_account()
        # equity is None/0 on a read failure — A2 treats either as fail-closed.
        assert acct.equity in (None, 0.0)
        assert acct.paper_only is True


# ---------------------------------------------------------------------------
# Engine-level tests in alpaca_paper mode
# ---------------------------------------------------------------------------

def _build_paper_engine(tmp_path, monkeypatch, fake: FakeAlpaca, *, audit: Path):
    """Build an Engine whose executor is an AlpacaAdapter backed by *fake*."""
    db_path = str(tmp_path / "paper.db")
    config = dataclasses.replace(
        load_config(),
        live_trading=False,
        executor_backend="alpaca_paper",
        alpaca_api_key="key",
        alpaca_secret_key="secret",
        db_path=db_path,
        audit_path=str(audit),
        metrics_path=str(tmp_path / "metrics.jsonl"),
        # Hermetic: do NOT inherit the real .env's kill-switch / alert URLs
        # (tests control these explicitly so they pass regardless of go-live state).
        kill_switch_url="",
        alert_webhook_url="",
    )
    clock = BacktestClock(_AS_OF)
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    _seed_cluster_buy(conn, lambda: _AS_OF.isoformat(), ticker="AAPL", n_buyers=3)
    pit = _build_pit_with_price("AAPL")

    adapter = AlpacaAdapter(
        config=config,
        http_post=fake.http_post,
        http_get=fake.http_get,
        http_delete=fake.http_delete,
    )

    # build_engine calls build_executor(config) with no kwargs; swap it for our
    # fake-backed adapter so no network seam is exercised.
    monkeypatch.setattr("arbiter.engine.build_executor", lambda cfg: adapter)

    from arbiter.engine import build_engine
    eng = build_engine(config, conn=conn, pit=pit, clock=clock)
    return eng, conn, config


@pytest.fixture()
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


def test_engine_uses_adapter_no_seed_no_snapshot(tmp_path, monkeypatch, audit_path):
    """In adapter mode build_engine must NOT seed and run_cycle must NOT snapshot."""
    fake = FakeAlpaca()

    seed_calls: list = []
    snap_calls: list = []
    import arbiter.execution.position_store as ps
    monkeypatch.setattr(ps, "seed_executor", lambda *a, **k: seed_calls.append(1))
    monkeypatch.setattr(ps, "snapshot_executor", lambda *a, **k: snap_calls.append(1))

    eng, conn, config = _build_paper_engine(tmp_path, monkeypatch, fake, audit=audit_path)
    assert isinstance(eng.executor, AlpacaAdapter)
    eng.run_cycle(as_of=_AS_OF)

    assert seed_calls == [], "seed_executor must not be called in adapter mode"
    assert snap_calls == [], "snapshot_executor must not be called in adapter mode"


def test_kill_switch_skipped_when_url_empty(tmp_path, monkeypatch, audit_path):
    """live_trading=false + empty kill_switch_url → gate skipped, cycle runs."""
    fake = FakeAlpaca()
    eng, conn, config = _build_paper_engine(tmp_path, monkeypatch, fake, audit=audit_path)
    assert config.kill_switch_url == ""

    result = eng.run_cycle(as_of=_AS_OF)
    assert eng.paused is False
    assert getattr(result, "paused_by_alert", False) is False


def test_filled_advances_idea_to_monitored(tmp_path, monkeypatch, audit_path):
    fake = FakeAlpaca(fill_mode="filled", cash=1_000_000.0, equity=1_000_000.0, last_equity=1_000_000.0)
    eng, conn, config = _build_paper_engine(tmp_path, monkeypatch, fake, audit=audit_path)

    result = eng.run_cycle(as_of=_AS_OF)
    assert result.orders_submitted >= 1

    # The order row is filled and the idea reached MONITORED.
    order = conn.execute("SELECT status, qty FROM orders WHERE ticker='AAPL'").fetchone()
    assert order["status"] == "filled"
    states = [r["state"] for r in conn.execute("SELECT state FROM ideas WHERE ticker='AAPL'")]
    assert IdeaState.MONITORED.value in states
    # qty persisted as whole SHARES, not the dollar notional.
    assert order["qty"] == float(int(order["qty"]))
    assert order["qty"] >= 1.0


def test_pending_does_not_advance_then_reconcile_promotes(tmp_path, monkeypatch, audit_path):
    """A pending order leaves the idea pre-MONITORED; next cycle reconciles it."""
    fake = FakeAlpaca(fill_mode="pending", cash=1_000_000.0, equity=1_000_000.0, last_equity=1_000_000.0)
    eng, conn, config = _build_paper_engine(tmp_path, monkeypatch, fake, audit=audit_path)

    result = eng.run_cycle(as_of=_AS_OF)
    # Order placed but pending → not counted as submitted (idea not advanced).
    assert result.orders_submitted == 0
    order = conn.execute("SELECT order_id, status FROM orders WHERE ticker='AAPL'").fetchone()
    assert order is not None
    assert order["status"] == "pending"
    states = [r["state"] for r in conn.execute("SELECT state FROM ideas WHERE ticker='AAPL'")]
    assert IdeaState.MONITORED.value not in states

    # Simulate the broker filling the pending order, then run the next cycle.
    fake.fill_order(order["order_id"])
    next_as_of = _AS_OF + timedelta(days=1)
    eng.run_cycle(as_of=next_as_of)

    order2 = conn.execute("SELECT status FROM orders WHERE order_id=?", (order["order_id"],)).fetchone()
    assert order2["status"] == "filled"
    states2 = [r["state"] for r in conn.execute("SELECT state FROM ideas WHERE ticker='AAPL'")]
    assert IdeaState.MONITORED.value in states2


def test_a2_fail_closed_on_zero_equity(tmp_path, monkeypatch, audit_path):
    """Zero broker equity → no orders this cycle (no 100k phantom), AND a
    critical alert is fired so a broker outage pages the operator."""
    fake = FakeAlpaca(equity=0.0)
    eng, conn, config = _build_paper_engine(tmp_path, monkeypatch, fake, audit=audit_path)

    result = eng.run_cycle(as_of=_AS_OF)
    assert result.ideas_processed == 0
    assert result.orders_submitted == 0
    assert conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"] == 0
    # A broker account-read failure must surface as a critical alert, not a
    # silent no-op (it is safe but the operator needs to know).
    audit_text = Path(audit_path).read_text() if Path(audit_path).exists() else ""
    assert "Broker account read failed" in audit_text


def test_status_mode_aware(tmp_path, monkeypatch, audit_path):
    fake = FakeAlpaca(fill_mode="filled", cash=1_000_000.0, equity=1_000_000.0, last_equity=1_000_000.0)
    eng, conn, config = _build_paper_engine(tmp_path, monkeypatch, fake, audit=audit_path)
    eng.run_cycle(as_of=_AS_OF)

    st = eng.status()
    assert st["is_sim"] is False
    assert st["executor_backend"] == "alpaca_paper"
    # open_positions sourced from the broker, not the sim snapshot.
    assert st["open_positions"] == len(eng.executor.get_positions())
    # realized_pl is NOT presented as truth in adapter mode.
    assert st["realized_pl"] is None


def test_rejected_trips_breaker_and_pauses(tmp_path, monkeypatch, audit_path):
    """A rejected broker order trips the breaker, raises BrokerError, pauses."""
    fake = FakeAlpaca(cash=1_000_000.0, equity=1_000_000.0, last_equity=1_000_000.0)

    def rejecting_post(url, headers, json_body):
        raise RuntimeError("HTTP 503")

    db_path = str(tmp_path / "paper.db")
    config = dataclasses.replace(
        load_config(),
        live_trading=False,
        executor_backend="alpaca_paper",
        alpaca_api_key="key",
        alpaca_secret_key="secret",
        db_path=db_path,
        audit_path=str(audit_path),
        metrics_path=str(tmp_path / "metrics.jsonl"),
    )
    clock = BacktestClock(_AS_OF)
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    _seed_cluster_buy(conn, lambda: _AS_OF.isoformat(), ticker="AAPL", n_buyers=3)
    pit = _build_pit_with_price("AAPL")
    adapter = AlpacaAdapter(
        config=config,
        http_post=rejecting_post,
        http_get=fake.http_get,
        http_delete=fake.http_delete,
    )
    monkeypatch.setattr("arbiter.engine.build_executor", lambda cfg: adapter)
    from arbiter.engine import build_engine
    eng = build_engine(config, conn=conn, pit=pit, clock=clock)

    eng.run_cycle(as_of=_AS_OF)
    # The broker rejection trips the breaker and auto-pauses the engine.
    assert "broker_non_200" in eng.breaker.any_tripped(conn)
    assert eng.paused is True


# ---------------------------------------------------------------------------
# Paper-only structural guarantee (audit lane, §2)
# ---------------------------------------------------------------------------

def test_no_live_base_url_in_package():
    """No live-money Alpaca trading endpoint string exists anywhere in the package."""
    import re
    pkg = Path(__file__).resolve().parents[2] / "arbiter"
    # Match api.alpaca.markets but NOT paper-api.alpaca.markets / data.alpaca.markets.
    live = re.compile(r"(?<![\w.-])api\.alpaca\.markets")
    offenders = []
    for path in pkg.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in live.finditer(text):
            # exclude paper-api / data subdomains explicitly
            start = max(0, m.start() - 6)
            ctx = text[start:m.end()]
            if "paper-" in ctx or "data." in ctx:
                continue
            offenders.append(str(path))
    assert offenders == [], f"Live Alpaca base URL found in: {offenders}"
