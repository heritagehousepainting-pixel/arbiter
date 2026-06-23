"""Security + robustness regression tests for the EDGAR ingest lane.

Covers findings from the 2026-06-19 security/correctness audit:

* SSRF / path-traversal: CIK / accession / primary_document are sanitized
  (digits / dash / single-segment only) before reaching a URL — hostile values
  from an untrusted response body cannot escape ``www.sec.gov`` or the filing
  directory.
* Untrusted-response robustness: malformed / scalar / null JSON and
  malformed / empty / non-XML filing bodies never raise — they degrade to
  ``[]`` / ``{}`` / a skipped row.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from arbiter.ingest.edgar.client import (
    EdgarClient,
    EdgarError,
    _parse_company_tickers,
    _parse_submissions_json,
    _sanitize_accession,
    _sanitize_cik,
    _sanitize_primary_doc,
)
from arbiter.ingest.edgar.parser import parse_form4
from arbiter.ingest.edgar.sc13_parser import parse_sc13

from tests.ingest.edgar.conftest import make_config, make_resp


def _client(get_side_effect) -> tuple[EdgarClient, MagicMock]:
    http = MagicMock()
    http.get.side_effect = get_side_effect
    client = EdgarClient(
        config=make_config(),
        http_client=http,
        sleep_fn=lambda _: None,
    )
    return client, http


# ---------------------------------------------------------------------------
# SSRF / path-traversal sanitizers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    ["../etc", "@evil.com", "0000320193/../../x", "http://evil", "12 34", ""],
)
def test_sanitize_cik_rejects_hostile(value):
    with pytest.raises(EdgarError):
        _sanitize_cik(value)


def test_sanitize_cik_accepts_and_pads():
    assert _sanitize_cik("320193") == "0000320193"
    assert _sanitize_cik("0000320193") == "0000320193"
    assert _sanitize_cik("CIK0000320193") == "0000320193"


@pytest.mark.parametrize("value", ["../../x", "0001/../2", "@host", "a; rm", ""])
def test_sanitize_accession_rejects_hostile(value):
    with pytest.raises(EdgarError):
        _sanitize_accession(value)


def test_sanitize_accession_accepts_dashed():
    assert _sanitize_accession("0001234567-26-000001") == "0001234567-26-000001"


@pytest.mark.parametrize(
    "value",
    ["../../etc/passwd", "a\\b.xml", "..", "/abs.xml", "", "a/../b.xml", "x/./y.xml"],
)
def test_sanitize_primary_doc_rejects_traversal(value):
    with pytest.raises(EdgarError):
        _sanitize_primary_doc(value)


def test_sanitize_primary_doc_accepts_filename():
    assert _sanitize_primary_doc("form4_new.xml") == "form4_new.xml"
    # SEC's real XSL-viewer path is a legitimate relative subdir (not traversal).
    assert _sanitize_primary_doc("xslF345X06/form4.xml") == "xslF345X06/form4.xml"
    assert _sanitize_primary_doc("a/b.xml") == "a/b.xml"


def test_get_form4_xml_blocks_traversal_accession():
    client, _ = _client(lambda url: make_resp("<x/>"))
    with pytest.raises(EdgarError):
        client.get_form4_xml(
            "../../../etc/passwd", "0000320193", primary_document="p.xml"
        )


def test_get_form4_xml_blocks_host_injection_cik():
    client, _ = _client(lambda url: make_resp("<x/>"))
    with pytest.raises(EdgarError):
        client.get_form4_xml(
            "0001234567-26-000010", "@evil.com/x", primary_document="p.xml"
        )


def test_get_form4_xml_stays_on_sec_host():
    """Even a traversal primary_document cannot escape the filing dir.

    A non-".xml" hostile primary_document is ignored (suffix mismatch) and the
    index-scrape fallback is used; every URL stays under www.sec.gov.
    """
    seen: list[str] = []

    def _get(url: str):
        seen.append(url)
        if "index" in url:
            return make_resp(
                'href="/Archives/edgar/data/320193/000123456726000010/real.xml"'
            )
        return make_resp("<x/>")

    client, _ = _client(_get)
    client.get_form4_xml(
        "0001234567-26-000010",
        "0000320193",
        primary_document="../../../etc/passwd",
    )
    assert seen, "expected at least one request"
    for url in seen:
        assert url.startswith("https://www.sec.gov/Archives/edgar/data/")
        assert ".." not in url
        assert "etc/passwd" not in url


# ---------------------------------------------------------------------------
# Untrusted-JSON robustness — never raise
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "body",
    ["null", "123", "true", '"a string"', "[1, 2, 3]", "{}", "not json at all", ""],
)
def test_parse_company_tickers_never_raises(body):
    out = _parse_company_tickers(body)
    assert isinstance(out, dict)


def test_parse_company_tickers_skips_non_numeric_cik():
    # Non-numeric cik_str is a corrupt record — dropped, not turned into a
    # garbage padded CIK that would flow into a URL.
    body = '{"0": {"cik_str": "NOTNUM", "ticker": "X"}, "1": {"cik_str": 320193, "ticker": "AAPL"}}'
    out = _parse_company_tickers(body)
    assert out == {"AAPL": "0000320193"}


@pytest.mark.parametrize(
    "body",
    [
        "null",
        "123",
        "true",
        '"a string"',
        "[1, 2, 3]",
        "{}",
        '{"filings": [1, 2]}',
        '{"filings": {"recent": "oops"}}',
        '{"filings": {"recent": {"form": "notalist"}}}',
        "not json at all",
        "",
    ],
)
def test_parse_submissions_json_never_raises(body):
    out = _parse_submissions_json(body, "0000320193", form_types={"4"})
    assert isinstance(out, list)


def test_parse_submissions_json_mismatched_array_lengths():
    # Parallel arrays of differing length must not index out of range; we take
    # the shortest common prefix.
    body = (
        '{"filings": {"recent": {'
        '"form": ["4", "4", "4"], '
        '"accessionNumber": ["0001-26-1"], '
        '"filingDate": ["2026-01-01"], '
        '"primaryDocument": ["p.xml"]}}}'
    )
    out = _parse_submissions_json(body, "0000320193", form_types={"4"})
    assert len(out) == 1
    assert out[0]["accession"] == "0001-26-1"


# ---------------------------------------------------------------------------
# Untrusted filing-body robustness — parsers never raise
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "not xml at all",
        "<unclosed>",
        '<?xml version="1.0"?><a>&undefined;</a>',
        '{"json": true}',
        # well-formed XML but no usable filing date -> unusable, not a crash
        "<ownershipDocument></ownershipDocument>",
    ],
)
def test_parse_form4_never_raises(body):
    assert parse_form4(body, "AAPL", "0001-26-1") == []


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "not xml",
        "<unclosed>",
        '<?xml version="1.0"?><a>&undefined;</a>',
        '{"json": true}',
        # structured XML that parses but has no event/filing date
        "<edgarSubmission></edgarSubmission>",
    ],
)
def test_parse_sc13_never_raises(body):
    assert parse_sc13(body, "AAPL", "0001-26-1", schedule="13D") == []
