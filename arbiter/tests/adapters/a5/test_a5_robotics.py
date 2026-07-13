"""Unit tests for the probationary A5.robotics advisor pipeline (#3d).

Mirrors tests/refresh/test_a4_macro.py. Safety properties under test:
- kill-switch: disabled config -> [] (dormant by default);
- look-ahead: BacktestClock -> [] (live-only);
- tradeable-only: never an Opinion for a non-priceable universe symbol;
- significance-gated (a5_min_stance / a5_min_confidence);
- weight-capped: emitted confidence bounded by a5_weight_cap;
- recency-windowed: a stale trigger-hit no longer nudges.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from arbiter.adapters.a5 import ADVISOR_ID, gather_a5_opinions
from arbiter.contract.opinion import default_registry, validate_opinion
from arbiter.data.clock import BacktestClock
from arbiter.robotics_signal.store import create_table, persist_signals
from arbiter.robotics_signal.types import RoboticsDevelopment

_UTC = timezone.utc
NOW = datetime(2026, 7, 13, 8, 0, tzinfo=_UTC)


class _LiveClock:
    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now


def _cfg(**over):
    base = dict(robotics_advisor_enabled=True, a5_advisor_id="A5.robotics",
               a5_min_stance=0.25, a5_min_confidence=0.0, a5_weight_cap=0.25)
    base.update(over)
    return SimpleNamespace(**base)


def _hit(trigger_name: str, headline: str = "h") -> RoboticsDevelopment:
    return RoboticsDevelopment(headline=headline, summary="s", category="integrator",
                               symbols=[trigger_name], trigger_hit=True,
                               trigger_name=trigger_name, sources=["https://ex/x"])


def _conn_with(devs, as_of=NOW) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    create_table(c)
    persist_signals(c, devs, as_of)
    return c


def test_advisor_registered():
    assert ADVISOR_ID in default_registry.all_ids()


def test_disabled_returns_empty():
    # NVDA is priceable — so only the kill-switch keeps it silent here.
    c = _conn_with([_hit("NVDA")])
    assert gather_a5_opinions(c, _LiveClock(NOW), _cfg(robotics_advisor_enabled=False)) == []


def test_inert_under_backtest_clock():
    c = _conn_with([_hit("NVDA")])
    assert gather_a5_opinions(c, BacktestClock(NOW), _cfg()) == []


def test_priceable_trigger_hit_becomes_opinion():
    c = _conn_with([_hit("NVDA", "Jetson Thor design win")])
    ops = gather_a5_opinions(c, _LiveClock(NOW), _cfg())
    assert len(ops) == 1
    op = ops[0]
    assert op.advisor_id == "A5.robotics"
    assert op.ticker == "NVDA"
    assert op.stance_score > 0.0  # a fired trigger is bullish on the robotics thesis
    assert op.horizon_days == 7
    validate_opinion(op)  # must not raise


def test_non_priceable_trigger_filtered():
    # NEURA is a private (priceable=False) universe symbol — never tradeable.
    c = _conn_with([_hit("NEURA")])
    assert gather_a5_opinions(c, _LiveClock(NOW), _cfg()) == []


def test_symbol_absent_from_universe_filtered():
    c = _conn_with([_hit("NOTAREALSYMBOL")])
    assert gather_a5_opinions(c, _LiveClock(NOW), _cfg()) == []


def test_non_trigger_development_ignored():
    plain = RoboticsDevelopment(headline="Nvidia ships Thor", summary="s",
                                category="compute", symbols=["NVDA"],
                                trigger_hit=False, trigger_name=None,
                                sources=["https://ex/y"])
    c = _conn_with([plain])
    assert gather_a5_opinions(c, _LiveClock(NOW), _cfg()) == []


def test_below_min_stance_gated():
    c = _conn_with([_hit("NVDA")])
    assert gather_a5_opinions(c, _LiveClock(NOW), _cfg(a5_min_stance=0.9)) == []


def test_below_min_confidence_gated():
    c = _conn_with([_hit("NVDA")])
    assert gather_a5_opinions(c, _LiveClock(NOW), _cfg(a5_min_confidence=0.9)) == []


def test_weight_cap_bounds_confidence():
    c = _conn_with([_hit("NVDA")])
    ops = gather_a5_opinions(c, _LiveClock(NOW), _cfg(a5_weight_cap=0.1))
    assert len(ops) == 1
    assert ops[0].confidence == 0.1


def test_stale_trigger_excluded():
    c = _conn_with([_hit("NVDA")], as_of=NOW - timedelta(days=30))
    assert gather_a5_opinions(c, _LiveClock(NOW), _cfg()) == []


def test_dedupes_same_symbol_and_headline():
    c = _conn_with([_hit("NVDA", "same headline"), _hit("NVDA", "same headline")])
    ops = gather_a5_opinions(c, _LiveClock(NOW), _cfg())
    assert len(ops) == 1


def test_missing_table_fails_closed():
    c = sqlite3.connect(":memory:")  # no create_table / migration
    assert gather_a5_opinions(c, _LiveClock(NOW), _cfg()) == []
