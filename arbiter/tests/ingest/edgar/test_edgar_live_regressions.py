"""Offline regressions for three live-only EDGAR bugs found during the
2026-06-19 live verification (real SEC fetches; fixtures had missed them).

1. ``_sanitize_primary_doc`` was too strict — it rejected SEC's legitimate
   XSL-viewer subdirectory ``"xslF345X06/form4.xml"`` (the audit's SSRF fix
   over-reached). It must allow relative subdirs but still block traversal.
2. The ``Archives/edgar/data/`` path needs the *un-padded* integer CIK; the
   zero-padded form 301-redirects.
3. ``primaryDocument`` points at the XSL-*rendered* HTML viewer, not the raw
   ``ownershipDocument`` XML — the ``xsl…/`` prefix must be stripped to fetch
   the parseable XML.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from arbiter.ingest.edgar.client import (
    EdgarClient,
    EdgarError,
    _sanitize_primary_doc,
    _strip_xsl_prefix,
)

from .conftest import make_config, make_resp


# --- 1. sanitizer: real subdir allowed, traversal still blocked -------------

def test_sanitize_primary_doc_allows_real_xsl_subdir() -> None:
    assert _sanitize_primary_doc("xslF345X06/form4.xml") == "xslF345X06/form4.xml"
    assert _sanitize_primary_doc("form4.xml") == "form4.xml"


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../etc/passwd",
        "/etc/passwd",
        "a/../../x",
        "foo/../bar",
        "..\\windows",
        "x/./y",
        "",
        "..",
        ".",
    ],
)
def test_sanitize_primary_doc_blocks_traversal(hostile: str) -> None:
    with pytest.raises(EdgarError):
        _sanitize_primary_doc(hostile)


# --- 2 + 3. xsl-prefix strip --------------------------------------------------

def test_strip_xsl_prefix() -> None:
    assert _strip_xsl_prefix("xslF345X06/form4.xml") == "form4.xml"
    assert _strip_xsl_prefix("XSLF345X05/doc.xml") == "doc.xml"  # case-insensitive
    assert _strip_xsl_prefix("form4.xml") == "form4.xml"  # no-op, no prefix
    assert _strip_xsl_prefix("subdir/form4.xml") == "subdir/form4.xml"  # non-xsl kept


def _url_recording_http() -> MagicMock:
    http = MagicMock()
    http.get = MagicMock(return_value=make_resp("<ownershipDocument/>"))
    return http


def test_get_form4_xml_unpads_cik_and_strips_xsl() -> None:
    """The fast-path URL must use the un-padded CIK and the raw (non-xsl) doc."""
    http = _url_recording_http()
    client = EdgarClient(
        config=make_config(),
        http_client=http,
        sleep_fn=lambda _: None,
    )
    client.get_form4_xml(
        "0001140361-26-025622",
        "0000320193",  # zero-padded — must be un-padded in the Archives URL
        primary_document="xslF345X06/form4.xml",  # viewer path — xsl must be stripped
    )
    url = http.get.call_args[0][0]
    assert "/data/320193/" in url, f"CIK not un-padded: {url}"
    assert "/0001140361" not in url.split("/data/")[1].split("/")[0]  # cik segment only
    assert url.endswith("/000114036126025622/form4.xml"), url
    assert "xslF345X06" not in url, f"xsl viewer prefix not stripped: {url}"
