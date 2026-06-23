"""Tests for arbiter/db/connection.py — WAL-mode connection factory."""
from __future__ import annotations

import sqlite3

import pytest

from arbiter.db.connection import get_connection


class TestGetConnection:
    def test_returns_sqlite_connection(self, memory_db: str) -> None:
        conn = get_connection(memory_db)
        assert isinstance(conn, sqlite3.Connection)
        conn.close()

    def test_row_factory_is_sqlite_row(self, memory_db: str) -> None:
        conn = get_connection(memory_db)
        assert conn.row_factory is sqlite3.Row
        conn.close()

    def test_wal_mode_enabled(self, memory_db: str) -> None:
        """WAL mode falls back to 'memory' for :memory: DBs — that's expected."""
        conn = get_connection(memory_db)
        row = conn.execute("PRAGMA journal_mode;").fetchone()
        # :memory: returns "memory", a file-based DB would return "wal"
        assert row[0] in ("wal", "memory")
        conn.close()

    def test_wal_mode_on_file(self, tmp_db: str) -> None:
        """File-based DB should actually be in WAL mode."""
        conn = get_connection(tmp_db)
        row = conn.execute("PRAGMA journal_mode;").fetchone()
        assert row[0] == "wal"
        conn.close()

    def test_foreign_keys_enabled(self, memory_db: str) -> None:
        conn = get_connection(memory_db)
        row = conn.execute("PRAGMA foreign_keys;").fetchone()
        assert row[0] == 1
        conn.close()

    def test_can_execute_queries(self, memory_db: str) -> None:
        conn = get_connection(memory_db)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'hello')")
        conn.commit()
        row = conn.execute("SELECT val FROM t WHERE id=1").fetchone()
        assert row["val"] == "hello"
        conn.close()

    def test_creates_parent_dir(self, tmp_path: "Path") -> None:
        from pathlib import Path
        nested = tmp_path / "nested" / "dir" / "arbiter.db"
        conn = get_connection(str(nested))
        assert nested.exists()
        conn.close()
