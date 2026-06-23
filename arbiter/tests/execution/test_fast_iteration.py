"""Engine.run_fast_iteration + C0/C2/C4 tests — sub-project #3, OFFLINE."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from arbiter.config import load_config
from arbiter.data.clock import BacktestClock, Clock
from arbiter.data.current_price import NullCurrentPriceProvider, AlpacaCurrentPriceSource
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
_AS_OF = datetime(2025, 3, 17, 14, 0, 0, tzinfo=_UTC)  # Mon, market hours


class _FakeProvider:
    """Scripted current-price provider."""

    def __init__(self, prices: dict[str, float | None]):
        self.prices = prices

    def current_price(self, ticker):
        return self.prices.get(ticker)

    def current_prices(self, tickers):
        return {t: self.prices[t] for t in tickers if self.prices.get(t) is not None}


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


def _seed(conn, *, ticker, shares, bucket, entry_date, horizon_days, idea_id=None):
    idea_id = idea_id or generate_ulid()
    idea = Idea(idea_id=idea_id, ticker=ticker, thesis="t", horizon_days=horizon_days,
                state=IdeaState.NASCENT, as_of=_AS_OF - timedelta(days=horizon_days),
                dedupe_key=(ticker, bucket.value))
    idea_store.persist_new_idea(conn, idea, created_at=_AS_OF)
    conn.execute("UPDATE ideas SET state=? WHERE idea_id=?", (IdeaState.MONITORED.value, idea_id))
    conn.commit()
    from arbiter.policy.exits import compute_exits
    exits = compute_exits(bucket=bucket, side=OrderSide.BUY, entry_price=300.0, entry_date=entry_date)
    insert_row(conn, "orders", {
        "order_id": generate_ulid(), "dedup_hash": generate_ulid(),
        "ticker": ticker, "side": OrderSide.BUY.value, "qty": float(shares),
        "horizon_bucket": bucket.value, "entry_date": str(entry_date),
        "advisor_signature": "A1.insider:sig",
        "exits_json": json.dumps(exits, default=str),
        "status": "filled", "created_at": _AS_OF.isoformat(),
        "idea_id": idea_id,
    })
    return idea_id


def _build_engine(tmp_path, monkeypatch, executor, pit, *, provider=None):
    db_path = str(tmp_path / "e.db")
    config = dataclasses.replace(
        load_config(), live_trading=False,
        executor_backend="alpaca_paper" if isinstance(executor, AlpacaAdapter) else "sim",
        alpaca_api_key="key", alpaca_secret_key="secret",
        db_path=db_path, audit_path=str(tmp_path / "a.jsonl"),
        metrics_path=str(tmp_path / "m.jsonl"),
    )
    clock = BacktestClock(_AS_OF)
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    monkeypatch.setattr("arbiter.engine.build_executor", lambda cfg: executor)
    from arbiter.engine import build_engine
    eng = build_engine(config, conn=conn, pit=pit, clock=clock)
    if provider is not None:
        eng.current_price_provider = provider
    return eng, conn, config


class TestFastIterationStop:
    def test_live_price_fires_stop_when_pit_close_is_above(self, tmp_path, monkeypatch):
        """LIVE price below the stop fires early_exit even though daily PIT close is above."""
        ex = SimExecutor(starting_cash=1_000_000.0)
        ex.place(OrderIntent(generate_ulid(), "AAPL", OrderSide.BUY, qty=10.0, limit_price=300.0))
        # Daily PIT close is 305 (ABOVE the stop of ~285 for MEDIUM=5%).
        pit = _pit("AAPL", close=305.0)
        # Live price 270 is BELOW the stop → must fire.
        provider = _FakeProvider({"AAPL": 270.0})
        eng, conn, _ = _build_engine(tmp_path, monkeypatch, ex, pit, provider=provider)
        idea_id = _seed(conn, ticker="AAPL", shares=10, bucket=HorizonBucket.MEDIUM,
                        entry_date=_AS_OF.date() - timedelta(days=10), horizon_days=75)

        eng.run_fast_iteration(_AS_OF)

        assert "AAPL" not in ex.get_positions()
        state = conn.execute("SELECT state FROM ideas WHERE idea_id=?", (idea_id,)).fetchone()["state"]
        assert state == IdeaState.CLOSED.value
        outs = conn.execute("SELECT label_kind FROM outcomes WHERE idea_id=?", (idea_id,)).fetchall()
        # C5: a complete, non-duplicated outcome on a fast-iteration closeout.
        assert len(outs) == 1 and outs[0]["label_kind"] == "early_exit"

    def test_no_stop_when_live_price_above(self, tmp_path, monkeypatch):
        ex = SimExecutor(starting_cash=1_000_000.0)
        ex.place(OrderIntent(generate_ulid(), "AAPL", OrderSide.BUY, qty=10.0, limit_price=300.0))
        pit = _pit("AAPL", close=270.0)  # daily PIT close BELOW stop (would fire)
        provider = _FakeProvider({"AAPL": 305.0})  # live ABOVE stop → no fire
        eng, conn, _ = _build_engine(tmp_path, monkeypatch, ex, pit, provider=provider)
        _seed(conn, ticker="AAPL", shares=10, bucket=HorizonBucket.MEDIUM,
              entry_date=_AS_OF.date() - timedelta(days=10), horizon_days=75)

        eng.run_fast_iteration(_AS_OF)

        # Live price (305) used, NOT the daily PIT close (270) → position held.
        assert "AAPL" in ex.get_positions()

    def test_none_live_price_falls_back_to_pit(self, tmp_path, monkeypatch):
        ex = SimExecutor(starting_cash=1_000_000.0)
        ex.place(OrderIntent(generate_ulid(), "AAPL", OrderSide.BUY, qty=10.0, limit_price=300.0))
        pit = _pit("AAPL", close=270.0)  # below stop → fires via fallback
        provider = _FakeProvider({"AAPL": None})
        eng, conn, _ = _build_engine(tmp_path, monkeypatch, ex, pit, provider=provider)
        _seed(conn, ticker="AAPL", shares=10, bucket=HorizonBucket.MEDIUM,
              entry_date=_AS_OF.date() - timedelta(days=10), horizon_days=75)

        eng.run_fast_iteration(_AS_OF)
        assert "AAPL" not in ex.get_positions()  # daily PIT fallback fired the stop

    def test_no_entries_or_signals_on_fast_iteration(self, tmp_path, monkeypatch):
        """Fast iteration places NO entries even when signals exist."""
        ex = SimExecutor(starting_cash=1_000_000.0)
        pit = _pit("AAPL", close=305.0)
        eng, conn, _ = _build_engine(tmp_path, monkeypatch, ex, pit,
                                     provider=_FakeProvider({"AAPL": 305.0}))
        # No positions, no ideas → fast iteration is a clean no-op (no orders).
        before = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
        eng.run_fast_iteration(_AS_OF)
        after = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
        assert after == before


class TestC0PITPurity:
    def test_sim_yields_null_provider(self, tmp_path, monkeypatch):
        ex = SimExecutor(starting_cash=1_000.0)
        pit = _pit("AAPL", close=100.0)
        eng, _, _ = _build_engine(tmp_path, monkeypatch, ex, pit)
        assert isinstance(eng.current_price_provider, NullCurrentPriceProvider)

    def test_backtest_clock_with_alpaca_paper_yields_null(self, tmp_path):
        """C0: backtest config WITH executor_backend=alpaca_paper → Null provider."""
        from arbiter.engine import build_engine
        db_path = str(tmp_path / "bt.db")
        config = dataclasses.replace(
            load_config(), live_trading=False, executor_backend="alpaca_paper",
            alpaca_api_key="key", alpaca_secret_key="secret", db_path=db_path,
            audit_path=str(tmp_path / "a.jsonl"), metrics_path=str(tmp_path / "m.jsonl"),
        )
        conn = get_connection(db_path)
        run_migrations(conn, applied_at=_AS_OF.isoformat())
        pit = _pit("AAPL", close=100.0)
        eng = build_engine(config, conn=conn, pit=pit, clock=BacktestClock(_AS_OF))
        assert isinstance(eng.current_price_provider, NullCurrentPriceProvider)

    def test_live_clock_alpaca_paper_yields_live_source(self, tmp_path):
        from arbiter.engine import build_engine
        db_path = str(tmp_path / "live.db")
        config = dataclasses.replace(
            load_config(), live_trading=False, executor_backend="alpaca_paper",
            alpaca_api_key="key", alpaca_secret_key="secret", db_path=db_path,
            audit_path=str(tmp_path / "a.jsonl"), metrics_path=str(tmp_path / "m.jsonl"),
        )
        conn = get_connection(db_path)
        run_migrations(conn, applied_at=_AS_OF.isoformat())
        pit = _pit("AAPL", close=100.0)
        eng = build_engine(config, conn=conn, pit=pit, clock=Clock())
        assert isinstance(eng.current_price_provider, AlpacaCurrentPriceSource)
