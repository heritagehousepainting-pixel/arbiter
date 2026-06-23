"""Shared fixtures/helpers for the MiroFish (A2) offline contract tests.

Everything here is deterministic and offline: in-memory SQLite, a fake idea
duck-type, and a canned ``/analyze`` response.  No real network, no real
sleeps, no ``datetime.now()``.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

import pytest

_UTC = timezone.utc

# Fixed information timestamp used across the suite.
AS_OF = datetime(2025, 6, 15, 14, 0, 0, tzinfo=_UTC)


class FakeIdea:
    """Minimal duck-type for Idea (Lane 13) — only the fields A2 reads."""

    def __init__(
        self,
        ticker: str = "AAPL",
        thesis: str = "Strong insider buying post-earnings",
        horizon_days: int = 45,
    ) -> None:
        self.ticker = ticker
        self.thesis = thesis
        self.horizon_days = horizon_days


def make_memory_db() -> sqlite3.Connection:
    """Return a fresh in-memory SQLite conn with the mirofish cache table.

    ``row_factory`` is ``sqlite3.Row`` (run_cache.get reads columns by name).
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mirofish_run_cache (
            id                   TEXT PRIMARY KEY,
            idea_fingerprint     TEXT NOT NULL,
            as_of_date           TEXT NOT NULL,
            run_id               TEXT NOT NULL,
            raw_opinions_json    TEXT NOT NULL,
            is_forward_test_only INTEGER NOT NULL DEFAULT 1,
            created_at           TEXT NOT NULL,
            UNIQUE (idea_fingerprint, as_of_date)
        )
        """
    )
    conn.commit()
    return conn


def mirofish_response(run_id: str = "RUN_TEST_01") -> dict[str, Any]:
    """Fake MiroFish /analyze response with SHORT + MEDIUM opinions."""
    return {
        "run_id": run_id,
        "opinions": [
            {
                "stance_score": 0.65,
                "confidence": 0.72,
                "horizon_days": 14,   # → SHORT bucket
                "rationale": "Insider buying cluster near 52-week low",
                "source_fingerprint": "fp_short_abc123",
            },
            {
                "stance_score": 0.50,
                "confidence": 0.60,
                "horizon_days": 60,   # → MEDIUM bucket
                "rationale": "Balance sheet improvement over 2 quarters",
                "source_fingerprint": "fp_medium_def456",
            },
        ],
    }


def httpx_connection_error() -> Any:
    """Return a side_effect that raises httpx.ConnectError on each call."""
    import httpx

    def _raise(*args: Any, **kwargs: Any) -> None:
        raise httpx.ConnectError("Connection refused by MiroFish")

    return _raise


# ---------------------------------------------------------------------------
# Pytest fixtures (thin wrappers over the helpers above)
# ---------------------------------------------------------------------------


@pytest.fixture
def as_of() -> datetime:
    return AS_OF


@pytest.fixture
def fake_idea() -> FakeIdea:
    return FakeIdea()


@pytest.fixture
def memory_db() -> sqlite3.Connection:
    conn = make_memory_db()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``time.sleep`` a no-op inside the http_client so connect-retry
    backoffs never block the suite (offline + fast)."""
    import arbiter.adapters.mirofish.http_client as hc

    monkeypatch.setattr(hc.time, "sleep", lambda *_a, **_k: None)


@pytest.fixture(autouse=True)
def _no_mirofish_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure MIROFISH_ENDPOINT is never set during the suite (deterministic;
    tests inject endpoints explicitly via the client)."""
    monkeypatch.delenv("MIROFISH_ENDPOINT", raising=False)
