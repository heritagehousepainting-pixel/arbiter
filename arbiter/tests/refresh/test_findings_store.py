import sqlite3
from datetime import datetime, timedelta, timezone

from arbiter.refresh.findings_store import (
    create_table, persist_findings, read_active_findings,
)
from arbiter.refresh.types import MacroFinding, Severity


def _conn():
    c = sqlite3.connect(":memory:")
    create_table(c)
    return c


def test_persist_and_read_active():
    c = _conn()
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    f = MacroFinding(summary="CPI", severity=Severity.HIGH,
                     affected_tickers=["UBER", "LYFT"], sources=["reuters.com"])
    assert persist_findings(c, [f], now) == 1
    active = read_active_findings(c, now)
    assert len(active) == 1
    assert active[0].affected_tickers == ["UBER", "LYFT"]
    assert active[0].severity == Severity.HIGH


def test_expired_findings_excluded():
    c = _conn()
    old = datetime(2026, 6, 1, tzinfo=timezone.utc)
    persist_findings(c, [MacroFinding("old", Severity.LOW, ["X"], [])], old)
    later = old + timedelta(days=30)
    assert read_active_findings(c, later) == []
