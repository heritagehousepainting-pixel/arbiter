"""Shared offline fixtures for the EDGAR ingest tests.

All HTTP is mocked; no real network, no real sleeps, no wall-clock reads.
"""
from __future__ import annotations

import pathlib
from unittest.mock import MagicMock

import pytest

from arbiter.config import Config


FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_config(user_agent: str = "ArbiterTest test@example.com") -> Config:
    """Build a minimal Config with the given (possibly empty) user-agent."""
    return Config(
        live_trading=False,
        executor_backend="sim",
        db_path="data/arbiter.db",
        audit_path="data/audit.jsonl",
        metrics_path="data/metrics.jsonl",
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
        edgar_user_agent=user_agent,
        kill_switch_url="",
        alert_webhook_url="",
    )


def make_resp(text: str, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    return resp


@pytest.fixture
def config() -> Config:
    return make_config()
