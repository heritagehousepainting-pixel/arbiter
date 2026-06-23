"""SQLite WAL-mode connection factory for Arbiter.

Per INTERFACES.md §10:
- Single ``arbiter.db`` (SQLite WAL)
- ``row_factory = sqlite3.Row``
- Path read from ``Config.db_path``

Lane 1 provides ONLY the factory.  Schema/migrations/helpers are lane 2.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Return a WAL-mode SQLite connection with ``sqlite3.Row`` row factory.

    Args:
        db_path: Path to the SQLite file.  If ``None``, reads from the
                 default ``Config`` (``data/arbiter.db`` relative to the
                 project root).  Passing a path explicitly is preferred in
                 tests (use ``:memory:`` for isolation).
    """
    if db_path is None:
        # Lazy import to avoid circular deps and allow tests to pass db_path
        from arbiter.config import load_config
        cfg = load_config()
        db_path = cfg.db_path

    resolved = Path(db_path)
    if str(db_path) != ":memory:":
        resolved.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(resolved))
    conn.row_factory = sqlite3.Row

    # Enable WAL mode (INTERFACES.md §10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    # Explicit 5s busy timeout so concurrent advisor-thread reads + writer
    # block instead of failing instantly with "database is locked"
    # (F2-sqlite-concurrency).  Do not rely on CPython's undocumented default.
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.commit()

    return conn
