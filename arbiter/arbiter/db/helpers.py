"""Insert-only row helpers for Arbiter's SQLite store (Lane 2).

Public API:
    generate_ulid() -> str
    insert_row(conn, table, row) -> str
    supersede_row(conn, table, old_id, new_row) -> str

Design contract (INTERFACES.md §10, §11.2):
    - ``insert_row`` is append-only; it never UPDATEs existing rows.
    - ``supersede_row`` is the ONLY place in the codebase that issues an
      in-place UPDATE — it flips ``is_superseded = 1`` on the old row only.
    - Corrections always go in as a fresh row that carries ``supersedes_id``.
"""
from __future__ import annotations

import sqlite3

from ulid import ULID


# ---------------------------------------------------------------------------
# ULID generator
# ---------------------------------------------------------------------------

def generate_ulid() -> str:
    """Return a new Crockford-base32 ULID as an uppercase string."""
    return str(ULID())


# ---------------------------------------------------------------------------
# Insert helper
# ---------------------------------------------------------------------------

def insert_row(conn: sqlite3.Connection, table: str, row: dict) -> str:
    """Insert *row* into *table* and return the primary-key ULID.

    If ``row`` already contains an ``"id"`` key that value is used as-is;
    otherwise a fresh ULID is generated and injected.  The ``id`` column is
    assumed to be the primary key for all core tables (orders use
    ``order_id``; this helper uses ``"id"`` unless ``"order_id"`` is already
    present in the dict).

    Returns:
        The string ULID that was written as the primary key.
    """
    row = dict(row)  # copy; do not mutate caller's dict

    # Determine the PK column name used by this table.
    pk_col = _pk_column(table)

    if pk_col not in row:
        row[pk_col] = generate_ulid()

    pk_value: str = row[pk_col]

    columns = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"  # noqa: S608

    conn.execute(sql, list(row.values()))
    conn.commit()

    return pk_value


# ---------------------------------------------------------------------------
# Internal: insert without commit (for atomic supersede)
# ---------------------------------------------------------------------------

def _insert_row_no_commit(conn: sqlite3.Connection, table: str, row: dict) -> str:
    """Like ``insert_row`` but does NOT commit — caller owns the transaction.

    Used exclusively by ``supersede_row`` so that the insert and the
    ``is_superseded`` flag-flip can be committed atomically in one
    transaction, preventing a crash between the two operations from leaving
    both rows active.
    """
    row = dict(row)

    pk_col = _pk_column(table)
    if pk_col not in row:
        row[pk_col] = generate_ulid()

    pk_value: str = row[pk_col]

    columns = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"  # noqa: S608

    conn.execute(sql, list(row.values()))
    return pk_value


# ---------------------------------------------------------------------------
# Supersede helper (the ONLY UPDATE in the codebase)
# ---------------------------------------------------------------------------

def supersede_row(
    conn: sqlite3.Connection,
    table: str,
    old_id: str,
    new_row: dict,
) -> str:
    """Insert *new_row* as a correction and mark the old row superseded.

    Steps (ATOMIC — both happen in a single transaction):
    1. Insert *new_row* with ``supersedes_id = old_id`` (and a fresh ULID pk
       if not already present).
    2. Flip ``is_superseded = 1`` on the old row — **the only in-place UPDATE
       permitted in the whole codebase** (INTERFACES.md §11.2).

    Both operations are wrapped in one BEGIN/COMMIT so a crash between them
    cannot leave both rows active (no double-count risk).

    Returns:
        The string ULID of the newly inserted (correcting) row.
    """
    new_row = dict(new_row)  # copy; do not mutate caller's dict

    # Stamp the back-reference.
    new_row["supersedes_id"] = old_id

    pk_col = _pk_column(table)

    # Perform both the insert and the flag-flip inside one atomic transaction.
    # We cannot rely on conn.isolation_level because callers may use WAL mode
    # with autocommit off or on.  Use an explicit SAVEPOINT so this is safe
    # whether or not a parent transaction is already open.
    conn.execute("SAVEPOINT supersede_op")
    try:
        new_id = _insert_row_no_commit(conn, table, new_row)
        conn.execute(
            f"UPDATE {table} SET is_superseded = 1 WHERE {pk_col} = ?",  # noqa: S608
            (old_id,),
        )
        conn.execute("RELEASE supersede_op")
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO supersede_op")
        conn.execute("RELEASE supersede_op")
        raise

    return new_id


def supersede_rows(
    conn: sqlite3.Connection,
    table: str,
    old_ids: list[str],
    new_row: dict,
) -> str:
    """Atomically supersede MANY prior rows with a single correcting row.

    Inserts *new_row* once (``supersedes_id`` = the first/most-recent old id)
    and flips ``is_superseded = 1`` on **all** of *old_ids* — the insert plus
    every flip happen in ONE transaction, so a crash can never leave any of the
    superseded rows active (the failure mode that per-row commits allowed).

    Used by the ingest writer for multi-amendment chains. Returns the ULID of
    the inserted correcting row. If *old_ids* is empty, raises ValueError.
    """
    if not old_ids:
        raise ValueError("supersede_rows requires at least one old_id")

    new_row = dict(new_row)
    new_row["supersedes_id"] = old_ids[0]
    pk_col = _pk_column(table)

    conn.execute("SAVEPOINT supersede_many_op")
    try:
        new_id = _insert_row_no_commit(conn, table, new_row)
        for old_id in old_ids:
            conn.execute(
                f"UPDATE {table} SET is_superseded = 1 WHERE {pk_col} = ?",  # noqa: S608
                (old_id,),
            )
        conn.execute("RELEASE supersede_many_op")
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO supersede_many_op")
        conn.execute("RELEASE supersede_many_op")
        raise

    return new_id


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _pk_column(table: str) -> str:
    """Return the primary-key column name for *table*.

    All core tables use ``"id"`` except ``orders`` (``"order_id"``),
    ``ideas`` (``"idea_id"``), ``advisor_registry`` (``"advisor_id"``),
    and ``breaker_state`` (``"breaker_name"``).
    """
    _overrides: dict[str, str] = {
        "orders": "order_id",
        "ideas": "idea_id",
        "advisor_registry": "advisor_id",
        "breaker_state": "breaker_name",
    }
    return _overrides.get(table, "id")
