"""Graceful-skip behavior when EDGAR_USER_AGENT is unset.

``from_config_or_none`` returns None + one WARNING; direct construction still
raises ValueError (back-compat). Fully offline.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import structlog

from arbiter.ingest.edgar.client import EdgarClient

from tests.ingest.edgar.conftest import make_config


def test_from_config_or_none_empty_ua_returns_none_one_warning():
    cfg = make_config(user_agent="")
    with structlog.testing.capture_logs() as logs:
        client = EdgarClient.from_config_or_none(
            cfg, http_client=MagicMock(), sleep_fn=lambda _: None
        )
    assert client is None
    warnings = [
        e for e in logs
        if e.get("log_level") == "warning"
        and e.get("event") == "edgar.disabled_no_user_agent"
    ]
    assert len(warnings) == 1


def test_from_config_or_none_whitespace_ua_returns_none():
    cfg = make_config(user_agent="   ")
    client = EdgarClient.from_config_or_none(
        cfg, http_client=MagicMock(), sleep_fn=lambda _: None
    )
    assert client is None


def test_from_config_or_none_valid_ua_returns_client():
    cfg = make_config(user_agent="ArbiterTest test@example.com")
    client = EdgarClient.from_config_or_none(
        cfg, http_client=MagicMock(), sleep_fn=lambda _: None
    )
    assert isinstance(client, EdgarClient)
    assert client._user_agent == "ArbiterTest test@example.com"


def test_direct_construction_empty_ua_still_raises():
    cfg = make_config(user_agent="")
    with pytest.raises(ValueError, match="edgar_user_agent"):
        EdgarClient(config=cfg, http_client=MagicMock(), sleep_fn=lambda _: None)
