"""Tests for arbiter.orchestrator.idea_store — WP-A (Phase-2 persistence).

Covers:
  - persist_new_idea + reload round-trip (all fields preserved)
  - INSERT OR IGNORE idempotency: re-persisting same idea_id is a no-op
  - update_idea_state does an in-place UPDATE reflected on reload
  - update_idea_state emits an "idea_state_transition" audit line
  - load_ideas_by_state filters by the requested state set
  - load_ideas_by_state excludes superseded rows
  - load_active_ideas excludes CLOSED and ABANDONED
  - tz-aware as_of preserved through persist/reload
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from arbiter.contract.seams import Idea
from arbiter.db.audit import read_audit
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.orchestrator.idea_store import (
    load_active_ideas,
    load_ideas_by_state,
    persist_new_idea,
    update_idea_state,
)
from arbiter.types import IdeaState

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Migrated SQLite connection (fresh per test)."""
    db_path = str(tmp_path / "test_ideas.db")
    c = get_connection(db_path)
    run_migrations(c)
    return c


@pytest.fixture()
def audit_file(tmp_path: Path) -> Path:
    return tmp_path / "test_audit.jsonl"


def _ts(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def _make_idea(
    *,
    idea_id: str = "01IDEA00000000000000000001",
    ticker: str = "AAPL",
    thesis: str = "insider cluster buy",
    horizon_days: int = 30,
    state: IdeaState = IdeaState.MONITORED,
    as_of: datetime | None = None,
    dedupe_key: tuple[str, str] = ("AAPL", "SHORT"),
) -> Idea:
    return Idea(
        idea_id=idea_id,
        ticker=ticker,
        thesis=thesis,
        horizon_days=horizon_days,
        state=state,
        as_of=as_of if as_of is not None else _ts(2025, 1, 10),
        dedupe_key=dedupe_key,
    )


# ---------------------------------------------------------------------------
# 1. persist_new_idea + reload round-trip
# ---------------------------------------------------------------------------

class TestPersistAndReload:
    def test_persist_then_reload_preserves_all_fields(self, conn):
        idea = _make_idea(
            idea_id="IDEA_RT",
            ticker="MSFT",
            thesis="congress purchase",
            horizon_days=90,
            state=IdeaState.MONITORED,
            as_of=_ts(2025, 2, 3),
            dedupe_key=("MSFT", "MEDIUM"),
        )
        persist_new_idea(conn, idea, created_at=_ts(2025, 2, 4))

        loaded = load_ideas_by_state(conn, {IdeaState.MONITORED})
        assert len(loaded) == 1
        got = loaded[0]
        assert got.idea_id == "IDEA_RT"
        assert got.ticker == "MSFT"
        assert got.thesis == "congress purchase"
        assert got.horizon_days == 90
        assert got.state == IdeaState.MONITORED
        assert got.as_of == _ts(2025, 2, 3)
        assert got.dedupe_key == ("MSFT", "MEDIUM")

    def test_reloaded_state_is_ideastate_enum(self, conn):
        persist_new_idea(conn, _make_idea(state=IdeaState.GATHERING),
                         created_at=_ts(2025, 1, 1))
        loaded = load_ideas_by_state(conn, {IdeaState.GATHERING})
        assert isinstance(loaded[0].state, IdeaState)

    def test_row_columns_written_correctly(self, conn):
        idea = _make_idea(
            idea_id="IDEA_COLS",
            state=IdeaState.EXECUTED,
            dedupe_key=("AAPL", "SHORT"),
        )
        persist_new_idea(conn, idea, created_at=_ts(2025, 3, 5))
        row = conn.execute(
            "SELECT * FROM ideas WHERE idea_id = ?", ("IDEA_COLS",)
        ).fetchone()
        assert row["state"] == "EXECUTED"
        assert row["dedupe_key_ticker"] == "AAPL"
        assert row["dedupe_key_bucket"] == "SHORT"
        assert row["updated_state_at"] == _ts(2025, 3, 5).isoformat()
        assert row["as_of"] == _ts(2025, 1, 10).isoformat()
        assert row["is_superseded"] == 0


# ---------------------------------------------------------------------------
# 2. Idempotent re-persist (INSERT OR IGNORE)
# ---------------------------------------------------------------------------

class TestIdempotent:
    def test_re_persist_same_id_is_noop(self, conn):
        idea = _make_idea(idea_id="IDEA_DUP", state=IdeaState.MONITORED)
        persist_new_idea(conn, idea, created_at=_ts(2025, 1, 1))
        # Re-persist with a DIFFERENT state — must NOT overwrite.
        idea2 = _make_idea(idea_id="IDEA_DUP", state=IdeaState.CLOSED)
        persist_new_idea(conn, idea2, created_at=_ts(2025, 1, 2))

        count = conn.execute(
            "SELECT count(*) FROM ideas WHERE idea_id = ?", ("IDEA_DUP",)
        ).fetchone()[0]
        assert count == 1
        row = conn.execute(
            "SELECT state FROM ideas WHERE idea_id = ?", ("IDEA_DUP",)
        ).fetchone()
        # Original state preserved (INSERT OR IGNORE — no overwrite).
        assert row["state"] == "MONITORED"


# ---------------------------------------------------------------------------
# 3. update_idea_state — in-place UPDATE
# ---------------------------------------------------------------------------

class TestUpdateState:
    def test_update_reflected_on_reload(self, conn):
        idea = _make_idea(idea_id="IDEA_UP", state=IdeaState.MONITORED)
        persist_new_idea(conn, idea, created_at=_ts(2025, 1, 1))

        update_idea_state(
            conn, "IDEA_UP", IdeaState.OUTCOME_READY,
            updated_state_at=_ts(2025, 1, 5),
        )

        # No longer in MONITORED.
        assert load_ideas_by_state(conn, {IdeaState.MONITORED}) == []
        ready = load_ideas_by_state(conn, {IdeaState.OUTCOME_READY})
        assert len(ready) == 1
        assert ready[0].state == IdeaState.OUTCOME_READY

    def test_update_writes_updated_state_at(self, conn):
        persist_new_idea(conn, _make_idea(idea_id="IDEA_TS"),
                         created_at=_ts(2025, 1, 1))
        update_idea_state(
            conn, "IDEA_TS", IdeaState.CLOSED,
            updated_state_at=_ts(2025, 6, 6),
        )
        row = conn.execute(
            "SELECT state, updated_state_at FROM ideas WHERE idea_id = ?",
            ("IDEA_TS",),
        ).fetchone()
        assert row["state"] == "CLOSED"
        assert row["updated_state_at"] == _ts(2025, 6, 6).isoformat()

    def test_update_audit_event_and_payload(self, conn, audit_file):
        persist_new_idea(conn, _make_idea(idea_id="IDEA_AUD2"),
                         created_at=_ts(2025, 1, 1))
        update_idea_state(
            conn, "IDEA_AUD2", IdeaState.CLOSED,
            updated_state_at=_ts(2025, 1, 9),
            audit_path=audit_file,
        )
        records = read_audit(audit_file)
        transitions = [r for r in records if r["event"] == "idea_state_transition"]
        assert len(transitions) == 1
        payload = transitions[0]["payload"]
        assert payload["idea_id"] == "IDEA_AUD2"
        assert payload["new_state"] == "CLOSED"


# ---------------------------------------------------------------------------
# 3b. update_idea_state — FSM legality check (log-only, never blocks)
# ---------------------------------------------------------------------------

class TestUpdateStateLegalityCheck:
    """The store is the durable source of truth: it logs (does NOT block)
    illegal FSM transitions so divergence surfaces in logs/audit, while
    callers retain FSM enforcement responsibility (§11.2 carve-out)."""

    def test_legal_transition_logs_no_warning(self, conn, caplog):
        # NASCENT → GATHERING is a legal FSM edge.
        persist_new_idea(conn, _make_idea(idea_id="IDEA_LEGAL",
                                          state=IdeaState.NASCENT),
                         created_at=_ts(2025, 1, 1))
        with caplog.at_level("WARNING"):
            update_idea_state(
                conn, "IDEA_LEGAL", IdeaState.GATHERING,
                updated_state_at=_ts(2025, 1, 2),
            )
        # No warnings emitted for a legal transition.
        assert [r for r in caplog.records if r.levelname == "WARNING"] == []
        # And the update applied.
        rows = load_ideas_by_state(conn, {IdeaState.GATHERING})
        assert len(rows) == 1
        assert rows[0].idea_id == "IDEA_LEGAL"

    def test_illegal_transition_warns_but_still_updates(self, conn, caplog):
        # NASCENT → CLOSED is NOT a legal FSM edge.
        persist_new_idea(conn, _make_idea(idea_id="IDEA_ILLEGAL",
                                          state=IdeaState.NASCENT),
                         created_at=_ts(2025, 1, 1))
        with caplog.at_level("WARNING"):
            update_idea_state(
                conn, "IDEA_ILLEGAL", IdeaState.CLOSED,
                updated_state_at=_ts(2025, 1, 2),
            )
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "IDEA_ILLEGAL" in msg
        assert "NASCENT" in msg
        assert "CLOSED" in msg
        # Log-only: the UPDATE still went through.
        row = conn.execute(
            "SELECT state FROM ideas WHERE idea_id = ?", ("IDEA_ILLEGAL",)
        ).fetchone()
        assert row["state"] == "CLOSED"

    def test_update_missing_idea_warns_and_does_not_crash(self, conn, caplog):
        with caplog.at_level("WARNING"):
            # No idea with this id has been persisted.
            update_idea_state(
                conn, "IDEA_MISSING", IdeaState.GATHERING,
                updated_state_at=_ts(2025, 1, 2),
            )
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "IDEA_MISSING" in warnings[0].getMessage()
        # Row genuinely absent; UPDATE was a no-op but did not raise.
        row = conn.execute(
            "SELECT state FROM ideas WHERE idea_id = ?", ("IDEA_MISSING",)
        ).fetchone()
        assert row is None


# ---------------------------------------------------------------------------
# 4. load_ideas_by_state filtering
# ---------------------------------------------------------------------------

class TestLoadByState:
    def _populate(self, conn):
        states = {
            "I_NASC": IdeaState.NASCENT,
            "I_MON": IdeaState.MONITORED,
            "I_RDY": IdeaState.OUTCOME_READY,
            "I_CLO": IdeaState.CLOSED,
            "I_ABD": IdeaState.ABANDONED,
        }
        for i, (iid, st) in enumerate(states.items()):
            persist_new_idea(
                conn,
                _make_idea(idea_id=iid, state=st, ticker=f"T{i}",
                           dedupe_key=(f"T{i}", "SHORT")),
                created_at=_ts(2025, 1, i + 1),
            )

    def test_filters_single_state(self, conn):
        self._populate(conn)
        rows = load_ideas_by_state(conn, {IdeaState.MONITORED})
        assert len(rows) == 1
        assert rows[0].idea_id == "I_MON"

    def test_filters_multiple_states(self, conn):
        self._populate(conn)
        rows = load_ideas_by_state(
            conn, {IdeaState.MONITORED, IdeaState.OUTCOME_READY}
        )
        ids = {r.idea_id for r in rows}
        assert ids == {"I_MON", "I_RDY"}

    def test_empty_state_set_returns_empty(self, conn):
        self._populate(conn)
        assert load_ideas_by_state(conn, set()) == []

    def test_excludes_superseded_rows(self, conn):
        persist_new_idea(conn, _make_idea(idea_id="I_SUP", state=IdeaState.MONITORED),
                         created_at=_ts(2025, 1, 1))
        conn.execute(
            "UPDATE ideas SET is_superseded = 1 WHERE idea_id = ?", ("I_SUP",)
        )
        conn.commit()
        assert load_ideas_by_state(conn, {IdeaState.MONITORED}) == []


# ---------------------------------------------------------------------------
# 5. load_active_ideas
# ---------------------------------------------------------------------------

class TestLoadActive:
    def test_excludes_closed_and_abandoned(self, conn):
        rows_in = [
            ("A_NASC", IdeaState.NASCENT),
            ("A_GATH", IdeaState.GATHERING),
            ("A_PROV", IdeaState.PROVISIONAL_DECIDED),
            ("A_FIN", IdeaState.FINAL_DECIDED),
            ("A_EXEC", IdeaState.EXECUTED),
            ("A_MON", IdeaState.MONITORED),
            ("A_RDY", IdeaState.OUTCOME_READY),
            ("A_CLO", IdeaState.CLOSED),
            ("A_ABD", IdeaState.ABANDONED),
        ]
        for i, (iid, st) in enumerate(rows_in):
            persist_new_idea(
                conn,
                _make_idea(idea_id=iid, state=st, ticker=f"X{i}",
                           dedupe_key=(f"X{i}", "SHORT")),
                created_at=_ts(2025, 1, i + 1),
            )
        active = load_active_ideas(conn)
        ids = {r.idea_id for r in active}
        assert "A_CLO" not in ids
        assert "A_ABD" not in ids
        assert ids == {
            "A_NASC", "A_GATH", "A_PROV", "A_FIN",
            "A_EXEC", "A_MON", "A_RDY",
        }

    def test_empty_db_returns_empty(self, conn):
        assert load_active_ideas(conn) == []


# ---------------------------------------------------------------------------
# 6. tz-aware as_of preserved
# ---------------------------------------------------------------------------

class TestTimezone:
    def test_tz_aware_as_of_preserved(self, conn):
        as_of = datetime(2025, 4, 1, 14, 30, 0, tzinfo=UTC)
        persist_new_idea(
            conn,
            _make_idea(idea_id="I_TZ", state=IdeaState.MONITORED, as_of=as_of),
            created_at=_ts(2025, 4, 2),
        )
        loaded = load_ideas_by_state(conn, {IdeaState.MONITORED})
        assert loaded[0].as_of == as_of
        assert loaded[0].as_of.tzinfo is not None

    def test_naive_stored_as_of_gets_utc_on_reload(self, conn):
        """If a row's as_of has no tzinfo, reload attaches UTC."""
        persist_new_idea(
            conn,
            _make_idea(idea_id="I_NAIVE", state=IdeaState.MONITORED),
            created_at=_ts(2025, 1, 1),
        )
        # Overwrite as_of with a naive ISO string directly.
        conn.execute(
            "UPDATE ideas SET as_of = ? WHERE idea_id = ?",
            ("2025-01-10T00:00:00", "I_NAIVE"),
        )
        conn.commit()
        loaded = load_ideas_by_state(conn, {IdeaState.MONITORED})
        assert loaded[0].as_of.tzinfo is timezone.utc
