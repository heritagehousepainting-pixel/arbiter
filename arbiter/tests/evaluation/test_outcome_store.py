"""Tests for arbiter.evaluation.outcome_store — Lane 14a.

Covers:
  - store_outcome round-trips: inserted row matches ResolvedOutcome fields
  - store_outcome returns a ULID primary key
  - audit line is written on insert
  - supersede_outcome creates new row + flips is_superseded on old row
  - query_outcomes filters by idea_id / advisor_id / ticker
  - query_outcomes excludes superseded rows by default
  - Insert-only: no UPDATE of non-supersede fields
  - abstained flag serialised as int 0/1
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from arbiter.contract.seams import ResolvedOutcome
from arbiter.db.audit import read_audit
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.evaluation.outcome_store import (
    query_outcomes,
    store_outcome,
    supersede_outcome,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Migrated SQLite connection (fresh per test)."""
    db_path = str(tmp_path / "test_outcomes.db")
    c = get_connection(db_path)
    run_migrations(c)
    return c


@pytest.fixture()
def audit_file(tmp_path: Path) -> Path:
    return tmp_path / "test_audit.jsonl"


def _ts(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def _make_outcome(
    *,
    idea_id: str = "01IDEA00000000000000000001",
    advisor_id: str = "A1.insider",
    ticker: str = "AAPL",
    alpha_bps: float = 150.0,
    binary: int = 1,
    advisor_confidence: float = 0.8,
    abstained: bool = False,
    horizon_days: int = 30,
    label_kind: str = "normal",
) -> ResolvedOutcome:
    return ResolvedOutcome(
        idea_id=idea_id,
        advisor_id=advisor_id,
        ticker=ticker,
        alpha_bps=alpha_bps,
        binary=binary,
        advisor_confidence=advisor_confidence,
        stance_score=float(binary),
        abstained=abstained,
        horizon_days=horizon_days,
        label_kind=label_kind,
    )


# ---------------------------------------------------------------------------
# 1. Round-trip: store and retrieve
# ---------------------------------------------------------------------------

class TestStoreOutcome:
    def test_store_returns_ulid(self, conn, audit_file):
        outcome = _make_outcome()
        pk = store_outcome(outcome, conn, as_of=_ts(2025, 1, 10), audit_path=audit_file)
        # ULID is 26-char Crockford base32
        assert isinstance(pk, str)
        assert len(pk) == 26

    def test_store_persists_all_fields(self, conn, audit_file):
        outcome = _make_outcome(
            idea_id="IDEA01",
            advisor_id="A2.mirofish",
            ticker="MSFT",
            alpha_bps=-42.5,
            binary=-1,
            advisor_confidence=0.65,
            abstained=False,
            horizon_days=60,
            label_kind="early_exit",
        )
        pk = store_outcome(outcome, conn, as_of=_ts(2025, 2, 1), audit_path=audit_file)

        row = conn.execute(
            "SELECT * FROM outcomes WHERE id = ?", (pk,)
        ).fetchone()

        assert row is not None
        assert row["idea_id"] == "IDEA01"
        assert row["advisor_id"] == "A2.mirofish"
        assert row["ticker"] == "MSFT"
        assert row["alpha_bps"] == pytest.approx(-42.5)
        assert row["binary"] == -1
        assert row["advisor_confidence"] == pytest.approx(0.65)
        assert row["abstained"] == 0      # False → 0
        assert row["horizon_days"] == 60
        assert row["label_kind"] == "early_exit"
        assert row["is_superseded"] == 0
        assert "2025-02-01" in row["created_at"]

    def test_store_abstained_flag_as_int(self, conn, audit_file):
        outcome = _make_outcome(abstained=True, binary=0, alpha_bps=0.0)
        pk = store_outcome(outcome, conn, as_of=_ts(2025, 3, 1), audit_path=audit_file)
        row = conn.execute(
            "SELECT abstained FROM outcomes WHERE id = ?", (pk,)
        ).fetchone()
        assert row["abstained"] == 1   # True → 1

    def test_store_multiple_outcomes_no_interference(self, conn, audit_file):
        """Multiple inserts each produce their own independent row."""
        ids = []
        for i in range(5):
            pk = store_outcome(
                _make_outcome(ticker=f"TKR{i}", alpha_bps=float(i * 10)),
                conn,
                as_of=_ts(2025, 4, i + 1),
                audit_path=audit_file,
            )
            ids.append(pk)
        assert len(set(ids)) == 5, "Each insert must produce a unique PK"
        count = conn.execute("SELECT count(*) FROM outcomes").fetchone()[0]
        assert count == 5


# ---------------------------------------------------------------------------
# 2. Audit line
# ---------------------------------------------------------------------------

class TestAuditLine:
    def test_audit_line_written_on_insert(self, conn, audit_file):
        outcome = _make_outcome(ticker="GOOG", alpha_bps=300.0)
        pk = store_outcome(outcome, conn, as_of=_ts(2025, 5, 1), audit_path=audit_file)

        records = read_audit(audit_file)
        assert len(records) == 1
        rec = records[0]
        assert rec["event"] == "insert_outcome"
        assert rec["payload"]["id"] == pk
        assert rec["payload"]["ticker"] == "GOOG"
        assert rec["payload"]["alpha_bps"] == pytest.approx(300.0)

    def test_audit_timestamp_matches_as_of(self, conn, audit_file):
        as_of = _ts(2025, 5, 15)
        store_outcome(_make_outcome(), conn, as_of=as_of, audit_path=audit_file)
        records = read_audit(audit_file)
        assert records[0]["ts"] == as_of.isoformat()


# ---------------------------------------------------------------------------
# 3. Supersede
# ---------------------------------------------------------------------------

class TestSupersede:
    def test_supersede_creates_new_row(self, conn, audit_file):
        old_pk = store_outcome(
            _make_outcome(alpha_bps=50.0, binary=1),
            conn,
            as_of=_ts(2025, 6, 1),
            audit_path=audit_file,
        )
        corrected = _make_outcome(alpha_bps=75.0, binary=1, label_kind="reversal")
        new_pk = supersede_outcome(
            old_pk,
            corrected,
            conn,
            as_of=_ts(2025, 6, 2),
            audit_path=audit_file,
        )
        assert new_pk != old_pk
        total = conn.execute("SELECT count(*) FROM outcomes").fetchone()[0]
        assert total == 2

    def test_supersede_flips_is_superseded(self, conn, audit_file):
        old_pk = store_outcome(
            _make_outcome(alpha_bps=10.0),
            conn,
            as_of=_ts(2025, 7, 1),
            audit_path=audit_file,
        )
        before = conn.execute(
            "SELECT is_superseded FROM outcomes WHERE id = ?", (old_pk,)
        ).fetchone()
        assert before["is_superseded"] == 0

        supersede_outcome(
            old_pk,
            _make_outcome(alpha_bps=20.0),
            conn,
            as_of=_ts(2025, 7, 2),
            audit_path=audit_file,
        )

        after = conn.execute(
            "SELECT is_superseded FROM outcomes WHERE id = ?", (old_pk,)
        ).fetchone()
        assert after["is_superseded"] == 1

    def test_supersede_new_row_carries_supersedes_id(self, conn, audit_file):
        old_pk = store_outcome(
            _make_outcome(alpha_bps=-30.0),
            conn,
            as_of=_ts(2025, 8, 1),
            audit_path=audit_file,
        )
        new_pk = supersede_outcome(
            old_pk,
            _make_outcome(alpha_bps=-10.0, label_kind="partial"),
            conn,
            as_of=_ts(2025, 8, 2),
            audit_path=audit_file,
        )
        new_row = conn.execute(
            "SELECT supersedes_id FROM outcomes WHERE id = ?", (new_pk,)
        ).fetchone()
        assert new_row["supersedes_id"] == old_pk

    def test_supersede_audit_line_event(self, conn, audit_file):
        old_pk = store_outcome(
            _make_outcome(),
            conn,
            as_of=_ts(2025, 9, 1),
            audit_path=audit_file,
        )
        supersede_outcome(
            old_pk,
            _make_outcome(alpha_bps=999.0),
            conn,
            as_of=_ts(2025, 9, 2),
            audit_path=audit_file,
        )
        records = read_audit(audit_file)
        events = [r["event"] for r in records]
        assert "insert_outcome" in events
        assert "supersede_outcome" in events
        sup_record = next(r for r in records if r["event"] == "supersede_outcome")
        assert sup_record["payload"]["old_id"] == old_pk


# ---------------------------------------------------------------------------
# 4. Query helpers
# ---------------------------------------------------------------------------

class TestQueryOutcomes:
    def _populate(self, conn, audit_file) -> dict[str, str]:
        """Insert 3 distinct outcomes; return {label: pk}."""
        pks = {}
        pks["idea_a"] = store_outcome(
            _make_outcome(idea_id="IDEA_A", advisor_id="A1.x", ticker="AAPL", alpha_bps=100.0),
            conn, as_of=_ts(2025, 1, 1), audit_path=audit_file,
        )
        pks["idea_b"] = store_outcome(
            _make_outcome(idea_id="IDEA_B", advisor_id="A1.x", ticker="MSFT", alpha_bps=200.0),
            conn, as_of=_ts(2025, 1, 2), audit_path=audit_file,
        )
        pks["idea_c"] = store_outcome(
            _make_outcome(idea_id="IDEA_C", advisor_id="A2.y", ticker="AAPL", alpha_bps=300.0),
            conn, as_of=_ts(2025, 1, 3), audit_path=audit_file,
        )
        return pks

    def test_query_all(self, conn, audit_file):
        self._populate(conn, audit_file)
        rows = query_outcomes(conn)
        assert len(rows) == 3

    def test_query_by_idea_id(self, conn, audit_file):
        self._populate(conn, audit_file)
        rows = query_outcomes(conn, idea_id="IDEA_A")
        assert len(rows) == 1
        assert rows[0]["idea_id"] == "IDEA_A"

    def test_query_by_advisor_id(self, conn, audit_file):
        self._populate(conn, audit_file)
        rows = query_outcomes(conn, advisor_id="A1.x")
        assert len(rows) == 2
        for r in rows:
            assert r["advisor_id"] == "A1.x"

    def test_query_by_ticker(self, conn, audit_file):
        self._populate(conn, audit_file)
        rows = query_outcomes(conn, ticker="AAPL")
        assert len(rows) == 2
        for r in rows:
            assert r["ticker"] == "AAPL"

    def test_query_excludes_superseded_by_default(self, conn, audit_file):
        pks = self._populate(conn, audit_file)
        # Supersede IDEA_A
        supersede_outcome(
            pks["idea_a"],
            _make_outcome(idea_id="IDEA_A", alpha_bps=150.0),
            conn,
            as_of=_ts(2025, 1, 10),
            audit_path=audit_file,
        )
        rows = query_outcomes(conn)
        # Total rows = 3 original + 1 correcting = 4, but superseded IDEA_A excluded
        assert len(rows) == 3   # 2 untouched + 1 new correcting row
        idea_a_rows = [r for r in rows if r["idea_id"] == "IDEA_A"]
        assert len(idea_a_rows) == 1
        assert idea_a_rows[0]["alpha_bps"] == pytest.approx(150.0)

    def test_query_includes_superseded_when_flag_set(self, conn, audit_file):
        pks = self._populate(conn, audit_file)
        supersede_outcome(
            pks["idea_a"],
            _make_outcome(idea_id="IDEA_A", alpha_bps=150.0),
            conn,
            as_of=_ts(2025, 1, 10),
            audit_path=audit_file,
        )
        rows = query_outcomes(conn, include_superseded=True)
        assert len(rows) == 4   # 3 original + 1 correcting row

    def test_query_empty_table(self, conn):
        rows = query_outcomes(conn)
        assert rows == []

    def test_query_result_is_list_of_dicts(self, conn, audit_file):
        store_outcome(_make_outcome(), conn, as_of=_ts(2025, 2, 1), audit_path=audit_file)
        rows = query_outcomes(conn)
        assert isinstance(rows, list)
        assert isinstance(rows[0], dict)

    def test_query_combined_filters(self, conn, audit_file):
        """advisor_id + ticker combined filter."""
        self._populate(conn, audit_file)
        rows = query_outcomes(conn, advisor_id="A1.x", ticker="AAPL")
        assert len(rows) == 1
        assert rows[0]["idea_id"] == "IDEA_A"


# ---------------------------------------------------------------------------
# 5. Insert-only contract
# ---------------------------------------------------------------------------

class TestInsertOnly:
    def test_insert_does_not_update_existing_rows(self, conn, audit_file):
        """Inserting a new outcome must not modify any existing row."""
        pk1 = store_outcome(
            _make_outcome(alpha_bps=100.0),
            conn, as_of=_ts(2025, 1, 1), audit_path=audit_file,
        )
        # Insert second outcome
        store_outcome(
            _make_outcome(alpha_bps=200.0, ticker="MSFT"),
            conn, as_of=_ts(2025, 1, 2), audit_path=audit_file,
        )
        # First row unchanged
        row = conn.execute(
            "SELECT alpha_bps, is_superseded FROM outcomes WHERE id = ?", (pk1,)
        ).fetchone()
        assert row["alpha_bps"] == pytest.approx(100.0)
        assert row["is_superseded"] == 0


# ---------------------------------------------------------------------------
# 6. All label_kinds round-trip through store
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", [
    "normal", "early_exit", "reversal", "corporate_event", "partial"
])
def test_all_label_kinds_store_and_retrieve(kind, conn, audit_file, tmp_path):
    outcome = _make_outcome(label_kind=kind)
    pk = store_outcome(outcome, conn, as_of=_ts(2025, 6, 1), audit_path=audit_file)
    row = conn.execute("SELECT label_kind FROM outcomes WHERE id = ?", (pk,)).fetchone()
    assert row["label_kind"] == kind
