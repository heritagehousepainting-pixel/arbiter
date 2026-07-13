"""Windowed trigger-hit reader feeding the probationary A5.robotics advisor (#3d).

``read_active_trigger_hits`` mirrors ``findings_store.read_active_findings``:
it returns ONLY trigger-hits, and only those inside a recency window, so a stale
signal can never keep nudging the engine. Most-recent first.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from arbiter.robotics_signal.store import (
    create_table,
    persist_signals,
    read_active_trigger_hits,
)
from arbiter.robotics_signal.types import RoboticsDevelopment

_UTC = timezone.utc
NOW = datetime(2026, 7, 13, 8, 0, tzinfo=_UTC)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    create_table(c)
    return c


def _hit(name: str, headline: str = "h") -> RoboticsDevelopment:
    return RoboticsDevelopment(headline=headline, summary="s", category="integrator",
                               symbols=[name], trigger_hit=True, trigger_name=name,
                               sources=["https://ex/x"])


def _plain(sym: str = "NVDA") -> RoboticsDevelopment:
    return RoboticsDevelopment(headline="plain", summary="s", category="compute",
                               symbols=[sym], trigger_hit=False, trigger_name=None,
                               sources=["https://ex/y"])


def test_returns_only_trigger_hits():
    c = _conn()
    persist_signals(c, [_plain(), _hit("NEURA")], NOW)
    rows = read_active_trigger_hits(c, NOW)
    assert [r["trigger_name"] for r in rows] == ["NEURA"]
    assert rows[0]["trigger_hit"] is True
    assert rows[0]["symbols"] == ["NEURA"]


def test_excludes_hits_older_than_window():
    c = _conn()
    persist_signals(c, [_hit("MUJIN")], NOW - timedelta(days=30))  # stale
    persist_signals(c, [_hit("NEURA")], NOW - timedelta(days=2))   # fresh
    rows = read_active_trigger_hits(c, NOW, window_days=7)
    assert [r["trigger_name"] for r in rows] == ["NEURA"]


def test_most_recent_first():
    c = _conn()
    persist_signals(c, [_hit("A", "older")], NOW - timedelta(days=5))
    persist_signals(c, [_hit("B", "newer")], NOW - timedelta(days=1))
    rows = read_active_trigger_hits(c, NOW)
    assert [r["trigger_name"] for r in rows] == ["B", "A"]


def test_empty_when_no_hits():
    c = _conn()
    persist_signals(c, [_plain()], NOW)
    assert read_active_trigger_hits(c, NOW) == []
