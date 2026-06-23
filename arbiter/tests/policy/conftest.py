"""Shared fixtures for policy tests — Lane 12a."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from arbiter.config import Config
from arbiter.contract.seams import FusionOutput, TradingDecision
from arbiter.types import DegradationLevel, HorizonBucket


# ---------------------------------------------------------------------------
# Canonical test date / clock
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)
_ENTRY_DATE = date(2026, 6, 19)


class FakeClock:
    """Deterministic clock for tests — no datetime.now()."""

    def __init__(self, as_of: datetime = _AS_OF) -> None:
        self._as_of = as_of

    def now(self) -> datetime:
        return self._as_of


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def as_of() -> datetime:
    return _AS_OF


@pytest.fixture()
def entry_date() -> date:
    return _ENTRY_DATE


# ---------------------------------------------------------------------------
# Config fixture with default caps matching arbiter.toml
# ---------------------------------------------------------------------------

@pytest.fixture()
def cfg() -> Config:
    """Config with canonical default caps."""
    return Config(
        live_trading=False,
        executor_backend="sim",
        db_path=":memory:",
        audit_path="/dev/null",
        metrics_path="/dev/null",
        max_position_pct=0.05,     # 5%
        max_sector_pct=0.20,       # 20%
        max_gross_pct=0.80,        # 80%
        max_open_positions=20,
        adv_cap_pct=0.02,          # 2% of ADV
        alpaca_api_key="",
        alpaca_secret_key="",
        alpaca_paper_base_url="",
        alpaca_data_base_url="",
        alpaca_timeout=20.0,
        edgar_user_agent="",
        kill_switch_url="",
        alert_webhook_url="",
    )


# ---------------------------------------------------------------------------
# Gate fixtures
# ---------------------------------------------------------------------------

def _make_gate(allowed: bool, size_multiplier: float, level: DegradationLevel):
    """Return a gate callable that always returns the given TradingDecision."""
    decision = TradingDecision(
        allowed=allowed,
        size_multiplier=size_multiplier,
        level=level,
        reasons=[],
    )

    def gate(account, live_advisor_count: int) -> TradingDecision:
        return decision

    return gate


@pytest.fixture()
def normal_gate():
    """Gate: NORMAL, allowed=True, multiplier=1.0."""
    return _make_gate(True, 1.0, DegradationLevel.NORMAL)


@pytest.fixture()
def degraded_gate():
    """Gate: DEGRADED (1 advisor), allowed=True, multiplier=0.25."""
    return _make_gate(True, 0.25, DegradationLevel.DEGRADED)


@pytest.fixture()
def halted_gate():
    """Gate: HALTED, allowed=False, multiplier=0.0."""
    return _make_gate(False, 0.0, DegradationLevel.HALTED)


# ---------------------------------------------------------------------------
# FusionOutput factory
# ---------------------------------------------------------------------------

def make_fusion(
    bucket: HorizonBucket = HorizonBucket.SHORT,
    conviction: float = 0.5,
    cold_start: bool = False,
    n_opinions: int = 3,
) -> FusionOutput:
    return FusionOutput(
        bucket=bucket,
        conviction=conviction,
        dispersion=0.1,
        effective_n=float(n_opinions),
        n_opinions=n_opinions,
        advisor_contributions={"A1.insider": conviction * 0.6, "A1.congress": conviction * 0.4},
        vetoes=[],
        cold_start=cold_start,
    )


# ---------------------------------------------------------------------------
# ADV providers
# ---------------------------------------------------------------------------

def adv_always(adv_usd: float):
    """Return an adv_provider that always returns adv_usd."""
    def provider(ticker: str, as_of: datetime) -> float | None:
        return adv_usd
    return provider


def adv_missing():
    """Return an adv_provider that always returns None."""
    def provider(ticker: str, as_of: datetime) -> float | None:
        return None
    return provider


# ---------------------------------------------------------------------------
# Account stub
# ---------------------------------------------------------------------------

@pytest.fixture()
def account():
    return object()
