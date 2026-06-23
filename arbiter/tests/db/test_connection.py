"""Tests for the SQLite connection factory (arbiter.db.connection)."""
from __future__ import annotations

from arbiter.db.connection import get_connection


def test_busy_timeout_is_5000() -> None:
    """A fresh connection must report busy_timeout == 5000 (5s).

    Guards against relying on CPython's undocumented default (F2-sqlite-concurrency).
    """
    conn = get_connection(":memory:")
    try:
        (value,) = conn.execute("PRAGMA busy_timeout").fetchone()
        assert value == 5000
    finally:
        conn.close()


def test_wal_and_fk_still_set() -> None:
    """busy_timeout addition must not disturb the existing pragmas."""
    conn = get_connection(":memory:")
    try:
        # foreign_keys stays ON
        (fk,) = conn.execute("PRAGMA foreign_keys").fetchone()
        assert fk == 1
        # row_factory unchanged
        import sqlite3

        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()
