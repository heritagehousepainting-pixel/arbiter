"""Tests for form13f_normalize — schema and delta logic.

Task 1 adds only the schema test. Later tasks append more tests to this file.
"""
from __future__ import annotations

import sqlite3

import pytest

from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations


def _migrated_conn() -> sqlite3.Connection:
    conn = get_connection(":memory:")
    run_migrations(conn)
    return conn


def test_migrations_create_form13f_tables():
    conn = _migrated_conn()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(form13f_holdings)")}
    assert {"person_id", "accession", "filing_date", "report_date", "cusip",
            "ticker", "issuer_name", "value_usd", "shares", "put_call"} <= cols
    map_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cusip_map)")}
    assert {"cusip", "ticker", "confidence"} <= map_cols


def test_config_form13f_defaults():
    """Task 2: FORM13F_* config fields exist with correct defaults."""
    from arbiter.config import load_config, Config
    cfg = load_config()
    assert cfg.form13f_min_position_usd == 10_000_000.0
    assert cfg.form13f_min_book_fraction == 0.005
    assert cfg.form13f_min_delta_fraction == 0.25
    assert cfg.form13f_first_filing_top_k == 5
    assert cfg.form13f_max_conviction == 0.7
    assert cfg.form13f_manager_ciks is None


# ---------------------------------------------------------------------------
# Task 7: Holdings store + delta engine
# ---------------------------------------------------------------------------
from arbiter.config import load_config, Config  # noqa: E402
from arbiter.ingest.edgar import form13f_normalize as fn  # noqa: E402

ASSETS = {
    "NVIDIA CORP": "NVDA",
    "APPLE INC": "AAPL",
    "TESLA INC": "TSLA",
    "AMAZON COM INC": "AMZN",
    "META PLATFORMS INC": "META",
    "MICROSOFT CORP": "MSFT",
}
NOW = "2026-06-23T00:00:00+00:00"


def _store(c, pid, acc, fdate, rdate, holdings):
    return fn.store_holdings(
        c, pid, acc, fdate, rdate, holdings,
        asset_lookup=lambda: ASSETS, now_iso=NOW,
    )


def _h(name, cusip, value, shares, put_call=None):
    return {
        "issuer_name": name,
        "cusip": cusip,
        "value_usd": value,
        "shares": shares,
        "put_call": put_call,
    }


def test_first_filing_emits_top_k_conviction_snapshot():
    c = _migrated_conn()
    cfg = load_config()
    # 6 holdings of descending value; top-5 should fire as new "P".
    hs = [
        _h("NVIDIA CORP",       "67066G104", 60e6, 1000),
        _h("APPLE INC",         "037833100", 50e6, 1000),
        _h("TESLA INC",         "88160R101", 40e6, 1000),
        _h("AMAZON COM INC",    "023135106", 30e6, 1000),
        _h("META PLATFORMS INC","30303M102", 20e6, 1000),
        _h("MICROSOFT CORP",    "594918104", 11e6, 1000),
    ]
    _store(c, "p1", "acc1", "2026-05-15T00:00:00+00:00", "2026-03-31", hs)
    deltas = fn.compute_deltas(c, "p1", "2026-03-31", config=cfg)
    assert len(deltas) == 5  # top-5 only
    assert all(d["txn_type"] == "P" and d["source"] == "form13f" for d in deltas)
    # PIT: filing_ts is the filing date, never the report_date.
    assert all(d["filing_ts"].startswith("2026-05-15") for d in deltas)


def test_new_exit_add_trim_flat():
    c = _migrated_conn()
    cfg = load_config()
    # Q1: four positions including META which will be fully exited by Q2.
    q1 = [
        _h("NVIDIA CORP",       "67066G104", 60e6, 1000),
        _h("APPLE INC",         "037833100", 60e6, 1000),
        _h("TESLA INC",         "88160R101", 60e6, 1000),
        _h("META PLATFORMS INC","30303M102", 60e6, 1000),  # full exit in Q2
    ]
    _store(c, "p1", "a1", "2026-02-14T00:00:00+00:00", "2025-12-31", q1)
    fn.compute_deltas(c, "p1", "2025-12-31", config=cfg)  # consume first-filing baseline

    # Q2: NVDA flat, AAPL +100%, TSLA -60%, AMZN new, META absent (exit).
    q2 = [
        _h("NVIDIA CORP",    "67066G104", 60e6, 1000),  # flat -> no signal
        _h("APPLE INC",      "037833100", 60e6, 2000),  # +100% add -> P
        _h("TESLA INC",      "88160R101", 60e6,  400),  # -60% trim -> S
        _h("AMAZON COM INC", "023135106", 60e6, 1000),  # new -> P
        # META absent -> full exit -> S
    ]
    _store(c, "p1", "a2", "2026-05-15T00:00:00+00:00", "2026-03-31", q2)
    deltas = {d["ticker"]: d for d in fn.compute_deltas(c, "p1", "2026-03-31", config=cfg)}

    assert "NVDA" not in deltas                       # flat, no signal
    assert deltas["AAPL"]["txn_type"] == "P"          # add
    assert deltas["TSLA"]["txn_type"] == "S"          # trim
    assert deltas["AMZN"]["txn_type"] == "P"          # new position
    assert deltas["META"]["txn_type"] == "S"          # full exit


def test_noise_floors_drop_small_positions():
    c = _migrated_conn()
    cfg = load_config()
    # value below FORM13F_MIN_POSITION_USD ($10M) -> not a tradeable delta.
    _store(c, "p1", "a1", "2026-05-15T00:00:00+00:00", "2026-03-31",
           [_h("APPLE INC", "037833100", 5e6, 10)])
    assert fn.compute_deltas(c, "p1", "2026-03-31", config=cfg) == []


def test_unresolved_cusip_dropped():
    c = _migrated_conn()
    cfg = load_config()
    n = _store(c, "p1", "a1", "2026-05-15T00:00:00+00:00", "2026-03-31",
               [_h("OBSCURE PLC", "ZZZ999999", 60e6, 1000)])
    assert n == 0  # unresolved -> not stored
