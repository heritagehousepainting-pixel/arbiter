"""Tier-2 #8 — counterfactual outcome sweep for unexecuted FINAL_DECIDED ideas."""
from __future__ import annotations

import json
from datetime import timedelta

from arbiter.contract.seams import Idea
from arbiter.data.clock import BacktestClock
from arbiter.db.helpers import generate_ulid, insert_row
from arbiter.orchestrator import idea_store
from arbiter.orchestrator.outcome_runner import run_unexecuted_sweep
from arbiter.types import HorizonBucket, IdeaState, OrderSide

from tests.execution.test_exit_monitor import _AS_OF, _migrated_conn, _pit_with


def _advisor_id_for(idea):
    return "A1.insider"


def _seed_decided_idea(conn, *, ticker="NVDA", horizon_days=7, days_old=10):
    """A FINAL_DECIDED idea whose horizon elapsed ``days_old - horizon_days`` ago."""
    idea_id = generate_ulid()
    idea = Idea(
        idea_id=idea_id, ticker=ticker, thesis="t", horizon_days=horizon_days,
        state=IdeaState.NASCENT, as_of=_AS_OF - timedelta(days=days_old),
        dedupe_key=(ticker, HorizonBucket.SHORT.value),
    )
    idea_store.persist_new_idea(conn, idea, created_at=_AS_OF)
    conn.execute(
        "UPDATE ideas SET state=? WHERE idea_id=?",
        (IdeaState.FINAL_DECIDED.value, idea_id),
    )
    conn.commit()
    return idea_id


def _sweep(conn, pit):
    return run_unexecuted_sweep(
        conn, pit=pit, clock=BacktestClock(_AS_OF),
        advisor_id_for=_advisor_id_for,
    )


def test_elapsed_unexecuted_idea_labeled_and_abandoned(tmp_path):
    conn = _migrated_conn(tmp_path)
    pit, _ = _pit_with("NVDA", close=100.0)
    idea_id = _seed_decided_idea(conn)

    oids = _sweep(conn, pit)

    assert oids, "expected at least one counterfactual outcome"
    state = conn.execute(
        "SELECT state FROM ideas WHERE idea_id=?", (idea_id,)
    ).fetchone()["state"]
    assert state == IdeaState.ABANDONED.value
    kinds = {
        r["label_kind"]
        for r in conn.execute("SELECT label_kind FROM outcomes").fetchall()
    }
    assert kinds == {"counterfactual"}


def test_unelapsed_idea_left_alone(tmp_path):
    conn = _migrated_conn(tmp_path)
    pit, _ = _pit_with("NVDA", close=100.0)
    idea_id = _seed_decided_idea(conn, horizon_days=90, days_old=10)

    assert _sweep(conn, pit) == []
    assert conn.execute(
        "SELECT state FROM ideas WHERE idea_id=?", (idea_id,)
    ).fetchone()["state"] == IdeaState.FINAL_DECIDED.value


def test_idea_with_live_order_is_skipped(tmp_path):
    """A pending order owns the lifecycle — the sweep must not touch the idea."""
    conn = _migrated_conn(tmp_path)
    pit, _ = _pit_with("NVDA", close=100.0)
    idea_id = _seed_decided_idea(conn)
    insert_row(conn, "orders", {
        "order_id": generate_ulid(), "dedup_hash": generate_ulid(),
        "ticker": "NVDA", "side": OrderSide.BUY.value, "qty": 1.0,
        "horizon_bucket": HorizonBucket.SHORT.value,
        "entry_date": str(_AS_OF.date()),
        "advisor_signature": "A1.insider:sig",
        "exits_json": json.dumps({}), "status": "pending",
        "created_at": _AS_OF.isoformat(), "idea_id": idea_id,
    })

    assert _sweep(conn, pit) == []
    assert conn.execute(
        "SELECT state FROM ideas WHERE idea_id=?", (idea_id,)
    ).fetchone()["state"] == IdeaState.FINAL_DECIDED.value


def test_idea_with_expired_order_still_sweeps(tmp_path):
    """Expired/rejected orders don't own the idea — counterfactual still fires
    (belt-and-suspenders with Tier-2 #6, which usually ABANDONs these first)."""
    conn = _migrated_conn(tmp_path)
    pit, _ = _pit_with("NVDA", close=100.0)
    idea_id = _seed_decided_idea(conn)
    insert_row(conn, "orders", {
        "order_id": generate_ulid(), "dedup_hash": generate_ulid(),
        "ticker": "NVDA", "side": OrderSide.BUY.value, "qty": 1.0,
        "horizon_bucket": HorizonBucket.SHORT.value,
        "entry_date": str(_AS_OF.date()),
        "advisor_signature": "A1.insider:sig",
        "exits_json": json.dumps({}), "status": "expired",
        "created_at": _AS_OF.isoformat(), "idea_id": idea_id,
    })

    oids = _sweep(conn, pit)
    assert oids
    assert conn.execute(
        "SELECT state FROM ideas WHERE idea_id=?", (idea_id,)
    ).fetchone()["state"] == IdeaState.ABANDONED.value
