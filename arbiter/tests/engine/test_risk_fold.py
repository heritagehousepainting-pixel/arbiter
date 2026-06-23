"""Engine-level notional-vs-realized risk-fold tests (Wave 2, work item 3).

Verifies the engine folds REALIZED notional (avg_fill_price × filled_qty) into
the RiskBook on a PARTIAL fill, and the REQUESTED notional (order.qty) on a full
fill.  The fold is dormant under the stock SimExecutor (which always fills
fully), so these tests inject a partial-fill executor and SPY on RiskBook.add to
observe exactly what notional was folded.

All offline: temp SQLite + BacktestClock + FixtureSource PIT.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import arbiter.policy.book as _book_mod
from arbiter.config import load_config
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.connection import get_connection
from arbiter.db.helpers import generate_ulid
from arbiter.db.migrate import run_migrations
from arbiter.engine import build_engine
from arbiter.ingest.writer import write_filing
from arbiter.shared.executor import ExecutionReport, OrderIntent
from arbiter.types import OrderSide

_UTC = timezone.utc
_AS_OF = datetime(2025, 3, 15, 12, 0, 0, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_cluster_buy(conn, clock_fn, ticker="AAPL", n_buyers=3, amount=500_000.0):
    for i in range(n_buyers):
        raw = {
            "source": "form4",
            "ticker": ticker,
            "person_id": generate_ulid(),
            "filing_ts": (_AS_OF - timedelta(days=5 + i)).isoformat(),
            "txn_type": "P",
            "shares": 1000.0,
            "price": 150.0,
            "amount_low": amount,
            "amount_high": amount * 1.2,
            "is_10b5_1": False,
            "is_amendment": False,
            "accession": generate_ulid(),
            "raw_json": None,
        }
        write_filing(conn, raw, clock_fn)


def _build_pit(ticker="AAPL"):
    fixture = FixtureSource()
    ts_seed = _AS_OF - timedelta(days=1)
    fixture.add("price_close", ticker, ts_seed, 150.0)
    fixture.add("price_open", ticker, ts_seed, 150.0)
    fixture.add("spread", ticker, ts_seed, 0.01)
    fixture.add("adv_20d", ticker, ts_seed, 10_000_000.0)
    pit = PITGateway()
    for src in ("price_close", "price_open", "spread", "adv_20d"):
        pit.register_source(src, fixture)
    return pit


def _make_engine(tmp_path: Path):
    db_path = str(tmp_path / "fold.db")
    config = dataclasses.replace(
        load_config(),
        live_trading=False,
        executor_backend="sim",
        db_path=db_path,
        audit_path=str(tmp_path / "audit.jsonl"),
        metrics_path=str(tmp_path / "metrics.jsonl"),
    )
    clock = BacktestClock(_AS_OF)
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    _seed_cluster_buy(conn, lambda: _AS_OF.isoformat())
    pit = _build_pit()
    eng = build_engine(config, conn=conn, pit=pit, clock=clock)
    return eng, conn


def _make_partial_place(real_place, fill_fraction, captured):
    """Wrap an executor.place to return a PARTIAL fill of ``fill_fraction``.

    Records the realized notional (avg_fill_price × filled_qty) in ``captured``
    so the test can pin the fold against it.  SELLs pass through unchanged.
    """
    def _place(intent: OrderIntent) -> ExecutionReport:
        if intent.side is not OrderSide.BUY:
            return real_place(intent)
        fill_price = intent.limit_price if intent.limit_price is not None else 0.0
        filled = intent.qty * fill_fraction
        captured["realized_notional"] = fill_price * filled
        captured["requested_shares"] = intent.qty
        return ExecutionReport(
            order_id=intent.order_id,
            ticker=intent.ticker,
            side=OrderSide.BUY,
            status="partial",
            filled_qty=filled,
            avg_fill_price=fill_price,
            gross_notional=fill_price * filled,
            realized_pl=None,
            reject_reason="",
            executor="partial_sim",
            paper_only=True,
        )
    return _place


def _spy_riskbook_add(monkeypatch):
    """Patch RiskBook.add to record every (ticker, notional) folded; returns list."""
    calls: list[tuple[str, float]] = []
    real_add = _book_mod.RiskBook.add

    def _spy(self, ticker, notional_usd):
        calls.append((ticker, float(notional_usd)))
        return real_add(self, ticker, notional_usd)

    monkeypatch.setattr(_book_mod.RiskBook, "add", _spy)
    return calls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_fold_uses_filled_notional_on_partial(tmp_path, monkeypatch):
    """A 40% partial fill folds the REALIZED notional (avg_fill_price × filled_qty)."""
    eng, conn = _make_engine(tmp_path)
    calls = _spy_riskbook_add(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(
        eng.executor, "place",
        _make_partial_place(eng.executor.place, 0.4, captured),
    )

    eng.run_cycle(as_of=_AS_OF)

    folds = [n for (t, n) in calls if t == "AAPL"]
    assert folds, "expected at least one fold for AAPL"
    # The fold must equal the realized notional, NOT the requested.
    assert folds[-1] == pytest.approx(captured["realized_notional"])
    assert folds[-1] > 0.0


def test_fold_uses_requested_on_full_fill(tmp_path, monkeypatch):
    """A full fill (stock SimExecutor) folds the REQUESTED notional unchanged."""
    eng, conn = _make_engine(tmp_path)
    calls = _spy_riskbook_add(monkeypatch)

    eng.run_cycle(as_of=_AS_OF)

    folds = [n for (t, n) in calls if t == "AAPL"]
    assert folds, "expected a fold on a full fill"
    # On a full fill the engine folds float(order.qty) — the requested notional,
    # which is a positive whole-dollar amount from position sizing.
    assert folds[-1] > 0.0


def test_fold_regression_two_orders(tmp_path, monkeypatch):
    """A partial fold is strictly SMALLER than the full-fill fold of the SAME order.

    Same engine config + cash → identical position sizing → identical requested
    notional.  The only difference is fill ratio, so the observable behavior
    change (partial folds less consumed headroom) is isolated.
    """
    # Full-fill run → folds requested notional.
    eng_full, _ = _make_engine(tmp_path / "full")
    calls_full = _spy_riskbook_add(monkeypatch)
    eng_full.run_cycle(as_of=_AS_OF)
    full_fold = max(n for (t, n) in calls_full if t == "AAPL")

    # Partial-fill run (30%) on a fresh, identically-configured engine.
    monkeypatch.undo()
    eng_part, _ = _make_engine(tmp_path / "part")
    calls_part = _spy_riskbook_add(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(
        eng_part.executor, "place",
        _make_partial_place(eng_part.executor.place, 0.3, captured),
    )
    eng_part.run_cycle(as_of=_AS_OF)
    part_fold = max(n for (t, n) in calls_part if t == "AAPL")

    assert part_fold < full_fold, (
        f"partial fold {part_fold} must be < full/requested fold {full_fold}"
    )
    # ~30% of the full (requested) notional, modulo limit-price slippage.
    assert part_fold == pytest.approx(full_fold * 0.3, rel=0.06)
