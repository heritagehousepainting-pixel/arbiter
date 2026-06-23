"""Migration runner for Arbiter's SQLite schema (Lane 2).

Applies ``migrations/*.sql`` files in lexical order.  Tracks which files have
already been applied in a ``schema_migrations`` table so re-runs are a no-op
(idempotent).

Other lanes drop additional ``NNN_<lane>.sql`` fragments into the migrations
directory; this runner picks them up automatically on the next invocation.

Crash-atomicity guarantee
--------------------------
Each migration's SQL body AND the corresponding ``schema_migrations`` INSERT
are committed in **one** transaction.  If the process dies mid-migration the
record is absent, so the runner simply re-applies the file on next startup.

ALTER TABLE idempotency
------------------------
``ALTER TABLE … ADD COLUMN`` statements raise ``OperationalError: duplicate
column name`` when the column already exists (SQLite has no IF NOT EXISTS for
ALTER).  The runner detects these statements and silently skips them when the
column is already present, making partial/crashed migrations re-runnable.

Usage:
    from arbiter.db.migrate import run_migrations
    from arbiter.db.connection import get_connection

    conn = get_connection(":memory:")
    run_migrations(conn)
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

# Matches:  ALTER TABLE <tbl> ADD COLUMN <col> ...
_ALTER_ADD_RE = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)",
    re.IGNORECASE,
)


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if *column* already exists in *table*."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()  # noqa: S608
    return any(row[1] == column for row in rows)


def _split_statements(sql_text: str) -> list[str]:
    """Split a SQL script into individual statements, stripping comments."""
    # Remove -- line comments, then split on semicolons.
    no_comments = re.sub(r"--[^\n]*", "", sql_text)
    stmts = [s.strip() for s in no_comments.split(";")]
    return [s for s in stmts if s]


def run_migrations(
    conn: sqlite3.Connection,
    migrations_dir: Path | None = None,
    *,
    applied_at: str = "NO_CLOCK",
) -> list[str]:
    """Apply pending migration SQL files in lexical order.

    Args:
        conn: An open SQLite connection (WAL, row_factory already set by
              ``get_connection()``).
        migrations_dir: Directory containing ``*.sql`` files.  Defaults to
                        ``arbiter/db/migrations/`` next to this file.
        applied_at: Timestamp string to record in ``schema_migrations``.
                    Pass the Lane-3 clock value; defaults to sentinel
                    ``"NO_CLOCK"`` (same pattern as MetricsWriter).

    Returns:
        List of filenames that were newly applied (empty if nothing pending).
    """
    if migrations_dir is None:
        migrations_dir = _MIGRATIONS_DIR

    # Ensure the bookkeeping table exists first.
    # executescript issues an implicit COMMIT; safe here because this is the
    # very first operation and there is nothing to lose.
    conn.executescript(_BOOTSTRAP_SQL)
    conn.commit()

    already_applied: set[str] = {
        row[0]
        for row in conn.execute("SELECT filename FROM schema_migrations").fetchall()
    }

    sql_files = sorted(migrations_dir.glob("*.sql"))
    newly_applied: list[str] = []

    for sql_path in sql_files:
        fname = sql_path.name
        if fname in already_applied:
            continue

        sql_text = sql_path.read_text(encoding="utf-8")

        # --- Apply + record atomically -----------------------------------
        # We cannot use executescript here because it issues an implicit
        # COMMIT before running, which would break our atomicity goal.
        # Instead, split the file into individual statements and run each
        # with conn.execute(), then record and commit in one shot.
        conn.execute("BEGIN")
        try:
            for stmt in _split_statements(sql_text):
                # Guard ALTER TABLE ADD COLUMN against duplicate-column errors.
                m = _ALTER_ADD_RE.match(stmt)
                if m:
                    tbl, col = m.group(1), m.group(2)
                    if _column_exists(conn, tbl, col):
                        continue  # column already present — skip silently
                conn.execute(stmt)

            conn.execute(
                "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
                (fname, applied_at),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        newly_applied.append(fname)

    return newly_applied
