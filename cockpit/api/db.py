"""Read-only access to the live arbiter SQLite DB.

HARD RULE: this opens the DB in immutable/read-only mode so the cockpit can
NEVER mutate trading state.  Every connection is ``mode=ro`` via URI.  Any
accidental write raises ``sqlite3.OperationalError`` ('attempt to write a
readonly database') — which the API tests assert.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# Repo root = .../poly_bot ; the live DB lives under arbiter/data/.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = _REPO_ROOT / "arbiter" / "data" / "arbiter.db"


def db_path() -> Path:
    return Path(os.environ.get("COCKPIT_DB_PATH", str(DEFAULT_DB_PATH)))


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    """Open the arbiter DB strictly read-only (URI ``mode=ro``).

    ``mode=ro`` opens an existing DB for reading only; writes raise.  We also
    set a short busy_timeout so a concurrent WAL writer (the daemon) never
    blocks a read for long.
    """
    p = Path(path) if path is not None else db_path()
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 3000")
    return conn


def db_reachable(path: str | os.PathLike | None = None) -> bool:
    try:
        conn = connect(path)
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return True
    except Exception:
        return False
