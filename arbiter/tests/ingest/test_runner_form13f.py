"""Tests for the Form 13F roster seed and runner ingest.

Task 3 adds the roster-shape test; Task 11 appends ingest tests.
"""
from __future__ import annotations

import inspect
import re
import sqlite3

from arbiter.data.fund_managers import FUND_MANAGERS, manager_ciks


def test_roster_shape_and_ciks():
    assert len(FUND_MANAGERS) >= 11
    names = {m.name for m in FUND_MANAGERS}
    assert "Leopold Aschenbrenner" in names
    assert "Cathie Wood" in names and "Michael Burry" in names
    for m in FUND_MANAGERS:
        assert re.fullmatch(r"\d{10}", m.cik), f"{m.name} cik not 10-digit: {m.cik}"
    assert set(manager_ciks()) == {m.cik for m in FUND_MANAGERS}


# ---------------------------------------------------------------------------
# Task 11: runner ingest tests
# ---------------------------------------------------------------------------

NOW_ISO = "2026-06-23T00:00:00+00:00"


class _FakeEdgar:
    """Fake EdgarClient that returns one 13F-HR filing per CIK."""

    def search_form13f_filings(self, cik, *, count=8):
        return [
            {
                "cik": cik,
                "accession": f"acc-{cik}",
                "filed_at": "2026-05-15",
                "report_date": "2026-03-31",
                "primary_document": "primary_doc.xml",
                "is_amendment": False,
            }
        ]

    def get_form13f_info_table(self, accession, cik):
        return (
            "<informationTable xmlns='http://www.sec.gov/edgar/document/thirteenf/informationtable'>"
            "<infoTable><nameOfIssuer>NVIDIA CORP</nameOfIssuer><cusip>67066G104</cusip>"
            "<value>60000000</value><shrsOrPrnAmt><sshPrnamt>1000</sshPrnamt></shrsOrPrnAmt>"
            "</infoTable></informationTable>"
        )

    def close(self):
        pass


def _make_migrated_conn(tmp_path):
    """Return an in-memory SQLite connection with all migrations applied."""
    from arbiter.db.migrate import run_migrations
    from arbiter.db.connection import get_connection

    conn = get_connection(str(tmp_path / "t.db"))
    run_migrations(conn)
    return conn


def _make_config(*, edgar_user_agent: str = "TestBot test@example.com"):
    """Return a minimal Config suitable for runner tests."""
    from arbiter.config import Config

    return Config(
        live_trading=False,
        executor_backend="sim",
        db_path=":memory:",
        audit_path="/tmp/test_audit.jsonl",
        metrics_path="/tmp/test_metrics.jsonl",
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
        edgar_user_agent=edgar_user_agent,
        kill_switch_url="",
        alert_webhook_url="",
    )


def test_ingest_form13f_writes_people_holdings_and_filing(monkeypatch, tmp_path):
    """Happy path: fake edgar + asset lookup → people, holdings, and filings written."""
    from arbiter.ingest import runner

    monkeypatch.setattr(runner, "_make_edgar_for_form13f", lambda cfg: _FakeEdgar())
    monkeypatch.setattr(runner, "_alpaca_asset_lookup", lambda cfg: (lambda: {"NVIDIA CORP": "NVDA"}))

    conn = _make_migrated_conn(tmp_path)
    cfg = _make_config()
    summary = runner.IngestSummary(sources=("form13f",))

    runner._ingest_form13f(cfg, conn=conn, clock=lambda: NOW_ISO, summary=summary)

    # All managers registered as people with source="form13f"
    count_people = conn.execute(
        "SELECT COUNT(*) FROM people WHERE source='form13f'"
    ).fetchone()[0]
    assert count_people >= 11, f"Expected >= 11 people, got {count_people}"

    # Holdings stored in form13f_holdings
    count_holdings = conn.execute(
        "SELECT COUNT(*) FROM form13f_holdings"
    ).fetchone()[0]
    assert count_holdings >= 1, f"Expected >= 1 holding row, got {count_holdings}"

    # First-filing top-K delta written as form13f filing
    count_filings = conn.execute(
        "SELECT COUNT(*) FROM filings WHERE source='form13f'"
    ).fetchone()[0]
    assert count_filings >= 1, f"Expected >= 1 form13f filing, got {count_filings}"


def test_ingest_form13f_ua_empty_guard(monkeypatch, tmp_path):
    """Empty edgar_user_agent → form13f is skipped, no crash, no holdings."""
    from arbiter.ingest import runner

    edgar_called = []
    monkeypatch.setattr(
        runner, "_make_edgar_for_form13f",
        lambda cfg: edgar_called.append(1) or _FakeEdgar()
    )

    conn = _make_migrated_conn(tmp_path)
    cfg = _make_config(edgar_user_agent="")
    summary = runner.IngestSummary(sources=("form13f",))

    runner._ingest_form13f(cfg, conn=conn, clock=lambda: NOW_ISO, summary=summary)

    # No edgar calls made when UA is empty
    assert edgar_called == [], "EdgarClient should not be called when UA is empty"
    count_holdings = conn.execute("SELECT COUNT(*) FROM form13f_holdings").fetchone()[0]
    assert count_holdings == 0


def test_form13f_in_default_sources():
    """'form13f' must be in run_ingest's default sources tuple."""
    from arbiter.ingest.runner import run_ingest

    sig = inspect.signature(run_ingest)
    default_sources = sig.parameters["sources"].default
    assert "form13f" in default_sources, (
        f"'form13f' not in run_ingest default sources: {default_sources}"
    )


class _FakeEdgarTwoQuarters:
    """Fake EdgarClient returning TWO quarters for a CIK (newer + older).

    Newer quarter (2026-03-31) holds 2000 sh NVDA; older (2025-12-31) holds
    1000 sh.  A correct ingest stores BOTH quarters but emits ONE delta signal
    (the newest, +100% add) — NOT two first-filing snapshots.
    """

    def search_form13f_filings(self, cik, *, count=8):
        return [
            {"cik": cik, "accession": f"acc-new-{cik}", "filed_at": "2026-05-15",
             "report_date": "2026-03-31", "primary_document": "p.xml", "is_amendment": False},
            {"cik": cik, "accession": f"acc-old-{cik}", "filed_at": "2026-02-14",
             "report_date": "2025-12-31", "primary_document": "p.xml", "is_amendment": False},
        ]

    def get_form13f_info_table(self, accession, cik):
        shares = 2000 if "new" in accession else 1000  # +100% Q-over-Q
        return (
            "<informationTable xmlns='http://www.sec.gov/edgar/document/thirteenf/informationtable'>"
            "<infoTable><nameOfIssuer>NVIDIA CORP</nameOfIssuer><cusip>67066G104</cusip>"
            f"<value>60000000</value><shrsOrPrnAmt><sshPrnamt>{shares}</sshPrnamt></shrsOrPrnAmt>"
            "</infoTable></informationTable>"
        )

    def close(self):
        pass


def test_two_quarters_emit_one_delta_not_double_firstfiling(monkeypatch, tmp_path):
    """Two stored quarters → ONE 'add' delta for the newest, no double first-filing.

    Regression for the live-smoke bug where processing both report_dates
    newest-first made each a first_filing_topk snapshot (duplicate signals).
    """
    import dataclasses
    from arbiter.ingest import runner

    monkeypatch.setattr(runner, "_make_edgar_for_form13f", lambda cfg: _FakeEdgarTwoQuarters())
    monkeypatch.setattr(runner, "_alpaca_asset_lookup", lambda cfg: (lambda: {}))  # NVDA via seed

    conn = _make_migrated_conn(tmp_path)
    # Restrict to a single manager so the assertion is unambiguous.
    cfg = dataclasses.replace(_make_config(), form13f_manager_ciks=("0001697748",))
    summary = runner.IngestSummary(sources=("form13f",))

    runner._ingest_form13f(cfg, conn=conn, clock=lambda: NOW_ISO, summary=summary)

    # BOTH quarters' holdings stored (baseline + current).
    n_holdings = conn.execute(
        "SELECT COUNT(*) FROM form13f_holdings WHERE ticker='NVDA'"
    ).fetchone()[0]
    assert n_holdings == 2, f"expected both quarters stored, got {n_holdings}"

    # Exactly ONE delta signal, and it is an 'add' (real delta), not first_filing.
    rows = conn.execute(
        "SELECT txn_type, json_extract(raw_json, '$.reason') AS reason "
        "FROM filings WHERE source='form13f' AND ticker='NVDA'"
    ).fetchall()
    assert len(rows) == 1, f"expected 1 delta signal, got {len(rows)}: {[dict(r) for r in rows]}"
    assert rows[0]["txn_type"] == "P"
    assert rows[0]["reason"] == "add"


class _FakeEdgarMultiTicker:
    """One quarter holding 3 distinct tickers (all megacap-seed CUSIPs)."""

    def search_form13f_filings(self, cik, *, count=8):
        return [{
            "cik": cik, "accession": f"acc-{cik}", "filed_at": "2026-05-15",
            "report_date": "2026-03-31", "primary_document": "p.xml", "is_amendment": False,
        }]

    def get_form13f_info_table(self, accession, cik):
        # NVDA / AAPL / TSLA all in the resolver seed; each > $10M and material.
        return (
            "<informationTable xmlns='http://www.sec.gov/edgar/document/thirteenf/informationtable'>"
            "<infoTable><nameOfIssuer>NVIDIA CORP</nameOfIssuer><cusip>67066G104</cusip>"
            "<value>50000000</value><shrsOrPrnAmt><sshPrnamt>1000</sshPrnamt></shrsOrPrnAmt></infoTable>"
            "<infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><cusip>037833100</cusip>"
            "<value>40000000</value><shrsOrPrnAmt><sshPrnamt>1000</sshPrnamt></shrsOrPrnAmt></infoTable>"
            "<infoTable><nameOfIssuer>TESLA INC</nameOfIssuer><cusip>88160R101</cusip>"
            "<value>30000000</value><shrsOrPrnAmt><sshPrnamt>1000</sshPrnamt></shrsOrPrnAmt></infoTable>"
            "</informationTable>"
        )

    def close(self):
        pass


def test_multiple_tickers_in_one_filing_not_collapsed(monkeypatch, tmp_path):
    """3 tickers in one 13F → 3 distinct signals (regression for txn_idx=0 collision).

    write_filing dedups by (accession, txn_idx); without a unique txn_idx per
    ticker, all 3 would collapse to ONE filings row.
    """
    import dataclasses
    from arbiter.ingest import runner

    monkeypatch.setattr(runner, "_make_edgar_for_form13f", lambda cfg: _FakeEdgarMultiTicker())
    monkeypatch.setattr(runner, "_alpaca_asset_lookup", lambda cfg: (lambda: {}))  # all via seed

    conn = _make_migrated_conn(tmp_path)
    cfg = dataclasses.replace(_make_config(), form13f_manager_ciks=("0001697748",))
    summary = runner.IngestSummary(sources=("form13f",))

    runner._ingest_form13f(cfg, conn=conn, clock=lambda: NOW_ISO, summary=summary)

    tickers = {
        r[0] for r in conn.execute(
            "SELECT ticker FROM filings WHERE source='form13f'"
        ).fetchall()
    }
    assert tickers == {"NVDA", "AAPL", "TSLA"}, f"expected 3 distinct signals, got {tickers}"
