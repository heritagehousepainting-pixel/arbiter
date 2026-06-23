"""Tests for arbiter.gate.ramp — paper→live staged ramp.

Covers:
- Ramp advances only one stage at a time (10→25→50→100)
- Advancing past 100 % raises StageLimitError
- init_ramp starts at 10 %
- init_ramp on already-initialised ramp raises RampAlreadyInitialised
- advance_stage without init raises NoStageSetError
- current_stage returns None before init
- stage_multiplier maps correctly; returns 0.0 before init
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from arbiter.gate.ramp import (
    STAGE_ORDER,
    NoStageSetError,
    RampAlreadyInitialised,
    StageLimitError,
    advance_stage,
    current_stage,
    init_ramp,
    stage_multiplier,
)


UTC = timezone.utc
NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)
OPERATOR = "test-operator"


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE gate_ramp (
            id          TEXT PRIMARY KEY,
            stage_pct   INTEGER NOT NULL,
            advanced_by TEXT NOT NULL,
            advanced_at TEXT NOT NULL,
            note        TEXT
        )
        """
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestStageOrder:

    def test_stage_order_values(self):
        assert STAGE_ORDER == (10, 25, 50, 100)

    def test_stage_order_is_tuple(self):
        assert isinstance(STAGE_ORDER, tuple)


# ---------------------------------------------------------------------------
# Before init
# ---------------------------------------------------------------------------

class TestBeforeInit:

    def test_current_stage_none_before_init(self):
        conn = _conn()
        assert current_stage(conn) is None

    def test_stage_multiplier_zero_before_init(self):
        conn = _conn()
        assert stage_multiplier(conn) == 0.0

    def test_advance_before_init_raises_no_stage_error(self):
        conn = _conn()
        with pytest.raises(NoStageSetError):
            advance_stage(conn, advanced_by=OPERATOR, as_of=NOW)


# ---------------------------------------------------------------------------
# init_ramp
# ---------------------------------------------------------------------------

class TestInitRamp:

    def test_init_sets_stage_10(self):
        conn = _conn()
        result = init_ramp(conn, advanced_by=OPERATOR, as_of=NOW)
        assert result == 10

    def test_current_stage_after_init(self):
        conn = _conn()
        init_ramp(conn, advanced_by=OPERATOR, as_of=NOW)
        assert current_stage(conn) == 10

    def test_init_twice_raises(self):
        conn = _conn()
        init_ramp(conn, advanced_by=OPERATOR, as_of=NOW)
        with pytest.raises(RampAlreadyInitialised):
            init_ramp(conn, advanced_by=OPERATOR, as_of=NOW)

    def test_init_stores_advanced_by(self):
        conn = _conn()
        init_ramp(conn, advanced_by="alice", as_of=NOW)
        row = conn.execute("SELECT advanced_by FROM gate_ramp").fetchone()
        assert row["advanced_by"] == "alice"


# ---------------------------------------------------------------------------
# advance_stage — one stage at a time
# ---------------------------------------------------------------------------

class TestAdvanceStage:

    def _conn_at_stage(self, stage: int) -> sqlite3.Connection:
        """Return a conn initialised to the given stage."""
        conn = _conn()
        init_ramp(conn, advanced_by=OPERATOR, as_of=NOW)
        stages = list(STAGE_ORDER)
        current_idx = stages.index(10)
        target_idx = stages.index(stage)
        for _ in range(current_idx, target_idx):
            advance_stage(conn, advanced_by=OPERATOR, as_of=NOW)
        return conn

    def test_advance_10_to_25(self):
        conn = _conn()
        init_ramp(conn, advanced_by=OPERATOR, as_of=NOW)
        result = advance_stage(conn, advanced_by=OPERATOR, as_of=NOW)
        assert result == 25
        assert current_stage(conn) == 25

    def test_advance_25_to_50(self):
        conn = self._conn_at_stage(25)
        result = advance_stage(conn, advanced_by=OPERATOR, as_of=NOW)
        assert result == 50

    def test_advance_50_to_100(self):
        conn = self._conn_at_stage(50)
        result = advance_stage(conn, advanced_by=OPERATOR, as_of=NOW)
        assert result == 100

    def test_cannot_advance_past_100(self):
        conn = self._conn_at_stage(100)
        with pytest.raises(StageLimitError):
            advance_stage(conn, advanced_by=OPERATOR, as_of=NOW)

    def test_advance_only_one_stage_per_call(self):
        """Each advance_stage() moves exactly one step."""
        conn = _conn()
        init_ramp(conn, advanced_by=OPERATOR, as_of=NOW)

        stages_observed = [current_stage(conn)]
        for _ in range(3):   # 10→25→50→100
            advance_stage(conn, advanced_by=OPERATOR, as_of=NOW)
            stages_observed.append(current_stage(conn))

        assert stages_observed == [10, 25, 50, 100]

    def test_full_ramp_sequence(self):
        """Walk through all 4 stages and verify each step."""
        conn = _conn()
        init_ramp(conn, advanced_by=OPERATOR, as_of=NOW)

        assert current_stage(conn) == 10
        advance_stage(conn, advanced_by=OPERATOR, as_of=NOW)
        assert current_stage(conn) == 25
        advance_stage(conn, advanced_by=OPERATOR, as_of=NOW)
        assert current_stage(conn) == 50
        advance_stage(conn, advanced_by=OPERATOR, as_of=NOW)
        assert current_stage(conn) == 100

        with pytest.raises(StageLimitError):
            advance_stage(conn, advanced_by=OPERATOR, as_of=NOW)

    def test_advance_stores_advanced_by(self):
        conn = _conn()
        init_ramp(conn, advanced_by=OPERATOR, as_of=NOW)
        advance_stage(conn, advanced_by="bob", as_of=NOW)
        rows = conn.execute(
            "SELECT advanced_by, stage_pct FROM gate_ramp ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert rows["advanced_by"] == "bob"
        assert rows["stage_pct"] == 25

    def test_note_stored_on_advance(self):
        conn = _conn()
        init_ramp(conn, advanced_by=OPERATOR, as_of=NOW)
        advance_stage(conn, advanced_by=OPERATOR, as_of=NOW, note="week-1 ramp")
        row = conn.execute(
            "SELECT note FROM gate_ramp ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert row["note"] == "week-1 ramp"


# ---------------------------------------------------------------------------
# stage_multiplier
# ---------------------------------------------------------------------------

class TestStageMultiplier:

    def test_multiplier_at_10(self):
        conn = _conn()
        init_ramp(conn, advanced_by=OPERATOR, as_of=NOW)
        assert stage_multiplier(conn) == pytest.approx(0.10)

    def test_multiplier_at_25(self):
        conn = _conn()
        init_ramp(conn, advanced_by=OPERATOR, as_of=NOW)
        advance_stage(conn, advanced_by=OPERATOR, as_of=NOW)
        assert stage_multiplier(conn) == pytest.approx(0.25)

    def test_multiplier_at_50(self):
        conn = _conn()
        init_ramp(conn, advanced_by=OPERATOR, as_of=NOW)
        advance_stage(conn, advanced_by=OPERATOR, as_of=NOW)
        advance_stage(conn, advanced_by=OPERATOR, as_of=NOW)
        assert stage_multiplier(conn) == pytest.approx(0.50)

    def test_multiplier_at_100(self):
        conn = _conn()
        init_ramp(conn, advanced_by=OPERATOR, as_of=NOW)
        for _ in range(3):
            advance_stage(conn, advanced_by=OPERATOR, as_of=NOW)
        assert stage_multiplier(conn) == pytest.approx(1.00)

    def test_multiplier_zero_before_init(self):
        conn = _conn()
        assert stage_multiplier(conn) == 0.0
