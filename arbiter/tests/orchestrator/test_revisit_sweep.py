"""Unfreeze Stage 3 — revisit sweep (standing idea book).

A FINAL_DECIDED idea that never produced an order used to die one-shot: its
(ticker, bucket) slot stayed blocked until horizon expiry and the thesis was
never re-fused against fresh opinions.  ``run_revisit_sweep`` recycles such
ideas daily: the old idea is ABANDONED (freeing the dedupe slot, existing
legal transition) and a FRESH ``NASCENT`` idea for the same (ticker, horizon)
is returned for the CURRENT cycle to process.

Guards covered here:
- eligible (>= min_age, horizon unexpired, no live/filled order) → recycled
- younger than min_age → untouched
- horizon elapsed → untouched (belongs to run_unexecuted_sweep)
- owned by a filled order → untouched
- limit caps the batch (oldest first)
- limit <= 0 disables the sweep
"""
from __future__ import annotations

from datetime import timedelta

from arbiter.contract.seams import Idea
from arbiter.data.clock import BacktestClock
from arbiter.db.helpers import generate_ulid
from arbiter.orchestrator import idea_store
from arbiter.orchestrator.outcome_runner import run_revisit_sweep
from arbiter.types import HorizonBucket, IdeaState

from tests.execution.test_exit_monitor import _AS_OF, _migrated_conn


def _seed_decided(conn, *, age_hours, ticker="NVDA", horizon_days=90,
                  as_of_age_days=1.0):
    """Persist a FINAL_DECIDED idea.

    ``age_hours`` — how long ago the state was last updated (revisit age basis).
    ``as_of_age_days`` — information age (horizon-elapsed basis).
    """
    idea_id = generate_ulid()
    info_ts = _AS_OF - timedelta(days=as_of_age_days)
    idea = Idea(
        idea_id=idea_id, ticker=ticker, thesis="t", horizon_days=horizon_days,
        state=IdeaState.NASCENT, as_of=info_ts,
        dedupe_key=(ticker, HorizonBucket.LONG.value),
    )
    idea_store.persist_new_idea(conn, idea, created_at=info_ts)
    stamped = (_AS_OF - timedelta(hours=age_hours)).isoformat()
    conn.execute(
        "UPDATE ideas SET state=?, updated_state_at=? WHERE idea_id=?",
        (IdeaState.FINAL_DECIDED.value, stamped, idea_id),
    )
    conn.commit()
    return idea_id


def _add_order(conn, idea_id, *, status="filled", ticker="NVDA"):
    conn.execute(
        "INSERT INTO orders (order_id, dedup_hash, ticker, side, qty, "
        "horizon_bucket, entry_date, advisor_signature, exits_json, status, "
        "created_at, idea_id) VALUES (?, ?, ?, 'BUY', 1.0, 'LONG', '2025-01-02', "
        "'sig', '{}', ?, ?, ?)",
        (generate_ulid(), generate_ulid(), ticker, status,
         _AS_OF.isoformat(), idea_id),
    )
    conn.commit()


def _sweep(conn, *, min_age_hours=24.0, limit=50):
    return run_revisit_sweep(
        conn, clock=BacktestClock(_AS_OF),
        min_age_hours=min_age_hours, limit=limit,
    )


def _state_of(conn, idea_id):
    return conn.execute(
        "SELECT state FROM ideas WHERE idea_id=?", (idea_id,)
    ).fetchone()["state"]


def test_eligible_idea_recycled(tmp_path):
    conn = _migrated_conn(tmp_path)
    idea_id = _seed_decided(conn, age_hours=30)

    fresh = _sweep(conn)

    assert _state_of(conn, idea_id) == IdeaState.ABANDONED.value
    assert len(fresh) == 1
    assert fresh[0].ticker == "NVDA"
    assert fresh[0].horizon_days == 90
    assert fresh[0].state is IdeaState.NASCENT
    assert fresh[0].idea_id != idea_id
    assert fresh[0].as_of == _AS_OF  # fresh info timestamp — horizon restarts


def test_young_idea_untouched(tmp_path):
    conn = _migrated_conn(tmp_path)
    idea_id = _seed_decided(conn, age_hours=3)

    assert _sweep(conn) == []
    assert _state_of(conn, idea_id) == IdeaState.FINAL_DECIDED.value


def test_horizon_elapsed_left_for_unexecuted_sweep(tmp_path):
    conn = _migrated_conn(tmp_path)
    idea_id = _seed_decided(conn, age_hours=30, horizon_days=7, as_of_age_days=10)

    assert _sweep(conn) == []
    assert _state_of(conn, idea_id) == IdeaState.FINAL_DECIDED.value


def test_order_owned_idea_untouched(tmp_path):
    conn = _migrated_conn(tmp_path)
    idea_id = _seed_decided(conn, age_hours=30)
    _add_order(conn, idea_id, status="filled")

    assert _sweep(conn) == []
    assert _state_of(conn, idea_id) == IdeaState.FINAL_DECIDED.value


def test_expired_order_does_not_own_idea(tmp_path):
    """An expired (never-filled) order doesn't block the recycle."""
    conn = _migrated_conn(tmp_path)
    idea_id = _seed_decided(conn, age_hours=30)
    _add_order(conn, idea_id, status="expired")

    fresh = _sweep(conn)
    assert len(fresh) == 1
    assert _state_of(conn, idea_id) == IdeaState.ABANDONED.value


def test_limit_caps_batch_oldest_first(tmp_path):
    conn = _migrated_conn(tmp_path)
    oldest = _seed_decided(conn, age_hours=72, ticker="AAA")
    _seed_decided(conn, age_hours=48, ticker="BBB")
    _seed_decided(conn, age_hours=30, ticker="CCC")

    fresh = _sweep(conn, limit=1)

    assert len(fresh) == 1
    assert fresh[0].ticker == "AAA"
    assert _state_of(conn, oldest) == IdeaState.ABANDONED.value


def test_limit_zero_disables(tmp_path):
    conn = _migrated_conn(tmp_path)
    idea_id = _seed_decided(conn, age_hours=30)

    assert _sweep(conn, limit=0) == []
    assert _state_of(conn, idea_id) == IdeaState.FINAL_DECIDED.value
