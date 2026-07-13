"""Tests for robotics-signal persistence + orchestrator persist step (#3c)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from arbiter.refresh.llm import FakeLLM
from arbiter.robotics_signal.orchestrator import run_robotics_scan
from arbiter.robotics_signal.store import create_table, persist_signals, read_recent_signals
from arbiter.robotics_signal.types import RoboticsDevelopment

AS_OF = datetime(2026, 7, 13, 8, 0)

_HIT = RoboticsDevelopment(headline="Bosch → Neura production order", summary="s",
                           category="integrator", symbols=["NEURA"],
                           trigger_hit=True, trigger_name="NEURA",
                           sources=["https://ex/neura"])
_PLAIN = RoboticsDevelopment(headline="Nvidia ships Thor", summary="s",
                             category="compute", symbols=["NVDA"], sources=["https://ex/nvda"])

CANNED = """```json
{"developments": [
  {"headline": "Bosch places production order with Neura Robotics", "summary": "s",
   "category": "integrator", "symbols": ["NEURA"], "trigger_hit": true,
   "trigger_name": "NEURA", "sources": ["https://ex/neura"]},
  {"headline": "Nvidia ships Jetson Thor", "summary": "s", "category": "compute",
   "symbols": ["NVDA"], "trigger_hit": false, "trigger_name": null,
   "sources": ["https://ex/nvda"]}
]}
```"""


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


class TestStore:
    def test_create_table_idempotent(self):
        c = _conn()
        create_table(c)
        create_table(c)  # no error on repeat

    def test_persist_and_read_roundtrip(self):
        c = _conn()
        n = persist_signals(c, [_PLAIN, _HIT], AS_OF)
        assert n == 2
        rows = read_recent_signals(c)
        assert len(rows) == 2
        # trigger-hits sort ahead within a timestamp
        assert rows[0]["trigger_hit"] is True and rows[0]["trigger_name"] == "NEURA"
        assert rows[0]["symbols"] == ["NEURA"]

    def test_read_empty(self):
        c = _conn()
        create_table(c)
        assert read_recent_signals(c) == []

    def test_persist_creates_table_without_migration(self):
        # persist on a fresh :memory: conn (no migration run) must still work
        c = _conn()
        assert persist_signals(c, [_HIT], AS_OF) == 1


class TestOrchestratorPersist:
    def test_scan_persists_developments(self):
        conn = _conn()
        engine = SimpleNamespace(
            config=SimpleNamespace(anthropic_api_key="test", robotics_model="",
                                   refresh_model="claude-opus-4-8"),
            clock=SimpleNamespace(now=lambda: AS_OF),
            alerting=MagicMock(),
            conn=conn,
        )
        report = run_robotics_scan(engine, llm=FakeLLM(CANNED))
        assert report.scan.available is True
        rows = read_recent_signals(conn)
        assert len(rows) == 2
        assert any(r["trigger_hit"] for r in rows)

    def test_no_conn_skips_persist_without_error(self):
        engine = SimpleNamespace(
            config=SimpleNamespace(anthropic_api_key="test", robotics_model="",
                                   refresh_model="claude-opus-4-8"),
            clock=SimpleNamespace(now=lambda: AS_OF),
            alerting=MagicMock(),
        )  # no conn attribute
        report = run_robotics_scan(engine, llm=FakeLLM(CANNED))
        assert report.scan.available is True  # did not raise
