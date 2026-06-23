"""Shared pytest fixtures for the MiroFish A2 service (offline-only).

Kept deliberately minimal: an env-clearing autouse fixture (so no test can
accidentally read a real key or hit the network) and a fixed as_of. Each build
lane owns the richer fixtures specific to its own tests.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

_SECRET_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "ALPACA_DATA_FEED",
    "EDGAR_USER_AGENT",
    "MIROFISH_MODEL",
    "MIROFISH_HOST",
    "MIROFISH_PORT",
    "MIROFISH_FAKE_LLM",
    "MIROFISH_CACHE_TTL_SECONDS",
)


@pytest.fixture(autouse=True)
def clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset every key/config env var so tests are hermetic."""
    for name in _SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def as_of_utc() -> datetime:
    """A fixed tz-aware UTC information timestamp for deterministic tests."""
    return datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
