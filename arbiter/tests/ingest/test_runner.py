"""Tests for arbiter.ingest.runner — Lane 5 orchestration.

All network I/O is MOCKED.  No real HTTP calls in any test.

Covers:
  - form4 + congress both ingest filings into the ``filings`` table.
  - Empty EDGAR_UA → form4 is skipped (noted in summary), congress still runs.
  - Idempotent re-run writes no duplicate rows.
  - A malformed filing (raises inside the per-filing loop) is skipped without
    aborting the rest of the batch.
"""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from arbiter.config import Config
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.ingest.runner import run_ingest, IngestSummary


# ---------------------------------------------------------------------------
# Helpers / constants
# ---------------------------------------------------------------------------

CLOCK_TS = "2026-06-19T12:00:00+00:00"


def _clock() -> str:
    return CLOCK_TS


def _make_config(*, edgar_user_agent: str = "TestBot test@example.com") -> Config:
    """Return a minimal Config suitable for runner tests."""
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


# ---------------------------------------------------------------------------
# Minimal fake Form 4 XML (single open-market buy, no 10b5-1)
# ---------------------------------------------------------------------------

_FORM4_XML = """\
<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2026-06-10</periodOfReport>
  <filingDate>2026-06-12</filingDate>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0009999999</rptOwnerCik>
      <rptOwnerName>Test Insider</rptOwnerName>
    </reportingOwnerId>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding>
        <transactionCode>P</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>150.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

# Fake EDGAR search result (list returned by client.search_form4_filings)
_EDGAR_SEARCH_RESULT = [
    {"accession": "0000000001-26-000001", "cik": "0009999999", "filed_at": "2026-06-12"},
]

# Fake House disclosure raw records
_HOUSE_RAW = [
    {
        "name": "Jane Congress",
        "bioguide_id": "C000001",
        "ticker": "AAPL",
        "transaction_type": "Purchase",
        "amount": "$15,001 - $50,000",
        "filing_date": "06/15/2026",
    },
]

# Fake Senate disclosure raw records
_SENATE_RAW = [
    {
        "first_name": "John",
        "last_name": "Senator",
        "bioguide_id": "S000001",
        "ticker": "MSFT",
        "transaction_type": "Sale",
        "amount": "$50,001 - $100,000",
        "disclosure_date": "2026-06-14",
    },
]

# Fake normalized House RawFilings (what the new fetch_house_ptrs pipeline returns).
# The congress path now calls the module-level fetch_house_ptrs() rather than a
# client method, so tests patch arbiter.ingest.runner.fetch_house_ptrs.
_HOUSE_FILINGS = [
    {
        "source": "congress",
        "ticker": "AAPL",
        "person_id": None,
        "person_name": "Jane Congress",
        "filing_ts": "2026-06-15T00:00:00+00:00",
        "txn_type": "P",
        "txn_idx": 0,
        "shares": None,
        "price": None,
        "amount_low": 15001.0,
        "amount_high": 50000.0,
        "is_10b5_1": False,
        "is_amendment": False,
        "accession": "H-90000001-0",
        "raw_json": "{}",
    },
]


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    """Migrated SQLite connection (tmp file so audit helpers work)."""
    db = str(tmp_path / "runner_test.db")
    c = get_connection(db)
    run_migrations(c)
    return c


@pytest.fixture(autouse=True)
def _no_senate_network(monkeypatch):
    """Stop runner congress tests from hitting LIVE efdsearch.

    run_ingest -> _ingest_congress calls the module-level fetch_senate_ptrs;
    stub it to [] so no test performs a real Senate network request (the live
    CSRF flow took ~50s/test). Tests that exercise the House path still patch
    fetch_house_ptrs explicitly; Senate behavior is covered offline in
    tests/ingest/test_senate.py.
    """
    monkeypatch.setattr("arbiter.ingest.runner.fetch_senate_ptrs", lambda *a, **k: [])


# ---------------------------------------------------------------------------
# Fake client factories (inject-able instead of patching)
# ---------------------------------------------------------------------------

def _make_fake_edgar_client(
    search_results: list[dict] | None = None,
    xml_text: str = _FORM4_XML,
    search_side_effect: Exception | None = None,
    xml_side_effect: Exception | None = None,
) -> MagicMock:
    """Return a MagicMock that behaves like an EdgarClient."""
    client = MagicMock()

    if search_side_effect is not None:
        client.search_form4_filings.side_effect = search_side_effect
    else:
        client.search_form4_filings.return_value = (
            search_results if search_results is not None else _EDGAR_SEARCH_RESULT
        )

    if xml_side_effect is not None:
        client.get_form4_xml.side_effect = xml_side_effect
    else:
        client.get_form4_xml.return_value = xml_text

    client.__enter__ = lambda s: s
    client.__exit__ = MagicMock(return_value=False)
    return client


def _make_fake_congress_client(
    house_raw: list[dict] | None = None,
    senate_raw: list[dict] | None = None,
    house_side_effect: Exception | None = None,
    senate_side_effect: Exception | None = None,
) -> MagicMock:
    """Return a MagicMock that behaves like a CongressClient."""
    client = MagicMock()

    if house_side_effect is not None:
        client.fetch_house.side_effect = house_side_effect
    else:
        client.fetch_house.return_value = (
            house_raw if house_raw is not None else _HOUSE_RAW
        )

    if senate_side_effect is not None:
        client.fetch_senate.side_effect = senate_side_effect
    else:
        client.fetch_senate.return_value = (
            senate_raw if senate_raw is not None else _SENATE_RAW
        )

    return client


# ---------------------------------------------------------------------------
# Test 1: form4 + congress both ingest into the filings table
# ---------------------------------------------------------------------------

def test_both_sources_write_to_filings(conn) -> None:
    """Happy path: form4 and congress both produce rows in the filings table."""
    config = _make_config()
    fake_edgar = _make_fake_edgar_client()
    fake_congress = _make_fake_congress_client()

    with (
        patch("arbiter.ingest.runner.EdgarClient", return_value=fake_edgar),
        patch("arbiter.ingest.runner.CongressClient", return_value=fake_congress),
        patch("arbiter.ingest.runner.fetch_house_ptrs", return_value=list(_HOUSE_FILINGS)),
    ):
        summary = run_ingest(
            config,
            conn=conn,
            clock=_clock,
            sources=("form4", "congress"),
            tickers=["AAPL"],
            lookback_days=7,
        )

    assert isinstance(summary, IngestSummary)

    # At least one filing written from each source.
    form4_rows = conn.execute(
        "SELECT id FROM filings WHERE source = 'form4'"
    ).fetchall()
    congress_rows = conn.execute(
        "SELECT id FROM filings WHERE source = 'congress'"
    ).fetchall()

    assert len(form4_rows) >= 1, "At least one form4 filing must be written"
    assert len(congress_rows) >= 1, "At least one congress filing must be written"

    assert summary.n_written >= 2
    assert summary.n_fetched >= 2


# ---------------------------------------------------------------------------
# Test 2: empty EDGAR_UA → form4 skipped, congress still runs
# ---------------------------------------------------------------------------

def test_empty_edgar_ua_skips_form4_but_runs_congress(conn) -> None:
    """If edgar_user_agent is empty, form4 is skipped (no crash).

    Congress must still ingest successfully.  A note must appear in the
    summary describing why form4 was skipped.
    """
    config = _make_config(edgar_user_agent="")  # empty UA
    fake_congress = _make_fake_congress_client()

    # EdgarClient must NOT be called at all (no UA → we never construct it).
    mock_edgar_cls = MagicMock()

    with (
        patch("arbiter.ingest.runner.EdgarClient", mock_edgar_cls),
        patch("arbiter.ingest.runner.CongressClient", return_value=fake_congress),
        patch("arbiter.ingest.runner.fetch_house_ptrs", return_value=list(_HOUSE_FILINGS)),
    ):
        summary = run_ingest(
            config,
            conn=conn,
            clock=_clock,
            sources=("form4", "congress"),
            tickers=["AAPL"],
        )

    # form4 source must be skipped — EdgarClient never instantiated.
    mock_edgar_cls.assert_not_called()

    # Congress must have written rows.
    congress_rows = conn.execute(
        "SELECT id FROM filings WHERE source = 'congress'"
    ).fetchall()
    assert len(congress_rows) >= 1, "Congress must still write rows when form4 is skipped"

    # Summary must carry a note about the skip.
    assert summary.notes, "A note must be present explaining form4 was skipped"
    assert any("edgar_user_agent" in n.lower() or "form4 skipped" in n.lower()
               for n in summary.notes), \
        f"Note must mention edgar_user_agent or form4 skipped; got: {summary.notes}"

    # No crash — summary must have zero critical errors from the run.
    assert summary.n_written >= 1, "At least congress writes must succeed"

    # per_source must record form4 as having errors (the skip message).
    assert "form4" in summary.per_source
    assert summary.per_source["form4"].errors, "form4 SourceSummary must record the skip"


# ---------------------------------------------------------------------------
# Test 3: idempotent re-run writes no duplicates
# ---------------------------------------------------------------------------

def test_idempotent_rerun_no_duplicates(conn) -> None:
    """Running the ingest twice with identical data must not create duplicate rows."""
    config = _make_config()
    fake_edgar = _make_fake_edgar_client()
    fake_congress = _make_fake_congress_client()

    def _run():
        # Re-create fake clients each call (return same data).
        fe = _make_fake_edgar_client()
        fc = _make_fake_congress_client()
        with (
            patch("arbiter.ingest.runner.EdgarClient", return_value=fe),
            patch("arbiter.ingest.runner.CongressClient", return_value=fc),
        ):
            return run_ingest(
                config,
                conn=conn,
                clock=_clock,
                sources=("form4", "congress"),
                tickers=["AAPL"],
            )

    summary1 = _run()
    total_after_first = conn.execute(
        "SELECT count(*) FROM filings WHERE is_superseded = 0"
    ).fetchone()[0]

    summary2 = _run()
    total_after_second = conn.execute(
        "SELECT count(*) FROM filings WHERE is_superseded = 0"
    ).fetchone()[0]

    assert total_after_first > 0, "First run must write rows"
    assert total_after_second == total_after_first, (
        f"Second run must not add duplicate rows; "
        f"first={total_after_first}, second={total_after_second}"
    )
    # Second run should write 0 new rows.
    assert summary2.n_written == 0, (
        f"Re-run must write 0 new rows, got {summary2.n_written}"
    )


# ---------------------------------------------------------------------------
# Test 4: malformed filing skipped without aborting the batch
# ---------------------------------------------------------------------------

def test_malformed_filing_skipped_batch_continues(conn) -> None:
    """A malformed/unparseable filing must be skipped; the rest of the batch proceeds.

    We simulate this by having the first ticker raise during search (network error),
    while the second ticker succeeds.  Both congress chambers still run.
    """
    config = _make_config()

    # Edgar: first ticker raises, rest are fine via a side_effect list.
    edgar_client = MagicMock()
    # search raises for the first call, returns results for subsequent ones.
    edgar_client.search_form4_filings.side_effect = [
        RuntimeError("simulated network glitch"),   # ticker 1 fails
        _EDGAR_SEARCH_RESULT,                        # ticker 2 succeeds
    ]
    edgar_client.get_form4_xml.return_value = _FORM4_XML
    edgar_client.close = MagicMock()

    fake_congress = _make_fake_congress_client()

    with (
        patch("arbiter.ingest.runner.EdgarClient", return_value=edgar_client),
        patch("arbiter.ingest.runner.CongressClient", return_value=fake_congress),
    ):
        summary = run_ingest(
            config,
            conn=conn,
            clock=_clock,
            sources=("form4", "congress"),
            tickers=["AAPL", "MSFT"],  # two tickers; AAPL will fail
        )

    # The run must not crash — we get a summary back.
    assert isinstance(summary, IngestSummary)

    # At least one successful write must have occurred (MSFT form4 or congress).
    assert summary.n_written >= 1, (
        f"At least one write must succeed despite the error; got n_written={summary.n_written}"
    )

    # The error must be recorded but NOT re-raised.
    assert summary.errors or summary.per_source.get("form4", SourceSummary()).errors, \
        "Error from the failed ticker must appear in summary"


# ---------------------------------------------------------------------------
# Test 5: only form4 source requested — congress not called
# ---------------------------------------------------------------------------

def test_only_form4_source_congress_not_called(conn) -> None:
    """When sources=('form4',), CongressClient must not be instantiated."""
    config = _make_config()
    fake_edgar = _make_fake_edgar_client()
    mock_congress_cls = MagicMock()

    with (
        patch("arbiter.ingest.runner.EdgarClient", return_value=fake_edgar),
        patch("arbiter.ingest.runner.CongressClient", mock_congress_cls),
    ):
        summary = run_ingest(
            config,
            conn=conn,
            clock=_clock,
            sources=("form4",),
            tickers=["AAPL"],
        )

    mock_congress_cls.assert_not_called()
    assert "congress" not in summary.per_source


# ---------------------------------------------------------------------------
# Test 6: only congress source requested — edgar not called
# ---------------------------------------------------------------------------

def test_only_congress_source_edgar_not_called(conn) -> None:
    """When sources=('congress',), EdgarClient must not be instantiated."""
    config = _make_config()
    fake_congress = _make_fake_congress_client()
    mock_edgar_cls = MagicMock()

    with (
        patch("arbiter.ingest.runner.EdgarClient", mock_edgar_cls),
        patch("arbiter.ingest.runner.CongressClient", return_value=fake_congress),
        patch("arbiter.ingest.runner.fetch_house_ptrs", return_value=list(_HOUSE_FILINGS)),
    ):
        summary = run_ingest(
            config,
            conn=conn,
            clock=_clock,
            sources=("congress",),
        )

    mock_edgar_cls.assert_not_called()
    assert "form4" not in summary.per_source
    assert "congress" in summary.per_source
    assert summary.n_written >= 1


# ---------------------------------------------------------------------------
# Test 7: per-source breakdown in summary
# ---------------------------------------------------------------------------

def test_per_source_breakdown_populated(conn) -> None:
    """IngestSummary.per_source must be populated with SourceSummary for each source."""
    config = _make_config()
    fake_edgar = _make_fake_edgar_client()
    fake_congress = _make_fake_congress_client()

    with (
        patch("arbiter.ingest.runner.EdgarClient", return_value=fake_edgar),
        patch("arbiter.ingest.runner.CongressClient", return_value=fake_congress),
    ):
        summary = run_ingest(
            config,
            conn=conn,
            clock=_clock,
            sources=("form4", "congress"),
            tickers=["AAPL"],
        )

    assert "form4" in summary.per_source
    assert "congress" in summary.per_source
    # Per-source n_written must add up to aggregate n_written.
    total_written = sum(s.n_written for s in summary.per_source.values())
    assert total_written == summary.n_written


# ---------------------------------------------------------------------------
# Import guard for SourceSummary (used in test 4)
# ---------------------------------------------------------------------------

from arbiter.ingest.runner import SourceSummary  # noqa: E402 (after tests, keeps imports clean)


# ---------------------------------------------------------------------------
# Wave 2 — Schedule 13D/13G (form13d) ingest
# ---------------------------------------------------------------------------

# A normalized 13D RawFiling as produced by normalize_sc13 (source="form13d").
_SC13_RAW = {
    "source": "form13d",
    "ticker": "AAPL",
    "person_id": None,
    "person_name": "Activist Capital LP",
    "filing_ts": "2026-06-15T00:00:00+00:00",
    "txn_type": "P",
    "txn_idx": 0,
    "shares": 1_000_000.0,
    "price": None,
    "amount_low": None,
    "amount_high": None,
    "is_10b5_1": False,
    "is_amendment": False,
    "accession": "0000000002-26-000002",
    "raw_json": json.dumps({"schedule": "13D", "percent_of_class": 8.5, "is_activist": True}),
}

_SC13_SEARCH_RESULT = [
    {"accession": "0000000002-26-000002", "cik": "0009999999",
     "schedule": "13D", "primary_document": "doc.htm"},
]


def _make_fake_sc13_client() -> MagicMock:
    """A MagicMock EdgarClient with the sc13 search/doc methods stubbed."""
    client = MagicMock()
    client.search_sc13_filings.return_value = list(_SC13_SEARCH_RESULT)
    client.get_sc13_doc.return_value = "<doc/>"
    client.close = MagicMock()
    return client


def test_form13d_inert_when_no_user_agent(conn) -> None:
    """Empty edgar_user_agent → form13d skipped (no crash); congress still runs."""
    config = _make_config(edgar_user_agent="")
    mock_edgar_cls = MagicMock()

    with (
        patch("arbiter.ingest.runner.EdgarClient", mock_edgar_cls),
        patch("arbiter.ingest.runner.fetch_house_ptrs", return_value=list(_HOUSE_FILINGS)),
    ):
        summary = run_ingest(
            config, conn=conn, clock=_clock,
            sources=("form13d", "congress"),
        )

    mock_edgar_cls.assert_not_called()  # never constructed without a UA
    assert "form13d" in summary.per_source
    assert summary.per_source["form13d"].n_written == 0
    assert any("form13d" in n.lower() for n in summary.notes)
    # congress still ran
    assert "congress" in summary.per_source


def test_form13d_ingests_rows(conn) -> None:
    """A normalized 13D RawFiling is written with source='form13d'."""
    config = _make_config()
    fake = _make_fake_sc13_client()

    with (
        patch("arbiter.ingest.runner.EdgarClient", return_value=fake),
        patch("arbiter.ingest.runner.parse_sc13", return_value=[{"_": 1}]),
        patch("arbiter.ingest.runner.normalize_sc13", return_value=[dict(_SC13_RAW)]),
    ):
        summary = run_ingest(
            config, conn=conn, clock=_clock,
            sources=("form13d",), tickers=["AAPL"],
        )

    assert summary.per_source["form13d"].n_written == 1
    rows = conn.execute(
        "SELECT COUNT(*) FROM filings WHERE source='form13d'"
    ).fetchone()[0]
    assert rows == 1


def test_default_sources_includes_form13d(conn) -> None:
    """run_ingest's DEFAULT sources trigger a form13d per-source summary."""
    config = _make_config()
    fake = _make_fake_sc13_client()

    with (
        patch("arbiter.ingest.runner.EdgarClient", return_value=fake),
        patch("arbiter.ingest.runner.parse_sc13", return_value=[{"_": 1}]),
        patch("arbiter.ingest.runner.normalize_sc13", return_value=[dict(_SC13_RAW)]),
        patch("arbiter.ingest.runner.fetch_house_ptrs", return_value=list(_HOUSE_FILINGS)),
    ):
        summary = run_ingest(config, conn=conn, clock=_clock, tickers=["AAPL"])

    assert "form13d" in summary.per_source


def test_form13d_fault_isolated(conn) -> None:
    """A parse exception on one ticker increments n_skipped and continues."""
    config = _make_config()
    fake = _make_fake_sc13_client()

    with (
        patch("arbiter.ingest.runner.EdgarClient", return_value=fake),
        patch("arbiter.ingest.runner.parse_sc13", side_effect=ValueError("bad doc")),
    ):
        summary = run_ingest(
            config, conn=conn, clock=_clock,
            sources=("form13d",), tickers=["AAPL", "MSFT"],
        )

    src = summary.per_source["form13d"]
    assert src.n_skipped >= 1
    assert src.n_written == 0
    assert src.errors  # error recorded, but no crash
