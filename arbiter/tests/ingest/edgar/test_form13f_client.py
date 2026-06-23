"""Tests for EdgarClient Form 13F discovery and info-table fetch methods.

All HTTP is mocked — no real network calls.
"""
from __future__ import annotations

import pytest

from arbiter.config import load_config
from arbiter.ingest.edgar.client import (
    EdgarClient,
    _extract_form13f_table_filename,
)
from tests.ingest.edgar.conftest import make_config


# ---------------------------------------------------------------------------
# Fixtures / canned bodies
# ---------------------------------------------------------------------------

SUBMISSIONS = """{
  "cik": "1697748",
  "name": "ARK INVESTMENT MANAGEMENT LLC",
  "filings": {
    "recent": {
      "form": ["13F-HR", "4", "13F-HR/A"],
      "accessionNumber": [
        "0001697748-26-000010",
        "0000000000-26-000001",
        "0001697748-26-000005"
      ],
      "filingDate": ["2026-05-15", "2026-05-01", "2026-02-14"],
      "reportDate": ["2026-03-31", "", "2025-12-31"],
      "primaryDocument": ["primary_doc.xml", "x", "primary_doc.xml"]
    }
  }
}"""

# Minimal index page for the 13F filing — contains both the cover XML
# (primary_doc) and the information-table XML.
INDEX_HTML = """<html><body>
<table>
  <tr><td><a href="/Archives/edgar/data/1697748/000169774826000010/primary_doc.xml">primary_doc.xml</a></td></tr>
  <tr><td><a href="/Archives/edgar/data/1697748/000169774826000010/arkk-20260331_infotable.xml">arkk-20260331_infotable.xml</a></td></tr>
</table>
</body></html>"""

INFO_TABLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>TESLA INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>88160R101</cusip>
    <value>12345</value>
    <shrsOrPrnAmt><sshPrnamt>100000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
  </infoTable>
</informationTable>"""


def _make_client() -> EdgarClient:
    cfg = make_config()
    return EdgarClient(config=cfg, sleep_fn=lambda _: None)


# ---------------------------------------------------------------------------
# search_form13f_filings
# ---------------------------------------------------------------------------

def test_search_form13f_filings_returns_only_13f_forms(monkeypatch):
    c = _make_client()
    monkeypatch.setattr(c, "_get", lambda url, **k: SUBMISSIONS)
    rows = c.search_form13f_filings("0001697748", count=8)
    accessions = {r["accession"] for r in rows}
    # Only the two 13F-HR rows; not the Form 4
    assert accessions == {"0001697748-26-000010", "0001697748-26-000005"}


def test_search_form13f_filings_report_date_and_is_amendment(monkeypatch):
    c = _make_client()
    monkeypatch.setattr(c, "_get", lambda url, **k: SUBMISSIONS)
    rows = c.search_form13f_filings("0001697748", count=8)

    hr = next(r for r in rows if r["accession"] == "0001697748-26-000010")
    assert hr["report_date"] == "2026-03-31"
    assert hr["is_amendment"] is False
    assert hr["filed_at"] == "2026-05-15"

    amd = next(r for r in rows if r["accession"] == "0001697748-26-000005")
    assert amd["report_date"] == "2025-12-31"
    assert amd["is_amendment"] is True


def test_search_form13f_filings_standard_keys_present(monkeypatch):
    c = _make_client()
    monkeypatch.setattr(c, "_get", lambda url, **k: SUBMISSIONS)
    rows = c.search_form13f_filings("0001697748", count=8)
    for row in rows:
        assert set(row.keys()) >= {"cik", "accession", "filed_at",
                                   "report_date", "primary_document",
                                   "is_amendment"}


def test_search_form13f_filings_no_form_key_in_output(monkeypatch):
    """The raw 'form' key must be consumed internally, not leaked to the caller."""
    c = _make_client()
    monkeypatch.setattr(c, "_get", lambda url, **k: SUBMISSIONS)
    rows = c.search_form13f_filings("0001697748", count=8)
    for row in rows:
        assert "form" not in row


def test_search_form13f_filings_count_respected(monkeypatch):
    c = _make_client()
    monkeypatch.setattr(c, "_get", lambda url, **k: SUBMISSIONS)
    rows = c.search_form13f_filings("0001697748", count=1)
    assert len(rows) == 1


def test_search_form13f_filings_malformed_body(monkeypatch):
    c = _make_client()
    monkeypatch.setattr(c, "_get", lambda url, **k: "not json {")
    rows = c.search_form13f_filings("0001697748")
    assert rows == []


# ---------------------------------------------------------------------------
# existing form4 / sc13 callers unaffected by _parse_submissions_json change
# ---------------------------------------------------------------------------

def test_parse_submissions_json_report_date_absent_for_form4(monkeypatch):
    """Form-4 rows don't have reportDate; they get report_date='' — not an error."""
    from arbiter.ingest.edgar.client import _parse_submissions_json
    body = """{
        "filings": {"recent": {
            "form": ["4"],
            "accessionNumber": ["0001234567-26-000001"],
            "filingDate": ["2026-03-01"],
            "primaryDocument": ["form4.xml"]
        }}
    }"""
    rows = _parse_submissions_json(body, "0001234567", form_types={"4"}, count=5)
    assert len(rows) == 1
    assert rows[0]["report_date"] == ""


def test_parse_submissions_json_no_report_date_array(monkeypatch):
    """If reportDate array entirely absent, gracefully defaults to ''."""
    from arbiter.ingest.edgar.client import _parse_submissions_json
    body = """{
        "filings": {"recent": {
            "form": ["13F-HR"],
            "accessionNumber": ["0001697748-26-000010"],
            "filingDate": ["2026-05-15"],
            "primaryDocument": ["primary_doc.xml"]
        }}
    }"""
    rows = _parse_submissions_json(body, "0001697748",
                                   form_types={"13F-HR"}, count=5)
    assert len(rows) == 1
    assert rows[0]["report_date"] == ""


# ---------------------------------------------------------------------------
# get_form13f_info_table
# ---------------------------------------------------------------------------

def test_get_form13f_info_table_fetches_infotable_xml(monkeypatch):
    c = _make_client()
    calls: list[str] = []

    def fake_get(url: str, **k) -> str:
        calls.append(url)
        if "index.htm" in url:
            return INDEX_HTML
        return INFO_TABLE_XML

    monkeypatch.setattr(c, "_get", fake_get)
    result = c.get_form13f_info_table("0001697748-26-000010", "1697748")
    assert "informationTable" in result or "<informationTable" in result or "TESLA" in result
    # Must have fetched the index and the infotable doc
    assert any("index.htm" in u for u in calls)
    assert any("infotable" in u for u in calls)


def test_get_form13f_info_table_no_xml_returns_empty(monkeypatch):
    c = _make_client()

    def fake_get(url: str, **k) -> str:
        if "index.htm" in url:
            return "<html><body><a href='doc.txt'>doc.txt</a></body></html>"
        return ""

    monkeypatch.setattr(c, "_get", fake_get)
    result = c.get_form13f_info_table("0001697748-26-000010", "1697748")
    assert result == ""


# ---------------------------------------------------------------------------
# _extract_form13f_table_filename  (unit-level)
# ---------------------------------------------------------------------------

def test_extract_form13f_table_filename_prefers_infotable():
    html = """<html><body>
    <a href="/data/123/000123/primary_doc.xml">primary_doc.xml</a>
    <a href="/data/123/000123/arkk-20260331_infotable.xml">arkk-20260331_infotable.xml</a>
    </body></html>"""
    result = _extract_form13f_table_filename(html)
    assert result == "arkk-20260331_infotable.xml"


def test_extract_form13f_table_filename_falls_back_to_any_xml():
    html = """<html><body>
    <a href="/data/123/000123/other.xml">other.xml</a>
    </body></html>"""
    result = _extract_form13f_table_filename(html)
    assert result == "other.xml"


def test_extract_form13f_table_filename_excludes_primary_doc():
    html = """<html><body>
    <a href="/data/123/000123/primary_doc.xml">primary_doc.xml</a>
    </body></html>"""
    result = _extract_form13f_table_filename(html)
    assert result is None


def test_extract_form13f_table_filename_no_xml_returns_none():
    html = "<html><body><a href='/doc.txt'>doc.txt</a></body></html>"
    result = _extract_form13f_table_filename(html)
    assert result is None


def test_extract_form13f_table_filename_form13f_keyword():
    html = """<html><body>
    <a href="/data/123/000123/form13f_holdings.xml">form13f_holdings.xml</a>
    </body></html>"""
    result = _extract_form13f_table_filename(html)
    assert result == "form13f_holdings.xml"
