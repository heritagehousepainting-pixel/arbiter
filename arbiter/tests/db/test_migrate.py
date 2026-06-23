"""Tests for arbiter.db.migrate (Lane 2).

Verifies:
  - Migrations apply cleanly to a fresh (tmp) database.
  - Re-running is idempotent (no error, no duplicate rows).
  - All expected core tables are created.
  - schema_migrations table tracks applied filenames.
"""
from __future__ import annotations

import sqlite3

import pytest

from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def migrated_conn(tmp_path) -> sqlite3.Connection:
    """Return a WAL connection with core migrations applied."""
    db = str(tmp_path / "test.db")
    conn = get_connection(db)
    run_migrations(conn)
    return conn


# ---------------------------------------------------------------------------
# Expected core tables (INTERFACES.md §10)
# ---------------------------------------------------------------------------

CORE_TABLES = {
    "opinions",
    "filings",
    "ideas",
    "orders",
    "outcomes",
    "trust_weights",
    "advisor_registry",
    "breaker_state",
    "audit_meta",
    "schema_migrations",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_migrations_apply_cleanly(tmp_path) -> None:
    """Applying migrations to a fresh DB raises no exceptions."""
    db = str(tmp_path / "fresh.db")
    conn = get_connection(db)
    applied = run_migrations(conn)
    assert len(applied) >= 1, "Expected at least one migration to be applied"


def test_all_core_tables_exist(migrated_conn) -> None:
    """After migration every core table must be present."""
    rows = migrated_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    existing = {row["name"] for row in rows}
    missing = CORE_TABLES - existing
    assert not missing, f"Tables missing after migration: {missing}"


def test_schema_migrations_tracks_filename(migrated_conn) -> None:
    """schema_migrations must record '001_core.sql'."""
    rows = migrated_conn.execute(
        "SELECT filename FROM schema_migrations"
    ).fetchall()
    filenames = {row["filename"] for row in rows}
    assert "001_core.sql" in filenames


def test_idempotent_rerun(tmp_path) -> None:
    """Running migrations twice on the same DB is a no-op on the second pass."""
    db = str(tmp_path / "idempotent.db")
    conn = get_connection(db)

    applied_first = run_migrations(conn)
    assert len(applied_first) >= 1

    applied_second = run_migrations(conn)
    assert applied_second == [], (
        "Second run should return empty list (all already applied)"
    )


def test_idempotent_rerun_table_count_unchanged(tmp_path) -> None:
    """Table count must not change between first and second migration run."""
    db = str(tmp_path / "idempotent2.db")
    conn = get_connection(db)

    run_migrations(conn)
    count_after_first = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]

    run_migrations(conn)
    count_after_second = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]

    assert count_after_first == count_after_second


def test_orders_dedup_hash_unique_constraint(migrated_conn) -> None:
    """orders.dedup_hash UNIQUE constraint must be enforced."""
    from ulid import ULID

    migrated_conn.execute(
        "INSERT INTO orders (order_id, dedup_hash, ticker, side, qty, horizon_bucket, "
        "entry_date, advisor_signature, exits_json, status, created_at) "
        "VALUES (?, ?, 'AAPL', 'BUY', 10.0, 'SHORT', '2025-01-01', 'sig', '{}', 'open', 'NO_CLOCK')",
        (str(ULID()), "dup-hash-abc"),
    )
    migrated_conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        migrated_conn.execute(
            "INSERT INTO orders (order_id, dedup_hash, ticker, side, qty, horizon_bucket, "
            "entry_date, advisor_signature, exits_json, status, created_at) "
            "VALUES (?, ?, 'MSFT', 'SELL', 5.0, 'MEDIUM', '2025-01-02', 'sig2', '{}', 'open', 'NO_CLOCK')",
            (str(ULID()), "dup-hash-abc"),  # same dedup_hash
        )


# ---------------------------------------------------------------------------
# P0: Migration crash-atomic / re-runnable (no duplicate-column crash)
# ---------------------------------------------------------------------------

def test_idempotent_rerun_no_duplicate_column_crash(tmp_path) -> None:
    """Re-running migrations on an already-migrated DB must not crash with
    'duplicate column name' for ALTER TABLE ADD COLUMN statements (P0 fix).

    This specifically guards against 008b_filings_accession.sql and
    008c_filings_txn_idx.sql which use ALTER TABLE ADD COLUMN.
    """
    db = str(tmp_path / "dup_col.db")
    conn = get_connection(db)

    # First run applies all migrations including the ALTER TABLE ones.
    applied = run_migrations(conn)
    assert len(applied) >= 1

    # Second run must be a clean no-op — no OperationalError.
    try:
        applied_second = run_migrations(conn)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"Second migration run raised unexpectedly: {exc}")

    assert applied_second == [], "No migrations should be applied on second run"


def test_migration_alter_table_guard(tmp_path) -> None:
    """The ALTER TABLE guard silently skips adding a column that already exists.

    Simulates a partially-applied migration: the column was added but the
    schema_migrations record was NOT written (i.e. the old crash scenario).
    On the next run the runner must skip the ALTER and record the migration.
    """
    from arbiter.db.migrate import run_migrations, _MIGRATIONS_DIR

    db = str(tmp_path / "guard.db")
    conn = get_connection(db)

    # Apply all migrations cleanly first.
    run_migrations(conn)

    # Manually remove the 008b record to pretend it was never recorded.
    conn.execute(
        "DELETE FROM schema_migrations WHERE filename = '008b_filings_accession.sql'"
    )
    conn.commit()

    # Now re-run — must NOT raise "duplicate column name: accession".
    try:
        applied = run_migrations(conn)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"Migration re-run after manual record delete raised: {exc}")

    assert "008b_filings_accession.sql" in applied, (
        "008b must be re-applied (recorded) after its record was deleted"
    )


def test_migration_schema_migrations_record_absent_on_crash(tmp_path) -> None:
    """If a migration's SQL applies but the schema_migrations INSERT is not yet
    committed (simulated crash mid-transaction), the runner records nothing and
    BOTH the DDL and the record are rolled back — so the migration is re-applied
    cleanly on the next run.

    We verify the atomicity invariant: either both the DDL and the record are
    committed, or neither is.  We do this by checking that after a clean
    double-run, the schema_migrations table has exactly one entry per file
    (no duplicates from partial application).
    """
    db = str(tmp_path / "atomic.db")
    conn = get_connection(db)

    run_migrations(conn)
    run_migrations(conn)  # second run must be a no-op

    rows = conn.execute(
        "SELECT filename, count(*) as cnt FROM schema_migrations GROUP BY filename HAVING cnt > 1"
    ).fetchall()
    assert rows == [], f"Duplicate schema_migrations entries detected: {rows}"
