"""run_cache tests — roundtrip, backtest guard, duplicate-write integrity."""
from __future__ import annotations

import sqlite3

import pytest

from arbiter.adapters.mirofish import run_cache

from .conftest import make_memory_db


def test_put_and_get_roundtrip() -> None:
    conn = make_memory_db()
    fingerprint = "fp_test_001"
    date_str = "2025-06-15"
    run_id = "RG_CACHE_TEST"
    raw = [{"stance_score": 0.5, "confidence": 0.7, "run_group_id": run_id}]

    assert run_cache.get(conn, fingerprint, date_str) is None

    row_id = run_cache.put(conn, fingerprint, date_str, raw, run_id)
    assert row_id  # non-empty ULID

    cached = run_cache.get(conn, fingerprint, date_str)
    assert cached is not None
    assert cached[0]["stance_score"] == 0.5


def test_backtest_cache_guard() -> None:
    conn = make_memory_db()
    with pytest.raises(run_cache.BacktestCacheError, match="backtest"):
        run_cache.get(conn, "fp_test", "2025-01-01", is_backtest=True)


def test_duplicate_write_raises_integrity_error() -> None:
    """Two writes for the same (fingerprint, date) collide on the UNIQUE
    constraint (the adapter catches this as non-fatal)."""
    conn = make_memory_db()
    raw = [{"stance_score": 0.5}]
    run_cache.put(conn, "fp_dup", "2025-06-15", raw, "RG1")
    with pytest.raises(sqlite3.IntegrityError):
        run_cache.put(conn, "fp_dup", "2025-06-15", raw, "RG2")


def test_created_at_defaults_to_no_clock_sentinel() -> None:
    """When created_at is omitted, the NO_CLOCK sentinel is stored (no
    wall-clock read)."""
    conn = make_memory_db()
    run_cache.put(conn, "fp_nc", "2025-06-15", [{"x": 1}], "RG")
    row = conn.execute(
        "SELECT created_at FROM mirofish_run_cache WHERE idea_fingerprint='fp_nc'"
    ).fetchone()
    assert row["created_at"] == "NO_CLOCK"


def test_created_at_stored_when_supplied() -> None:
    conn = make_memory_db()
    ts = "2025-06-15T14:00:00+00:00"
    run_cache.put(conn, "fp_ts", "2025-06-15", [{"x": 1}], "RG", created_at=ts)
    row = conn.execute(
        "SELECT created_at FROM mirofish_run_cache WHERE idea_fingerprint='fp_ts'"
    ).fetchone()
    assert row["created_at"] == ts
