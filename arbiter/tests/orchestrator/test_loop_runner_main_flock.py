"""Tests for ``arbiter.orchestrator.loop_runner.main`` — the cron entrypoint.

W-TESTHARDEN seam #1: ``main()`` is the real ``arbiter run`` entrypoint with
the daemon-coexistence flock guard (C6).  It is production-only wiring that
``test_loop_runner.py`` (which only tests ``run_once``) never exercises.

These tests drive the REAL ``main()`` function with:
  - a fake engine (clock/conn/config + run_cycle) so no real DB/network,
  - the three lazily-imported collaborators (``build_engine``, ``run_ingest``,
    ``_acquire_single_instance_lock``) monkeypatched at their source modules,

and assert two things a concurrent-DB-write bug would break:
  1. When the one-shot HOLDS the single-instance lock, it runs ingest + cycle
     and releases the lock afterward.
  2. When the daemon ALREADY holds the flock (lock acquire returns None), the
     one-shot no-ops cleanly — ingest and cycle are NEVER called, so two
     processes never mutate the same SQLite DB concurrently.

OFFLINE: no real flock, no real engine, no network, no real sleep.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import pytest

import arbiter.engine as engine_mod
import arbiter.ingest.runner as ingest_runner_mod
import arbiter.runtime.daemon as daemon_mod
from arbiter.orchestrator import loop_runner
from arbiter.orchestrator.loop_runner import RunReport

_UTC = timezone.utc
_NOW = datetime(2026, 6, 19, 18, 30, 0, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, now: datetime = _NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class _FakeConfig:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.audit_path = "/tmp/audit.jsonl"


class _FakeEngine:
    """Minimal stand-in for the real Engine that ``main()`` builds."""

    def __init__(self, db_path: str) -> None:
        self.clock = _FakeClock()
        self.conn = object()  # opaque — never touched directly by main()
        self.config = _FakeConfig(db_path)
        self.cycle_calls: list[datetime] = []

    def run_cycle(self, *, as_of: datetime) -> dict[str, Any]:
        self.cycle_calls.append(as_of)
        return {"orders_submitted": 1, "as_of": as_of.isoformat()}


class _FakeLockFd:
    """Stand-in for the flock file descriptor; records close()."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _wire(monkeypatch, tmp_path, *, lock_fd):
    """Patch main()'s three lazy collaborators; return (engine, ingest_calls).

    ``lock_fd`` is what ``_acquire_single_instance_lock`` returns — a fake fd to
    simulate holding the lock, or None to simulate the daemon already holding it.
    """
    db_path = str(tmp_path / "arbiter.db")
    engine = _FakeEngine(db_path)
    ingest_calls: list[Any] = []

    def _fake_build_engine(config=None):
        return engine

    def _fake_run_ingest(config, *, conn, clock):
        # clock here is a Callable[[], str]; exercise it like the real wiring.
        ingest_calls.append(clock())

    def _fake_acquire(pidfile):
        # Assert main() builds the pidfile next to the DB (concurrent-write guard
        # is keyed to the DB directory).
        assert pidfile == os.path.join(os.path.dirname(db_path) or ".", "arbiter-daemon.pid")
        return lock_fd

    monkeypatch.setattr(engine_mod, "build_engine", _fake_build_engine)
    monkeypatch.setattr(ingest_runner_mod, "run_ingest", _fake_run_ingest)
    monkeypatch.setattr(daemon_mod, "_acquire_single_instance_lock", _fake_acquire)

    return engine, ingest_calls


# ---------------------------------------------------------------------------
# Holds the lock → runs ingest + cycle, releases the lock
# ---------------------------------------------------------------------------


class TestMainHoldsLock:
    def test_runs_ingest_and_cycle_when_lock_held(self, monkeypatch, tmp_path) -> None:
        lock_fd = _FakeLockFd()
        engine, ingest_calls = _wire(monkeypatch, tmp_path, lock_fd=lock_fd)

        report = loop_runner.main(config=None)

        # Ingest ran exactly once (clock callable produced an ISO timestamp).
        assert len(ingest_calls) == 1
        assert ingest_calls[0] == _NOW.isoformat()
        # Cycle ran exactly once with as_of from the engine clock.
        assert engine.cycle_calls == [_NOW]
        # The RunReport carries the cycle result and a clean ingest.
        assert isinstance(report, RunReport)
        assert report.ingest_ok is True
        assert report.ingest_error is None
        assert report.cycle_result == {"orders_submitted": 1, "as_of": _NOW.isoformat()}

    def test_releases_lock_after_run(self, monkeypatch, tmp_path) -> None:
        lock_fd = _FakeLockFd()
        _wire(monkeypatch, tmp_path, lock_fd=lock_fd)

        loop_runner.main(config=None)

        assert lock_fd.closed is True, "the single-instance flock must be released"

    def test_lock_released_even_when_cycle_raises(self, monkeypatch, tmp_path) -> None:
        """The flock is released in a finally — a cycle blow-up must not leak it."""
        lock_fd = _FakeLockFd()
        engine, _ = _wire(monkeypatch, tmp_path, lock_fd=lock_fd)

        def _boom(*, as_of):
            raise RuntimeError("engine exploded")

        engine.run_cycle = _boom  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="engine exploded"):
            loop_runner.main(config=None)

        assert lock_fd.closed is True, "lock must be released even on cycle failure"


# ---------------------------------------------------------------------------
# Daemon already holds the flock → no-op (concurrent-DB-write guard, C6)
# ---------------------------------------------------------------------------


class TestMainDaemonHoldsLock:
    def test_noops_when_daemon_holds_lock(self, monkeypatch, tmp_path) -> None:
        """acquire returns None (daemon owns it) → ingest and cycle NEVER run."""
        engine, ingest_calls = _wire(monkeypatch, tmp_path, lock_fd=None)

        report = loop_runner.main(config=None)

        # The crux: NOTHING mutated the DB — no ingest, no cycle.
        assert ingest_calls == [], "ingest must NOT run when the daemon holds the lock"
        assert engine.cycle_calls == [], "cycle must NOT run when the daemon holds the lock"

    def test_noop_returns_clean_report(self, monkeypatch, tmp_path) -> None:
        engine, _ = _wire(monkeypatch, tmp_path, lock_fd=None)

        report = loop_runner.main(config=None)

        assert isinstance(report, RunReport)
        assert report.as_of == _NOW
        assert report.ingest_ok is True
        assert report.ingest_error is None
        # No cycle ran → no cycle result.
        assert report.cycle_result is None
