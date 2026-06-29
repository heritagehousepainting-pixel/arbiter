# tests/refresh/test_a4_macro.py
import sqlite3
from datetime import datetime, timezone

from arbiter.adapters.a4 import gather_a4_opinions, ADVISOR_ID
from arbiter.contract.opinion import default_registry, validate_opinion
from arbiter.refresh.findings_store import create_table, persist_findings
from arbiter.refresh.types import MacroFinding, Severity
from arbiter.data.clock import BacktestClock


class _LiveClock:
    def __init__(self, now): self._now = now
    def now(self): return self._now


def _cfg():
    from types import SimpleNamespace
    return SimpleNamespace(a4_advisor_id="A4.macro", a4_min_confidence=0.0)


def test_advisor_registered():
    assert ADVISOR_ID in default_registry.all_ids()  # or membership check the registry exposes


def test_findings_become_valid_opinions():
    c = sqlite3.connect(":memory:")
    create_table(c)
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    persist_findings(c, [MacroFinding("CPI risk", Severity.HIGH, ["UBER"],
                                      ["reuters.com"])], now)
    ops = gather_a4_opinions(c, _LiveClock(now), _cfg())
    assert len(ops) == 1
    op = ops[0]
    assert op.advisor_id == "A4.macro" and op.ticker == "UBER"
    assert -1.0 <= op.stance_score <= 1.0 and op.horizon_days == 7
    validate_opinion(op)  # must not raise


def test_inert_under_backtest_clock():
    c = sqlite3.connect(":memory:")
    create_table(c)
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    persist_findings(c, [MacroFinding("x", Severity.HIGH, ["UBER"], [])], now)
    assert gather_a4_opinions(c, BacktestClock(now), _cfg()) == []
