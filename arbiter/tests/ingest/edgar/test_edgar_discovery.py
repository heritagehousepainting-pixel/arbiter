"""Form-4 + 13D/13G discovery tests (submissions-JSON transport).

Regression coverage for the original bug: discovery used to yield empty
``accession``/``cik`` (browse-edgar atom). These tests assert non-empty
``accession``/``cik``/``primary_document`` from the submissions JSON.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from arbiter.ingest.edgar.client import EdgarClient
from arbiter.ingest.edgar.normalize import normalize
from arbiter.ingest.edgar.parser import parse_form4

from tests.ingest.edgar.conftest import make_config, make_resp, read_fixture


COMPANY_TICKERS = read_fixture("company_tickers.json")
SUBMISSIONS_AAPL = read_fixture("submissions_AAPL.json")
SUBMISSIONS_EMPTY = read_fixture("submissions_empty.json")

# A minimal Form-4 XML for the end-to-end test.
FORM4_BUY_XML = """\
<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2026-03-20</periodOfReport>
  <filingDate>2026-03-20</filingDate>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001234567</rptOwnerCik>
      <rptOwnerName>Jane Q. Insider</rptOwnerName>
    </reportingOwnerId>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>5000</value></transactionShares>
        <transactionPricePerShare><value>42.75</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


def _routed_http(routes: dict[str, str]) -> MagicMock:
    """Return a MagicMock httpx client whose .get routes by URL substring."""
    mock_http = MagicMock()

    def _get(url: str):
        for needle, body in routes.items():
            if needle in url:
                return make_resp(body)
        raise AssertionError(f"unexpected URL requested: {url}")

    mock_http.get.side_effect = _get
    return mock_http


def _client(mock_http: MagicMock, ua: str = "ArbiterTest test@example.com") -> EdgarClient:
    return EdgarClient(
        config=make_config(ua),
        http_client=mock_http,
        sleep_fn=lambda _: None,
    )


# ---------------------------------------------------------------------------
# get_cik_for_ticker
# ---------------------------------------------------------------------------

def test_get_cik_for_ticker_zero_padded():
    mock_http = _routed_http({"company_tickers.json": COMPANY_TICKERS})
    client = _client(mock_http)
    assert client.get_cik_for_ticker("AAPL") == "0000320193"
    assert client.get_cik_for_ticker("aapl") == "0000320193"  # case-insensitive
    # Map is cached: a second call makes no extra HTTP request.
    client.get_cik_for_ticker("MSFT")
    assert mock_http.get.call_count == 1


def test_get_cik_for_ticker_unknown_returns_none():
    mock_http = _routed_http({"company_tickers.json": COMPANY_TICKERS})
    client = _client(mock_http)
    assert client.get_cik_for_ticker("NOPE") is None


# ---------------------------------------------------------------------------
# search_form4_filings
# ---------------------------------------------------------------------------

def test_search_form4_filings_returns_only_form4_nonempty():
    mock_http = _routed_http(
        {
            "company_tickers.json": COMPANY_TICKERS,
            "submissions/CIK": SUBMISSIONS_AAPL,
        }
    )
    client = _client(mock_http)
    rows = client.search_form4_filings("AAPL")

    # Only the two form == "4" rows survive.
    assert len(rows) == 2
    # Newest-first ordering preserved from the JSON.
    assert rows[0]["accession"] == "0001234567-26-000010"
    assert rows[1]["accession"] == "0001234567-26-000006"
    for r in rows:
        # Regression assertions: accession + cik + primary_document non-empty.
        assert r["accession"]
        assert r["cik"] == "0000320193"
        assert r["primary_document"]
        assert r["filed_at"]


def test_search_form4_filings_empty_submissions():
    mock_http = _routed_http(
        {
            "company_tickers.json": COMPANY_TICKERS,
            "submissions/CIK": SUBMISSIONS_EMPTY,
        }
    )
    client = _client(mock_http)
    assert client.search_form4_filings("AAPL") == []


def test_search_form4_filings_unresolvable_ticker():
    mock_http = _routed_http({"company_tickers.json": COMPANY_TICKERS})
    client = _client(mock_http)
    # Unknown ticker -> no CIK -> [], and no submissions GET attempted.
    assert client.search_form4_filings("NOPE") == []
    assert mock_http.get.call_count == 1  # only company_tickers fetched


# ---------------------------------------------------------------------------
# get_form4_xml primary-document fast path vs index fallback
# ---------------------------------------------------------------------------

def test_get_form4_xml_primary_document_skips_index():
    mock_http = MagicMock()
    mock_http.get.return_value = make_resp(FORM4_BUY_XML)
    client = _client(mock_http)

    result = client.get_form4_xml(
        "0001234567-26-000010", "0000320193", primary_document="form4_new.xml"
    )
    assert result == FORM4_BUY_XML
    # Exactly one GET (the doc URL), no index round-trip.
    assert mock_http.get.call_count == 1
    called_url = mock_http.get.call_args[0][0]
    assert called_url.endswith("/000123456726000010/form4_new.xml")
    assert "index.htm" not in called_url


def test_get_form4_xml_falls_back_to_index_scrape():
    index_html = (
        'href="/Archives/edgar/data/320193/000123456726000010/form4_new.xml"'
    )
    mock_http = MagicMock()
    mock_http.get.side_effect = [make_resp(index_html), make_resp(FORM4_BUY_XML)]
    client = _client(mock_http)

    result = client.get_form4_xml("0001234567-26-000010", "0000320193")
    assert result == FORM4_BUY_XML
    assert mock_http.get.call_count == 2
    first_url = mock_http.get.call_args_list[0][0][0]
    assert first_url.endswith("-index.htm")


# ---------------------------------------------------------------------------
# end-to-end: discovery -> fetch -> parse -> normalize
# ---------------------------------------------------------------------------

def test_form4_end_to_end_yields_rawfiling():
    mock_http = _routed_http(
        {
            "company_tickers.json": COMPANY_TICKERS,
            "submissions/CIK": SUBMISSIONS_AAPL,
            "form4_new.xml": FORM4_BUY_XML,
            "form4_old.xml": FORM4_BUY_XML,
        }
    )
    client = _client(mock_http)

    rows = client.search_form4_filings("AAPL")
    assert rows
    first = rows[0]
    xml = client.get_form4_xml(
        first["accession"], first["cik"], primary_document=first["primary_document"]
    )
    parsed = parse_form4(xml, "AAPL", first["accession"])
    normalized = normalize(parsed)

    assert len(normalized) >= 1
    assert normalized[0]["source"] == "form4"
    assert normalized[0]["txn_type"] == "P"


# ---------------------------------------------------------------------------
# search_sc13_filings
# ---------------------------------------------------------------------------

def test_search_sc13_filings_returns_only_sc13_tagged():
    mock_http = _routed_http(
        {
            "company_tickers.json": COMPANY_TICKERS,
            "submissions/CIK": SUBMISSIONS_AAPL,
        }
    )
    client = _client(mock_http)
    rows = client.search_sc13_filings("AAPL")

    assert len(rows) == 2  # SC 13D + SC 13G/A
    sc13d = rows[0]
    assert sc13d["accession"] == "0009999999-26-000008"
    assert sc13d["schedule"] == "13D"
    assert sc13d["is_amendment"] is False
    assert sc13d["primary_document"] == "sc13d.xml"
    assert "form" not in sc13d  # internal key popped

    sc13ga = rows[1]
    assert sc13ga["schedule"] == "13G"
    assert sc13ga["is_amendment"] is True


def test_search_sc13_filings_unresolvable_ticker():
    mock_http = _routed_http({"company_tickers.json": COMPANY_TICKERS})
    client = _client(mock_http)
    assert client.search_sc13_filings("NOPE") == []


def test_get_sc13_doc_primary_txt_skips_index():
    body = read_fixture("sc13_header_only.txt")
    mock_http = MagicMock()
    mock_http.get.return_value = make_resp(body)
    client = _client(mock_http)

    result = client.get_sc13_doc(
        "0005555555-26-000001", "0000320193", primary_document="sc13.txt"
    )
    assert result == body
    assert mock_http.get.call_count == 1
    assert mock_http.get.call_args[0][0].endswith("/sc13.txt")
