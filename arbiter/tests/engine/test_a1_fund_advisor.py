"""Engine-level wiring tests for the A1.fund (form13f) advisor — OFFLINE.

Tests:
  1. Unit: _build_a1_fund_fn returns a callable that emits an A1.fund opinion
     for a seeded form13f filing.
  2. Orphan-attribution regression: run_cycle with a form13f-seeded DB spawns a
     LONG-bucket idea and persists the A1.fund opinion LINKED to that idea
     (idea_id set, no orphan) — mirrors test_a3_wiring.py's A3 orphan test.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arbiter.config import load_config
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.connection import get_connection
from arbiter.db.helpers import generate_ulid
from arbiter.db.migrate import run_migrations
from arbiter.engine import build_engine

_UTC = timezone.utc
_AS_OF = datetime(2026, 6, 23, 12, 0, 0, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_form13f_filing(db_path: str) -> None:
    """Insert a single form13f filing into the DB at db_path."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO filings "
            "(id, source, ticker, person_id, filing_ts, txn_type, "
            "txn_idx, shares, is_10b5_1, is_amendment, is_superseded, "
            "accession, raw_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                generate_ulid(),
                "form13f",
                "NVDA",
                "p_fund1",
                "2026-05-15T00:00:00+00:00",
                "P",
                0,
                1000,
                0,
                0,
                0,
                "acc_fund1",
                json.dumps({
                    "reason": "new",
                    "book_fraction": 0.5,
                    "value_usd": 60_000_000,
                }),
                "2026-05-15T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _build_pit(ticker: str) -> PITGateway:
    fx = FixtureSource()
    ts = _AS_OF - timedelta(days=1)
    fx.add("price_close", ticker, ts, 800.0)
    fx.add("price_open", ticker, ts, 800.0)
    fx.add("spread", ticker, ts, 0.01)
    fx.add("adv_20d", ticker, ts, 20_000_000.0)
    pit = PITGateway()
    for src in ("price_close", "price_open", "spread", "adv_20d"):
        pit.register_source(src, fx)
    return pit


def _make_engine(tmp_path: Path, ticker: str = "NVDA"):
    db_path = str(tmp_path / "fund.db")
    config = dataclasses.replace(
        load_config(),
        live_trading=False,
        executor_backend="sim",
        db_path=db_path,
        audit_path=str(tmp_path / "audit.jsonl"),
        metrics_path=str(tmp_path / "metrics.jsonl"),
        kill_switch_url="",
        alert_webhook_url="",
    )
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    pit = _build_pit(ticker)
    eng = build_engine(config, conn=conn, pit=pit, clock=BacktestClock(_AS_OF))
    return eng, conn, db_path


# ---------------------------------------------------------------------------
# Unit test: _build_a1_fund_fn
# ---------------------------------------------------------------------------

def test_a1_fund_fn_emits_opinion(tmp_path):
    """_build_a1_fund_fn callable emits an Opinion with advisor_id='A1.fund'."""
    from arbiter.engine.advisors import _build_a1_fund_fn

    db_path = str(tmp_path / "unit.db")
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    conn.close()

    _seed_form13f_filing(db_path)

    fn = _build_a1_fund_fn(db_path, pit=None, clock=BacktestClock(_AS_OF))
    op = fn()

    assert op is not None, "Expected an opinion but got None"
    assert op.advisor_id == "A1.fund"
    assert op.ticker == "NVDA"


def test_a1_fund_fn_returns_none_when_no_signals(tmp_path):
    """Returns None when there are no form13f filings."""
    from arbiter.engine.advisors import _build_a1_fund_fn

    db_path = str(tmp_path / "empty.db")
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    conn.close()

    fn = _build_a1_fund_fn(db_path, pit=None, clock=BacktestClock(_AS_OF))
    op = fn()
    assert op is None


# ---------------------------------------------------------------------------
# Orphan-attribution regression: A1.fund opinion must link to its spawned idea
# ---------------------------------------------------------------------------

def test_a1_fund_spawns_long_idea_and_links_opinion(tmp_path):
    """A form13f-seeded DB causes run_cycle to SPAWN a LONG-bucket idea for
    NVDA and persist the A1.fund opinion LINKED to that idea (idea_id set, not
    orphaned).

    Mirrors test_a3_wiring.py::test_a3_spawns_short_idea_and_links_opinion
    for the 180d/LONG bucket.
    """
    eng, conn, db_path = _make_engine(tmp_path, ticker="NVDA")
    _seed_form13f_filing(db_path)

    eng.run_cycle(as_of=_AS_OF)

    # A LONG-bucket idea was spawned for NVDA.
    idea = conn.execute(
        "SELECT idea_id, dedupe_key_bucket FROM ideas "
        "WHERE dedupe_key_ticker = 'NVDA' AND is_superseded = 0"
    ).fetchone()
    assert idea is not None, "A1.fund did not spawn an idea for NVDA"
    assert idea["dedupe_key_bucket"] == "LONG", (
        f"Expected LONG bucket, got {idea['dedupe_key_bucket']!r}"
    )

    # The A1.fund opinion was persisted AND linked to that idea (not orphaned).
    op = conn.execute(
        "SELECT advisor_id, idea_id FROM opinions "
        "WHERE advisor_id = 'A1.fund' AND ticker = 'NVDA'"
    ).fetchone()
    assert op is not None, "A1.fund opinion was not persisted"
    assert op["idea_id"] == idea["idea_id"], (
        f"A1.fund opinion orphaned from its idea: "
        f"op.idea_id={op['idea_id']!r}, idea.idea_id={idea['idea_id']!r}"
    )
