"""Hermeticity guard — the test suite must NEVER reach the live alert webhook.

Regression for the incident where a full ``pytest`` run POSTed real *critical*
alerts to the user's ntfy phone topic, because ``load_config()`` read the live
``ALERT_WEBHOOK_URL`` from ``.env`` and a test built a real ``Alerting`` from it.

These tests assert the two guards in ``conftest.py`` are in force:
  1. env scrub  → ``load_config().alert_webhook_url`` resolves to "".
  2. backstop   → the alerting module's ``httpx.post`` is neutralized so even a
     hardcoded real URL cannot put a packet on the wire.
"""
from __future__ import annotations

from datetime import datetime, timezone

from arbiter.config import load_config


_AS_OF = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)


def test_loaded_config_has_no_live_webhook_or_kill_switch():
    """The real .env values must be scrubbed for the whole test session."""
    cfg = load_config()
    assert cfg.alert_webhook_url == "", (
        "ALERT_WEBHOOK_URL leaked into tests — a real Alerting would page the phone"
    )
    assert cfg.kill_switch_url == ""


def test_critical_alert_to_real_host_does_not_egress(monkeypatch):
    """A critical alert hardcoded at the real ntfy host must NOT hit the network.

    The autouse ``_block_real_alert_webhook`` fixture drops POSTs to real infra
    hosts, so ``httpx.post`` is never reached. The auto-pause sentinel still
    returns (delivery-independent).
    """
    import dataclasses

    import httpx

    from arbiter.safety.alerting import Alerting, AutoPauseSentinel

    posted: list = []
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: posted.append((a, k)), raising=True
    )

    cfg = dataclasses.replace(
        load_config(),
        alert_webhook_url="https://ntfy.sh/should-never-be-hit",
        audit_path="/tmp/test_hermeticity_audit.jsonl",
    )
    result = Alerting(config=cfg).alert(
        "critical",
        "hermeticity probe — must not leave the machine",
        {"probe": True},
        as_of=_AS_OF,
    )
    assert isinstance(result, AutoPauseSentinel)
    assert posted == [], "alert webhook POST to a real host was NOT blocked"
