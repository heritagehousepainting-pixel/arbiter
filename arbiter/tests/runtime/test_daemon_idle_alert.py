"""Idle-capital alert (unfreeze Stage 4 — deployment pressure) — OFFLINE.

``_run_post_close_sweep`` computes deployment (1 − cash/equity) at each
open→closed edge.  Deployment below 50% for 3 CONSECUTIVE closed sessions →
one warning-tier alert (with the last cycle-funnel counts attached), then the
counter resets.  A deployed session resets the streak.  All fail-safe.
"""
from __future__ import annotations

from datetime import datetime, timezone

from arbiter.data.clock import BacktestClock
from arbiter.runtime.daemon import DaemonState, _run_post_close_sweep

_UTC = timezone.utc
_NOW = datetime(2025, 6, 19, 20, 5, tzinfo=_UTC)


class _Account:
    def __init__(self, cash, equity):
        self.cash = cash
        self.equity = equity


class _FakeExec:
    def __init__(self, cash, equity):
        self._acct = _Account(cash, equity)

    def get_account(self):
        return self._acct

    def get_positions(self):
        return {}


class _FakeAlerting:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def alert(self, level, message, ctx=None):
        self.calls.append((level, message, ctx or {}))


class _Cfg:
    audit_path = "/dev/null"


class _FakeEngine:
    def __init__(self, cash, equity):
        self.executor = _FakeExec(cash, equity)
        self.alerting = _FakeAlerting()
        self.clock = BacktestClock(_NOW)
        self.config = _Cfg()
        self.conn = None
        self.pit = None
        self.last_cycle_funnel = {"ideas": 4, "size_zero": 3, "submitted": 0}


def test_three_idle_sessions_fire_one_warning():
    eng = _FakeEngine(cash=8_600.0, equity=10_000.0)  # 14% deployed
    state = DaemonState()

    _run_post_close_sweep(eng, _NOW, state)
    _run_post_close_sweep(eng, _NOW, state)
    assert eng.alerting.calls == []  # not yet — 2 sessions

    _run_post_close_sweep(eng, _NOW, state)
    warnings = [c for c in eng.alerting.calls if c[0] == "warning"]
    assert len(warnings) == 1
    level, message, ctx = warnings[0]
    assert "idle" in message.lower()
    assert ctx["deployment_pct"] == 14.0
    assert ctx["size_zero"] == 3  # funnel attached — WHY it is idle
    assert state.idle_sessions == 0  # reset after firing


def test_deployed_session_resets_streak():
    eng = _FakeEngine(cash=8_600.0, equity=10_000.0)
    state = DaemonState()
    _run_post_close_sweep(eng, _NOW, state)
    _run_post_close_sweep(eng, _NOW, state)

    eng.executor._acct = _Account(cash=2_000.0, equity=10_000.0)  # 80% deployed
    _run_post_close_sweep(eng, _NOW, state)
    assert state.idle_sessions == 0

    eng.executor._acct = _Account(cash=8_600.0, equity=10_000.0)
    _run_post_close_sweep(eng, _NOW, state)
    assert eng.alerting.calls == []  # streak restarted — no alert yet


def test_zero_equity_guard_no_crash_no_count():
    eng = _FakeEngine(cash=0.0, equity=0.0)
    state = DaemonState()
    _run_post_close_sweep(eng, _NOW, state)
    assert state.idle_sessions == 0
    assert eng.alerting.calls == []


def test_threshold_config_override():
    """A config-driven threshold (two-working-books) governs the idle test:
    60% deployed is idle under a 0.75 threshold but fine under 0.50."""
    eng = _FakeEngine(cash=4_000.0, equity=10_000.0)  # 60% deployed
    eng.config.idle_deployment_threshold = 0.75
    state = DaemonState()
    for _ in range(3):
        _run_post_close_sweep(eng, _NOW, state)
    assert len(eng.alerting.calls) == 1  # 60% < 75% → idle → fires

    eng2 = _FakeEngine(cash=4_000.0, equity=10_000.0)
    eng2.config.idle_deployment_threshold = 0.50
    state2 = DaemonState()
    for _ in range(3):
        _run_post_close_sweep(eng2, _NOW, state2)
    assert eng2.alerting.calls == []  # 60% >= 50% → deployed → never fires
