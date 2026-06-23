"""Phase-2 persistence integration test — WP-D.

Verifies the engine/cycle wiring of the three Phase-2 modules:
  - idea_store: ideas are persisted with their lifecycle state,
  - position_store: sim_positions is populated after a fill and drives
    status()["open_positions"],
  - cross-run dedupe: a held ticker does not produce a fresh BUY idea on the
    next cycle.

Fully offline (FixtureSource PIT + BacktestClock + tmp SQLite) and fast.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arbiter.config import load_config
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.connection import get_connection
from arbiter.db.helpers import generate_ulid
from arbiter.db.migrate import run_migrations
from arbiter.engine import build_engine
from arbiter.ingest.writer import write_filing

_UTC = timezone.utc
_AS_OF = datetime(2025, 3, 15, 12, 0, 0, tzinfo=_UTC)


def _seed_cluster_buy(
    conn: sqlite3.Connection,
    ticker: str = "AAPL",
    n_buyers: int = 3,
) -> None:
    """Insert n_buyers distinct insider Form 4 buys within 30 days of as_of."""
    for i in range(n_buyers):
        raw = {
            "source": "form4",
            "ticker": ticker,
            "person_id": generate_ulid(),
            "filing_ts": (_AS_OF - timedelta(days=5 + i)).isoformat(),
            "txn_type": "P",
            "shares": 1000.0,
            "price": 150.0,
            "amount_low": 500_000.0,
            "amount_high": 600_000.0,
            "is_10b5_1": False,
            "is_amendment": False,
            "accession": generate_ulid(),
            "raw_json": None,
        }
        write_filing(conn, raw, lambda: _AS_OF.isoformat())


def _build_pit(ticker: str = "AAPL") -> PITGateway:
    fixture = FixtureSource()
    ts_seed = _AS_OF - timedelta(days=1)
    fixture.add("price_close", ticker, ts_seed, 150.0)
    fixture.add("price_open", ticker, ts_seed, 150.0)
    fixture.add("spread", ticker, ts_seed, 0.01)
    fixture.add("adv_20d", ticker, ts_seed, 10_000_000.0)

    pit = PITGateway()
    pit.register_source("price_close", fixture)
    pit.register_source("price_open", fixture)
    pit.register_source("spread", fixture)
    pit.register_source("adv_20d", fixture)
    return pit


@pytest.fixture()
def engine_and_conn(tmp_path: Path):
    db_path = str(tmp_path / "phase2.db")
    config = dataclasses.replace(
        load_config(),
        live_trading=False,
        executor_backend="sim",
        db_path=db_path,
        audit_path=str(tmp_path / "audit.jsonl"),
        metrics_path=str(tmp_path / "metrics.jsonl"),
    )
    clock = BacktestClock(_AS_OF)
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    _seed_cluster_buy(conn, ticker="AAPL", n_buyers=3)
    pit = _build_pit("AAPL")
    eng = build_engine(config, conn=conn, pit=pit, clock=clock)
    return eng, conn


def test_ideas_persisted_with_states(engine_and_conn):
    """After a cycle that fills, the ideas table has the idea in MONITORED."""
    eng, conn = engine_and_conn

    result = eng.run_cycle(as_of=_AS_OF)
    assert result.orders_submitted >= 1, (
        f"expected a fill; errors={result.errors}"
    )

    rows = conn.execute(
        "SELECT idea_id, ticker, state FROM ideas WHERE ticker = 'AAPL'"
    ).fetchall()
    assert len(rows) >= 1, "ideas table should have at least one AAPL row"
    states = {r["state"] for r in rows}
    # The filled idea must have advanced all the way to MONITORED and been
    # persisted there via the on_transition callback.
    assert "MONITORED" in states, f"expected a MONITORED idea, got {states}"


def test_sim_positions_populated_after_fill(engine_and_conn):
    """A fill snapshots into sim_positions and drives status()."""
    eng, conn = engine_and_conn

    eng.run_cycle(as_of=_AS_OF)

    pos_rows = conn.execute(
        "SELECT ticker, shares FROM sim_positions WHERE shares > 0"
    ).fetchall()
    assert len(pos_rows) >= 1, "sim_positions should be populated after a fill"
    assert any(r["ticker"] == "AAPL" for r in pos_rows)

    # status() open_positions must come from the durable store and match.
    status = eng.status()
    assert status["open_positions"] == len(pos_rows), (
        f"status open_positions {status['open_positions']} != "
        f"durable count {len(pos_rows)}"
    )
    assert status["open_positions"] >= 1


def test_status_open_positions_from_durable_store(tmp_path: Path):
    """open_positions reflects the durable store even with an empty executor.

    Proves status() reads sim_positions, not the in-memory executor: we seed a
    durable position row directly and confirm status() reports it without any
    in-memory position existing.
    """
    db_path = str(tmp_path / "durable.db")
    config = dataclasses.replace(
        load_config(),
        live_trading=False,
        executor_backend="sim",
        db_path=db_path,
        audit_path=str(tmp_path / "audit.jsonl"),
        metrics_path=str(tmp_path / "metrics.jsonl"),
    )
    clock = BacktestClock(_AS_OF)
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    pit = _build_pit("AAPL")
    eng = build_engine(config, conn=conn, pit=pit, clock=clock)

    # No fills yet → durable store empty → 0 open positions.
    assert eng.status()["open_positions"] == 0

    # Write a durable position row directly (not in the executor).
    conn.execute(
        "INSERT INTO sim_positions (ticker, shares, avg_price, updated_at) "
        "VALUES ('MSFT', 10, 300.0, ?)",
        (_AS_OF.isoformat(),),
    )
    conn.commit()

    assert eng.status()["open_positions"] == 1


def test_second_cycle_does_not_double_buy_held_ticker(engine_and_conn):
    """A second run must not create a fresh BUY idea for an already-held name."""
    eng, conn = engine_and_conn

    eng.run_cycle(as_of=_AS_OF)
    ideas_after_first = conn.execute(
        "SELECT COUNT(*) FROM ideas WHERE ticker = 'AAPL'"
    ).fetchone()[0]
    orders_after_first = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE ticker = 'AAPL'"
    ).fetchone()[0]
    assert ideas_after_first >= 1
    assert orders_after_first >= 1

    # Second cycle, same as_of. AAPL is now held → no new idea, no new order.
    eng.run_cycle(as_of=_AS_OF)
    ideas_after_second = conn.execute(
        "SELECT COUNT(*) FROM ideas WHERE ticker = 'AAPL'"
    ).fetchone()[0]
    orders_after_second = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE ticker = 'AAPL'"
    ).fetchone()[0]

    assert ideas_after_second == ideas_after_first, (
        "second cycle created a new idea for a held ticker (double-buy)"
    )
    assert orders_after_second == orders_after_first, (
        "second cycle created a new order for a held ticker (double-buy)"
    )
