"""MirofishHTTPClient tests — breaker, timeout/retry policy, bad-body
handling, scheme guard, and the loopback-only inference egress."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from arbiter.adapters.mirofish.egress import EgressViolation
from arbiter.adapters.mirofish.http_client import (
    CONNECT_RETRIES,
    MirofishBadResponse,
    MirofishHTTPClient,
    MirofishUnavailable,
)

from .conftest import httpx_connection_error, mirofish_response


def _ok_response(body: dict | list | str = None) -> MagicMock:
    """A MagicMock httpx.Response whose .json() returns *body*."""
    resp = MagicMock(spec=httpx.Response)
    resp.raise_for_status.return_value = None
    resp.json.return_value = (
        mirofish_response() if body is None else body
    )
    return resp


# ---------------------------------------------------------------------------
# Disabled / scheme guard
# ---------------------------------------------------------------------------


def test_analyze_raises_unavailable_when_no_endpoint() -> None:
    client = MirofishHTTPClient(endpoint=None)
    with pytest.raises(MirofishUnavailable):
        client.analyze("AAPL", "2025-06-15T14:00:00+00:00", "fp")


def test_analyze_raises_unavailable_on_schemeless_endpoint() -> None:
    """A scheme-less base (urlparse mis-reads host as scheme) fails closed."""
    client = MirofishHTTPClient(endpoint="localhost:8765")
    with patch("httpx.post") as mock_post:
        with pytest.raises(MirofishUnavailable):
            client.analyze("AAPL", "2025-06-15T14:00:00+00:00", "fp")
    mock_post.assert_not_called()  # never touches the network


# ---------------------------------------------------------------------------
# Inference egress (loopback-only) enforced through analyze
# ---------------------------------------------------------------------------


def test_analyze_rejects_nonlocal_endpoint() -> None:
    """A non-loopback (but allowlisted) endpoint raises EgressViolation,
    before any socket I/O."""
    client = MirofishHTTPClient(endpoint="https://data.sec.gov")
    with patch("httpx.post") as mock_post:
        with pytest.raises(EgressViolation):
            client.analyze("AAPL", "2025-06-15T14:00:00+00:00", "fp")
    mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Breaker bookkeeping
# ---------------------------------------------------------------------------


def test_breaker_fires_once_after_threshold() -> None:
    fired: list[int] = []
    client = MirofishHTTPClient(
        endpoint="http://localhost:8765",
        breaker=lambda: fired.append(1),
        breaker_threshold=3,
    )
    with patch("httpx.post", side_effect=httpx_connection_error()):
        for _ in range(3):
            with pytest.raises(httpx.ConnectError):
                client.analyze("AAPL", "2025-06-15T14:00:00+00:00", "fp")
    assert len(fired) == 1
    assert client.consecutive_failures == 3


def test_breaker_resets_on_success() -> None:
    client = MirofishHTTPClient(
        endpoint="http://localhost:8765", breaker_threshold=3
    )
    with patch("httpx.post", side_effect=httpx_connection_error()):
        for _ in range(2):
            with pytest.raises(httpx.ConnectError):
                client.analyze("AAPL", "2025-06-15T14:00:00+00:00", "fp")
    assert client.consecutive_failures == 2

    with patch("httpx.post", return_value=_ok_response()):
        client.analyze("AAPL", "2025-06-15T14:00:00+00:00", "fp")
    assert client.consecutive_failures == 0


# ---------------------------------------------------------------------------
# Timeout: a real failure, NOT retried
# ---------------------------------------------------------------------------


def test_timeout_is_failure_and_not_retried() -> None:
    """A read timeout means an in-flight 20-min run is underway — it must
    advance the breaker counter AND must not re-launch (call_count == 1)."""
    client = MirofishHTTPClient(endpoint="http://localhost:8765")
    with patch(
        "httpx.post", side_effect=httpx.TimeoutException("read timeout")
    ) as mock_post:
        with pytest.raises(httpx.TimeoutException):
            client.analyze("AAPL", "2025-06-15T14:00:00+00:00", "fp")
    assert mock_post.call_count == 1
    assert client.consecutive_failures == 1


# ---------------------------------------------------------------------------
# Connect-retry: cold socket retried up to CONNECT_RETRIES
# ---------------------------------------------------------------------------


def test_connect_error_retries_then_succeeds() -> None:
    """Two ConnectErrors then a success → retried, returns the body, breaker
    counter stays 0 (the final attempt succeeded)."""
    client = MirofishHTTPClient(endpoint="http://localhost:8765")
    side_effects = [
        httpx.ConnectError("cold socket"),
        httpx.ConnectError("cold socket"),
        _ok_response(),
    ]
    with patch("httpx.post", side_effect=side_effects) as mock_post:
        data = client.analyze("AAPL", "2025-06-15T14:00:00+00:00", "fp")
    assert mock_post.call_count == 3  # 1 + CONNECT_RETRIES
    assert "opinions" in data
    assert client.consecutive_failures == 0


def test_connect_error_exhausts_retries_and_records_failure() -> None:
    """ConnectError on every attempt → re-raised, breaker advanced by ONE
    streak (a single logical failure, not one per attempt)."""
    client = MirofishHTTPClient(endpoint="http://localhost:8765")
    with patch(
        "httpx.post", side_effect=httpx.ConnectError("cold socket")
    ) as mock_post:
        with pytest.raises(httpx.ConnectError):
            client.analyze("AAPL", "2025-06-15T14:00:00+00:00", "fp")
    assert mock_post.call_count == CONNECT_RETRIES + 1
    assert client.consecutive_failures == 1


# ---------------------------------------------------------------------------
# Bad body is NOT an outage — does not advance the breaker
# ---------------------------------------------------------------------------


def test_non_json_body_is_bad_response_not_outage() -> None:
    """A reachable 200 with a non-JSON body raises MirofishBadResponse and
    does NOT advance the consecutive-failure counter (service is up)."""
    client = MirofishHTTPClient(endpoint="http://localhost:8765")
    resp = MagicMock(spec=httpx.Response)
    resp.raise_for_status.return_value = None
    resp.json.side_effect = ValueError("not json")
    with patch("httpx.post", return_value=resp):
        with pytest.raises(MirofishBadResponse):
            client.analyze("AAPL", "2025-06-15T14:00:00+00:00", "fp")
    assert client.consecutive_failures == 0


def test_non_dict_body_is_bad_response_not_outage() -> None:
    """A JSON *list* (non-dict) top-level body is a bad response, not an
    outage."""
    client = MirofishHTTPClient(endpoint="http://localhost:8765")
    with patch("httpx.post", return_value=_ok_response(body=["a", "b"])):
        with pytest.raises(MirofishBadResponse):
            client.analyze("AAPL", "2025-06-15T14:00:00+00:00", "fp")
    assert client.consecutive_failures == 0
