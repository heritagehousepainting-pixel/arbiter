"""Schedule 13D/13G parser tests (offline; embedded + file fixtures)."""
from __future__ import annotations

from datetime import datetime

from arbiter.ingest.edgar.sc13_parser import parse_sc13

from tests.ingest.edgar.conftest import read_fixture


SC13D = read_fixture("sc13d_structured.xml")
SC13G = read_fixture("sc13g_structured.xml")
SC13DA = read_fixture("sc13da_amendment.xml")
SC13_HEADER = read_fixture("sc13_header_only.txt")


def _assert_tz_aware(ts: str) -> None:
    dt = datetime.fromisoformat(ts)
    assert dt.tzinfo is not None


def test_parse_sc13d_structured():
    rows = parse_sc13(SC13D, "AAPL", "0009999999-26-000008", schedule="13D")
    assert len(rows) == 1
    row = rows[0]
    assert row["schedule"] == "13D"
    assert row["is_activist"] is True
    assert row["is_amendment"] is False
    assert row["percent_of_class"] == 8.5
    assert row["aggregate_amount"] == 1300000.0
    assert row["transaction_code"] == "P"
    assert row["person_id"] == "0007777777"
    assert row["person_name"] == "Activist Capital LP"
    assert row["ticker"] == "AAPL"
    assert row["cusip"] == "037833100"
    assert row["is_10b5_1"] is False
    assert row["txn_idx"] == 0
    _assert_tz_aware(row["filing_ts"])
    # Event date preferred over filing date.
    assert row["filing_ts"].startswith("2026-03-15")


def test_parse_sc13g_structured():
    rows = parse_sc13(SC13G, "AAPL", "0008888888-26-000007", schedule="13G")
    assert len(rows) == 1
    row = rows[0]
    assert row["schedule"] == "13G"
    assert row["is_activist"] is False
    assert row["percent_of_class"] == 6.2
    assert row["transaction_code"] == "P"


def test_parse_sc13da_amendment_exit():
    rows = parse_sc13(SC13DA, "AAPL", "0007777777-26-000002", schedule="13D")
    assert len(rows) == 1
    row = rows[0]
    assert row["is_amendment"] is True
    assert row["schedule"] == "13D"
    assert row["percent_of_class"] == 4.1
    # Dropped below 5% threshold via amendment -> exit/reduction.
    assert row["transaction_code"] == "S"


def test_parse_sc13_header_only_fallback():
    rows = parse_sc13(SC13_HEADER, "AAPL", "0005555555-26-000001", schedule="13D")
    assert len(rows) == 1
    row = rows[0]
    assert row["schedule"] == "13D"
    assert row["is_activist"] is True
    assert row["person_name"] == "OLD STYLE ACTIVIST FUND"
    assert row["person_id"] == "0005555555"
    assert row["cusip"] == "037833100"
    assert row["percent_of_class"] == 7.3
    assert row["transaction_code"] == "P"
    _assert_tz_aware(row["filing_ts"])
    assert row["filing_ts"].startswith("2026-03-10")


def test_parse_sc13_empty_input():
    assert parse_sc13("", "AAPL", "x", schedule="13D") == []
    assert parse_sc13("   ", "AAPL", "x", schedule="13D") == []


def test_parse_sc13_malformed_xml_no_exception():
    bad = "<edgarSubmission><documentType>SC 13D</documentType><unclosed>"
    # Malformed XML -> falls through to header parse -> no date -> [] (no raise).
    rows = parse_sc13(bad, "AAPL", "x", schedule="13D")
    assert rows == []


def test_parse_sc13_schedule_hint_used_when_doctype_silent():
    xml = (
        '<?xml version="1.0"?><edgarSubmission>'
        "<filingDate>2026-05-01</filingDate>"
        "<percentOfClass>9.0</percentOfClass></edgarSubmission>"
    )
    rows = parse_sc13(xml, "AAPL", "x", schedule="13G")
    assert rows[0]["schedule"] == "13G"
    assert rows[0]["is_activist"] is False


# ---------------------------------------------------------------------------
# Modern structured 13D schema (schemaVersion X02xx) — regression for the bug
# where every recent (2025-2026) activist 13D parsed empty because the parser
# only understood the legacy tag layout / ISO dates / no-namespace docs.
# ---------------------------------------------------------------------------

SC13D_X02 = read_fixture("sc13_structured_x02.xml")


def test_parse_sc13_modern_x02_schema():
    rows = parse_sc13(SC13D_X02, "QRVO", "0000921895-26-000115", schedule="13D")
    assert len(rows) == 1
    row = rows[0]
    assert row["schedule"] == "13D"
    assert row["is_amendment"] is True            # "SCHEDULE 13D/A"
    assert row["is_activist"] is True
    # MM/DD/YYYY dateOfEvent parsed to a recent ISO timestamp.
    assert row["filing_ts"].startswith("2026-06-02")
    _assert_tz_aware(row["filing_ts"])
    # Lead reporting person (first block), not a co-filer.
    assert row["person_id"] == "0001517137"
    assert row["person_name"] == "Starboard Value LP"
    assert row["percent_of_class"] == 6.4
    assert row["aggregate_amount"] == 5611526.0
    # Subject (issuer) surfaced for downstream ticker resolution.
    assert row["subject_cik"] == "0001604778"
    assert row["subject_name"] == "Qorvo, Inc."
    assert row["cusip"] == "74736K101"
    assert row["transaction_code"] == "P"         # amendment still >=5%? 6.4 -> P
