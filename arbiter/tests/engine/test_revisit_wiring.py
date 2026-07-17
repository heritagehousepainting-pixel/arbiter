"""Engine wiring for the revisit sweep (unfreeze Stage 3) — OFFLINE.

A FINAL_DECIDED idea that never executed and is ≥ min-age old must be
recycled INTO THE CURRENT CYCLE: old idea ABANDONED, a fresh idea processed
this cycle (even when there are no new filing signals — the standing book
alone keeps quiet days alive).
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arbiter.config import load_config
from arbiter.contract.seams import Idea
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.connection import get_connection
from arbiter.db.helpers import generate_ulid
from arbiter.db.migrate import run_migrations
from arbiter.engine import build_engine
from arbiter.orchestrator import idea_store
from arbiter.types import HorizonBucket, IdeaState

_UTC = timezone.utc
_AS_OF = datetime(2025, 3, 15, 12, 0, 0, tzinfo=_UTC)


def _make_engine(tmp_path: Path):
    db_path = str(tmp_path / "revisit.db")
    config = dataclasses.replace(
        load_config(), live_trading=False, executor_backend="sim",
        db_path=db_path, audit_path=str(tmp_path / "audit.jsonl"),
        metrics_path=str(tmp_path / "metrics.jsonl"),
        kill_switch_url="", alert_webhook_url="",
    )
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    fx = FixtureSource()
    pit = PITGateway()
    for src in ("price_close", "price_open", "spread", "adv_20d"):
        pit.register_source(src, fx)
    eng = build_engine(config, conn=conn, pit=pit, clock=BacktestClock(_AS_OF))
    return eng, conn


def _seed_decided(conn, *, age_hours=30.0, ticker="NVDA", horizon_days=90):
    idea_id = generate_ulid()
    info_ts = _AS_OF - timedelta(days=1)
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


def test_revisit_feeds_current_cycle_without_fresh_signals(tmp_path):
    eng, conn = _make_engine(tmp_path)
    old_id = _seed_decided(conn, age_hours=30.0)

    result = eng.run_cycle(as_of=_AS_OF)

    assert result.ideas_processed >= 1, (
        "revived idea did not enter the cycle (no_signals bail-out?)"
    )
    assert conn.execute(
        "SELECT state FROM ideas WHERE idea_id=?", (old_id,)
    ).fetchone()["state"] == IdeaState.ABANDONED.value
    fresh = conn.execute(
        "SELECT idea_id FROM ideas WHERE dedupe_key_ticker='NVDA' "
        "AND idea_id != ? AND is_superseded = 0",
        (old_id,),
    ).fetchall()
    assert len(fresh) == 1, "expected exactly one recycled NVDA idea"


def test_young_decided_idea_not_recycled(tmp_path):
    eng, conn = _make_engine(tmp_path)
    old_id = _seed_decided(conn, age_hours=2.0)

    result = eng.run_cycle(as_of=_AS_OF)

    assert conn.execute(
        "SELECT state FROM ideas WHERE idea_id=?", (old_id,)
    ).fetchone()["state"] == IdeaState.FINAL_DECIDED.value
