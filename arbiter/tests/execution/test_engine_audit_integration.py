"""B-ENGINE integration tests — the audit fixes wired into the engine.

These tests exercise the SEAMS the unit tests hid (A2 risk-cap binding, D3
reconciler wiring, D2 idea_id advance, F3 crash snapshot, A4 breaker wiring).
All OFFLINE — the broker is the in-memory ``FakeAlpaca`` (no network).
"""
from __future__ import annotations

import dataclasses
from datetime import timedelta
from pathlib import Path

import pytest

from arbiter.config import load_config
from arbiter.data.clock import BacktestClock
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.execution.alpaca_adapter import AlpacaAdapter
from arbiter.shared.executor import OrderIntent
from arbiter.shared.sim_executor import SimExecutor
from arbiter.types import IdeaState, OrderSide

from tests.execution._fake_alpaca import FakeAlpaca
from tests.integration.test_end_to_end import (
    _AS_OF,
    _seed_cluster_buy,
    _build_pit_with_price,
)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _paper_config(tmp_path, audit: Path):
    return dataclasses.replace(
        load_config(),
        live_trading=False,
        executor_backend="alpaca_paper",
        alpaca_api_key="key",
        alpaca_secret_key="secret",
        db_path=str(tmp_path / "paper.db"),
        audit_path=str(audit),
        metrics_path=str(tmp_path / "metrics.jsonl"),
    )


def _build_paper_engine(tmp_path, monkeypatch, fake: FakeAlpaca, *, audit: Path,
                        ticker: str = "AAPL"):
    config = _paper_config(tmp_path, audit)
    clock = BacktestClock(_AS_OF)
    conn = get_connection(config.db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    _seed_cluster_buy(conn, lambda: _AS_OF.isoformat(), ticker=ticker, n_buyers=3)
    pit = _build_pit_with_price(ticker)

    adapter = AlpacaAdapter(
        config=config,
        http_post=fake.http_post,
        http_get=fake.http_get,
        http_delete=fake.http_delete,
    )
    monkeypatch.setattr("arbiter.engine.build_executor", lambda cfg: adapter)
    from arbiter.engine import build_engine
    eng = build_engine(config, conn=conn, pit=pit, clock=clock)
    return eng, conn, config


@pytest.fixture()
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


def _seed_broker_positions(fake: FakeAlpaca, positions: dict[str, tuple[float, float]]) -> None:
    """positions = {symbol: (shares, avg_price)}."""
    for symbol, (shares, price) in positions.items():
        fake.positions[symbol] = {
            "symbol": symbol,
            "qty": str(shares),
            "avg_entry_price": str(price),
        }


# ---------------------------------------------------------------------------
# A2 — book-aware caps now BIND with a populated book
# ---------------------------------------------------------------------------

def test_open_count_cap_rejects_when_book_full(tmp_path, monkeypatch, audit_path):
    """A book already at max_open_positions blocks a NEW name (A2 P0)."""
    fake = FakeAlpaca(fill_mode="filled", cash=10_000_000.0,
                      equity=10_000_000.0, last_equity=10_000_000.0)
    # Pre-seed exactly max_open_positions (8) OTHER held names → at capacity.
    held = {f"H{i}": (10.0, 100.0) for i in range(8)}
    _seed_broker_positions(fake, held)

    # Hermetic: pin the cap this test's book is sized for (conftest blanks the
    # live .env override; the toml default is 20).
    monkeypatch.setenv("ARBITER_MAX_OPEN_POSITIONS", "8")
    eng, conn, config = _build_paper_engine(tmp_path, monkeypatch, fake, audit=audit_path)
    assert config.max_open_positions == 8

    result = eng.run_cycle(as_of=_AS_OF)
    # The open-count cap zeroes the size → no NEW order for AAPL.
    assert result.orders_submitted == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM orders WHERE ticker='AAPL'"
    ).fetchone()["c"] == 0


def test_gross_cap_rejects_when_book_near_gross_limit(tmp_path, monkeypatch, audit_path):
    """A book already at the gross cap leaves zero headroom for a new order."""
    equity = 100_000.0
    fake = FakeAlpaca(fill_mode="filled", cash=equity,
                      equity=equity, last_equity=equity)
    # One held name worth ~50% of equity == max_gross_pct (0.50) → no headroom.
    # 350 sh * $150 = $52,500 > 0.50 * 100k. Sector differs from AAPL (IT).
    _seed_broker_positions(fake, {"XOM": (350.0, 150.0)})  # Energy sector

    # Hermetic: pin the gross cap this test's book is sized for (conftest
    # blanks the live .env override; the toml default is 0.80).
    monkeypatch.setenv("ARBITER_MAX_GROSS_PCT", "0.50")
    eng, conn, config = _build_paper_engine(tmp_path, monkeypatch, fake, audit=audit_path)
    assert config.max_gross_pct == 0.5

    result = eng.run_cycle(as_of=_AS_OF)
    assert result.orders_submitted == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM orders WHERE ticker='AAPL'"
    ).fetchone()["c"] == 0


def test_empty_book_allows_order(tmp_path, monkeypatch, audit_path):
    """Control: an EMPTY book leaves the happy path intact (order goes through)."""
    fake = FakeAlpaca(fill_mode="filled", cash=1_000_000.0,
                      equity=1_000_000.0, last_equity=1_000_000.0)
    eng, conn, config = _build_paper_engine(tmp_path, monkeypatch, fake, audit=audit_path)

    result = eng.run_cycle(as_of=_AS_OF)
    assert result.orders_submitted >= 1


# ---------------------------------------------------------------------------
# D3 — reconciler surfaces a BROKER_ONLY orphan
# ---------------------------------------------------------------------------

def test_reconciler_surfaces_broker_only_orphan(tmp_path, monkeypatch, audit_path):
    """A broker position with no local order is surfaced as BROKER_ONLY + alert."""
    fake = FakeAlpaca(fill_mode="filled", cash=1_000_000.0,
                      equity=1_000_000.0, last_equity=1_000_000.0)
    # An orphan: broker holds ZZZ but the local ledger has no order for it.
    _seed_broker_positions(fake, {"ZZZ": (5.0, 200.0)})

    alerts: list = []

    eng, conn, config = _build_paper_engine(tmp_path, monkeypatch, fake, audit=audit_path)
    orig_alert = eng.alerting.alert

    def _spy(tier, message, ctx, *, as_of):
        alerts.append((tier, message, ctx))
        return orig_alert(tier, message, ctx, as_of=as_of)

    monkeypatch.setattr(eng.alerting, "alert", _spy)

    eng.run_cycle(as_of=_AS_OF)

    # A divergence alert mentioning the orphan was raised for human review.
    div_alerts = [a for a in alerts if "divergence" in a[1].lower()]
    assert div_alerts, "expected a reconciler divergence alert"
    tickers = [d["ticker"] for d in div_alerts[0][2]["divergences"]]
    kinds = [d["kind"] for d in div_alerts[0][2]["divergences"]]
    assert "ZZZ" in tickers
    assert "BROKER_ONLY" in kinds


# ---------------------------------------------------------------------------
# D2 — idea_id join advances the RIGHT idea on a reconciled fill
# ---------------------------------------------------------------------------

def test_idea_id_join_advances_correct_idea(tmp_path, monkeypatch, audit_path):
    """Two ideas share (ticker,bucket); the fill advances exactly the order's idea_id."""
    fake = FakeAlpaca(fill_mode="pending", cash=1_000_000.0,
                      equity=1_000_000.0, last_equity=1_000_000.0)
    eng, conn, config = _build_paper_engine(tmp_path, monkeypatch, fake, audit=audit_path)

    # First cycle: places a pending order, links it to its idea via idea_id.
    eng.run_cycle(as_of=_AS_OF)
    order = conn.execute(
        "SELECT order_id, idea_id, ticker, horizon_bucket FROM orders WHERE ticker='AAPL'"
    ).fetchone()
    assert order is not None
    assert order["idea_id"] is not None, "order should be linked to its idea by idea_id"
    owning_idea = order["idea_id"]

    # Plant a SECOND, DECOY idea sharing the same (ticker, bucket) that is NOT the
    # order's owner. The fragile (ticker,bucket) join (ORDER BY created_at DESC)
    # would advance THIS newer one; the idea_id join must advance the real owner.
    from arbiter.orchestrator.idea import make_idea
    from arbiter.orchestrator import idea_store
    # Same ticker + horizon_days → same dedupe bucket as the real AAPL idea.
    real_horizon = conn.execute(
        "SELECT horizon_days FROM ideas WHERE idea_id=?", (owning_idea,)
    ).fetchone()["horizon_days"]
    decoy_idea = make_idea(
        ticker="AAPL", thesis="decoy", horizon_days=int(real_horizon),
        as_of=_AS_OF + timedelta(hours=1),
    )
    idea_store.persist_new_idea(conn, decoy_idea, created_at=_AS_OF + timedelta(hours=1))
    decoy = decoy_idea.idea_id

    # Broker fills the pending order; next cycle reconciles it.
    fake.fill_order(order["order_id"])
    eng.run_cycle(as_of=_AS_OF + timedelta(days=1))

    owner_state = conn.execute(
        "SELECT state FROM ideas WHERE idea_id=?", (owning_idea,)
    ).fetchone()["state"]
    decoy_state = conn.execute(
        "SELECT state FROM ideas WHERE idea_id=?", (decoy,)
    ).fetchone()["state"]

    assert owner_state == IdeaState.MONITORED.value, "the order's OWN idea must advance"
    assert decoy_state != IdeaState.MONITORED.value, "the decoy idea must NOT advance"


# ---------------------------------------------------------------------------
# F3 — fast-iteration sim SELL is snapshotted (no resurrection on rebuild)
# ---------------------------------------------------------------------------

def _sim_engine(tmp_path, audit: Path, clock):
    config = dataclasses.replace(
        load_config(),
        live_trading=False,
        executor_backend="sim",
        db_path=str(tmp_path / "sim.db"),
        audit_path=str(audit),
        metrics_path=str(tmp_path / "metrics.jsonl"),
    )
    conn = get_connection(config.db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    pit = _build_pit_with_price("AAPL")
    from arbiter.engine import build_engine
    eng = build_engine(config, conn=conn, pit=pit, clock=clock)
    return eng, conn, config


def test_fast_iteration_sim_sell_is_snapshotted(tmp_path, monkeypatch, audit_path):
    """A fast-iteration SELL persists to the snapshot → no resurrection on rebuild."""
    clock = BacktestClock(_AS_OF)
    eng, conn, config = _sim_engine(tmp_path, audit_path, clock)

    # Plant a held position in the sim broker AND snapshot it (as a prior cycle would).
    eng.executor.place(OrderIntent("seed-1", "AAPL", OrderSide.BUY, qty=300.0, limit_price=150.0))
    from arbiter.execution import position_store
    position_store.snapshot_executor(conn, eng.executor, as_of=_AS_OF)
    assert "AAPL" in eng.executor.get_positions()

    # The fast iteration sells the position via the (monkeypatched) exit monitor,
    # which closes it in the in-memory broker. The engine must snapshot AFTER.
    def _fake_exit(now, opinions):
        eng.executor.place(OrderIntent("exit-1", "AAPL", OrderSide.SELL, qty=300.0, limit_price=160.0))
        return ["AAPL"]  # a sell happened

    monkeypatch.setattr(eng, "_run_exit_monitor", _fake_exit)
    eng.run_fast_iteration(now=_AS_OF + timedelta(minutes=10))

    # The position is gone in-memory.
    assert "AAPL" not in eng.executor.get_positions()

    # Rebuild a FRESH engine from the SAME db (simulating a crash + relaunch).
    # If the fast-iteration SELL was snapshotted, the closed position must NOT
    # resurrect on seed.
    monkeypatch.undo()
    from arbiter.engine import build_engine
    eng2 = build_engine(config, conn=conn, pit=_build_pit_with_price("AAPL"),
                        clock=BacktestClock(_AS_OF))
    assert "AAPL" not in eng2.executor.get_positions(), "closed position resurrected!"


# ---------------------------------------------------------------------------
# A4 — a daily-loss breaker trip halts the cycle
# ---------------------------------------------------------------------------

def test_daily_loss_breaker_trips_and_halts(tmp_path, monkeypatch, audit_path):
    """A >2% daily loss latches the daily_loss breaker → no orders this cycle."""
    # daily_pl = equity - last_equity = -3% → trips the -2% daily-loss breaker.
    fake = FakeAlpaca(fill_mode="filled", cash=1_000_000.0,
                      equity=970_000.0, last_equity=1_000_000.0)
    eng, conn, config = _build_paper_engine(tmp_path, monkeypatch, fake, audit=audit_path)

    result = eng.run_cycle(as_of=_AS_OF)

    # The daily-loss breaker latched, and no order was submitted this cycle.
    assert "daily_loss" in eng.breaker.any_tripped(conn)
    assert result.orders_submitted == 0
