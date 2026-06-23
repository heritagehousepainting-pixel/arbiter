"""Tests for Congress HTTP client — Layer L1.

Covers:
  - fetch_house_index: hits correct ``{year}FD.zip`` URL, returns bytes
  - fetch_ptr_pdf: hits correct ``ptr-pdfs/{year}/{doc_id}.pdf`` URL, returns bytes
  - non-200 responses raise ``CongressFetchError``
  - Senate stubs (fetch_senate_index / fetch_senate_ptrs) return without crashing

ALL network is mocked — zero real HTTP calls.  We inject a fake ``httpx.Client``
via the constructor so the mock intercepts at the transport layer, exercising
the same code path as production without needing respx or unittest.mock patches.

Design
------
- ``from __future__ import annotations`` (py3.11+ convention, INTERFACES.md §11)
- Uses a minimal ``FakeTransport`` that records which URL was called and returns
  a configurable status code + body.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from arbiter.ingest.congress.client import (
    CongressClient,
    CongressFetchError,
    HOUSE_BASE_URL,
)


# ---------------------------------------------------------------------------
# Fake transport — intercepts all HTTP at the transport layer
# ---------------------------------------------------------------------------


class FakeTransport(httpx.BaseTransport):
    """Minimal fake HTTPX transport.

    Records the last request URL and returns a configurable response.

    Parameters
    ----------
    status_code:
        HTTP status code to return (default 200).
    body:
        Raw response bytes (default b"fake-payload").
    """

    def __init__(
        self,
        status_code: int = 200,
        body: bytes = b"fake-payload",
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.requested_urls: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requested_urls.append(str(request.url))
        return httpx.Response(
            status_code=self.status_code,
            content=self.body,
            request=request,
        )


def _make_client(status_code: int = 200, body: bytes = b"fake-payload") -> tuple[CongressClient, FakeTransport]:
    """Return a (CongressClient, FakeTransport) pair wired together."""
    transport = FakeTransport(status_code=status_code, body=body)
    http = httpx.Client(transport=transport, base_url="")
    client = CongressClient(http_client=http)
    return client, transport


# ---------------------------------------------------------------------------
# fetch_house_index
# ---------------------------------------------------------------------------


class TestFetchHouseIndex:
    def test_returns_bytes(self) -> None:
        client, _ = _make_client(body=b"PK\x03\x04fake-zip")
        result = client.fetch_house_index(2026)
        assert isinstance(result, bytes)
        assert result == b"PK\x03\x04fake-zip"

    def test_hits_correct_url_pattern(self) -> None:
        client, transport = _make_client()
        client.fetch_house_index(2026)
        assert len(transport.requested_urls) == 1
        url = transport.requested_urls[0]
        assert "2026FD.zip" in url

    def test_url_contains_year(self) -> None:
        client, transport = _make_client()
        client.fetch_house_index(2024)
        url = transport.requested_urls[0]
        assert "2024FD.zip" in url

    def test_url_contains_house_base(self) -> None:
        client, transport = _make_client()
        client.fetch_house_index(2026)
        url = transport.requested_urls[0]
        assert "disclosures-clerk.house.gov" in url

    def test_url_correct_path_structure(self) -> None:
        """URL must be .../public_disc/financial-pdfs/{year}FD.zip"""
        client, transport = _make_client()
        client.fetch_house_index(2026)
        url = transport.requested_urls[0]
        assert "/public_disc/financial-pdfs/2026FD.zip" in url

    def test_non_200_raises_congress_fetch_error(self) -> None:
        client, _ = _make_client(status_code=404)
        with pytest.raises(CongressFetchError) as exc_info:
            client.fetch_house_index(2026)
        assert exc_info.value.status_code == 404

    def test_500_raises_congress_fetch_error(self) -> None:
        client, _ = _make_client(status_code=500)
        with pytest.raises(CongressFetchError) as exc_info:
            client.fetch_house_index(2026)
        assert exc_info.value.status_code == 500

    def test_error_contains_url(self) -> None:
        client, _ = _make_client(status_code=404)
        with pytest.raises(CongressFetchError) as exc_info:
            client.fetch_house_index(2026)
        err = exc_info.value
        assert err.url is not None
        assert "2026FD.zip" in err.url

    def test_different_years_produce_different_urls(self) -> None:
        client, transport = _make_client()
        client.fetch_house_index(2024)
        client.fetch_house_index(2025)
        assert "2024FD.zip" in transport.requested_urls[0]
        assert "2025FD.zip" in transport.requested_urls[1]


# ---------------------------------------------------------------------------
# fetch_ptr_pdf
# ---------------------------------------------------------------------------


class TestFetchPtrPdf:
    def test_returns_bytes(self) -> None:
        pdf_bytes = b"%PDF-1.4 fake"
        client, _ = _make_client(body=pdf_bytes)
        result = client.fetch_ptr_pdf(2026, "20034201")
        assert isinstance(result, bytes)
        assert result == pdf_bytes

    def test_hits_correct_url_pattern(self) -> None:
        client, transport = _make_client()
        client.fetch_ptr_pdf(2026, "20034201")
        url = transport.requested_urls[0]
        assert "ptr-pdfs/2026/20034201.pdf" in url

    def test_url_contains_year_and_doc_id(self) -> None:
        client, transport = _make_client()
        client.fetch_ptr_pdf(2024, "20033751")
        url = transport.requested_urls[0]
        assert "2024" in url
        assert "20033751.pdf" in url

    def test_url_correct_path_structure(self) -> None:
        """URL must be .../public_disc/ptr-pdfs/{year}/{doc_id}.pdf"""
        client, transport = _make_client()
        client.fetch_ptr_pdf(2026, "20034201")
        url = transport.requested_urls[0]
        assert "/public_disc/ptr-pdfs/2026/20034201.pdf" in url

    def test_url_contains_house_base(self) -> None:
        client, transport = _make_client()
        client.fetch_ptr_pdf(2026, "20034201")
        url = transport.requested_urls[0]
        assert "disclosures-clerk.house.gov" in url

    def test_non_200_raises_congress_fetch_error(self) -> None:
        """404 on a DocID (e.g. old scanned filing removed) must raise."""
        client, _ = _make_client(status_code=404)
        with pytest.raises(CongressFetchError) as exc_info:
            client.fetch_ptr_pdf(2026, "8068")
        assert exc_info.value.status_code == 404

    def test_error_is_congress_fetch_error_type(self) -> None:
        client, _ = _make_client(status_code=403)
        with pytest.raises(CongressFetchError):
            client.fetch_ptr_pdf(2026, "20034201")

    def test_error_contains_url(self) -> None:
        client, _ = _make_client(status_code=404)
        with pytest.raises(CongressFetchError) as exc_info:
            client.fetch_ptr_pdf(2026, "20034201")
        assert "20034201.pdf" in exc_info.value.url

    def test_short_doc_id_scanned_filing(self) -> None:
        """Short doc_id (scanned/paper) hits the same URL pattern — L3 handles detection."""
        client, transport = _make_client()
        client.fetch_ptr_pdf(2026, "8068")
        url = transport.requested_urls[0]
        assert "ptr-pdfs/2026/8068.pdf" in url

    def test_returns_exact_bytes_from_server(self) -> None:
        """Client must return the exact bytes — no decoding/parsing."""
        payload = b"\x00\x01\x02PDF binary content \xff\xfe"
        client, _ = _make_client(body=payload)
        result = client.fetch_ptr_pdf(2026, "20034201")
        assert result == payload


# ---------------------------------------------------------------------------
# CongressFetchError contract
# ---------------------------------------------------------------------------


class TestCongressFetchError:
    def test_has_url_attribute(self) -> None:
        err = CongressFetchError("test", url="https://example.com/test", status_code=404)
        assert err.url == "https://example.com/test"

    def test_has_status_code_attribute(self) -> None:
        err = CongressFetchError("test", url="https://example.com", status_code=404)
        assert err.status_code == 404

    def test_status_code_can_be_none(self) -> None:
        """Network errors (no response) set status_code=None."""
        err = CongressFetchError("network error", url="https://example.com", status_code=None)
        assert err.status_code is None

    def test_is_exception(self) -> None:
        err = CongressFetchError("msg", url="u")
        assert isinstance(err, Exception)


# ---------------------------------------------------------------------------
# Senate stubs — must not crash, return empty
# ---------------------------------------------------------------------------


class TestSenateStubbedMethods:
    """Senate fetch_* methods are v1 stubs — they return empty and log a warning."""

    def test_fetch_senate_index_returns_bytes(self) -> None:
        # Senate stub does NOT make any HTTP call — no transport needed
        client = CongressClient()
        result = client.fetch_senate_index(2026)
        assert isinstance(result, bytes)

    def test_fetch_senate_index_returns_empty_bytes(self) -> None:
        client = CongressClient()
        result = client.fetch_senate_index(2026)
        assert result == b""

    def test_fetch_senate_ptrs_returns_list(self) -> None:
        client = CongressClient()
        result = client.fetch_senate_ptrs(year=2026)
        assert isinstance(result, list)

    def test_fetch_senate_ptrs_returns_empty_list(self) -> None:
        client = CongressClient()
        result = client.fetch_senate_ptrs(year=2026)
        assert result == []

    def test_fetch_senate_index_does_not_raise(self) -> None:
        client = CongressClient()
        # Should complete without raising even with no network
        client.fetch_senate_index(2026)

    def test_fetch_senate_ptrs_does_not_raise(self) -> None:
        client = CongressClient()
        client.fetch_senate_ptrs(
            year=2026,
            first_name="Tommy",
            last_name="Tuberville",
            report_types=["ptr"],
        )

    def test_fetch_senate_index_accepts_any_year(self) -> None:
        client = CongressClient()
        for year in (2020, 2023, 2026):
            result = client.fetch_senate_index(year)
            assert result == b""

    def test_fetch_senate_ptrs_kwargs_accepted(self) -> None:
        """All keyword args are accepted without error."""
        client = CongressClient()
        result = client.fetch_senate_ptrs(
            year=2026,
            first_name="Jane",
            last_name="Doe",
            report_types=["ptr", "annual"],
        )
        assert result == []


# ---------------------------------------------------------------------------
# Injectable client — constructor injection works
# ---------------------------------------------------------------------------


class TestClientInjection:
    def test_custom_http_client_is_used(self) -> None:
        transport = FakeTransport(status_code=200, body=b"injected")
        http = httpx.Client(transport=transport)
        client = CongressClient(http_client=http)
        result = client.fetch_house_index(2026)
        assert result == b"injected"
        assert len(transport.requested_urls) == 1

    def test_default_client_created_when_none_provided(self) -> None:
        """Constructor with no http_client creates a real client (smoke test)."""
        # We just verify construction doesn't raise — no network call made here
        client = CongressClient()
        assert client._http is not None
