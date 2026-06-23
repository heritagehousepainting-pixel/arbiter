"""Shared fixtures for tests/execution/."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from arbiter.contract.seams import PaperOrder
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.shared.sim_executor import SimExecutor
from arbiter.types import HorizonBucket, OrderSide


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mem_conn():
    """In-memory SQLite connection with full schema migrated."""
    conn = get_connection(":memory:")
    run_migrations(conn)
    return conn


@pytest.fixture()
def tmp_audit(tmp_path: Path) -> Path:
    """Temporary audit JSONL path."""
    return tmp_path / "audit.jsonl"


# ---------------------------------------------------------------------------
# Clock fixture
# ---------------------------------------------------------------------------

class _FixedClock:
    """Deterministic clock returning a fixed UTC datetime."""

    def __init__(self, ts: datetime) -> None:
        self._ts = ts

    def now(self) -> datetime:
        return self._ts


@pytest.fixture()
def fixed_clock():
    """Clock pinned to 2024-01-15T12:00:00Z."""
    return _FixedClock(datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# PaperOrder factory
# ---------------------------------------------------------------------------

def make_paper_order(
    ticker: str = "AAPL",
    side: OrderSide = OrderSide.BUY,
    horizon: HorizonBucket = HorizonBucket.SHORT,
    entry_date: date | None = None,
    advisor_sig: str = "A1.insider",
    qty: float = 10.0,
    order_id: str | None = None,
) -> PaperOrder:
    """Build a minimal PaperOrder for testing."""
    from arbiter.db.helpers import generate_ulid
    from arbiter.execution.idempotency import dedup_hash as _dh
    import hashlib

    ed = entry_date or date(2024, 1, 15)
    raw = "|".join([ticker, side.value, horizon.value, str(ed), advisor_sig])
    dh = hashlib.sha256(raw.encode()).hexdigest()
    oid = order_id or generate_ulid()

    return PaperOrder(
        order_id=oid,
        dedup_hash=dh,
        ticker=ticker,
        side=side,
        qty=qty,
        horizon_bucket=horizon,
        entry_date=ed,
        advisor_signature=advisor_sig,
        exits={
            "stop_loss": 145.0,
            "horizon_expiry": str(date(2024, 3, 15)),
            "conviction_reversal": -0.3,
        },
    )


@pytest.fixture()
def paper_order():
    """A standard AAPL BUY PaperOrder."""
    return make_paper_order()


@pytest.fixture()
def sim_executor():
    """Fresh SimExecutor with $1,000,000 starting cash."""
    return SimExecutor(starting_cash=1_000_000.0)
