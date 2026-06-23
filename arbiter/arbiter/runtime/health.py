"""Observability & silent-failure watchdogs (WP B-HEALTH).

This module is the answer to the K1-observability FAIL: today ``/health`` is a
hardcoded ``{"status": "ok"}`` literal, so a comatose bot reads green, and every
silent-degradation surface (ingest writing 0 rows, idle ``orders_submitted=0``,
no fills, a high fallback-proxy attribution rate, a stale heartbeat, a stuck
pause, an unset alerting/kill-switch for unattended ops) fails invisibly.

``HealthMonitor`` is **self-contained**: it READS the durable artifacts the rest
of the system already writes — the daemon heartbeat file, ``metrics.jsonl``,
``audit.jsonl`` and the SQLite DB — and computes:

* a live ``/health`` report (``health_report``) that reflects REAL state:
  heartbeat age, last-cycle / last-fill time, paused flag, last ingest row count,
  recent fallback-proxy rate, and data-source-down signals — returning
  ``healthy: False`` with reasons when any tripwire fires; and
* a set of **watchdog checks** (``run_watchdogs``) — pure functions over the
  loaded artifacts that return structured :class:`Finding` objects the existing
  ``Alerting`` can emit.

There is **no engine coupling** — nothing here imports or mutates the engine, so
no engine edits are required.  Time is always supplied by the caller (``now``);
this module never calls ``datetime.now()`` (INTERFACES.md §11.1).  No network.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Tripwire thresholds (seconds / days / counts / rates).  Kept module-level so
# tests and ops can reason about them; conservative defaults for unattended ops.
# ---------------------------------------------------------------------------

# Heartbeat older than this => the daemon loop is wedged / dead.
HEARTBEAT_STALE_S: float = 30 * 60.0        # 30 min

# No fill in this many days while the bot is supposed to be trading.
NO_FILLS_DAYS: float = 5.0

# Consecutive recent cycles that all submitted 0 orders => possibly-inert.
IDLE_ZERO_ORDER_CYCLES: int = 5

# Fraction of recent attribution outcomes resolved via the horizon fallback
# proxy (no persisted opinion) above which the attribution is untrustworthy.
FALLBACK_PROXY_RATE: float = 0.5
FALLBACK_PROXY_MIN_SAMPLES: int = 5

# How many of the most-recent metrics rows the rate/idle checks look back over.
RECENT_METRICS_WINDOW: int = 50

_UTC = timezone.utc


# ---------------------------------------------------------------------------
# Structured finding (alerting-ready)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    """A single watchdog finding, shaped so ``Alerting.alert`` can emit it.

    Attributes
    ----------
    code:    Stable machine code (e.g. ``"stale_heartbeat"``).
    tier:    ``"info"`` | ``"warning"`` | ``"critical"`` — maps to alert tiers.
    message: Human-readable description.
    ctx:     Structured key/values (ages, counts, rates).
    """

    code: str
    tier: str
    message: str
    ctx: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp; return None on missing/sentinel/garbage."""
    if not value or not isinstance(value, str):
        return None
    if value in ("NO_CLOCK", "CLOCK_NOT_WIRED"):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt


def _age_s(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    return (now - ts).total_seconds()


# ---------------------------------------------------------------------------
# Artifact readers — every one is defensive: a missing / partial / corrupt
# artifact yields empty data, never an exception.
# ---------------------------------------------------------------------------

def read_heartbeat(path: str | None) -> dict[str, Any] | None:
    """Return the parsed heartbeat dict, or None if absent/unreadable."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_jsonl(path: str | None) -> list[dict[str, Any]]:
    """Read a JSONL file into a list of dicts; skip blank/corrupt lines."""
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def last_fill_at(db_path: str | None) -> datetime | None:
    """Return the timestamp of the most recent FILLED order, or None.

    Defensive: a missing DB / missing ``orders`` table yields None.
    """
    if not db_path or not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT MAX(created_at) FROM orders "
                "WHERE UPPER(status) IN ('FILLED', 'OPEN', 'ACTIVE')"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not rows or rows[0][0] is None:
        return None
    return _parse_ts(rows[0][0])


# ---------------------------------------------------------------------------
# Derived signals over loaded artifacts
# ---------------------------------------------------------------------------

def _last_event(metrics: list[dict], event: str) -> dict | None:
    """Return the most-recent metrics row with ``event``."""
    latest: dict | None = None
    latest_ts: datetime | None = None
    for row in metrics:
        if row.get("event") != event:
            continue
        ts = _parse_ts(row.get("recorded_at"))
        if ts is None:
            latest = latest or row
            continue
        if latest_ts is None or ts >= latest_ts:
            latest_ts = ts
            latest = row
    return latest


def _last_ingest(audit: list[dict]) -> dict | None:
    """Return the payload of the most-recent ``ingest`` audit row, or None."""
    latest: dict | None = None
    latest_ts: datetime | None = None
    for row in audit:
        if row.get("event") != "ingest":
            continue
        ts = _parse_ts(row.get("ts"))
        if latest is None or (ts is not None and (latest_ts is None or ts >= latest_ts)):
            latest = row
            latest_ts = ts
    return latest.get("payload", {}) if latest else None


def _recent_cycles(metrics: list[dict], n: int) -> list[dict]:
    """Return the last ``n`` ``cycle_complete`` rows in chronological order."""
    cycles = [r for r in metrics if r.get("event") == "cycle_complete"]
    return cycles[-n:]


def _fallback_proxy_rate(metrics: list[dict], n: int) -> tuple[float, int]:
    """(rate, sample_count) of fallback-proxy outcomes vs total resolved.

    The denominator is fallback-proxy events + cycle_complete events in the
    recent window (a cheap proxy for "resolution activity"): a clean run has
    near-zero fallback events, a broken-attribution run is dominated by them.
    """
    recent = metrics[-n:] if n else metrics
    fallback = sum(1 for r in recent if r.get("event") == "attribution.fallback_proxy")
    cycles = sum(1 for r in recent if r.get("event") == "cycle_complete")
    total = fallback + cycles
    if total == 0:
        return 0.0, 0
    return fallback / total, total


# ---------------------------------------------------------------------------
# HealthMonitor
# ---------------------------------------------------------------------------

@dataclass
class HealthMonitor:
    """Reads durable artifacts and computes health + watchdog findings.

    All paths and config flags are injected so the monitor is fully offline
    and decoupled from the engine.  Build one from a ``Config`` with
    :meth:`from_config`.
    """

    heartbeat_path: str | None
    metrics_path: str | None
    audit_path: str | None
    db_path: str | None
    alert_webhook_url: str = ""
    kill_switch_url: str = ""
    live_trading: bool = False

    @classmethod
    def from_config(cls, config: Any) -> "HealthMonitor":
        """Build a monitor from an Arbiter ``Config`` (duck-typed)."""
        return cls(
            heartbeat_path=getattr(config, "daemon_heartbeat_path", None),
            metrics_path=getattr(config, "metrics_path", None),
            audit_path=getattr(config, "audit_path", None),
            db_path=getattr(config, "db_path", None),
            alert_webhook_url=getattr(config, "alert_webhook_url", "") or "",
            kill_switch_url=getattr(config, "kill_switch_url", "") or "",
            live_trading=bool(getattr(config, "live_trading", False)),
        )

    # -- loading ---------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        return {
            "heartbeat": read_heartbeat(self.heartbeat_path),
            "metrics": read_jsonl(self.metrics_path),
            "audit": read_jsonl(self.audit_path),
            "last_fill": last_fill_at(self.db_path),
        }

    # -- live /health ----------------------------------------------------

    def health_report(self, now: datetime) -> dict[str, Any]:
        """Compute the live ``/health`` payload reflecting REAL state.

        Returns a JSON-serialisable dict with ``healthy`` (bool) and ``reasons``
        (list[str]) plus diagnostic fields.  ``healthy`` is False when any
        tripwire fires — a comatose bot can no longer read green.
        """
        data = self._load()
        hb = data["heartbeat"]
        metrics = data["metrics"]
        reasons: list[str] = []

        hb_ts = _parse_ts(hb.get("now")) if hb else None
        hb_age = _age_s(hb_ts, now)
        paused = bool(hb.get("paused")) if hb else None

        if hb is None:
            reasons.append("no heartbeat file (daemon not running?)")
        elif hb_age is None:
            reasons.append("heartbeat has no parseable timestamp")
        elif hb_age > HEARTBEAT_STALE_S:
            reasons.append(
                f"heartbeat stale ({hb_age / 60:.0f} min old, "
                f"limit {HEARTBEAT_STALE_S / 60:.0f} min)"
            )

        if paused:
            reasons.append("engine is paused")

        last_cycle = _last_event(metrics, "cycle_complete")
        last_cycle_at = last_cycle.get("recorded_at") if last_cycle else None

        last_fill = data["last_fill"]
        last_fill_iso = last_fill.isoformat() if last_fill else None

        ingest = _last_ingest(data["audit"])
        last_ingest_rows = ingest.get("n_written") if ingest else None

        rate, samples = _fallback_proxy_rate(metrics, RECENT_METRICS_WINDOW)
        if samples >= FALLBACK_PROXY_MIN_SAMPLES and rate > FALLBACK_PROXY_RATE:
            reasons.append(f"high fallback-proxy attribution rate ({rate:.0%})")

        data_source_down = ingest is not None and (last_ingest_rows == 0)
        if data_source_down:
            reasons.append("last ingest wrote 0 rows (data source down?)")

        return {
            "healthy": not reasons,
            "reasons": reasons,
            "mode": "live" if self.live_trading else "sim",
            "heartbeat_age_s": hb_age,
            "heartbeat_at": hb.get("now") if hb else None,
            "paused": paused,
            "last_cycle_at": last_cycle_at,
            "last_fill_at": last_fill_iso,
            "last_ingest_rows": last_ingest_rows,
            "fallback_proxy_rate": round(rate, 4),
            "data_source_down": data_source_down,
        }

    # -- watchdogs -------------------------------------------------------

    def run_watchdogs(self, now: datetime) -> list[Finding]:
        """Run all watchdog checks and return the firing findings.

        Pure over the loaded artifacts; the caller (daemon / cron / ops loop)
        emits each finding via the existing ``Alerting`` — this module does not
        edit or import the daemon.
        """
        data = self._load()
        findings: list[Finding] = []
        findings += check_stale_heartbeat(data["heartbeat"], now)
        findings += check_ingest_zero_rows(data["audit"])
        findings += check_no_recent_fills(data["last_fill"], now)
        findings += check_high_fallback_rate(data["metrics"])
        findings += check_idle_zero_orders(data["metrics"])
        findings += check_still_paused(data["heartbeat"])
        findings += check_unattended_config_gate(
            self.alert_webhook_url, self.kill_switch_url, self.live_trading
        )
        return findings

    def emit_findings(self, alerting: Any, now: datetime) -> list[Finding]:
        """Run the watchdogs and fire each finding through ``alerting``.

        ``alerting`` must expose ``alert(tier, message, ctx, *, as_of)`` (the
        existing ``arbiter.safety.alerting.Alerting``).  Wiring is optional —
        callers without an alerting instance can use ``run_watchdogs`` directly.
        """
        findings = self.run_watchdogs(now)
        for f in findings:
            try:
                alerting.alert(f.tier, f.message, {"code": f.code, **f.ctx}, as_of=now)
            except Exception:  # noqa: BLE001  (a bad sink must not break the sweep)
                continue
        return findings


# ---------------------------------------------------------------------------
# Watchdog pure functions — each returns 0 or 1 Finding.
# ---------------------------------------------------------------------------

def check_stale_heartbeat(hb: dict | None, now: datetime) -> list[Finding]:
    if hb is None:
        return [Finding(
            code="stale_heartbeat", tier="critical",
            message="no heartbeat file — the daemon may be dead",
            ctx={"age_s": None},
        )]
    age = _age_s(_parse_ts(hb.get("now")), now)
    if age is None or age > HEARTBEAT_STALE_S:
        return [Finding(
            code="stale_heartbeat", tier="critical",
            message=f"daemon heartbeat is stale ({(age or 0) / 60:.0f} min old)",
            ctx={"age_s": age, "limit_s": HEARTBEAT_STALE_S},
        )]
    return []


def check_ingest_zero_rows(audit: list[dict]) -> list[Finding]:
    ingest = _last_ingest(audit)
    if ingest is None:
        return []
    n_written = ingest.get("n_written")
    if n_written == 0:
        return [Finding(
            code="ingest_zero_rows", tier="warning",
            message="last ingest wrote 0 rows (Form-4 / Congress source down?)",
            ctx={"n_written": n_written, "n_fetched": ingest.get("n_fetched")},
        )]
    return []


def check_no_recent_fills(last_fill: datetime | None, now: datetime) -> list[Finding]:
    age = _age_s(last_fill, now)
    if last_fill is None or (age is not None and age > NO_FILLS_DAYS * 86400):
        days = (age / 86400) if age is not None else None
        return [Finding(
            code="no_recent_fills", tier="warning",
            message=(
                "no fills ever recorded" if last_fill is None
                else f"no fills in {days:.1f} days"
            ),
            ctx={"last_fill_age_days": days, "limit_days": NO_FILLS_DAYS},
        )]
    return []


def check_high_fallback_rate(metrics: list[dict]) -> list[Finding]:
    rate, samples = _fallback_proxy_rate(metrics, RECENT_METRICS_WINDOW)
    if samples >= FALLBACK_PROXY_MIN_SAMPLES and rate > FALLBACK_PROXY_RATE:
        return [Finding(
            code="high_fallback_proxy_rate", tier="warning",
            message=(
                f"{rate:.0%} of recent outcomes resolved via the horizon "
                "fallback proxy — attribution is degraded"
            ),
            ctx={"rate": round(rate, 4), "samples": samples,
                 "limit": FALLBACK_PROXY_RATE},
        )]
    return []


def check_idle_zero_orders(metrics: list[dict]) -> list[Finding]:
    cycles = _recent_cycles(metrics, IDLE_ZERO_ORDER_CYCLES)
    if len(cycles) < IDLE_ZERO_ORDER_CYCLES:
        return []
    if all((c.get("orders_submitted") or 0) == 0 for c in cycles):
        return [Finding(
            code="idle_zero_orders", tier="warning",
            message=(
                f"{IDLE_ZERO_ORDER_CYCLES} consecutive cycles submitted 0 "
                "orders — the bot may be silently inert"
            ),
            ctx={"cycles": len(cycles)},
        )]
    return []


def check_still_paused(hb: dict | None) -> list[Finding]:
    if hb and bool(hb.get("paused")):
        return [Finding(
            code="still_paused", tier="warning",
            message="engine is still paused — manual resume may be required",
            ctx={"paused": True},
        )]
    return []


def check_unattended_config_gate(
    alert_webhook_url: str, kill_switch_url: str, live_trading: bool
) -> list[Finding]:
    """Startup gate: for unattended ops the alerting webhook (and, when live,
    the kill-switch) must be configured — otherwise a critical alert / auto-pause
    has nowhere to go and the operator is never notified."""
    missing: list[str] = []
    if not alert_webhook_url:
        missing.append("alert_webhook_url")
    if live_trading and not kill_switch_url:
        missing.append("kill_switch_url")
    if missing:
        return [Finding(
            code="unattended_config_gate", tier="warning",
            message=(
                "unattended-ops config gate: "
                + ", ".join(missing)
                + " is unset (alerts/kill-switch have nowhere to go)"
            ),
            ctx={"missing": missing},
        )]
    return []
