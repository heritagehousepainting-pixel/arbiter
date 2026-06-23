"""Tests for _detect_fund_holdings (form13f detection path).

Task 8 of the 13F fund-manager advisor build.
"""
import json
import sqlite3
from datetime import datetime, timezone

from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.db.helpers import generate_ulid
from arbiter.signals.detection import detect_signals, SignalType

NOW = datetime(2026, 6, 23, tzinfo=timezone.utc)


def _conn():
    c = get_connection(":memory:")
    run_migrations(c)
    return c


_filing_counter = 0


def _filing(c, ticker, txn, book_frac, reason="new"):
    global _filing_counter
    _filing_counter += 1
    uid = generate_ulid()
    c.execute(
        "INSERT INTO filings (id, source, ticker, person_id, filing_ts, txn_type, "
        "txn_idx, shares, is_10b5_1, is_amendment, is_superseded, accession, raw_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            uid,
            "form13f",
            ticker,
            "p1",
            "2026-05-15T00:00:00+00:00",
            txn,
            _filing_counter,
            1000,
            0,
            0,
            0,
            f"acc{_filing_counter}",
            json.dumps({"reason": reason, "book_fraction": book_frac, "value_usd": 60e6}),
            "2026-05-15T00:00:00+00:00",
        ),
    )
    c.commit()


def test_fund_signal_conviction_capped_and_sign_meta():
    c = _conn()
    _filing(c, "NVDA", "P", 0.5, "new")   # very concentrated new buy
    _filing(c, "TSLA", "S", 0.3, "exit")
    sigs = [s for s in detect_signals(c, NOW) if s.source == "form13f"]
    assert {s.ticker for s in sigs} == {"NVDA", "TSLA"}
    nv = next(s for s in sigs if s.ticker == "NVDA")
    assert nv.signal_type == SignalType.FUND_HOLDING
    assert nv.conviction_score <= 0.7            # hard cap
    assert nv.meta["txn_type"] == "P"
    ts = next(s for s in sigs if s.ticker == "TSLA")
    assert ts.meta["txn_type"] == "S"
