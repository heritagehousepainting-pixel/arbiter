"""Same-cycle PIT guarantees for the learning loop (sub-project #4, P2-a / D0+R6).

Two binding guarantees, both offline (BacktestClock + FixtureSource + SimExecutor):

1. STRICT cutoff (D0): an ``outcomes`` row stamped at ``created_at == T`` — exactly
   what the exit monitor / reconcile close-out / end-of-cycle sweep write EARLIER in
   the same ``run_cycle`` — is NOT visible to that same cycle's learning step
   (``load_outcomes_for_learning(conn, T)``).  A row stamped strictly before T IS.

2. SWEEP ORDERING (R6): in a real ``run_cycle`` the outcome sweep runs strictly
   AFTER the learning step (``_build_learning_inputs``).  This locks the
   "you can't grade a decision using its own future result" PIT guarantee by a
   test, not just a comment — if a refactor moves the sweep earlier, this fails.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arbiter.config import load_config
from arbiter.contract.seams import ResolvedOutcome
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.connection import get_connection
from arbiter.db.helpers import generate_ulid
from arbiter.db.migrate import run_migrations
from arbiter.engine import build_engine
from arbiter.evaluation.outcome_store import store_outcome
from arbiter.ingest.writer import write_filing
from arbiter.shared.sim_executor import SimExecutor
from arbiter.trust import store as trust_store

_UTC = timezone.utc
T = datetime(2025, 6, 1, 12, 0, 0, tzinfo=_UTC)
INSIDER = "A1.insider"


def _conn() -> sqlite3.Connection:
    c = get_connection(":memory:")
    run_migrations(c)
    return c


def _store_outcome(conn: sqlite3.Connection, *, created_at: datetime, idea_id: str) -> None:
    o = ResolvedOutcome(
        idea_id=idea_id,
        advisor_id=INSIDER,
        ticker="AAA",
        alpha_bps=120.0,
        binary=1,
        advisor_confidence=0.8,
        stance_score=1.0,
        abstained=False,
        horizon_days=180,
        label_kind="normal",
    )
    store_outcome(o, conn, as_of=created_at)


# ---------------------------------------------------------------------------
# 1. Strict same-cycle cutoff (D0)
# ---------------------------------------------------------------------------

def test_outcome_stamped_at_T_excluded_from_same_cycle_learning():
    """An outcome resolved at exactly T (the cycle's now) is NOT used by this
    cycle's learning step; one resolved before T is."""
    conn = _conn()
    # Same-cycle close-out: stamped at exactly T (what the exit monitor does).
    _store_outcome(conn, created_at=T, idea_id="idea-same-cycle")
    # Prior-cycle outcome: stamped strictly before T.
    _store_outcome(conn, created_at=T - timedelta(days=1), idea_id="idea-prior")

    by_advisor = trust_store.load_outcomes_for_learning(conn, T)
    idea_ids = {o.idea_id for recs in by_advisor.values() for (o, _) in recs}

    # Strict ``<`` cutoff: the T-stamped row is invisible this cycle…
    assert "idea-same-cycle" not in idea_ids
    # …but the prior-cycle row is visible.
    assert "idea-prior" in idea_ids


# ---------------------------------------------------------------------------
# 2. Sweep ordering: sweep runs AFTER learning in run_cycle (R6)
# ---------------------------------------------------------------------------

def _make_filing_ts(days_before: int) -> str:
    return (T - timedelta(days=days_before)).isoformat()


def _seed_cluster_buy(conn: sqlite3.Connection, ticker: str = "AAPL", n: int = 3) -> None:
    for i in range(n):
        raw = {
            "source": "form4",
            "ticker": ticker,
            "person_id": generate_ulid(),
            "filing_ts": _make_filing_ts(5 + i),
            "txn_type": "P",
            "shares": 1000.0,
            "price": 150.0,
            "amount_low": 500_000.0,
            "amount_high": 600_000.0,
            "is_10b5_1": False,
            "is_amendment": False,
            "accession": generate_ulid(),
            "raw_json": None,
        }
        write_filing(conn, raw, lambda: T.isoformat())


def _build_pit(ticker: str = "AAPL") -> PITGateway:
    fixture = FixtureSource()
    ts_seed = T - timedelta(days=1)
    fixture.add("price_close", ticker, ts_seed, 150.0)
    fixture.add("price_open", ticker, ts_seed, 150.0)
    fixture.add("spread", ticker, ts_seed, 0.01)
    fixture.add("adv_20d", ticker, ts_seed, 10_000_000.0)
    pit = PITGateway()
    for src in ("price_close", "price_open", "spread", "adv_20d"):
        pit.register_source(src, fixture)
    return pit


def test_outcome_sweep_runs_after_learning_step(tmp_path: Path):
    """R6: ``run_outcome_sweep`` is invoked strictly AFTER ``_build_learning_inputs``
    within a single ``run_cycle`` — the strict-cutoff PIT guarantee is locked by a
    test, not just a comment/code ordering."""
    db_path = str(tmp_path / "pit.db")
    cfg = dataclasses.replace(
        load_config(),
        live_trading=False,
        executor_backend="sim",
        db_path=db_path,
        audit_path=str(tmp_path / "audit.jsonl"),
        metrics_path=str(tmp_path / "metrics.jsonl"),
    )
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=T.isoformat())
    _seed_cluster_buy(conn)
    eng = build_engine(cfg, conn=conn, pit=_build_pit(), clock=BacktestClock(T))
    assert isinstance(eng.executor, SimExecutor)

    calls: list[str] = []

    orig_learning = eng._build_learning_inputs

    def _spy_learning(now):
        calls.append("learning")
        return orig_learning(now)

    eng._build_learning_inputs = _spy_learning  # type: ignore[method-assign]

    # Spy the sweep at its call site (engine imports outcome_runner locally from
    # arbiter.orchestrator inside run_cycle).
    from arbiter.orchestrator import outcome_runner

    orig_sweep = outcome_runner.run_outcome_sweep

    def _spy_sweep(*args, **kwargs):
        calls.append("sweep")
        return orig_sweep(*args, **kwargs)

    outcome_runner.run_outcome_sweep = _spy_sweep  # type: ignore[assignment]
    try:
        eng.run_cycle(as_of=T)
    finally:
        outcome_runner.run_outcome_sweep = orig_sweep

    assert "learning" in calls, "learning step must run"
    assert "sweep" in calls, "outcome sweep must run"
    # The binding ordering: learning strictly before sweep.
    assert calls.index("learning") < calls.index("sweep")
