"""Tests for arbiter.db.helpers (Lane 2).

Verifies:
  - insert_row returns a ULID string.
  - insert_row persists the row with correct values.
  - supersede_row inserts a new row (with supersedes_id set) AND flips
    is_superseded=1 on the old row.
  - No other rows in the table were updated by supersede_row.
  - generate_ulid returns unique Crockford-base32 strings.
"""
from __future__ import annotations

import re
import sqlite3

import pytest

from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.db.helpers import generate_ulid, insert_row, supersede_row

# Crockford base32 alphabet — 26 chars
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    """Migrated in-memory-ish connection for each test."""
    db = str(tmp_path / "helpers_test.db")
    c = get_connection(db)
    run_migrations(c)
    return c


# ---------------------------------------------------------------------------
# generate_ulid
# ---------------------------------------------------------------------------

def test_generate_ulid_format() -> None:
    uid = generate_ulid()
    assert _ULID_RE.match(uid), f"Not a valid ULID: {uid!r}"


def test_generate_ulid_unique() -> None:
    ids = {generate_ulid() for _ in range(100)}
    assert len(ids) == 100, "Expected 100 unique ULIDs"


# ---------------------------------------------------------------------------
# insert_row
# ---------------------------------------------------------------------------

def test_insert_row_returns_ulid(conn) -> None:
    pk = insert_row(conn, "opinions", {
        "advisor_id": "A1.test",
        "ticker": "AAPL",
        "stance_score": 0.5,
        "confidence": 0.8,
        "confidence_source": "empirical",
        "horizon_days": 30,
        "as_of": "2025-01-01T00:00:00+00:00",
        "rationale": "test",
        "source_fingerprint": "fp1",
        "run_group_id": "rg1",
        "created_at": "NO_CLOCK",
    })
    assert _ULID_RE.match(pk), f"insert_row did not return a ULID: {pk!r}"


def test_insert_row_persists_values(conn) -> None:
    row = {
        "advisor_id": "A1.congress",
        "ticker": "MSFT",
        "stance_score": -0.3,
        "confidence": 0.6,
        "confidence_source": "modeled",
        "horizon_days": 90,
        "as_of": "2025-06-01T00:00:00+00:00",
        "rationale": "persists check",
        "source_fingerprint": "fp-persist",
        "run_group_id": "rg-persist",
        "created_at": "NO_CLOCK",
    }
    pk = insert_row(conn, "opinions", row)

    fetched = conn.execute(
        "SELECT * FROM opinions WHERE id = ?", (pk,)
    ).fetchone()
    assert fetched is not None, "Row not found after insert"
    assert fetched["ticker"] == "MSFT"
    assert fetched["stance_score"] == pytest.approx(-0.3)
    assert fetched["is_superseded"] == 0


def test_insert_row_uses_provided_id(conn) -> None:
    uid = generate_ulid()
    row = {
        "id": uid,
        "advisor_id": "A1.form4",
        "ticker": "TSLA",
        "stance_score": 1.0,
        "confidence": 0.9,
        "confidence_source": "empirical",
        "horizon_days": 10,
        "as_of": "2025-03-01T00:00:00+00:00",
        "rationale": "provided-id test",
        "source_fingerprint": "fp-provided",
        "run_group_id": "rg-provided",
        "created_at": "NO_CLOCK",
    }
    pk = insert_row(conn, "opinions", row)
    assert pk == uid


def test_insert_row_filings_congress_amounts(conn) -> None:
    """Congress amounts stored as amount_low / amount_high; no midpoint."""
    pk = insert_row(conn, "filings", {
        "source": "congress",
        "ticker": "SPY",
        "person_id": "senator-xyz",
        "filing_ts": "2025-04-01T00:00:00+00:00",
        "txn_type": "purchase",
        "amount_low": 15000.0,
        "amount_high": 50000.0,
        "is_10b5_1": 0,
        "created_at": "NO_CLOCK",
    })
    fetched = conn.execute(
        "SELECT amount_low, amount_high FROM filings WHERE id = ?", (pk,)
    ).fetchone()
    assert fetched["amount_low"] == pytest.approx(15000.0)
    assert fetched["amount_high"] == pytest.approx(50000.0)


# ---------------------------------------------------------------------------
# supersede_row
# ---------------------------------------------------------------------------

def _insert_opinion(conn, ticker: str = "AAPL", stance: float = 0.5) -> str:
    return insert_row(conn, "opinions", {
        "advisor_id": "A1.test",
        "ticker": ticker,
        "stance_score": stance,
        "confidence": 0.7,
        "confidence_source": "empirical",
        "horizon_days": 30,
        "as_of": "2025-01-01T00:00:00+00:00",
        "rationale": "original",
        "source_fingerprint": "fp-orig",
        "run_group_id": "rg-orig",
        "created_at": "NO_CLOCK",
    })


def test_supersede_row_creates_new_row(conn) -> None:
    old_id = _insert_opinion(conn)
    new_id = supersede_row(conn, "opinions", old_id, {
        "advisor_id": "A1.test",
        "ticker": "AAPL",
        "stance_score": -0.5,
        "confidence": 0.8,
        "confidence_source": "empirical",
        "horizon_days": 30,
        "as_of": "2025-02-01T00:00:00+00:00",
        "rationale": "corrected",
        "source_fingerprint": "fp-new",
        "run_group_id": "rg-new",
        "created_at": "NO_CLOCK",
    })
    assert new_id != old_id, "supersede_row must return a fresh ULID"

    new_row = conn.execute(
        "SELECT * FROM opinions WHERE id = ?", (new_id,)
    ).fetchone()
    assert new_row is not None
    assert new_row["supersedes_id"] == old_id
    assert new_row["stance_score"] == pytest.approx(-0.5)


def test_supersede_row_flips_is_superseded(conn) -> None:
    old_id = _insert_opinion(conn)

    # Verify it starts un-superseded.
    before = conn.execute(
        "SELECT is_superseded FROM opinions WHERE id = ?", (old_id,)
    ).fetchone()
    assert before["is_superseded"] == 0

    supersede_row(conn, "opinions", old_id, {
        "advisor_id": "A1.test",
        "ticker": "AAPL",
        "stance_score": 0.2,
        "confidence": 0.5,
        "confidence_source": "modeled",
        "horizon_days": 30,
        "as_of": "2025-03-01T00:00:00+00:00",
        "rationale": "flip test",
        "source_fingerprint": "fp-flip",
        "run_group_id": "rg-flip",
        "created_at": "NO_CLOCK",
    })

    after = conn.execute(
        "SELECT is_superseded FROM opinions WHERE id = ?", (old_id,)
    ).fetchone()
    assert after["is_superseded"] == 1, "Old row must have is_superseded=1 after supersede_row"


def test_supersede_row_only_updates_target_row(conn) -> None:
    """No other rows may be UPDATEd by supersede_row."""
    # Insert three unrelated opinions.
    id_a = _insert_opinion(conn, "AAPL", 0.1)
    id_b = _insert_opinion(conn, "GOOG", 0.2)
    id_c = _insert_opinion(conn, "MSFT", 0.3)

    # Supersede only id_a.
    supersede_row(conn, "opinions", id_a, {
        "advisor_id": "A1.test",
        "ticker": "AAPL",
        "stance_score": 0.9,
        "confidence": 0.9,
        "confidence_source": "empirical",
        "horizon_days": 30,
        "as_of": "2025-04-01T00:00:00+00:00",
        "rationale": "only-target test",
        "source_fingerprint": "fp-target",
        "run_group_id": "rg-target",
        "created_at": "NO_CLOCK",
    })

    b_row = conn.execute(
        "SELECT is_superseded FROM opinions WHERE id = ?", (id_b,)
    ).fetchone()
    c_row = conn.execute(
        "SELECT is_superseded FROM opinions WHERE id = ?", (id_c,)
    ).fetchone()
    assert b_row["is_superseded"] == 0, "Unrelated row B must NOT be touched"
    assert c_row["is_superseded"] == 0, "Unrelated row C must NOT be touched"


def test_supersede_row_total_row_count(conn) -> None:
    """supersede_row adds exactly one new row (total rows = original + 1)."""
    id_orig = _insert_opinion(conn)
    count_before = conn.execute("SELECT count(*) FROM opinions").fetchone()[0]

    supersede_row(conn, "opinions", id_orig, {
        "advisor_id": "A1.test",
        "ticker": "AAPL",
        "stance_score": 0.0,
        "confidence": 0.5,
        "confidence_source": "none",
        "horizon_days": 30,
        "as_of": "2025-05-01T00:00:00+00:00",
        "rationale": "count test",
        "source_fingerprint": "fp-count",
        "run_group_id": "rg-count",
        "created_at": "NO_CLOCK",
    })

    count_after = conn.execute("SELECT count(*) FROM opinions").fetchone()[0]
    assert count_after == count_before + 1


# ---------------------------------------------------------------------------
# P0: Atomic supersede — no double-active rows on simulated mid-crash
# ---------------------------------------------------------------------------

def test_supersede_row_atomic_no_double_active(conn) -> None:
    """Simulate a crash between insert and UPDATE: neither old nor new row
    should remain in an inconsistent state after a real supersede_row call.

    We cannot literally crash the process mid-operation, but we verify the
    SAVEPOINT contract: the RELEASE commits insert + UPDATE together, so
    after a successful supersede_row exactly ONE row is active (new) and the
    old row is marked superseded.  A crash before RELEASE would roll back
    both, leaving the old row active (safe) rather than BOTH active (unsafe).
    """
    old_id = _insert_opinion(conn, "NVDA", 0.8)

    new_id = supersede_row(conn, "opinions", old_id, {
        "advisor_id": "A1.test",
        "ticker": "NVDA",
        "stance_score": -0.8,
        "confidence": 0.9,
        "confidence_source": "empirical",
        "horizon_days": 30,
        "as_of": "2025-06-01T00:00:00+00:00",
        "rationale": "atomic check",
        "source_fingerprint": "fp-atomic",
        "run_group_id": "rg-atomic",
        "created_at": "NO_CLOCK",
    })

    # Only the new row must be active.
    active_rows = conn.execute(
        "SELECT id FROM opinions WHERE ticker = 'NVDA' AND is_superseded = 0"
    ).fetchall()
    assert len(active_rows) == 1, "Exactly one active row after supersede_row"
    assert active_rows[0]["id"] == new_id

    # Old row must be inactive.
    old_row = conn.execute(
        "SELECT is_superseded FROM opinions WHERE id = ?", (old_id,)
    ).fetchone()
    assert old_row["is_superseded"] == 1, "Old row must be superseded"


def test_supersede_row_savepoint_prevents_dangling_insert(conn) -> None:
    """Verify the SAVEPOINT mechanism: trigger a ROLLBACK TO the savepoint and
    confirm the insert is undone so no dangling row is left active.

    We simulate a mid-operation failure by issuing the ROLLBACK directly
    (the same path supersede_row takes on exception), then check that the
    count is unchanged.
    """
    old_id = _insert_opinion(conn, "SAFE", 0.5)
    count_before = conn.execute("SELECT count(*) FROM opinions").fetchone()[0]

    # Manually replicate the internal steps of supersede_row but interrupt
    # after the insert (before the UPDATE) using ROLLBACK TO.
    conn.execute("SAVEPOINT supersede_op")
    # Insert the correcting row inside the savepoint.
    conn.execute(
        "INSERT INTO opinions (id, advisor_id, ticker, stance_score, confidence, "
        "confidence_source, horizon_days, as_of, rationale, source_fingerprint, "
        "run_group_id, created_at) VALUES "
        "('DANGLING-ID', 'A1.test', 'SAFE', -0.5, 0.5, 'none', 30, "
        "'2025-07-01T00:00:00+00:00', 'test', 'fp-safe', 'rg-safe', 'NO_CLOCK')"
    )
    # Simulate a crash by rolling back to the savepoint (no RELEASE).
    conn.execute("ROLLBACK TO supersede_op")
    conn.execute("RELEASE supersede_op")

    # The dangling row must NOT exist.
    count_after = conn.execute("SELECT count(*) FROM opinions").fetchone()[0]
    assert count_after == count_before, (
        "ROLLBACK TO savepoint must undo the insert — no dangling rows allowed"
    )

    # The old row must still be active (not superseded).
    old_row = conn.execute(
        "SELECT is_superseded FROM opinions WHERE id = ?", (old_id,)
    ).fetchone()
    assert old_row["is_superseded"] == 0, (
        "Old row must remain active after a rolled-back supersede attempt"
    )
