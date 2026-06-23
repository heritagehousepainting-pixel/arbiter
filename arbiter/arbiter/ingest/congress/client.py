"""Congress disclosure HTTP client — Layer L1 (HTTP only, returns raw bytes).

Fetches raw bytes from:
  - House: https://disclosures-clerk.house.gov/  (primary v1 source)
  - Senate: https://efdsearch.senate.gov/search/ (CSRF-gated — stub in v1)

All parsing belongs to L2 (index.py) and L3 (ptr_pdf.py).
This module is pure HTTP: receive request, return raw bytes, raise on errors.

Design notes
------------
- Injectable httpx.Client for test mocking — zero real network in tests.
- ``from __future__ import annotations`` for py3.11+ forward references.
- No ``datetime.now()`` — callers pass year/doc_id explicitly.
- ``structlog`` for structured logging.
- ``CongressFetchError`` is the single public exception for non-200 responses.
"""
from __future__ import annotations

import structlog

import httpx

logger: structlog.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

HOUSE_BASE_URL = "https://disclosures-clerk.house.gov"
SENATE_EFD_URL = "https://efdsearch.senate.gov/search/"

DEFAULT_TIMEOUT: float = 30.0
USER_AGENT = "arbiter-congress-ingest/1.0 (contact: heritagehousepainting@gmail.com)"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CongressFetchError(Exception):
    """Raised when an HTTP request to a Congress disclosure endpoint fails.

    Attributes
    ----------
    url:
        The URL that was requested.
    status_code:
        The HTTP status code returned (e.g. 404, 500).  ``None`` if the
        request never received a response (network error).
    """

    def __init__(self, message: str, *, url: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class CongressClient:
    """HTTP client for Congress financial disclosure endpoints.

    Returns **raw bytes only** — all parsing is delegated to L2/L3.

    Parameters
    ----------
    timeout:
        HTTP timeout in seconds.  Defaults to ``DEFAULT_TIMEOUT`` (30s).
    http_client:
        Optional pre-configured ``httpx.Client``.  Inject a mock transport
        or a ``respx``-patched client in tests to avoid any real network I/O.
        When omitted, a real ``httpx.Client`` is created.
    """

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._timeout = timeout
        self._http = http_client or httpx.Client(
            timeout=self._timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    # House — primary v1 source
    # ------------------------------------------------------------------

    def fetch_house_index(self, year: int) -> bytes:
        """Fetch the House annual financial-disclosure index zip.

        Downloads ``{year}FD.zip`` which contains:
        - ``{year}FD.txt`` — TAB-delimited index (columns: Prefix Last First
          Suffix FilingType StateDst Year FilingDate DocID)
        - ``{year}FD.xml`` — same data in XML

        Parameters
        ----------
        year:
            The filing year (e.g. 2026).

        Returns
        -------
        bytes
            Raw zip file bytes.  Pass to ``index.py::parse_index`` for L2
            parsing.

        Raises
        ------
        CongressFetchError
            If the server returns a non-200 HTTP status.
        """
        url = f"{HOUSE_BASE_URL}/public_disc/financial-pdfs/{year}FD.zip"
        log = logger.bind(method="fetch_house_index", year=year, url=url)
        log.debug("fetching_house_index")
        return self._get_bytes(url)

    def fetch_ptr_pdf(self, year: int, doc_id: str) -> bytes:
        """Fetch a single House Periodic Transaction Report (PTR) PDF.

        Electronic filings (8-digit numeric DocID, e.g. ``20034201``) are
        text-extractable via pdfplumber.  Scanned/paper filings (short DocID,
        e.g. ``8068``) return an image PDF — L3 will detect and skip them.

        Parameters
        ----------
        year:
            The filing year (e.g. 2026).
        doc_id:
            The disclosure DocID as found in the House index (e.g.
            ``"20034201"`` or ``"8068"``).

        Returns
        -------
        bytes
            Raw PDF bytes.  Pass to ``ptr_pdf.py::extract_ptr_text`` for L3
            parsing.

        Raises
        ------
        CongressFetchError
            If the server returns a non-200 HTTP status (including 404 for
            unknown/scanned DocIDs that have been removed from the server).
        """
        url = f"{HOUSE_BASE_URL}/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
        log = logger.bind(method="fetch_ptr_pdf", year=year, doc_id=doc_id, url=url)
        log.debug("fetching_ptr_pdf")
        return self._get_bytes(url)

    # ------------------------------------------------------------------
    # Senate — CSRF/cookie flow via senate.py
    # ------------------------------------------------------------------

    def fetch_senate_index(self, year: int) -> bytes:
        """Senate eFD annual index — returns empty bytes.

        The Senate eFD does not publish a downloadable annual index zip like
        the House.  Transactions are discovered via the DataTables AJAX search
        at ``/search/report/data/``, which is handled by
        ``senate.fetch_senate_ptrs``.

        Returns
        -------
        bytes
            Always ``b""`` — use ``senate.fetch_senate_ptrs`` directly.
        """
        logger.debug(
            "senate_fetch_index_noop",
            reason="Senate eFD has no annual index zip; use senate.fetch_senate_ptrs",
            year=year,
        )
        return b""

    def fetch_senate_ptrs(
        self,
        *,
        year: int | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        report_types: list[str] | None = None,
    ) -> list[bytes]:
        """Senate PTR HTML pages via the eFD CSRF/cookie flow.

        Delegates to ``senate.fetch_senate_ptrs`` which handles the full
        three-step authentication + paginated search + per-report HTML fetch.

        Returns
        -------
        list[bytes]
            Empty list — callers should use ``fetch_senate_ptrs`` from
            ``arbiter.ingest.congress.senate`` or the orchestration helper in
            ``arbiter.ingest.congress`` directly.  This method exists for
            API symmetry; the senate module manages its own HTTP session
            (cookie jar + CSRF flow required).

        Notes
        -----
        The ``first_name``, ``last_name``, and ``report_types`` parameters are
        accepted for interface compatibility but are not forwarded — the senate
        module currently fetches all senators for the given year.
        """
        # Compatibility no-op: the real Senate flow (CSRF + cookie session +
        # paginated search + per-report HTML parse) lives in the `senate` module
        # and is orchestrated by `arbiter.ingest.congress.fetch_senate_ptrs`.
        # This method does NOT hit the network (doing so here would make every
        # caller — including tests — perform a live efdsearch request).
        logger.debug(
            "fetch_senate_ptrs: no-op; use arbiter.ingest.congress.fetch_senate_ptrs"
        )
        return []

    # ------------------------------------------------------------------
    # Internal HTTP primitive
    # ------------------------------------------------------------------

    def _get_bytes(self, url: str) -> bytes:
        """Perform a GET request and return the raw response bytes.

        This is the single choke-point for all real network I/O.
        Tests inject a mock ``httpx.Client`` via the constructor so this
        method is called normally — the mock transport intercepts at the
        transport layer.

        Parameters
        ----------
        url:
            The fully-qualified URL to GET.

        Returns
        -------
        bytes
            Raw response body.

        Raises
        ------
        CongressFetchError
            If the response status code is not 2xx.
        """
        try:
            response = self._http.get(url)
        except httpx.RequestError as exc:
            raise CongressFetchError(
                f"Network error fetching {url}: {exc}",
                url=url,
                status_code=None,
            ) from exc

        if response.status_code != 200:
            log = logger.bind(url=url, status_code=response.status_code)
            log.error("congress_fetch_non_200")
            raise CongressFetchError(
                f"Congress endpoint returned {response.status_code} for {url}",
                url=url,
                status_code=response.status_code,
            )

        return response.content
