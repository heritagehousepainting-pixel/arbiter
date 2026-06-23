"""Tests for arbiter.safety.kill_switch (Lane 4c).

All network is mocked — no real HTTP calls.

Scenarios covered:
  - Reachable endpoint returning halted=True  → is_halted True
  - Reachable endpoint returning halted=False → is_halted False
  - Unreachable endpoint (ConnectError)       → is_halted True (fail-closed)
  - HTTP error response (non-200)             → is_halted True (fail-closed)
  - Malformed JSON body                       → is_halted True (fail-closed)
  - Missing "halted" key in JSON              → is_halted True (default True)
  - No kill_switch_url configured             → is_halted True (fail-closed)
  - Cache: fresh hit avoids second HTTP call
  - Cache: expired entry triggers fresh fetch
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from arbiter.config import Config
from arbiter.safety.kill_switch import KillSwitch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(kill_switch_url: str = "http://infra.example/kill-switch") -> Config:
    """Return a minimal Config with the given kill_switch_url."""
    return Config(
        live_trading=False,
        executor_backend="sim",
        db_path=":memory:",
        audit_path="/tmp/test_audit_ks.jsonl",
        metrics_path="/tmp/test_metrics_ks.jsonl",
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
        kill_switch_url=kill_switch_url,
        alert_webhook_url="",
    )


def _utcnow() -> datetime:
    return datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)


def _mock_response(*, halted: bool, status_code: int = 200) -> MagicMock:
    """Build a fake httpx.Response-like object."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = {"halted": halted}
    # raise_for_status raises HTTPStatusError on 4xx/5xx
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Tests — reachable endpoint
# ---------------------------------------------------------------------------

def test_halted_true_when_endpoint_returns_halted_true() -> None:
    """Reachable endpoint returning halted=true → is_halted() True."""
    cfg = _make_config()
    ks = KillSwitch(config=cfg)
    with patch("httpx.get", return_value=_mock_response(halted=True)) as mock_get:
        result = ks.is_halted(as_of=_utcnow())
    assert result is True
    mock_get.assert_called_once()


def test_halted_false_when_endpoint_returns_halted_false() -> None:
    """Reachable endpoint returning halted=false → is_halted() False."""
    cfg = _make_config()
    ks = KillSwitch(config=cfg)
    with patch("httpx.get", return_value=_mock_response(halted=False)):
        result = ks.is_halted(as_of=_utcnow())
    assert result is False


# ---------------------------------------------------------------------------
# Tests — fail-closed scenarios
# ---------------------------------------------------------------------------

def test_fail_closed_on_connect_error() -> None:
    """Unreachable endpoint (ConnectError) → is_halted() True (fail-closed)."""
    cfg = _make_config()
    ks = KillSwitch(config=cfg)
    with patch("httpx.get", side_effect=httpx.ConnectError("Connection refused")):
        result = ks.is_halted(as_of=_utcnow())
    assert result is True


def test_fail_closed_on_timeout() -> None:
    """Request timeout → is_halted() True (fail-closed)."""
    cfg = _make_config()
    ks = KillSwitch(config=cfg)
    with patch("httpx.get", side_effect=httpx.TimeoutException("timed out")):
        result = ks.is_halted(as_of=_utcnow())
    assert result is True


def test_fail_closed_on_http_500() -> None:
    """HTTP 500 response → is_halted() True (fail-closed)."""
    cfg = _make_config()
    ks = KillSwitch(config=cfg)
    with patch("httpx.get", return_value=_mock_response(halted=False, status_code=500)):
        result = ks.is_halted(as_of=_utcnow())
    assert result is True


def test_fail_closed_on_malformed_json() -> None:
    """Malformed JSON body → is_halted() True (fail-closed)."""
    cfg = _make_config()
    ks = KillSwitch(config=cfg)
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.side_effect = ValueError("not valid JSON")
    with patch("httpx.get", return_value=resp):
        result = ks.is_halted(as_of=_utcnow())
    assert result is True


def test_fail_closed_when_halted_key_missing() -> None:
    """JSON without 'halted' key → defaults to True (fail-closed default)."""
    cfg = _make_config()
    ks = KillSwitch(config=cfg)
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"status": "ok"}  # no "halted" key
    with patch("httpx.get", return_value=resp):
        result = ks.is_halted(as_of=_utcnow())
    # body.get("halted", True) → True
    assert result is True


def test_fail_closed_when_no_url_configured() -> None:
    """Empty kill_switch_url → fail-closed (True) without any HTTP call."""
    cfg = _make_config(kill_switch_url="")
    ks = KillSwitch(config=cfg)
    with patch("httpx.get") as mock_get:
        result = ks.is_halted(as_of=_utcnow())
    assert result is True
    mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — caching
# ---------------------------------------------------------------------------

def test_cache_avoids_second_http_call() -> None:
    """Within TTL, a second call must NOT hit the network again."""
    cfg = _make_config()
    ks = KillSwitch(config=cfg, cache_ttl_seconds=60.0)
    now = _utcnow()

    with patch("httpx.get", return_value=_mock_response(halted=False)) as mock_get:
        first = ks.is_halted(as_of=now)
        # Small time advance, still within TTL.
        second = ks.is_halted(as_of=now + timedelta(seconds=10))

    assert first is False
    assert second is False
    # Only one real HTTP call should have been made.
    assert mock_get.call_count == 1


def test_cache_expired_triggers_fresh_fetch() -> None:
    """After TTL elapses, the next call must re-query the endpoint."""
    cfg = _make_config()
    ks = KillSwitch(config=cfg, cache_ttl_seconds=5.0)
    now = _utcnow()

    with patch("httpx.get", return_value=_mock_response(halted=False)) as mock_get:
        ks.is_halted(as_of=now)
        # Advance past TTL.
        ks.is_halted(as_of=now + timedelta(seconds=10))

    assert mock_get.call_count == 2


def test_cache_not_populated_on_error() -> None:
    """A failed fetch must not poison the cache; next call must retry."""
    cfg = _make_config()
    ks = KillSwitch(config=cfg, cache_ttl_seconds=60.0)
    now = _utcnow()

    with patch("httpx.get", side_effect=httpx.ConnectError("down")) as mock_get:
        first = ks.is_halted(as_of=now)
        second = ks.is_halted(as_of=now + timedelta(seconds=1))

    assert first is True
    assert second is True
    # Both calls must hit the network (no stale cache from error).
    assert mock_get.call_count == 2
