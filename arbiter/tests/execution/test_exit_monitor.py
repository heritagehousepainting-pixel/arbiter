"""Exit / sell monitor tests (sub-project #2) — OFFLINE.

Fake PIT (FixtureSource), BacktestClock, SimExecutor.  No network.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.connection import get_connection
from arbiter.db.helpers import generate_ulid, insert_row
from arbiter.db.migrate import run_migrations
from arbiter.execution import exit_monitor
from arbiter.execution.exit_monitor import (
    ExitDecision,
    evaluate_triggers,
    recompute_stop,
    run_exit_monitor,
)
from arbiter.policy.exits import _STOP_LOSS_BY_BUCKET, compute_exits
from arbiter.shared.executor import OrderIntent
from arbiter.shared.sim_executor import SimExecutor
from arbiter.contract.seams import Idea
from arbiter.orchestrator import idea_store
from arbiter.types import HorizonBucket, IdeaState, OrderSide

_UTC = timezone.utc
_AS_OF = datetime(2025, 3, 15, 12, 0, 0, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestRecomputeStop:
    def test_recompute_from_avg_price_not_phantom(self):
        # MEDIUM bucket stop = 5%.  avg_price 300 → stop 285, NOT 95 (phantom $100).
        stop = recompute_stop(300.0, HorizonBucket.MEDIUM)
        assert stop == pytest.approx(300.0 * (1 - 0.05))
        assert stop == pytest.approx(285.0)
        assert stop != pytest.approx(95.0)


class TestEvaluateTriggers:
    def _base(self, **kw):
        defaults = dict(
            avg_price=300.0,
            bucket=HorizonBucket.MEDIUM,
            horizon_expiry=date(2099, 1, 1),
            current_price=300.0,
            current_stance=None,
            now=_AS_OF,
        )
        defaults.update(kw)
        return evaluate_triggers(**defaults)

    def test_stop_loss_fires_below_recomputed_stop(self):
        # stop = 285; price 284 breaches.
        d = self._base(current_price=284.0)
        assert d is not None and d.reason == "stop_loss"
        assert d.label_kind == "early_exit"

    def test_no_stop_fire_above_stop(self):
        assert self._base(current_price=290.0) is None

    def test_horizon_fires_on_or_after_expiry(self):
        d = self._base(horizon_expiry=_AS_OF.date())
        assert d is not None and d.reason == "horizon"
        assert d.label_kind == "normal"

    def test_reversal_fires_on_fresh_opposite_stance(self):
        d = self._base(current_stance=-0.5)
        assert d is not None and d.reason == "reversal"
        assert d.label_kind == "reversal"

    def test_reversal_does_not_fire_on_absence(self):
        assert self._base(current_stance=None) is None

    def test_reversal_does_not_fire_on_same_side_stance(self):
        assert self._base(current_stance=0.5) is None

    def test_missing_price_no_stop_fire(self):
        # No price → stop cannot evaluate; nothing else fires either.
        assert self._base(current_price=None) is None

    def test_missing_price_still_allows_horizon(self):
        d = self._base(current_price=None, horizon_expiry=_AS_OF.date())
        assert d is not None and d.reason == "horizon"

    def test_priority_stop_over_reversal_over_horizon(self):
        d = self._base(
            current_price=284.0,  # stop breach
            current_stance=-0.5,  # reversal
            horizon_expiry=_AS_OF.date(),  # horizon
        )
        assert d.reason == "stop_loss"
        d2 = self._base(current_stance=-0.5, horizon_expiry=_AS_OF.date())
        assert d2.reason == "reversal"


# ---------------------------------------------------------------------------
# Orchestrator (sim, synchronous)
# ---------------------------------------------------------------------------

def _migrated_conn(tmp_path):
    db = str(tmp_path / "t.db")
    conn = get_connection(db)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    return conn


def _pit_with(ticker, *, close, spread=0.01):
    """Seed a flat price history for *ticker* + SPY so the labeler can resolve
    entry (idea.as_of+1 open) and exit prices.  ``close`` is the CURRENT close
    used by the monitor's stop check; entry/SPY are seeded flat earlier."""
    fx = FixtureSource()
    pit = PITGateway()
    for f in ("price_close", "price_open", "spread", "beta_252d"):
        pit.register_source(f, fx)

    # Flat early history (covers any idea entry date in the test window).
    early = _AS_OF - timedelta(days=400)
    for t in (ticker, "SPY"):
        fx.add("price_open", t, early, 100.0)
        fx.add("price_close", t, early, 100.0)
        fx.add("spread", t, early, spread)
        fx.add("beta_252d", t, early, 1.0)

    # Current marks at as_of-1: the monitor reads price_close for the stop;
    # the exit label uses the explicit SELL fill price (no PIT close read).
    ts = _AS_OF - timedelta(days=1)
    fx.add("price_close", ticker, ts, close)
    fx.add("price_open", ticker, ts, close)
    fx.add("spread", ticker, ts, spread)
    fx.add("price_close", "SPY", ts, 100.0)
    fx.add("price_open", "SPY", ts, 100.0)
    return pit, fx


def _seed_position_idea_order(
    conn, executor, *, ticker, shares, avg_price, bucket, entry_date,
    horizon_days, idea_id=None, idea_state=IdeaState.MONITORED,
):
    """Seed a held position + a MONITORED idea + a filled BUY order row."""
    # Position in the executor (buy fills the sim).
    executor.place(OrderIntent(generate_ulid(), ticker, OrderSide.BUY,
                               qty=float(shares), limit_price=avg_price))

    idea_id = idea_id or generate_ulid()
    idea = Idea(
        idea_id=idea_id, ticker=ticker, thesis="t",
        horizon_days=horizon_days, state=IdeaState.NASCENT,
        as_of=_AS_OF - timedelta(days=horizon_days),
        dedupe_key=(ticker, bucket.value),
    )
    idea_store.persist_new_idea(conn, idea, created_at=_AS_OF)
    # Force the idea to MONITORED directly (skip FSM legality which is log-only).
    conn.execute("UPDATE ideas SET state=? WHERE idea_id=?",
                 (idea_state.value, idea_id))
    conn.commit()

    exits = compute_exits(bucket=bucket, side=OrderSide.BUY,
                          entry_price=100.0,  # PHANTOM stop on purpose (B0 test)
                          entry_date=entry_date)
    order_id = generate_ulid()
    insert_row(conn, "orders", {
        "order_id": order_id,
        "dedup_hash": generate_ulid(),
        "ticker": ticker,
        "side": OrderSide.BUY.value,
        "qty": float(shares),
        "horizon_bucket": bucket.value,
        "entry_date": str(entry_date),
        "advisor_signature": "A1.insider:sig",
        "exits_json": json.dumps(exits, default=str),
        "status": "filled",
        "created_at": _AS_OF.isoformat(),
        "idea_id": idea_id,
    })
    return idea_id, order_id


def _advisor_id_for(idea):
    return "A1.insider"


class TestRunExitMonitorSim:
    def test_stop_loss_fires_sell_recomputed_from_avg_price(self, tmp_path):
        conn = _migrated_conn(tmp_path)
        ex = SimExecutor(starting_cash=1_000_000.0)
        # avg 300 → stop 285.  Current price 280 → breach.  Phantom stored stop=95.
        idea_id, _ = _seed_position_idea_order(
            conn, ex, ticker="AAPL", shares=10, avg_price=300.0,
            bucket=HorizonBucket.MEDIUM, entry_date=_AS_OF.date() - timedelta(days=10),
            horizon_days=75,
        )
        pit, _ = _pit_with("AAPL", close=280.0)
        clock = BacktestClock(_AS_OF)

        closed = run_exit_monitor(
            conn, ex, pit, clock,
            stance_by_ticker={}, advisor_id_for=_advisor_id_for,
            audit_path=str(tmp_path / "a.jsonl"),
        )
        assert idea_id in closed
        # Position gone, idea CLOSED, one outcome with early_exit.
        assert "AAPL" not in ex.get_positions()
        state = conn.execute("SELECT state FROM ideas WHERE idea_id=?", (idea_id,)).fetchone()["state"]
        assert state == IdeaState.CLOSED.value
        out = conn.execute("SELECT label_kind FROM outcomes WHERE idea_id=?", (idea_id,)).fetchall()
        assert len(out) == 1
        assert out[0]["label_kind"] == "early_exit"

    def test_no_stop_when_price_above_recomputed_stop(self, tmp_path):
        conn = _migrated_conn(tmp_path)
        ex = SimExecutor(starting_cash=1_000_000.0)
        # avg 300 → stop 285.  Current 290 → no breach.  Stored phantom stop=95
        # would (wrongly) never fire either, but the point is recompute is used.
        idea_id, _ = _seed_position_idea_order(
            conn, ex, ticker="AAPL", shares=10, avg_price=300.0,
            bucket=HorizonBucket.MEDIUM, entry_date=_AS_OF.date() - timedelta(days=10),
            horizon_days=75,
        )
        pit, _ = _pit_with("AAPL", close=290.0)
        clock = BacktestClock(_AS_OF)
        closed = run_exit_monitor(conn, ex, pit, clock, stance_by_ticker={},
                                  advisor_id_for=_advisor_id_for)
        assert closed == []
        assert "AAPL" in ex.get_positions()

    def test_horizon_expiry_fires_sell(self, tmp_path):
        conn = _migrated_conn(tmp_path)
        ex = SimExecutor(starting_cash=1_000_000.0)
        # entry_date 80 days ago, MEDIUM horizon = 75 → expired.
        idea_id, _ = _seed_position_idea_order(
            conn, ex, ticker="MSFT", shares=5, avg_price=200.0,
            bucket=HorizonBucket.MEDIUM, entry_date=_AS_OF.date() - timedelta(days=80),
            horizon_days=75,
        )
        pit, _ = _pit_with("MSFT", close=210.0)  # above stop → only horizon fires
        clock = BacktestClock(_AS_OF)
        closed = run_exit_monitor(conn, ex, pit, clock, stance_by_ticker={},
                                  advisor_id_for=_advisor_id_for)
        assert idea_id in closed
        out = conn.execute("SELECT label_kind FROM outcomes WHERE idea_id=?", (idea_id,)).fetchone()
        assert out["label_kind"] == "normal"

    def test_conviction_reversal_fires_on_fresh_opposite(self, tmp_path):
        conn = _migrated_conn(tmp_path)
        ex = SimExecutor(starting_cash=1_000_000.0)
        idea_id, _ = _seed_position_idea_order(
            conn, ex, ticker="NVDA", shares=3, avg_price=500.0,
            bucket=HorizonBucket.MEDIUM, entry_date=_AS_OF.date() - timedelta(days=5),
            horizon_days=75,
        )
        pit, _ = _pit_with("NVDA", close=510.0)  # above stop, not expired
        clock = BacktestClock(_AS_OF)
        closed = run_exit_monitor(
            conn, ex, pit, clock,
            stance_by_ticker={"NVDA": -0.6},  # fresh opposite stance
            advisor_id_for=_advisor_id_for,
        )
        assert idea_id in closed
        out = conn.execute("SELECT label_kind FROM outcomes WHERE idea_id=?", (idea_id,)).fetchone()
        assert out["label_kind"] == "reversal"

    def test_reversal_does_not_fire_absent_opinion(self, tmp_path):
        conn = _migrated_conn(tmp_path)
        ex = SimExecutor(starting_cash=1_000_000.0)
        _seed_position_idea_order(
            conn, ex, ticker="NVDA", shares=3, avg_price=500.0,
            bucket=HorizonBucket.MEDIUM, entry_date=_AS_OF.date() - timedelta(days=5),
            horizon_days=75,
        )
        pit, _ = _pit_with("NVDA", close=510.0)
        clock = BacktestClock(_AS_OF)
        closed = run_exit_monitor(conn, ex, pit, clock, stance_by_ticker={},
                                  advisor_id_for=_advisor_id_for)
        assert closed == []

    def test_sell_qty_is_held_shares_not_notional(self, tmp_path):
        conn = _migrated_conn(tmp_path)
        ex = SimExecutor(starting_cash=1_000_000.0)
        _seed_position_idea_order(
            conn, ex, ticker="AAPL", shares=10, avg_price=300.0,
            bucket=HorizonBucket.MEDIUM, entry_date=_AS_OF.date() - timedelta(days=10),
            horizon_days=75,
        )
        pit, _ = _pit_with("AAPL", close=280.0)
        clock = BacktestClock(_AS_OF)
        cash_before = ex.get_account().cash
        run_exit_monitor(conn, ex, pit, clock, stance_by_ticker={},
                         advisor_id_for=_advisor_id_for)
        sell = conn.execute(
            "SELECT qty FROM orders WHERE ticker='AAPL' AND side='SELL'"
        ).fetchone()
        # qty == 10 shares (NOT 10/price ≈ 0).
        assert sell["qty"] == 10.0
        # cash increased by ~ 10 * sell limit (proceeds).
        assert ex.get_account().cash > cash_before

    def test_sell_slippage_biases_limit_down(self, tmp_path):
        conn = _migrated_conn(tmp_path)
        ex = SimExecutor(starting_cash=1_000_000.0)
        _seed_position_idea_order(
            conn, ex, ticker="AAPL", shares=10, avg_price=300.0,
            bucket=HorizonBucket.MEDIUM, entry_date=_AS_OF.date() - timedelta(days=10),
            horizon_days=75,
        )
        pit, _ = _pit_with("AAPL", close=280.0, spread=0.10)
        clock = BacktestClock(_AS_OF)
        run_exit_monitor(conn, ex, pit, clock, stance_by_ticker={},
                         advisor_id_for=_advisor_id_for)
        # SimExecutor fills at the limit; realized proceeds / shares = limit.
        # Sell limit = 280*(1-0.0005) - 0.5*0.10 < 280.
        report = [r for r in ex._reports if r.side == OrderSide.SELL][-1]
        assert report.avg_fill_price < 280.0

    def test_idempotent_resell_blocked_across_cycles(self, tmp_path):
        conn = _migrated_conn(tmp_path)
        ex = SimExecutor(starting_cash=1_000_000.0)
        _seed_position_idea_order(
            conn, ex, ticker="AAPL", shares=10, avg_price=300.0,
            bucket=HorizonBucket.MEDIUM, entry_date=_AS_OF.date() - timedelta(days=10),
            horizon_days=75,
        )
        pit, _ = _pit_with("AAPL", close=280.0)
        clock = BacktestClock(_AS_OF)
        run_exit_monitor(conn, ex, pit, clock, stance_by_ticker={},
                         advisor_id_for=_advisor_id_for)
        n1 = conn.execute("SELECT COUNT(*) c FROM orders WHERE side='SELL'").fetchone()["c"]
        # Re-run same cycle: position is gone so no re-sell; but force a phantom
        # position back to prove the local-ledger dedup blocks a duplicate SELL.
        ex.place(OrderIntent(generate_ulid(), "AAPL", OrderSide.BUY, qty=10.0, limit_price=300.0))
        run_exit_monitor(conn, ex, pit, clock, stance_by_ticker={},
                         advisor_id_for=_advisor_id_for)
        n2 = conn.execute("SELECT COUNT(*) c FROM orders WHERE side='SELL'").fetchone()["c"]
        # No NEW sell row (same dedup_hash blocked by local ledger).
        assert n2 == n1

    def test_get_positions_failure_skips_monitor(self, tmp_path):
        conn = _migrated_conn(tmp_path)

        class BoomExec(SimExecutor):
            def get_positions(self):  # noqa: D102
                raise RuntimeError("broker flaky")

        ex = BoomExec(starting_cash=1_000_000.0)
        pit, _ = _pit_with("AAPL", close=280.0)
        clock = BacktestClock(_AS_OF)
        closed = run_exit_monitor(conn, ex, pit, clock, stance_by_ticker={},
                                  advisor_id_for=_advisor_id_for)
        assert closed == []
        assert conn.execute("SELECT COUNT(*) c FROM orders WHERE side='SELL'").fetchone()["c"] == 0


def _pit_current_mark_only(ticker, *, close, spread=0.01):
    """PIT with ONLY the current close/open mark for *ticker* (used by the
    monitor's stop check) — NO early entry-open / SPY history.  The exit-label
    therefore raises LookupError (missing entry-open + SPY bars).  Returns
    (pit, fx) so the caller can BACKFILL the missing bars before the retry."""
    fx = FixtureSource()
    pit = PITGateway()
    for f in ("price_close", "price_open", "spread", "beta_252d"):
        pit.register_source(f, fx)
    ts = _AS_OF - timedelta(days=1)
    fx.add("price_close", ticker, ts, close)
    fx.add("price_open", ticker, ts, close)
    fx.add("spread", ticker, ts, spread)
    return pit, fx


class TestRunExitMonitorCloseoutRetry:
    """P1: a filled SELL whose labeler raises LookupError must NOT strand the
    idea — it stays MONITORED with its SELL row + no outcome, no crash; a later
    close-out (bars present) labels + closes it exactly once."""

    def test_lookup_error_leaves_monitored_then_retry_closes_once(self, tmp_path):
        conn = _migrated_conn(tmp_path)
        ex = SimExecutor(starting_cash=1_000_000.0)
        horizon_days = 75
        # avg 300 → stop 285; current 280 → stop breach fires the SELL.
        idea_id, _ = _seed_position_idea_order(
            conn, ex, ticker="AAPL", shares=10, avg_price=300.0,
            bucket=HorizonBucket.MEDIUM,
            entry_date=_AS_OF.date() - timedelta(days=10),
            horizon_days=horizon_days,
        )
        # PIT has the current mark (stop fires, SELL fills) but NO entry-open /
        # SPY bars → outcome_labeler.label raises LookupError.
        pit, fx = _pit_current_mark_only("AAPL", close=280.0)
        clock = BacktestClock(_AS_OF)

        closed = run_exit_monitor(
            conn, ex, pit, clock, stance_by_ticker={},
            advisor_id_for=_advisor_id_for,
            audit_path=str(tmp_path / "a.jsonl"),
        )

        # No crash; close-out skipped (label raised) — idea NOT reported closed.
        assert closed == []
        # The SELL DID execute (sim): position gone, SELL row persisted FILLED.
        assert "AAPL" not in ex.get_positions()
        sell = conn.execute(
            "SELECT status FROM orders WHERE ticker='AAPL' AND side='SELL'"
        ).fetchall()
        assert len(sell) == 1 and sell[0]["status"] == "filled"
        # Idea STILL MONITORED, NO outcome written (not stranded CLOSED).
        state = conn.execute(
            "SELECT state FROM ideas WHERE idea_id=?", (idea_id,)
        ).fetchone()["state"]
        assert state == IdeaState.MONITORED.value
        assert conn.execute(
            "SELECT COUNT(*) c FROM outcomes WHERE idea_id=?", (idea_id,)
        ).fetchone()["c"] == 0

        # --- Backfill the missing PIT bars so a later cycle CAN label. ---
        # Entry day = next trading day after idea.as_of (_AS_OF - horizon_days).
        idea_as_of = _AS_OF - timedelta(days=horizon_days)
        for day_offset in range(-5, 5):
            d = idea_as_of + timedelta(days=day_offset)
            for t in ("AAPL", "SPY"):
                fx.add("price_open", t, d, 100.0)
                fx.add("price_close", t, d, 100.0)
                fx.add("beta_252d", t, d, 1.0)
        # SPY close at the exit cutoff (now) so r_spy resolves.
        fx.add("price_close", "SPY", _AS_OF - timedelta(days=1), 100.0)
        fx.add("price_open", "SPY", _AS_OF - timedelta(days=1), 100.0)

        # --- Next cycle: the close-out retry sweep labels + closes the idea. ---
        # Position is already gone, so this exercises the dedicated retry path.
        closed2 = run_exit_monitor(
            conn, ex, pit, clock, stance_by_ticker={},
            advisor_id_for=_advisor_id_for,
            audit_path=str(tmp_path / "a.jsonl"),
        )
        assert idea_id in closed2
        state2 = conn.execute(
            "SELECT state FROM ideas WHERE idea_id=?", (idea_id,)
        ).fetchone()["state"]
        assert state2 == IdeaState.CLOSED.value
        outs = conn.execute(
            "SELECT label_kind FROM outcomes WHERE idea_id=?", (idea_id,)
        ).fetchall()
        # Exactly ONE outcome, labeled early_exit (the stop-loss trigger).
        assert len(outs) == 1
        assert outs[0]["label_kind"] == "early_exit"

        # --- A THIRD cycle must NOT double-label (idempotent close-out). ---
        closed3 = run_exit_monitor(
            conn, ex, pit, clock, stance_by_ticker={},
            advisor_id_for=_advisor_id_for,
            audit_path=str(tmp_path / "a.jsonl"),
        )
        assert closed3 == []
        assert conn.execute(
            "SELECT COUNT(*) c FROM outcomes WHERE idea_id=?", (idea_id,)
        ).fetchone()["c"] == 1


class TestStrictSubsetRetry:
    """#5a (E0): a PARTIAL fan-out (advisor 1 written, crash before advisor 2)
    leaves the idea with ≥1 outcome — the OLD ``NOT EXISTS (any outcome)``
    selection would NEVER re-select it.  The strict-subset selection re-selects a
    MONITORED idea whose stored-advisor set ⊊ its linked-opinion-advisor set, so
    the retry writes the missing advisor and then flips CLOSED."""

    def _seed_opinion(self, conn, *, advisor_id, ticker, stance, conf, idea_id, as_of, fp):
        from arbiter.contract.opinion import Opinion
        from arbiter.signals import opinion_store
        from arbiter.types import ConfidenceSource

        op = Opinion(
            advisor_id=advisor_id, ticker=ticker, stance_score=stance,
            confidence=conf, confidence_source=ConfidenceSource.MODELED,
            horizon_days=75, as_of=as_of, rationale="t",
            source_fingerprint=fp, run_group_id="rg",
        )
        opinion_store.persist_opinion(conn, op, idea_id=idea_id, as_of=as_of)

    def test_partial_fanout_reselected_and_completed(self, tmp_path):
        from arbiter.evaluation import outcome_labeler, outcome_store

        conn = _migrated_conn(tmp_path)
        ex = SimExecutor(starting_cash=1_000_000.0)
        horizon_days = 75
        ticker = "AAPL"
        idea_id, _ = _seed_position_idea_order(
            conn, ex, ticker=ticker, shares=10, avg_price=300.0,
            bucket=HorizonBucket.MEDIUM,
            entry_date=_AS_OF.date() - timedelta(days=10),
            horizon_days=horizon_days,
        )
        idea_as_of = _AS_OF - timedelta(days=horizon_days)
        # TWO linked opinions for the idea.
        self._seed_opinion(conn, advisor_id="A1.insider", ticker=ticker, stance=0.9,
                           conf=0.8, idea_id=idea_id, as_of=idea_as_of, fp="fp-i")
        self._seed_opinion(conn, advisor_id="A1.congress", ticker=ticker, stance=0.3,
                           conf=0.5, idea_id=idea_id, as_of=idea_as_of, fp="fp-c")

        # Manually sell so the position is gone + a FILLED SELL row exists, but
        # leave the idea MONITORED (mimics the stranded close-out entry point).
        ex.place(OrderIntent(generate_ulid(), ticker, OrderSide.SELL,
                             qty=10.0, limit_price=280.0))
        insert_row(conn, "orders", {
            "order_id": generate_ulid(), "dedup_hash": generate_ulid(),
            "ticker": ticker, "side": OrderSide.SELL.value, "qty": 10.0,
            "horizon_bucket": HorizonBucket.MEDIUM.value,
            "entry_date": str(_AS_OF.date() - timedelta(days=10)),
            "advisor_signature": "A1.insider:sig",
            "exits_json": json.dumps({"exit_label_kind": "early_exit"}),
            "status": "filled", "created_at": _AS_OF.isoformat(),
        })

        # PIT bars so the labeler succeeds.
        pit, fx = _pit_current_mark_only(ticker, close=280.0)
        for day_offset in range(-5, 5):
            d = idea_as_of + timedelta(days=day_offset)
            for t in (ticker, "SPY"):
                fx.add("price_open", t, d, 100.0)
                fx.add("price_close", t, d, 100.0)
                fx.add("beta_252d", t, d, 1.0)
        fx.add("price_close", "SPY", _AS_OF - timedelta(days=1), 100.0)
        fx.add("price_open", "SPY", _AS_OF - timedelta(days=1), 100.0)

        # Simulate a PARTIAL write: store ONLY advisor 1's outcome.
        idea = idea_store.load_ideas_by_state(conn, {IdeaState.MONITORED})[0]
        partial = outcome_labeler.label(
            idea, pit=pit, cutoff_as_of=_AS_OF, advisor_id="A1.insider",
            advisor_confidence=0.8, stance_score=0.9, exit_price=280.0,
            exit_as_of=_AS_OF, label_kind="early_exit",
        )
        outcome_store.store_outcome(partial, conn, as_of=_AS_OF)
        assert conn.execute(
            "SELECT COUNT(*) c FROM outcomes WHERE idea_id=?", (idea_id,)
        ).fetchone()["c"] == 1
        assert conn.execute(
            "SELECT state FROM ideas WHERE idea_id=?", (idea_id,)
        ).fetchone()["state"] == IdeaState.MONITORED.value

        # Retry sweep: strict-subset re-selects the idea (stored={insider} ⊊
        # linked={insider,congress}) → writes the missing congress outcome, CLOSE.
        clock = BacktestClock(_AS_OF)
        closed = run_exit_monitor(
            conn, ex, pit, clock, stance_by_ticker={},
            advisor_id_for=_advisor_id_for,
            audit_path=str(tmp_path / "a.jsonl"),
        )
        assert idea_id in closed
        rows = conn.execute(
            "SELECT advisor_id FROM outcomes WHERE idea_id=? AND is_superseded=0",
            (idea_id,),
        ).fetchall()
        assert {r["advisor_id"] for r in rows} == {"A1.insider", "A1.congress"}
        assert conn.execute(
            "SELECT state FROM ideas WHERE idea_id=?", (idea_id,)
        ).fetchone()["state"] == IdeaState.CLOSED.value

        # Idempotent: a further sweep writes nothing more.
        closed2 = run_exit_monitor(
            conn, ex, pit, clock, stance_by_ticker={},
            advisor_id_for=_advisor_id_for,
            audit_path=str(tmp_path / "a.jsonl"),
        )
        assert closed2 == []
        assert conn.execute(
            "SELECT COUNT(*) c FROM outcomes WHERE idea_id=?", (idea_id,)
        ).fetchone()["c"] == 2
