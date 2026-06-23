"""SHORT-position support tests (2026-06-22) — OFFLINE.

Covers the three live-found defects + the two reconcile-path corrections needed
for shorts:

  * exit monitor manages shorts: inverted stop, BUY-to-cover, bullish reversal,
    horizon; a profitable short does NOT exit; long behavior unchanged.
  * engine reconcile advances a short's OPENING SELL → MONITORED and closes a
    short's COVER BUY (branch on exit-vs-opening, not side).
  * risk book counts a short's |market value| toward gross.
  * per-position breaker trips on a LOSING short (price up).
  * reconciler: a −3 local net reconciles clean against a −3 broker position.

No network: a purpose-built fake broker holds the short (SimExecutor is
long-only by construction); FakeAlpaca drives the adapter reconcile path.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from arbiter.data.clock import BacktestClock
from arbiter.db.connection import get_connection
from arbiter.db.helpers import generate_ulid, insert_row
from arbiter.db.migrate import run_migrations
from arbiter.contract.seams import Idea
from arbiter.execution import exit_monitor, reconciler
from arbiter.execution.exit_monitor import (
    build_exit_order,
    evaluate_triggers,
    is_exit_order,
    recompute_stop,
    run_exit_monitor,
)
from arbiter.orchestrator import idea_store
from arbiter.policy.exits import _STOP_LOSS_BY_BUCKET, compute_exits
from arbiter.shared.executor import (
    AccountSnapshot,
    Executor,
    ExecutionReport,
    OrderIntent,
    PositionSnapshot,
)
from arbiter.types import HorizonBucket, IdeaState, OrderSide

_UTC = timezone.utc
_AS_OF = datetime(2025, 3, 15, 12, 0, 0, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Fake broker that can HOLD a short and accept a BUY-to-cover (SimExecutor
# cannot — it filters shares>0 and rejects a SELL with no long).
# ---------------------------------------------------------------------------

class FakeShortBroker(Executor):
    name = "fakeshort"

    def __init__(self, positions: dict[str, PositionSnapshot]):
        self._positions = dict(positions)
        self.placed: list[OrderIntent] = []

    def place(self, intent: OrderIntent) -> ExecutionReport:
        self.placed.append(intent)
        price = intent.limit_price or 0.0
        pos = self._positions.get(intent.ticker)
        # BUY-to-cover reduces a short toward zero (removes it when flat).
        if intent.side == OrderSide.BUY and pos is not None and pos.shares < 0:
            new_shares = pos.shares + intent.qty
            if new_shares >= 0:
                self._positions.pop(intent.ticker, None)
            else:
                self._positions[intent.ticker] = dataclasses.replace(pos, shares=new_shares)
        return ExecutionReport(
            order_id=intent.order_id, ticker=intent.ticker, side=intent.side,
            status="filled", filled_qty=intent.qty, avg_fill_price=price,
            gross_notional=price * intent.qty, realized_pl=0.0, reject_reason="",
            executor=self.name, paper_only=True,
        )

    def cancel(self, order_id: str) -> ExecutionReport:  # pragma: no cover - unused
        raise NotImplementedError

    def get_positions(self) -> dict[str, PositionSnapshot]:
        return dict(self._positions)

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(cash=0.0, buying_power=0.0, realized_pl=0.0,
                               daily_pl=0.0, open_positions=len(self._positions),
                               paper_only=True, equity=0.0)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestRecomputeStopShort:
    def test_short_stop_is_above_entry(self):
        # MEDIUM frac = 5%: short stop = 22 * 1.05 = 23.10 (ABOVE entry).
        stop = recompute_stop(22.0, HorizonBucket.MEDIUM, is_short=True)
        assert stop == pytest.approx(22.0 * 1.05)
        # Long stop stays below entry (regression guard).
        assert recompute_stop(22.0, HorizonBucket.MEDIUM) == pytest.approx(22.0 * 0.95)


class TestEvaluateTriggersShort:
    def _short(self, **kw):
        defaults = dict(
            avg_price=22.0, bucket=HorizonBucket.MEDIUM,
            horizon_expiry=date(2099, 1, 1), current_price=22.0,
            current_stance=None, now=_AS_OF, is_short=True,
        )
        defaults.update(kw)
        return evaluate_triggers(**defaults)

    def test_short_stop_fires_when_price_rises_through_stop(self):
        # stop = 23.10; price 23.5 (ROSE past it) → stop_loss.
        d = self._short(current_price=23.5)
        assert d is not None and d.reason == "stop_loss"

    def test_profitable_short_price_drop_no_exit(self):
        # Price FELL (short in profit) → no stop, no exit.
        assert self._short(current_price=20.0) is None

    def test_short_reversal_fires_on_bullish_opinion(self):
        # A fresh BULLISH (positive) stance flips against the short.
        d = self._short(current_price=22.0, current_stance=0.6)
        assert d is not None and d.reason == "reversal"

    def test_short_reversal_does_not_fire_on_bearish_opinion(self):
        # A bearish stance AGREES with the short → no reversal.
        assert self._short(current_price=22.0, current_stance=-0.6) is None

    def test_short_horizon_fires(self):
        d = self._short(current_price=20.0, horizon_expiry=_AS_OF.date())
        assert d is not None and d.reason == "horizon"


class TestBuildExitOrderShort:
    def test_short_exit_is_buy_to_cover_abs_shares(self):
        pos = PositionSnapshot(ticker="T", shares=-3.0, avg_price=22.0)
        row = {
            "advisor_signature": "A2.mirofish:sig",
            "horizon_bucket": HorizonBucket.MEDIUM.value,
            "entry_date": "2026-06-22",
        }
        order = build_exit_order(position=pos, owning_order_row=row,
                                 exits={"exit_label_kind": "stop_loss"}, now=_AS_OF)
        assert order.side == OrderSide.BUY  # cover, not enlarge
        assert order.qty == 3.0            # abs(shares)

    def test_long_exit_is_still_sell(self):
        pos = PositionSnapshot(ticker="AAPL", shares=10.0, avg_price=300.0)
        row = {
            "advisor_signature": "A1.insider:sig",
            "horizon_bucket": HorizonBucket.MEDIUM.value,
            "entry_date": "2026-06-22",
        }
        order = build_exit_order(position=pos, owning_order_row=row,
                                 exits={"exit_label_kind": "stop_loss"}, now=_AS_OF)
        assert order.side == OrderSide.SELL
        assert order.qty == 10.0


class TestIsExitOrder:
    def test_exit_order_has_label_kind(self):
        assert is_exit_order({"exits_json": json.dumps({"exit_label_kind": "normal"})})

    def test_opening_order_has_no_label_kind(self):
        opening = compute_exits(bucket=HorizonBucket.MEDIUM, side=OrderSide.SELL,
                                entry_price=22.0, entry_date=date(2026, 6, 22))
        assert not is_exit_order({"exits_json": json.dumps(opening, default=str)})


# ---------------------------------------------------------------------------
# Orchestrator (fake short broker, synchronous cover)
# ---------------------------------------------------------------------------

def _migrated_conn(tmp_path):
    from arbiter.data.pit import FixtureSource, PITGateway
    db = str(tmp_path / "t.db")
    conn = get_connection(db)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    return conn


def _pit_with(ticker, *, close, spread=0.01):
    from arbiter.data.pit import FixtureSource, PITGateway
    fx = FixtureSource()
    pit = PITGateway()
    for f in ("price_close", "price_open", "spread", "beta_252d"):
        pit.register_source(f, fx)
    early = _AS_OF - timedelta(days=400)
    for t in (ticker, "SPY"):
        fx.add("price_open", t, early, 22.0)
        fx.add("price_close", t, early, 22.0)
        fx.add("spread", t, early, spread)
        fx.add("beta_252d", t, early, 1.0)
    ts = _AS_OF - timedelta(days=1)
    fx.add("price_close", ticker, ts, close)
    fx.add("price_open", ticker, ts, close)
    fx.add("spread", ticker, ts, spread)
    fx.add("price_close", "SPY", ts, 22.0)
    fx.add("price_open", "SPY", ts, 22.0)
    return pit, fx


def _seed_short(conn, *, ticker, shares, avg_price, bucket, entry_date,
                horizon_days, idea_state=IdeaState.MONITORED):
    """Seed a MONITORED idea + a filled OPENING SELL row (no exit_label_kind)."""
    idea_id = generate_ulid()
    idea = Idea(idea_id=idea_id, ticker=ticker, thesis="t", horizon_days=horizon_days,
                state=IdeaState.NASCENT, as_of=_AS_OF - timedelta(days=horizon_days),
                dedupe_key=(ticker, bucket.value))
    idea_store.persist_new_idea(conn, idea, created_at=_AS_OF)
    conn.execute("UPDATE ideas SET state=? WHERE idea_id=?", (idea_state.value, idea_id))
    conn.commit()
    exits = compute_exits(bucket=bucket, side=OrderSide.SELL, entry_price=avg_price,
                          entry_date=entry_date)
    order_id = generate_ulid()
    insert_row(conn, "orders", {
        "order_id": order_id, "dedup_hash": generate_ulid(), "ticker": ticker,
        "side": OrderSide.SELL.value, "qty": float(abs(shares)),
        "horizon_bucket": bucket.value, "entry_date": str(entry_date),
        "advisor_signature": "A2.mirofish:sig",
        "exits_json": json.dumps(exits, default=str),
        "status": "filled", "created_at": _AS_OF.isoformat(), "idea_id": idea_id,
    })
    return idea_id, order_id


def _advisor_id_for(idea):
    return "A2.mirofish"


class TestRunExitMonitorShort:
    def test_short_stop_fires_buy_to_cover_and_closes(self, tmp_path):
        conn = _migrated_conn(tmp_path)
        ex = FakeShortBroker({"T": PositionSnapshot("T", -3.0, 22.0)})
        idea_id, _ = _seed_short(
            conn, ticker="T", shares=-3, avg_price=22.0, bucket=HorizonBucket.MEDIUM,
            entry_date=_AS_OF.date() - timedelta(days=10), horizon_days=75)
        # avg 22 → short stop 23.10; current 24 → ROSE through → stop.
        pit, _ = _pit_with("T", close=24.0)
        clock = BacktestClock(_AS_OF)

        closed = run_exit_monitor(conn, ex, pit, clock, stance_by_ticker={},
                                  advisor_id_for=_advisor_id_for,
                                  audit_path=str(tmp_path / "a.jsonl"))
        assert idea_id in closed
        # The exit order placed was a BUY-to-cover for 3 shares.
        assert len(ex.placed) == 1
        assert ex.placed[0].side == OrderSide.BUY
        assert ex.placed[0].qty == 3
        assert "T" not in ex.get_positions()  # covered
        # Idea CLOSED with the stop trigger's label.
        state = conn.execute("SELECT state FROM ideas WHERE idea_id=?", (idea_id,)).fetchone()["state"]
        assert state == IdeaState.CLOSED.value
        order_row = conn.execute(
            "SELECT side, qty FROM orders WHERE ticker='T' AND side='BUY'").fetchone()
        assert order_row["side"] == "BUY" and order_row["qty"] == 3.0

    def test_profitable_short_no_exit(self, tmp_path):
        conn = _migrated_conn(tmp_path)
        ex = FakeShortBroker({"T": PositionSnapshot("T", -3.0, 22.0)})
        _seed_short(conn, ticker="T", shares=-3, avg_price=22.0, bucket=HorizonBucket.MEDIUM,
                    entry_date=_AS_OF.date() - timedelta(days=5), horizon_days=75)
        pit, _ = _pit_with("T", close=20.0)  # price FELL → short in profit
        clock = BacktestClock(_AS_OF)
        closed = run_exit_monitor(conn, ex, pit, clock, stance_by_ticker={},
                                  advisor_id_for=_advisor_id_for)
        assert closed == []
        assert ex.placed == []
        assert "T" in ex.get_positions()

    def test_short_reversal_on_bullish_opinion(self, tmp_path):
        conn = _migrated_conn(tmp_path)
        ex = FakeShortBroker({"UBER": PositionSnapshot("UBER", -1.0, 72.0)})
        idea_id, _ = _seed_short(
            conn, ticker="UBER", shares=-1, avg_price=72.0, bucket=HorizonBucket.MEDIUM,
            entry_date=_AS_OF.date() - timedelta(days=5), horizon_days=75)
        pit, _ = _pit_with("UBER", close=72.0)  # at entry → no stop
        clock = BacktestClock(_AS_OF)
        closed = run_exit_monitor(conn, ex, pit, clock,
                                  stance_by_ticker={"UBER": 0.6},  # bullish flip
                                  advisor_id_for=_advisor_id_for,
                                  audit_path=str(tmp_path / "a.jsonl"))
        assert idea_id in closed
        assert ex.placed[0].side == OrderSide.BUY
        out = conn.execute("SELECT label_kind FROM outcomes WHERE idea_id=?", (idea_id,)).fetchone()
        assert out["label_kind"] == "reversal"


# ---------------------------------------------------------------------------
# Risk book + per-position breaker (safety_ops)
# ---------------------------------------------------------------------------

class TestRiskBookShort:
    def test_short_counts_abs_market_value_in_gross(self, tmp_path):
        from arbiter.engine import safety_ops

        class _Eng:
            current_price_provider = None
            class pit:  # noqa: N801
                @staticmethod
                def get(field, ticker, now):
                    return 24.0

        snap = PositionSnapshot("T", -3.0, 22.0)
        mv = safety_ops.position_market_value(_Eng(), "T", snap, _AS_OF)
        # |−3| × 24 = 72 (positive exposure), NOT −72.
        assert mv == pytest.approx(72.0)


class TestPerPositionBreakerShort:
    """A LOSING short (price up) must be able to trip the per-position intraday
    breaker.  A signed long-only P&L formula would read a rising price as a GAIN
    and never trip — the sign-flip fixes that."""

    def _eng(self, conn, ex, current_price):
        from arbiter.safety.breakers import CircuitBreaker

        class _Provider:
            def current_price(self, ticker):
                return current_price

        class _Eng:
            pass
        e = _Eng()
        e.conn = conn
        e.clock = BacktestClock(_AS_OF)
        e.breaker = CircuitBreaker()
        e.executor = ex
        e.current_price_provider = _Provider()
        class _Cfg:
            audit_path = None
        e.config = _Cfg()
        return e

    def test_losing_short_trips_per_position(self, tmp_path):
        from arbiter.engine import safety_ops

        conn = _migrated_conn(tmp_path)
        ex = FakeShortBroker({"T": PositionSnapshot("T", -3.0, 22.0)})
        eng = self._eng(conn, ex, current_price=24.0)  # price UP → short losing ~9%
        acct = AccountSnapshot(cash=0.0, buying_power=0.0, realized_pl=0.0,
                               daily_pl=0.0, open_positions=1, paper_only=True, equity=10000.0)
        safety_ops.check_portfolio_breakers(eng, acct, _AS_OF)
        assert eng.breaker.is_tripped("per_position_intraday", conn)

    def test_profitable_short_does_not_trip(self, tmp_path):
        from arbiter.engine import safety_ops

        conn = _migrated_conn(tmp_path)
        ex = FakeShortBroker({"T": PositionSnapshot("T", -3.0, 22.0)})
        eng = self._eng(conn, ex, current_price=20.0)  # price DOWN → short winning
        acct = AccountSnapshot(cash=0.0, buying_power=0.0, realized_pl=0.0,
                               daily_pl=0.0, open_positions=1, paper_only=True, equity=10000.0)
        safety_ops.check_portfolio_breakers(eng, acct, _AS_OF)
        assert not eng.breaker.is_tripped("per_position_intraday", conn)


# ---------------------------------------------------------------------------
# Reconciler (already-net BUY−SELL, must KEEP shorts)
# ---------------------------------------------------------------------------

class TestReconcilerShort:
    def test_short_net_reconciles_clean(self, tmp_path):
        conn = _migrated_conn(tmp_path)
        # A net −3 short in the local ledger (one filled SELL of 3, no BUY).
        insert_row(conn, "orders", {
            "order_id": generate_ulid(), "dedup_hash": generate_ulid(), "ticker": "T",
            "side": OrderSide.SELL.value, "qty": 3.0, "horizon_bucket": "MEDIUM",
            "entry_date": "2026-06-22", "advisor_signature": "A2.mirofish:sig",
            "exits_json": "{}", "status": "filled", "created_at": _AS_OF.isoformat(),
        })
        ex = FakeShortBroker({"T": PositionSnapshot("T", -3.0, 22.0)})
        result = reconciler.reconcile(conn, ex, as_of=_AS_OF)
        assert result.clean, [d.detail for d in result.divergences]
        assert "T" in result.local_tickers


# ---------------------------------------------------------------------------
# Engine reconcile path (alpaca_paper) — the LIVE path for shorts.
# ---------------------------------------------------------------------------

def _fake_filled(fake, order_id, *, symbol, qty, price):
    fake.orders[order_id] = {
        "id": order_id, "client_order_id": order_id, "symbol": symbol,
        "qty": str(qty), "filled_qty": str(qty), "filled_avg_price": str(price),
        "limit_price": str(price), "status": "filled",
    }


class TestEngineReconcileShort:
    """A short OPENS with a SELL (must advance → MONITORED) and COVERS with a
    BUY (must close-out).  Branching on exit-vs-opening, not side, is what makes
    this correct — the old side-only branch sent a short's opening SELL down the
    close-out path, stranding the idea pre-MONITORED (exactly the live bug)."""

    def _adapter(self, fake):
        from arbiter.config import load_config
        from arbiter.execution.alpaca_adapter import AlpacaAdapter
        cfg = dataclasses.replace(load_config(), live_trading=False,
                                  executor_backend="alpaca_paper",
                                  alpaca_api_key="key", alpaca_secret_key="secret",
                                  kill_switch_url="")
        return AlpacaAdapter(config=cfg, http_post=fake.http_post,
                             http_get=fake.http_get, http_delete=fake.http_delete)

    def test_short_opening_sell_advances_idea_to_monitored(self, tmp_path, monkeypatch):
        from tests.execution._fake_alpaca import FakeAlpaca
        from tests.execution.test_exit_monitor_engine import _build_engine, _pit
        from arbiter.engine import reconcile as eng_reconcile

        fake = FakeAlpaca(cash=1_000_000.0)
        ex = self._adapter(fake)
        pit = _pit("T", close=22.0)
        eng, conn, _ = _build_engine(tmp_path, monkeypatch, ex, pit)
        # Idea sits pre-MONITORED (FINAL_DECIDED) with a PENDING opening SELL.
        idea_id, order_id = _seed_short(
            conn, ticker="T", shares=-3, avg_price=22.0, bucket=HorizonBucket.MEDIUM,
            entry_date=_AS_OF.date() - timedelta(days=1), horizon_days=75,
            idea_state=IdeaState.FINAL_DECIDED)
        conn.execute("UPDATE orders SET status='pending' WHERE order_id=?", (order_id,))
        conn.commit()
        _fake_filled(fake, order_id, symbol="T", qty=3, price=22.0)

        eng_reconcile.reconcile_pending_orders(eng, _AS_OF)

        # Opening SELL fill ADVANCES the idea (NOT close-out) — no outcome yet.
        state = conn.execute("SELECT state FROM ideas WHERE idea_id=?", (idea_id,)).fetchone()["state"]
        assert state == IdeaState.MONITORED.value
        assert conn.execute("SELECT COUNT(*) c FROM outcomes WHERE idea_id=?", (idea_id,)).fetchone()["c"] == 0
        assert conn.execute("SELECT status FROM orders WHERE order_id=?", (order_id,)).fetchone()["status"] == "filled"

    def test_short_cover_buy_closes_idea(self, tmp_path, monkeypatch):
        from tests.execution._fake_alpaca import FakeAlpaca
        from tests.execution.test_exit_monitor_engine import _build_engine, _pit
        from arbiter.engine import reconcile as eng_reconcile

        fake = FakeAlpaca(cash=1_000_000.0)
        ex = self._adapter(fake)
        pit = _pit("T", close=24.0)
        eng, conn, _ = _build_engine(tmp_path, monkeypatch, ex, pit)
        # MONITORED short + filled opening SELL + a PENDING cover BUY (exit order).
        idea_id, _ = _seed_short(
            conn, ticker="T", shares=-3, avg_price=22.0, bucket=HorizonBucket.MEDIUM,
            entry_date=_AS_OF.date() - timedelta(days=10), horizon_days=75,
            idea_state=IdeaState.MONITORED)
        cover_id = generate_ulid()
        insert_row(conn, "orders", {
            "order_id": cover_id, "dedup_hash": generate_ulid(), "ticker": "T",
            "side": OrderSide.BUY.value, "qty": 3.0, "horizon_bucket": "MEDIUM",
            "entry_date": str(_AS_OF.date() - timedelta(days=10)),
            "advisor_signature": "A2.mirofish:sig",
            "exits_json": json.dumps({"exit_label_kind": "early_exit"}),
            "status": "pending", "created_at": _AS_OF.isoformat(),
        })
        _fake_filled(fake, cover_id, symbol="T", qty=3, price=24.0)

        eng_reconcile.reconcile_pending_orders(eng, _AS_OF)

        # Cover BUY fill drives close-out → idea CLOSED + one outcome.
        state = conn.execute("SELECT state FROM ideas WHERE idea_id=?", (idea_id,)).fetchone()["state"]
        assert state == IdeaState.CLOSED.value
        outs = conn.execute("SELECT label_kind FROM outcomes WHERE idea_id=?", (idea_id,)).fetchall()
        assert len(outs) == 1 and outs[0]["label_kind"] == "early_exit"
