"""#3 — the daemon schedules the twice-weekly (Mon+Thu) robotics scan."""
from __future__ import annotations

from datetime import datetime, timezone

from arbiter.runtime.daemon import DaemonState, _maybe_robotics_scan

# 2025-03-17 Mon / 2025-03-20 Thu; 12:30 UTC = 08:30 EDT (DST) — inside the
# 08:00–09:30 ET pre-market window.
_MON_0830_ET = datetime(2025, 3, 17, 12, 30, 0, tzinfo=timezone.utc)
_THU_0830_ET = datetime(2025, 3, 20, 12, 30, 0, tzinfo=timezone.utc)
_TUE_0830_ET = datetime(2025, 3, 18, 12, 30, 0, tzinfo=timezone.utc)
_WED_0830_ET = datetime(2025, 3, 19, 12, 30, 0, tzinfo=timezone.utc)
_MON_1400_ET = datetime(2025, 3, 17, 18, 0, 0, tzinfo=timezone.utc)


class _Engine:
    class alerting:  # noqa: N801
        alerts: list = []

        @classmethod
        def alert(cls, tier, msg, ctx=None):
            cls.alerts.append((tier, msg))


def _patch_scan(monkeypatch, calls, fail=False):
    def fake(engine):
        calls.append(engine)
        if fail:
            raise RuntimeError("scan boom")

    import arbiter.robotics_signal.orchestrator as orch

    monkeypatch.setattr(orch, "run_robotics_scan", fake)


def test_fires_once_on_monday_premarket(monkeypatch):
    calls: list = []
    _patch_scan(monkeypatch, calls)
    state = DaemonState()
    _maybe_robotics_scan(_Engine(), _MON_0830_ET, state)
    _maybe_robotics_scan(_Engine(), _MON_0830_ET, state)  # dedup — same ET date
    assert len(calls) == 1
    assert state.last_robotics_scan_date is not None


def test_fires_on_thursday_premarket(monkeypatch):
    calls: list = []
    _patch_scan(monkeypatch, calls)
    state = DaemonState()
    _maybe_robotics_scan(_Engine(), _THU_0830_ET, state)
    assert len(calls) == 1


def test_skips_off_days_and_after_open(monkeypatch):
    calls: list = []
    _patch_scan(monkeypatch, calls)
    state = DaemonState()
    _maybe_robotics_scan(_Engine(), _TUE_0830_ET, state)
    _maybe_robotics_scan(_Engine(), _WED_0830_ET, state)
    _maybe_robotics_scan(_Engine(), _MON_1400_ET, state)  # after the window
    assert calls == []
    assert state.last_robotics_scan_date is None


def test_failure_alerts_but_never_raises(monkeypatch):
    calls: list = []
    _patch_scan(monkeypatch, calls, fail=True)
    _Engine.alerting.alerts.clear()
    state = DaemonState()
    _maybe_robotics_scan(_Engine(), _MON_0830_ET, state)  # must not raise
    assert len(calls) == 1
    assert _Engine.alerting.alerts  # warning surfaced
    # Dedup still set — a failing scan must not tight-loop the LLM call.
    assert state.last_robotics_scan_date is not None
