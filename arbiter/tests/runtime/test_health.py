"""Offline tests for the B-HEALTH observability watchdog (runtime/health.py).

All fixtures are temp files / a temp SQLite DB.  No network, no datetime.now()
— every check takes an explicit ``now`` so the tests are deterministic.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arbiter.runtime.health import (
    HealthMonitor,
    Finding,
)


UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _write_heartbeat(path: Path, *, now: datetime, paused: bool = False,
                     is_open: bool = True) -> None:
    path.write_text(json.dumps({
        "now": now.isoformat(),
        "is_open": is_open,
        "next_open": None,
        "next_close": None,
        "iteration_kind": "fast",
        "open_positions": 0,
        "paused": paused,
        "backoff_s": 0.0,
    }))


def _write_metrics(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _write_audit(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _make_orders_db(path: Path, orders: list[dict]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE orders (
            order_id TEXT PRIMARY KEY,
            dedup_hash TEXT,
            ticker TEXT, side TEXT, qty REAL,
            horizon_bucket TEXT, entry_date TEXT,
            advisor_signature TEXT, exits_json TEXT,
            status TEXT, created_at TEXT
        )
        """
    )
    for i, o in enumerate(orders):
        conn.execute(
            "INSERT INTO orders (order_id, dedup_hash, ticker, side, qty, "
            "horizon_bucket, entry_date, advisor_signature, exits_json, "
            "status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"o{i}", f"h{i}", o.get("ticker", "AAPL"), "BUY", 1.0,
             "MEDIUM", "2026-06-01", "sig", "{}",
             o.get("status", "FILLED"), o["created_at"]),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def artifacts(tmp_path):
    return {
        "heartbeat": tmp_path / "daemon.heartbeat",
        "metrics": tmp_path / "metrics.jsonl",
        "audit": tmp_path / "audit.jsonl",
        "db": tmp_path / "arbiter.db",
    }


def _monitor(artifacts, *, config_ok: bool = True) -> HealthMonitor:
    return HealthMonitor(
        heartbeat_path=str(artifacts["heartbeat"]),
        metrics_path=str(artifacts["metrics"]),
        audit_path=str(artifacts["audit"]),
        db_path=str(artifacts["db"]),
        alert_webhook_url="https://hook" if config_ok else "",
        kill_switch_url="https://kill" if config_ok else "",
        live_trading=False,
    )


# ---------------------------------------------------------------------------
# /health — heartbeat staleness
# ---------------------------------------------------------------------------

def test_health_unhealthy_on_stale_heartbeat(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    _write_heartbeat(artifacts["heartbeat"], now=now - timedelta(hours=3))
    mon = _monitor(artifacts)
    report = mon.health_report(now)
    assert report["healthy"] is False
    assert any("heartbeat" in r.lower() for r in report["reasons"])
    assert report["heartbeat_age_s"] == pytest.approx(3 * 3600, abs=1)


def test_health_healthy_on_fresh_heartbeat(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    _write_heartbeat(artifacts["heartbeat"], now=now - timedelta(seconds=30))
    _make_orders_db(artifacts["db"], [
        {"created_at": (now - timedelta(hours=2)).isoformat(), "status": "FILLED"},
    ])
    mon = _monitor(artifacts)
    report = mon.health_report(now)
    assert report["healthy"] is True
    assert report["reasons"] == []
    assert report["paused"] is False


def test_health_unhealthy_on_missing_heartbeat(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    mon = _monitor(artifacts)
    report = mon.health_report(now)
    assert report["healthy"] is False
    assert any("heartbeat" in r.lower() for r in report["reasons"])


def test_health_reports_paused_state(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    _write_heartbeat(artifacts["heartbeat"], now=now, paused=True)
    mon = _monitor(artifacts)
    report = mon.health_report(now)
    assert report["paused"] is True
    assert report["healthy"] is False
    assert any("pause" in r.lower() for r in report["reasons"])


def test_health_surfaces_last_cycle_and_fill(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    _write_heartbeat(artifacts["heartbeat"], now=now)
    cyc = (now - timedelta(minutes=10)).isoformat()
    _write_metrics(artifacts["metrics"], [
        {"event": "cycle_complete", "recorded_at": cyc,
         "ideas_processed": 3, "orders_submitted": 1, "opinions_gathered": 6},
    ])
    _make_orders_db(artifacts["db"], [
        {"created_at": (now - timedelta(hours=1)).isoformat(), "status": "FILLED"},
    ])
    mon = _monitor(artifacts)
    report = mon.health_report(now)
    assert report["last_cycle_at"] == cyc
    assert report["last_fill_at"] is not None


# ---------------------------------------------------------------------------
# Watchdogs — each fires on the right fixture, quiet on a healthy one
# ---------------------------------------------------------------------------

def test_watchdog_ingest_zero_rows_fires(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    _write_audit(artifacts["audit"], [
        {"ts": (now - timedelta(hours=1)).isoformat(), "event": "ingest",
         "payload": {"n_written": 0, "n_fetched": 0}},
    ])
    mon = _monitor(artifacts)
    findings = mon.run_watchdogs(now)
    codes = {f.code for f in findings}
    assert "ingest_zero_rows" in codes


def test_watchdog_ingest_zero_rows_quiet_when_rows_written(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    _write_audit(artifacts["audit"], [
        {"ts": (now - timedelta(hours=1)).isoformat(), "event": "ingest",
         "payload": {"n_written": 12, "n_fetched": 20}},
    ])
    _write_heartbeat(artifacts["heartbeat"], now=now)
    mon = _monitor(artifacts)
    findings = mon.run_watchdogs(now)
    assert "ingest_zero_rows" not in {f.code for f in findings}


def test_watchdog_no_fills_fires(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    _make_orders_db(artifacts["db"], [
        {"created_at": (now - timedelta(days=10)).isoformat(), "status": "FILLED"},
    ])
    mon = _monitor(artifacts)
    findings = mon.run_watchdogs(now)
    assert "no_recent_fills" in {f.code for f in findings}


def test_watchdog_no_fills_quiet_on_recent_fill(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    _make_orders_db(artifacts["db"], [
        {"created_at": (now - timedelta(days=1)).isoformat(), "status": "FILLED"},
    ])
    mon = _monitor(artifacts)
    findings = mon.run_watchdogs(now)
    assert "no_recent_fills" not in {f.code for f in findings}


def test_watchdog_high_fallback_rate_fires(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    rows = []
    # 8 fallback-proxy + 2 normal outcomes -> 80% fallback rate.
    for i in range(8):
        rows.append({"event": "attribution.fallback_proxy",
                     "recorded_at": (now - timedelta(hours=i)).isoformat(),
                     "idea_id": f"i{i}"})
    for i in range(2):
        rows.append({"event": "cycle_complete",
                     "recorded_at": (now - timedelta(hours=i)).isoformat(),
                     "orders_submitted": 1})
    _write_metrics(artifacts["metrics"], rows)
    mon = _monitor(artifacts)
    findings = mon.run_watchdogs(now)
    assert "high_fallback_proxy_rate" in {f.code for f in findings}


def test_watchdog_high_fallback_rate_quiet_when_low(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    rows = [
        {"event": "attribution.fallback_proxy",
         "recorded_at": now.isoformat(), "idea_id": "i0"},
    ]
    for i in range(20):
        rows.append({"event": "cycle_complete",
                     "recorded_at": (now - timedelta(hours=i)).isoformat(),
                     "orders_submitted": 1})
    _write_metrics(artifacts["metrics"], rows)
    mon = _monitor(artifacts)
    findings = mon.run_watchdogs(now)
    assert "high_fallback_proxy_rate" not in {f.code for f in findings}


def test_watchdog_stale_heartbeat_fires(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    _write_heartbeat(artifacts["heartbeat"], now=now - timedelta(hours=5))
    mon = _monitor(artifacts)
    findings = mon.run_watchdogs(now)
    assert "stale_heartbeat" in {f.code for f in findings}


def test_watchdog_stale_heartbeat_quiet_when_fresh(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    _write_heartbeat(artifacts["heartbeat"], now=now - timedelta(seconds=20))
    mon = _monitor(artifacts)
    findings = mon.run_watchdogs(now)
    assert "stale_heartbeat" not in {f.code for f in findings}


def test_watchdog_idle_cycles_zero_orders_fires(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    rows = [
        {"event": "cycle_complete",
         "recorded_at": (now - timedelta(hours=i)).isoformat(),
         "ideas_processed": 2, "orders_submitted": 0, "opinions_gathered": 4}
        for i in range(6)
    ]
    _write_metrics(artifacts["metrics"], rows)
    mon = _monitor(artifacts)
    findings = mon.run_watchdogs(now)
    assert "idle_zero_orders" in {f.code for f in findings}


def test_watchdog_idle_cycles_quiet_when_orders_flow(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    # Chronological order: oldest first, the most-recent cycle submitted 1.
    rows = [
        {"event": "cycle_complete",
         "recorded_at": (now - timedelta(hours=5 - i)).isoformat(),
         "orders_submitted": (1 if i == 5 else 0)}
        for i in range(6)
    ]
    _write_metrics(artifacts["metrics"], rows)
    mon = _monitor(artifacts)
    findings = mon.run_watchdogs(now)
    assert "idle_zero_orders" not in {f.code for f in findings}


def test_watchdog_still_paused_reminder_fires(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    _write_heartbeat(artifacts["heartbeat"], now=now, paused=True)
    mon = _monitor(artifacts)
    findings = mon.run_watchdogs(now)
    assert "still_paused" in {f.code for f in findings}


def test_watchdog_config_gate_fires_when_alerting_unset(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    _write_heartbeat(artifacts["heartbeat"], now=now)
    mon = _monitor(artifacts, config_ok=False)
    findings = mon.run_watchdogs(now)
    assert "unattended_config_gate" in {f.code for f in findings}


def test_watchdog_config_gate_quiet_when_set(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    _write_heartbeat(artifacts["heartbeat"], now=now)
    mon = _monitor(artifacts, config_ok=True)
    findings = mon.run_watchdogs(now)
    assert "unattended_config_gate" not in {f.code for f in findings}


def test_watchdogs_all_quiet_on_healthy_system(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    _write_heartbeat(artifacts["heartbeat"], now=now)
    _write_audit(artifacts["audit"], [
        {"ts": now.isoformat(), "event": "ingest",
         "payload": {"n_written": 5, "n_fetched": 9}},
    ])
    _write_metrics(artifacts["metrics"], [
        {"event": "cycle_complete", "recorded_at": now.isoformat(),
         "orders_submitted": 1},
    ])
    _make_orders_db(artifacts["db"], [
        {"created_at": (now - timedelta(days=1)).isoformat(), "status": "FILLED"},
    ])
    mon = _monitor(artifacts, config_ok=True)
    findings = mon.run_watchdogs(now)
    assert findings == [], [f.code for f in findings]


def test_finding_is_structured_for_alerting(artifacts):
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    _write_heartbeat(artifacts["heartbeat"], now=now - timedelta(hours=5))
    mon = _monitor(artifacts)
    findings = mon.run_watchdogs(now)
    f = next(f for f in findings if f.code == "stale_heartbeat")
    assert isinstance(f, Finding)
    assert f.tier in ("info", "warning", "critical")
    assert isinstance(f.message, str) and f.message
    assert isinstance(f.ctx, dict)


# ---------------------------------------------------------------------------
# /health wiring into the web server
# ---------------------------------------------------------------------------

def test_server_health_endpoint_reflects_state(tmp_path, monkeypatch):
    """The /health endpoint reads real artifacts via HealthMonitor."""
    from arbiter.web import server as server_mod

    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    hb = tmp_path / "hb"
    _write_heartbeat(hb, now=now - timedelta(hours=6))

    class _Cfg:
        live_trading = False
        db_path = str(tmp_path / "x.db")
        audit_path = str(tmp_path / "audit.jsonl")
        metrics_path = str(tmp_path / "metrics.jsonl")
        daemon_heartbeat_path = str(hb)
        alert_webhook_url = "https://hook"
        kill_switch_url = "https://kill"

    payload = server_mod.compute_health_payload(_Cfg(), now=now)
    assert payload["healthy"] is False
    assert any("heartbeat" in r.lower() for r in payload["reasons"])
