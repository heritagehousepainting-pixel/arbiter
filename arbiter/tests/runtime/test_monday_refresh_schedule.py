"""Tier-3 #11 — the daemon now schedules the Monday macro refresh."""
from __future__ import annotations

from datetime import datetime, timezone

from arbiter.runtime.daemon import DaemonState, _maybe_monday_refresh

# 2025-03-17 is a Monday; 13:00 UTC = 08:00 EDT (dst) / 09:00 EST... use 12:30
# UTC = 08:30 EDT — inside the 08:00–09:30 ET window under daylight time.
_MON_0830_ET = datetime(2025, 3, 17, 12, 30, 0, tzinfo=timezone.utc)
_TUE_0830_ET = datetime(2025, 3, 18, 12, 30, 0, tzinfo=timezone.utc)
_MON_1400_ET = datetime(2025, 3, 17, 18, 0, 0, tzinfo=timezone.utc)


class _Engine:
    class alerting:  # noqa: N801
        alerts: list = []

        @classmethod
        def alert(cls, tier, msg, ctx=None):
            cls.alerts.append((tier, msg))


def _patch_refresh(monkeypatch, calls, fail=False):
    def fake(engine):
        calls.append(engine)
        if fail:
            raise RuntimeError("scan boom")

    import arbiter.refresh.orchestrator as orch

    monkeypatch.setattr(orch, "run_monday_refresh", fake)


def test_fires_once_on_monday_premarket(monkeypatch):
    calls: list = []
    _patch_refresh(monkeypatch, calls)
    state = DaemonState()
    eng = _Engine()
    _maybe_monday_refresh(eng, _MON_0830_ET, state)
    _maybe_monday_refresh(eng, _MON_0830_ET, state)  # dedup — same ET date
    assert len(calls) == 1
    assert state.last_monday_refresh_date is not None


def test_skips_tuesday_and_after_open(monkeypatch):
    calls: list = []
    _patch_refresh(monkeypatch, calls)
    state = DaemonState()
    _maybe_monday_refresh(_Engine(), _TUE_0830_ET, state)
    _maybe_monday_refresh(_Engine(), _MON_1400_ET, state)
    assert calls == []
    assert state.last_monday_refresh_date is None


def test_failure_alerts_but_never_raises(monkeypatch):
    calls: list = []
    _patch_refresh(monkeypatch, calls, fail=True)
    _Engine.alerting.alerts.clear()
    state = DaemonState()
    _maybe_monday_refresh(_Engine(), _MON_0830_ET, state)  # must not raise
    assert len(calls) == 1
    assert _Engine.alerting.alerts  # warning surfaced
    # Dedup still set — a failing scan must not tight-loop the LLM call.
    assert state.last_monday_refresh_date is not None
