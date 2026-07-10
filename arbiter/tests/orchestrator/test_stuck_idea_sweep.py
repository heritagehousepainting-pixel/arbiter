"""2026-07-10 deadlock fix — stuck pre-execution sweep.

A cycle that AUTO-PAUSES mid-run (broker-fatal) strands its freshly created
ideas in GATHERING / PROVISIONAL_DECIDED.  Those states are ACTIVE for dedupe
(``_ACTIVE_STATES``), so the orphans block their (ticker, bucket) forever and
are never re-decided — a hard deadlock (168 live ideas on 2026-07-10).

``run_stuck_idea_sweep`` abandons any GATHERING / PROVISIONAL_DECIDED idea
whose ``updated_state_at`` is older than ``max_age_hours`` (i.e. it comes from
a PRIOR cycle, so it cannot be legitimately in-flight).  The next cycle then
regenerates + decides the ticker fresh.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from arbiter.config import load_config
from arbiter.contract.seams import Idea
from arbiter.data.clock import BacktestClock
from arbiter.db.helpers import generate_ulid
from arbiter.orchestrator import idea_store
from arbiter.orchestrator.idea import is_duplicate, make_idea
from arbiter.orchestrator.outcome_runner import run_stuck_idea_sweep
from arbiter.types import HorizonBucket, IdeaState

from tests.execution.test_exit_monitor import _AS_OF, _migrated_conn

_MAX_AGE_HOURS = 2.0


def _seed_idea(conn, *, state, age_hours, ticker="NVDA"):
    """Persist an idea whose ``updated_state_at`` is *age_hours* before _AS_OF."""
    idea_id = generate_ulid()
    stamped = _AS_OF - timedelta(hours=age_hours)
    idea = Idea(
        idea_id=idea_id, ticker=ticker, thesis="t", horizon_days=7,
        state=IdeaState.NASCENT, as_of=stamped,
        dedupe_key=(ticker, HorizonBucket.SHORT.value),
    )
    # persist_new_idea initialises updated_state_at = created_at = stamped;
    # the direct state UPDATE below deliberately leaves it there, exactly like
    # a row stranded mid-cycle before the deciding transitions ever ran.
    idea_store.persist_new_idea(conn, idea, created_at=stamped)
    conn.execute(
        "UPDATE ideas SET state=? WHERE idea_id=?", (state.value, idea_id)
    )
    conn.commit()
    return idea_id


def _sweep(conn, *, max_age_hours=_MAX_AGE_HOURS):
    return run_stuck_idea_sweep(
        conn, clock=BacktestClock(_AS_OF), max_age_hours=max_age_hours,
    )


def _state_of(conn, idea_id):
    return conn.execute(
        "SELECT state FROM ideas WHERE idea_id=?", (idea_id,)
    ).fetchone()["state"]


# ---------------------------------------------------------------------------
# (a) stale GATHERING → swept to ABANDONED
# ---------------------------------------------------------------------------

def test_stale_gathering_idea_abandoned(tmp_path):
    conn = _migrated_conn(tmp_path)
    idea_id = _seed_idea(conn, state=IdeaState.GATHERING, age_hours=5)

    swept = _sweep(conn)

    assert swept == [idea_id]
    assert _state_of(conn, idea_id) == IdeaState.ABANDONED.value


# ---------------------------------------------------------------------------
# (b) fresh GATHERING (current cycle) → NOT swept
# ---------------------------------------------------------------------------

def test_fresh_gathering_idea_untouched(tmp_path):
    conn = _migrated_conn(tmp_path)
    idea_id = _seed_idea(conn, state=IdeaState.GATHERING, age_hours=0.5)

    assert _sweep(conn) == []
    assert _state_of(conn, idea_id) == IdeaState.GATHERING.value


# ---------------------------------------------------------------------------
# (c) stale PROVISIONAL_DECIDED → swept to ABANDONED
# ---------------------------------------------------------------------------

def test_stale_provisional_decided_abandoned(tmp_path):
    conn = _migrated_conn(tmp_path)
    idea_id = _seed_idea(
        conn, state=IdeaState.PROVISIONAL_DECIDED, age_hours=5
    )

    swept = _sweep(conn)

    assert swept == [idea_id]
    assert _state_of(conn, idea_id) == IdeaState.ABANDONED.value


# ---------------------------------------------------------------------------
# (d) after the sweep the ticker no longer dedupe-blocks (the deadlock)
# ---------------------------------------------------------------------------

def test_swept_ticker_no_longer_dedupe_blocks(tmp_path):
    conn = _migrated_conn(tmp_path)
    _seed_idea(conn, state=IdeaState.GATHERING, age_hours=5, ticker="NVDA")

    fresh = make_idea("NVDA", "regenerated", 7, as_of=_AS_OF)

    # Before the sweep the stranded orphan blocks the (ticker, bucket).
    assert is_duplicate(fresh, idea_store.load_active_ideas(conn)) is True

    _sweep(conn)

    # After the sweep the slot is free — the next cycle regenerates it.
    assert is_duplicate(fresh, idea_store.load_active_ideas(conn)) is False


# ---------------------------------------------------------------------------
# Guards: other states / misconfiguration are never touched
# ---------------------------------------------------------------------------

def test_stale_final_decided_left_alone(tmp_path):
    """FINAL_DECIDED is owned by run_unexecuted_sweep, never this sweep."""
    conn = _migrated_conn(tmp_path)
    idea_id = _seed_idea(conn, state=IdeaState.FINAL_DECIDED, age_hours=5)

    assert _sweep(conn) == []
    assert _state_of(conn, idea_id) == IdeaState.FINAL_DECIDED.value


def test_nonpositive_threshold_disables_sweep(tmp_path):
    """A zero/negative threshold could abandon in-flight current-cycle ideas;
    fail safe by disabling the sweep instead."""
    conn = _migrated_conn(tmp_path)
    idea_id = _seed_idea(conn, state=IdeaState.GATHERING, age_hours=5)

    assert _sweep(conn, max_age_hours=0.0) == []
    assert _state_of(conn, idea_id) == IdeaState.GATHERING.value


# ---------------------------------------------------------------------------
# Config knob
# ---------------------------------------------------------------------------

def test_config_default_stuck_idea_max_age_hours(tmp_path: Path) -> None:
    toml = tmp_path / "arbiter.toml"
    toml.write_text("", encoding="utf-8")
    cfg = load_config(config_path=toml)
    assert cfg.stuck_idea_max_age_hours == 2.0


def test_config_env_override_stuck_idea_max_age_hours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARBITER_STUCK_IDEA_MAX_AGE_HOURS", "6")
    toml = tmp_path / "arbiter.toml"
    toml.write_text("", encoding="utf-8")
    cfg = load_config(config_path=toml)
    assert cfg.stuck_idea_max_age_hours == 6.0
