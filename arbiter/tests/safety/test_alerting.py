"""Tests for arbiter.safety.alerting (Lane 4c).

All network is mocked — no real HTTP calls.

Scenarios covered:
  - info alert: audited, does NOT post to webhook, returns None
  - warning alert: audited, does NOT post to webhook, returns None
  - critical alert: audited, POSTs to webhook (mocked), returns AutoPauseSentinel
  - critical alert: webhook failure does NOT suppress AutoPauseSentinel
  - critical alert: no webhook URL configured → no POST, sentinel still returned
  - audit log contains correct tier/message/ctx for each tier
  - webhook POST payload structure is correct
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from arbiter.config import Config
from arbiter.db.audit import read_audit
from arbiter.safety.alerting import AlertTier, Alerting, AutoPauseSentinel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(
    alert_webhook_url: str = "http://infra.example/webhook",
) -> Config:
    return Config(
        live_trading=False,
        executor_backend="sim",
        db_path=":memory:",
        audit_path="/tmp/test_audit_alert_UNUSED.jsonl",
        metrics_path="/tmp/test_metrics_alert.jsonl",
        max_position_pct=0.05,
        max_sector_pct=0.20,
        max_gross_pct=0.80,
        max_open_positions=20,
        adv_cap_pct=0.02,
        alpaca_api_key="",
        alpaca_secret_key="",
        alpaca_paper_base_url="https://paper-api.alpaca.markets",
        alpaca_data_base_url="https://data.alpaca.markets",
        alpaca_timeout=20.0,
        edgar_user_agent="test",
        kill_switch_url="",
        alert_webhook_url=alert_webhook_url,
    )


_AS_OF = datetime(2026, 6, 18, 14, 0, 0, tzinfo=timezone.utc)


def _mock_post_ok() -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Tests — info tier
# ---------------------------------------------------------------------------

def test_info_alert_is_audited(tmp_path: Path) -> None:
    """info alert writes one record to the audit log."""
    audit_path = str(tmp_path / "audit.jsonl")
    alerting = Alerting(config=_make_config(), audit_path=audit_path)

    alerting.alert("info", "price feed lagging", {"lag_ms": 300}, as_of=_AS_OF)

    records = read_audit(audit_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["event"] == "alert.info"
    assert rec["payload"]["tier"] == "info"
    assert rec["payload"]["message"] == "price feed lagging"
    assert rec["payload"]["ctx"]["lag_ms"] == 300


def test_info_alert_does_not_post_webhook(tmp_path: Path) -> None:
    """info alert must NOT call the webhook endpoint."""
    audit_path = str(tmp_path / "audit.jsonl")
    alerting = Alerting(config=_make_config(), audit_path=audit_path)

    with patch("httpx.post") as mock_post:
        alerting.alert("info", "routine info", {}, as_of=_AS_OF)

    mock_post.assert_not_called()


def test_info_alert_returns_none(tmp_path: Path) -> None:
    """info alert returns None (no auto-pause)."""
    audit_path = str(tmp_path / "audit.jsonl")
    alerting = Alerting(config=_make_config(), audit_path=audit_path)

    result = alerting.alert("info", "all good", {}, as_of=_AS_OF)
    assert result is None


# ---------------------------------------------------------------------------
# Tests — warning tier
# ---------------------------------------------------------------------------

def test_warning_alert_is_audited(tmp_path: Path) -> None:
    """warning alert writes to the audit log with tier='warning'."""
    audit_path = str(tmp_path / "audit.jsonl")
    alerting = Alerting(config=_make_config(), audit_path=audit_path)

    alerting.alert("warning", "low advisor count", {"count": 1}, as_of=_AS_OF)

    records = read_audit(audit_path)
    assert len(records) == 1
    assert records[0]["event"] == "alert.warning"
    assert records[0]["payload"]["tier"] == "warning"


def test_warning_alert_does_not_post_webhook(tmp_path: Path) -> None:
    """warning alert must NOT call the webhook endpoint."""
    audit_path = str(tmp_path / "audit.jsonl")
    alerting = Alerting(config=_make_config(), audit_path=audit_path)

    with patch("httpx.post") as mock_post:
        alerting.alert("warning", "low count", {}, as_of=_AS_OF)

    mock_post.assert_not_called()


def test_warning_alert_returns_none(tmp_path: Path) -> None:
    """warning alert returns None (no auto-pause)."""
    audit_path = str(tmp_path / "audit.jsonl")
    alerting = Alerting(config=_make_config(), audit_path=audit_path)

    result = alerting.alert("warning", "slightly elevated vol", {}, as_of=_AS_OF)
    assert result is None


# ---------------------------------------------------------------------------
# Tests — critical tier
# ---------------------------------------------------------------------------

def test_critical_alert_is_audited(tmp_path: Path) -> None:
    """critical alert writes to the audit log."""
    audit_path = str(tmp_path / "audit.jsonl")
    alerting = Alerting(config=_make_config(), audit_path=audit_path)

    with patch("httpx.post", return_value=_mock_post_ok()):
        alerting.alert(
            "critical",
            "daily loss limit breached",
            {"loss_pct": 2.1},
            as_of=_AS_OF,
        )

    records = read_audit(audit_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["event"] == "alert.critical"
    assert rec["payload"]["tier"] == "critical"
    assert rec["payload"]["message"] == "daily loss limit breached"
    assert rec["payload"]["ctx"]["loss_pct"] == 2.1


def test_critical_alert_posts_to_webhook(tmp_path: Path) -> None:
    """critical alert must POST to the configured webhook URL."""
    audit_path = str(tmp_path / "audit.jsonl")
    alerting = Alerting(config=_make_config(), audit_path=audit_path)

    with patch("httpx.post", return_value=_mock_post_ok()) as mock_post:
        alerting.alert(
            "critical",
            "circuit breaker triggered",
            {"reason": "daily_loss"},
            as_of=_AS_OF,
        )

    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    assert call_kwargs[0][0] == "http://infra.example/webhook"
    posted_json = call_kwargs[1]["json"]
    assert posted_json["tier"] == "critical"
    assert posted_json["message"] == "circuit breaker triggered"
    assert posted_json["ctx"]["reason"] == "daily_loss"
    assert "as_of" in posted_json


def test_critical_alert_returns_auto_pause_sentinel(tmp_path: Path) -> None:
    """critical alert returns AutoPauseSentinel (triggers engine auto-pause)."""
    audit_path = str(tmp_path / "audit.jsonl")
    alerting = Alerting(config=_make_config(), audit_path=audit_path)

    with patch("httpx.post", return_value=_mock_post_ok()):
        result = alerting.alert(
            "critical",
            "broker error threshold exceeded",
            {},
            as_of=_AS_OF,
        )

    assert isinstance(result, AutoPauseSentinel)
    assert result.tier == "critical"
    assert "broker error threshold exceeded" in result.message


def test_critical_alert_sentinel_returned_even_on_webhook_failure(
    tmp_path: Path,
) -> None:
    """Webhook delivery failure must NOT suppress the AutoPauseSentinel."""
    audit_path = str(tmp_path / "audit.jsonl")
    alerting = Alerting(config=_make_config(), audit_path=audit_path)

    with patch("httpx.post", side_effect=httpx.ConnectError("webhook unreachable")):
        result = alerting.alert(
            "critical",
            "something bad happened",
            {},
            as_of=_AS_OF,
        )

    # AutoPauseSentinel must still be returned even though the webhook failed.
    assert isinstance(result, AutoPauseSentinel)


def test_critical_alert_no_webhook_url_still_returns_sentinel(
    tmp_path: Path,
) -> None:
    """With no webhook URL, critical alert still returns AutoPauseSentinel (no POST)."""
    audit_path = str(tmp_path / "audit.jsonl")
    alerting = Alerting(
        config=_make_config(alert_webhook_url=""), audit_path=audit_path
    )

    with patch("httpx.post") as mock_post:
        result = alerting.alert(
            "critical",
            "emergency halt",
            {},
            as_of=_AS_OF,
        )

    mock_post.assert_not_called()
    assert isinstance(result, AutoPauseSentinel)


# ---------------------------------------------------------------------------
# Tests — cross-tier audit isolation
# ---------------------------------------------------------------------------

def test_multiple_tiers_all_audited(tmp_path: Path) -> None:
    """All three tiers accumulate independently in the audit log."""
    audit_path = str(tmp_path / "audit.jsonl")
    alerting = Alerting(config=_make_config(), audit_path=audit_path)

    alerting.alert("info", "startup", {}, as_of=_AS_OF)
    alerting.alert("warning", "elevated latency", {"ms": 200}, as_of=_AS_OF)

    with patch("httpx.post", return_value=_mock_post_ok()):
        alerting.alert("critical", "halting", {}, as_of=_AS_OF)

    records = read_audit(audit_path)
    assert len(records) == 3
    tiers = [r["payload"]["tier"] for r in records]
    assert tiers == ["info", "warning", "critical"]


def test_webhook_payload_contains_as_of_timestamp(tmp_path: Path) -> None:
    """The webhook POST body must include the as_of timestamp."""
    audit_path = str(tmp_path / "audit.jsonl")
    alerting = Alerting(config=_make_config(), audit_path=audit_path)

    with patch("httpx.post", return_value=_mock_post_ok()) as mock_post:
        alerting.alert("critical", "ts check", {}, as_of=_AS_OF)

    posted_json = mock_post.call_args[1]["json"]
    assert posted_json["as_of"] == _AS_OF.isoformat()
