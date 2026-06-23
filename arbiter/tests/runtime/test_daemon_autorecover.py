"""Daemon self-heal: a transient pause auto-recovers (autonomy), but the kill
switch is always respected and persistent failure caps out + pages."""
from __future__ import annotations

import sqlite3
import types
from datetime import datetime, timezone

from arbiter.runtime import daemon as D

_NOW = datetime(2026, 6, 22, 14, 0, 0, tzinfo=timezone.utc)


def _conn_with_latched_breaker() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE breaker_state (breaker_name TEXT PRIMARY KEY, latched INT, "
        "latched_at TEXT, reason TEXT)"
    )
    conn.execute(
        "INSERT INTO breaker_state VALUES ('broker_non_200', 1, 'NO_CLOCK', 'x')"
    )
    conn.commit()
    return conn


def _fake_engine(conn, resumed, alerts):
    return types.SimpleNamespace(
        paused=True,
        config=object(),
        conn=conn,
        resume=lambda: resumed.__setitem__("v", True),
        _fire_critical_alert=lambda **kw: alerts.append(kw.get("message", "")),
        # The success-path recovery notice goes through alerting directly (so it
        # does NOT re-pause); cap/risk paths still use _fire_critical_alert.
        alerting=types.SimpleNamespace(
            alert=lambda tier, message, ctx, *, as_of: alerts.append(message),
        ),
    )


def _patch_killswitch(monkeypatch, halted: bool):
    monkeypatch.setattr(
        "arbiter.safety.kill_switch.KillSwitch.is_halted",
        lambda self, *, as_of: halted,
    )


def _patch_breaker_reset(monkeypatch):
    monkeypatch.setattr(
        "arbiter.safety.breakers.CircuitBreaker.reset",
        lambda self, name, conn, *a, **k: conn.execute(
            "UPDATE breaker_state SET latched=0 WHERE breaker_name=?", (name,)
        ),
    )


def test_auto_recovers_when_killswitch_off(monkeypatch):
    _patch_killswitch(monkeypatch, halted=False)
    _patch_breaker_reset(monkeypatch)
    conn = _conn_with_latched_breaker()
    resumed, alerts = {"v": False}, []
    eng = _fake_engine(conn, resumed, alerts)
    st = D.DaemonState()

    D._auto_recover_if_paused(eng, _NOW, st)

    assert resumed["v"] is True
    assert st.consecutive_recoveries == 1
    assert conn.execute(
        "SELECT latched FROM breaker_state WHERE breaker_name='broker_non_200'"
    ).fetchone()[0] == 0
    assert any("auto-recovered" in m.lower() for m in alerts)


def test_recovery_does_not_repause_engine(monkeypatch):
    """Regression: the success-path recovery notice must NOT re-pause the engine
    it just resumed.  A critical alert returns an AutoPauseSentinel; routing the
    notice through ``_fire_critical_alert`` (which acts on the sentinel) would
    re-pause and burn the recovery budget.  This fake re-pauses ONLY if
    ``_fire_critical_alert`` is (wrongly) used for the success notice."""
    from arbiter.safety.alerting import AutoPauseSentinel

    _patch_killswitch(monkeypatch, halted=False)
    _patch_breaker_reset(monkeypatch)
    conn = _conn_with_latched_breaker()
    alerts: list[str] = []
    eng = types.SimpleNamespace(paused=True, config=object(), conn=conn)
    eng.resume = lambda: setattr(eng, "paused", False)
    # Realistic alerting: a critical alert returns a sentinel (no-op if discarded).
    eng.alerting = types.SimpleNamespace(
        alert=lambda tier, message, ctx, *, as_of: (
            alerts.append(message) or AutoPauseSentinel(message=message)
        )
    )
    # If the daemon wrongly routes the success notice here, the engine re-pauses.
    eng._fire_critical_alert = lambda **kw: (
        alerts.append(kw.get("message", "")), setattr(eng, "paused", True)
    )[0]
    st = D.DaemonState()

    D._auto_recover_if_paused(eng, _NOW, st)

    assert eng.paused is False  # resumed AND not re-paused
    assert st.consecutive_recoveries == 1
    assert any("auto-recovered" in m.lower() for m in alerts)


def test_respects_killswitch_halt(monkeypatch):
    _patch_killswitch(monkeypatch, halted=True)
    conn = _conn_with_latched_breaker()
    resumed, alerts = {"v": False}, []
    eng = _fake_engine(conn, resumed, alerts)
    st = D.DaemonState()

    D._auto_recover_if_paused(eng, _NOW, st)

    assert resumed["v"] is False  # kill switch halted -> stay paused
    assert st.consecutive_recoveries == 0


def test_caps_out_and_pages_once(monkeypatch):
    _patch_killswitch(monkeypatch, halted=False)
    _patch_breaker_reset(monkeypatch)
    conn = _conn_with_latched_breaker()
    resumed, alerts = {"v": False}, []
    eng = _fake_engine(conn, resumed, alerts)
    st = D.DaemonState()
    st.consecutive_recoveries = D._MAX_CONSECUTIVE_RECOVERIES  # at cap

    D._auto_recover_if_paused(eng, _NOW, st)
    D._auto_recover_if_paused(eng, _NOW, st)  # second call must NOT re-alert

    assert resumed["v"] is False  # capped -> do not resume
    assert sum("cap" in m.lower() for m in alerts) == 1  # paged exactly once


def test_risk_breaker_is_never_auto_recovered(monkeypatch):
    """A latched RISK breaker (daily_loss) must NOT auto-resume — stays paused + pages."""
    _patch_killswitch(monkeypatch, halted=False)
    _patch_breaker_reset(monkeypatch)
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE breaker_state (breaker_name TEXT PRIMARY KEY, latched INT, "
        "latched_at TEXT, reason TEXT)"
    )
    conn.execute("INSERT INTO breaker_state VALUES ('daily_loss', 1, 'NO_CLOCK', 'loss')")
    conn.commit()
    resumed, alerts = {"v": False}, []
    eng = _fake_engine(conn, resumed, alerts)
    st = D.DaemonState()

    D._auto_recover_if_paused(eng, _NOW, st)

    assert resumed["v"] is False  # risk breaker -> stay paused
    assert conn.execute(
        "SELECT latched FROM breaker_state WHERE breaker_name='daily_loss'"
    ).fetchone()[0] == 1  # NOT reset
    assert any("risk breaker" in m.lower() for m in alerts)


def test_noop_when_not_paused(monkeypatch):
    conn = _conn_with_latched_breaker()
    resumed, alerts = {"v": False}, []
    eng = _fake_engine(conn, resumed, alerts)
    eng.paused = False
    st = D.DaemonState()

    D._auto_recover_if_paused(eng, _NOW, st)
    assert resumed["v"] is False and st.consecutive_recoveries == 0
