"""EDGAR HTTP client — rate-limit aware, User-Agent declared.

This module is the ONLY place that makes network calls to EDGAR.
All tests mock this module — no real HTTP in unit tests.

Rate limits
-----------
SEC EDGAR's fair-use policy allows a maximum of **10 requests per second**
per IP address.  We default to 0.11 s between requests (≈ 9 req/s) with
an exponential back-off on 429 / 5xx responses.

EDGAR requires a User-Agent header of the form::

    Company Name email@example.com

This is read from ``Config.edgar_user_agent`` (INTERFACES.md §10b, field 5).
"""
from __future__ import annotations

import json
import re
import time
from typing import Callable

import httpx
import structlog

from arbiter.config import Config, load_config


log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
_DEFAULT_EDGAR_BASE = "https://www.sec.gov"
# data.sec.gov hosts the submissions JSON + company facts (separate host).
_DEFAULT_DATA_BASE = "https://data.sec.gov"
_MIN_INTERVAL_SEC = 0.11      # ≈ 9 req/s — below the 10/s hard cap
_MAX_RETRIES = 3
_BACKOFF_BASE = 2.0           # seconds; doubles per retry

# Static EDGAR map of {ticker -> cik}; fetched once per client instance.
_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
# Per-company submissions index keyed by zero-padded 10-digit CIK.
_SUBMISSIONS_URL_TMPL = "https://data.sec.gov/submissions/CIK{cik10}.json"

# 13D/13G submission form values as they appear in submissions JSON `form`.
_SC13_FORMS: frozenset[str] = frozenset(
    {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"}
)

# 13F-HR form values as they appear in submissions JSON `form`.
_13F_FORMS: frozenset[str] = frozenset({"13F-HR", "13F-HR/A"})


class EdgarError(Exception):
    """Raised when an EDGAR request fails after all retries."""


# ---------------------------------------------------------------------------
# SSRF / path-traversal guards
# ---------------------------------------------------------------------------
# EDGAR identifiers have strict shapes.  These values flow into URLs/paths and
# may originate from *untrusted* response bodies (submissions JSON,
# company_tickers.json), so they MUST be validated before interpolation —
# never trust the wire to keep them clean.
#
# - CIK: digits only (we zero-pad to 10).  No "/", "..", "@", scheme, etc.
# - Accession: digits and dashes only, e.g. "0001234567-26-000001".
# - Primary document: a single path segment — a bare filename, no separators.
_CIK_RE = re.compile(r"^[0-9]{1,10}$")
_ACCESSION_RE = re.compile(r"^[0-9-]{1,30}$")
_PRIMARY_DOC_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _sanitize_cik(cik: str) -> str:
    """Return a zero-padded 10-digit CIK, or raise on a hostile value.

    Strips a leading ``CIK`` prefix and surrounding whitespace, then enforces
    digits-only.  Rejects path-traversal / host-injection payloads
    (``../``, ``@host``, schemes) by construction.
    """
    raw = (cik or "").strip()
    if raw.upper().startswith("CIK"):
        raw = raw[3:]
    if not _CIK_RE.match(raw):
        raise EdgarError(f"Refusing to use unsafe CIK value: {cik!r}")
    return raw.zfill(10)


def _sanitize_accession(accession: str) -> str:
    """Return the accession unchanged if it is digits/dashes only, else raise."""
    raw = (accession or "").strip()
    if not _ACCESSION_RE.match(raw):
        raise EdgarError(f"Refusing to use unsafe accession value: {accession!r}")
    return raw


def _strip_xsl_prefix(doc_path: str) -> str:
    """Drop a leading SEC XSL viewer segment (``xslF345X06/…``).

    The submissions JSON ``primaryDocument`` for ownership forms points at the
    XSL-*rendered* HTML viewer (e.g. ``"xslF345X06/form4.xml"``), which is
    styled HTML, not the raw ``ownershipDocument`` XML the parser needs.  The
    raw XML lives at the same path with the ``xsl…/`` segment removed.  No-op
    when there is no such prefix (e.g. 13D/G plain documents).
    """
    head, sep, tail = doc_path.partition("/")
    if sep and head.lower().startswith("xsl"):
        return tail
    return doc_path


def _sanitize_primary_doc(primary_document: str) -> str:
    """Return the document path if it is a safe *relative* path under the
    filing directory.

    SEC's ``primaryDocument`` legitimately contains a subdirectory for the
    rendered viewer (e.g. ``"xslF345X06/form4.xml"``), so a single-segment
    rule is wrong.  We allow ``/``-separated relative segments but still reject
    anything that could escape the filing directory: backslashes, absolute
    paths (leading ``/``), traversal/dot-only segments (``.`` / ``..``), and
    any segment that is not a plain safe filename.
    """
    raw = (primary_document or "").strip()
    segments = raw.split("/")
    if (
        not raw
        or "\\" in raw
        or raw.startswith("/")  # absolute path
        or any(seg.strip(".") == "" for seg in segments)  # "", ".", ".."
        or not all(_PRIMARY_DOC_RE.match(seg) for seg in segments)
    ):
        raise EdgarError(
            f"Refusing to use unsafe primary_document value: {primary_document!r}"
        )
    return raw


class EdgarClient:
    """Thin EDGAR HTTP wrapper.

    Parameters
    ----------
    config:
        Loaded ``Config`` instance.  Uses ``config.edgar_user_agent``.
    base_url:
        Override for testing / staging.
    http_client:
        Injected ``httpx.Client`` (pass a mock in tests; ``None`` builds one).
    sleep_fn:
        Injected sleep callable (swap out in tests).
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        base_url: str = _DEFAULT_EDGAR_BASE,
        http_client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        cfg = config or load_config()
        self._user_agent = cfg.edgar_user_agent
        if not self._user_agent:
            raise ValueError(
                "Config.edgar_user_agent is empty.  "
                "Set [edgar] user_agent in arbiter.toml or EDGAR_USER_AGENT env var."
            )
        self._base_url = base_url.rstrip("/")
        self._http = http_client or httpx.Client(
            headers={"User-Agent": self._user_agent},
            timeout=30.0,
            follow_redirects=True,  # SEC 301s padded-CIK / legacy paths within sec.gov
        )
        self._sleep = sleep_fn
        self._last_request_ts: float = 0.0
        # Lazily-fetched {ticker -> cik10} map (company_tickers.json).
        self._ticker_cik_map: dict[str, str] | None = None

    @classmethod
    def from_config_or_none(
        cls,
        config: Config,
        *,
        base_url: str = _DEFAULT_EDGAR_BASE,
        http_client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> "EdgarClient | None":
        """Return a client, or ``None`` when ``config.edgar_user_agent`` is
        empty/whitespace.

        Unlike ``__init__`` (which raises ``ValueError`` for back-compat),
        this factory is the graceful-skip entry point: when the User-Agent is
        unset it logs **exactly one** WARNING and returns ``None`` so the
        whole EDGAR lane (Form-4 + 13D/G) goes inert without crashing.
        """
        if not (config.edgar_user_agent or "").strip():
            log.warning(
                "edgar.disabled_no_user_agent",
                reason="EDGAR_USER_AGENT unset; EDGAR ingest (Form-4 + 13D/G) inert",
            )
            return None
        return cls(
            config,
            base_url=base_url,
            http_client=http_client,
            sleep_fn=sleep_fn,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_cik_for_ticker(self, ticker: str) -> str | None:
        """Resolve ``ticker`` to its zero-padded 10-digit issuer CIK.

        Fetches ``company_tickers.json`` once per client instance and caches
        the parsed ``{ticker -> cik10}`` map on ``self._ticker_cik_map``.
        Returns ``None`` when the ticker is unknown.
        """
        if self._ticker_cik_map is None:
            raw = self._get(_COMPANY_TICKERS_URL)
            self._ticker_cik_map = _parse_company_tickers(raw)
        return self._ticker_cik_map.get(ticker.upper().strip())

    def get_form4_xml(
        self,
        accession: str,
        cik: str,
        *,
        primary_document: str | None = None,
    ) -> str:
        """Fetch the raw Form 4 XML for ``accession`` / ``cik``.

        When ``primary_document`` is known (and is an XML file), the document
        URL is built directly and the index round-trip is skipped (halving the
        request count).  When ``None``, fall back to the index-scrape path.

        Parameters
        ----------
        accession:
            EDGAR accession number with dashes, e.g. ``"0001234567-26-000001"``.
        cik:
            10-digit (zero-padded) CIK of the filer.
        primary_document:
            Optional primary-document filename from the submissions JSON.

        Returns
        -------
        Raw XML string.
        """
        return self._fetch_primary_doc(
            accession, cik, primary_document, suffixes=(".xml",)
        )

    def get_sc13_doc(
        self,
        accession: str,
        cik: str,
        *,
        primary_document: str | None = None,
    ) -> str:
        """Fetch the raw 13D/13G document (XML or plain-text) for ``accession``.

        Mirrors ``get_form4_xml`` but accepts ``.txt`` primary documents too
        (older schedules ship as plain-text ``<SEC-HEADER>`` filings).
        """
        return self._fetch_primary_doc(
            accession, cik, primary_document, suffixes=(".xml", ".txt")
        )

    def search_form4_filings(
        self,
        ticker: str,
        *,
        count: int = 20,
    ) -> list[dict]:
        """Discover recent Form-4 filings for ``ticker`` via submissions JSON.

        Returns a list of dicts with keys ``cik``, ``accession``,
        ``filed_at``, ``primary_document`` — newest-first.  Empty list when
        the ticker is unresolvable or no Form-4 filings exist.
        """
        cik = self.get_cik_for_ticker(ticker)
        if cik is None:
            log.debug("edgar.no_cik_for_ticker", ticker=ticker)
            return []
        body = self._get(_SUBMISSIONS_URL_TMPL.format(cik10=cik))
        return _parse_submissions_json(body, cik, form_types={"4"}, count=count)

    def search_sc13_filings(
        self,
        ticker: str,
        *,
        count: int = 20,
    ) -> list[dict]:
        """Discover recent 13D/13G filings made **against** ``ticker``.

        The submissions JSON of the subject company lists the 13D/G filings
        filed on it — exactly the rows we trade.  Each result dict carries
        ``cik``, ``accession``, ``filed_at``, ``primary_document``, plus
        ``schedule`` (``"13D"``/``"13G"``) and ``is_amendment``.
        """
        cik = self.get_cik_for_ticker(ticker)
        if cik is None:
            log.debug("edgar.no_cik_for_ticker", ticker=ticker)
            return []
        body = self._get(_SUBMISSIONS_URL_TMPL.format(cik10=cik))
        rows = _parse_submissions_json(
            body, cik, form_types=_SC13_FORMS, count=count, keep_form=True
        )
        for row in rows:
            form = row.pop("form", "")
            row["schedule"] = "13D" if "13D" in form else "13G"
            row["is_amendment"] = form.endswith("/A")
        return rows

    def search_form13f_filings(
        self,
        cik: str,
        *,
        count: int = 8,
    ) -> list[dict]:
        """Discover a manager's own recent 13F-HR filings via their submissions JSON.

        ``cik`` is the manager's own filer CIK — **not** a ticker.  No ticker→CIK
        lookup is performed; callers supply the manager CIK directly from the
        fund roster.

        Returns newest-first dicts with keys ``cik``, ``accession``,
        ``filed_at``, ``report_date``, ``primary_document``, ``is_amendment``.
        """
        body = self._get(_SUBMISSIONS_URL_TMPL.format(cik10=cik))
        rows = _parse_submissions_json(
            body, cik, form_types=_13F_FORMS, count=count, keep_form=True
        )
        for row in rows:
            form = row.pop("form", "")
            row["is_amendment"] = form.endswith("/A")
        return rows

    def get_form13f_info_table(self, accession: str, cik: str) -> str:
        """Fetch the 13F information-table XML for a filing.

        A 13F-HR filing has (at least) two documents: the cover-page XML
        (``primaryDocument``) and a separate information-table XML that holds
        the actual holdings.  This method scrapes the filing index page for a
        document whose filename contains ``infotable``, ``form13f``, or ``13f``
        (case-insensitive) and ends in ``.xml``.  Falls back to the first
        ``.xml`` that is not ``primary_doc`` if no preferred name is found.

        Returns the raw XML string, or ``""`` when no matching document can
        be located (never raises).
        """
        cik_s = _sanitize_cik(cik)
        accession_s = _sanitize_accession(accession)
        base = self._archives_base(cik_s, accession_s)
        index_html = self._get(f"{base}/{accession_s}-index.htm")
        filename = _extract_form13f_table_filename(index_html)
        if filename is None:
            log.debug(
                "edgar.form13f.no_infotable_found",
                accession=accession,
                cik=cik,
            )
            return ""
        safe_doc = _sanitize_primary_doc(filename)
        return self._get(f"{base}/{safe_doc}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _archives_base(self, cik: str, accession: str) -> str:
        """Build the EDGAR Archives base URL for a filing.

        ``cik`` and ``accession`` must already be sanitized (digits/dashes
        only) before calling this method — they are embedded directly into
        the URL.

        The Archives path uses the *un-padded* integer CIK (e.g. 320193);
        the zero-padded form (0000320193) 301-redirects.  ``int()`` is safe
        because ``_sanitize_cik`` guarantees digits-only.
        """
        cik_path = str(int(cik))
        accession_nodashes = accession.replace("-", "")
        return f"{self._base_url}/Archives/edgar/data/{cik_path}/{accession_nodashes}"

    def _fetch_primary_doc(
        self,
        accession: str,
        cik: str,
        primary_document: str | None,
        *,
        suffixes: tuple[str, ...],
    ) -> str:
        """Fetch a filing's primary document.

        When ``primary_document`` is provided and ends in one of ``suffixes``,
        build the document URL directly (one GET).  Otherwise scrape the index
        page for the first matching document (two GETs — fallback path).

        All identifiers are sanitized (digits/dash/segment-only) before they
        reach a URL — they may have come from an untrusted response body.
        """
        cik = _sanitize_cik(cik)
        accession = _sanitize_accession(accession)
        base = self._archives_base(cik, accession)
        if primary_document and primary_document.lower().endswith(suffixes):
            safe_doc = _strip_xsl_prefix(_sanitize_primary_doc(primary_document))
            return self._get(f"{base}/{safe_doc}")

        # Fallback: scrape the index page for the primary document filename.
        index_html = self._get(f"{base}/{accession}-index.htm")
        filename = _sanitize_primary_doc(
            _extract_doc_filename(index_html, accession, suffixes)
        )
        return self._get(f"{base}/{filename}")

    def _get(self, url: str) -> str:
        """GET ``url`` with rate-limiting and retry logic."""
        self._rate_limit()
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._http.get(url)
                if resp.status_code == 200:
                    return resp.text
                if resp.status_code in {429, 503}:
                    wait = _BACKOFF_BASE ** (attempt + 1)
                    self._sleep(wait)
                    continue
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                last_exc = exc
                self._sleep(_BACKOFF_BASE ** (attempt + 1))
        raise EdgarError(
            f"Failed to GET {url!r} after {_MAX_RETRIES} attempts"
        ) from last_exc

    def _rate_limit(self) -> None:
        """Sleep if we're calling faster than _MIN_INTERVAL_SEC."""
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < _MIN_INTERVAL_SEC:
            self._sleep(_MIN_INTERVAL_SEC - elapsed)
        self._last_request_ts = time.monotonic()

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._http.close()

    def __enter__(self) -> EdgarClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Private parsing helpers (no network)
# ---------------------------------------------------------------------------

def _extract_doc_filename(
    index_html: str, accession: str, suffixes: tuple[str, ...]
) -> str:
    """Extract the primary document filename from the filing index page.

    Scans for the first link ending in any of ``suffixes`` (preference order
    follows ``suffixes``).  Falls back to ``<accession>.<first-suffix>`` when
    heuristics fail.
    """
    import re  # local import to keep module-level clean

    for suffix in suffixes:
        match = re.search(
            rf'href="[^"]*?/([^/"]+{re.escape(suffix)})"',
            index_html,
            re.IGNORECASE,
        )
        if match:
            return match.group(1)
    # Fallback: the primary document often shares the accession name.
    return f"{accession}{suffixes[0]}"


def _extract_form13f_table_filename(index_html: str) -> str | None:
    """Extract the information-table XML filename from a 13F filing index page.

    A 13F-HR filing contains at least two XML documents: the cover-page XML
    (``primaryDocument``) and the separate information-table XML that holds the
    actual fund holdings.  This function finds the information-table document
    by inspecting ``href`` attributes ending in ``.xml``.

    Preference order:
    1. Any ``.xml`` whose basename contains ``infotable``, ``form13f``, or
       ``13f`` (case-insensitive), excluding ``primary_doc``.
    2. The first ``.xml`` that is not named ``primary_doc`` (any case).

    Returns ``None`` when no suitable XML can be found (never raises).
    """
    # Collect all .xml hrefs from the index page.
    xml_matches = re.findall(
        r'href="[^"]*?/([^/"]+\.xml)"',
        index_html,
        re.IGNORECASE,
    )
    if not xml_matches:
        return None

    _preferred_keywords = re.compile(
        r"(infotable|form13f|13f)", re.IGNORECASE
    )
    _primary_doc = re.compile(r"primary.?doc", re.IGNORECASE)

    # Pass 1: preferred — contains a 13F-table keyword and is not primary_doc.
    for name in xml_matches:
        if not _primary_doc.search(name) and _preferred_keywords.search(name):
            return name

    # Pass 2: fallback — any .xml that is not primary_doc.
    for name in xml_matches:
        if not _primary_doc.search(name):
            return name

    return None


def _parse_company_tickers(raw: str) -> dict[str, str]:
    """Parse ``company_tickers.json`` into ``{TICKER -> cik10}``.

    The file is a JSON object of integer-string keys to
    ``{"cik_str": int, "ticker": str, "title": str}`` records.  CIK is
    zero-padded to 10 digits (the form the submissions URL + Form-4 fetch
    expect).  Tolerant of a malformed body (returns ``{}``).
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if isinstance(data, dict):
        records = data.values()
    elif isinstance(data, list):
        records = data
    else:
        # Hostile/empty body: a JSON scalar (null, int, str, bool) is neither
        # a map nor a list — bail cleanly rather than iterating a non-iterable.
        return {}
    out: dict[str, str] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        ticker = str(rec.get("ticker", "")).upper().strip()
        cik_str = rec.get("cik_str")
        if not ticker or cik_str is None:
            continue
        # CIK must be numeric; a non-numeric value is a corrupt record and
        # would otherwise produce a garbage CIK that flows into a URL.
        try:
            cik10 = _sanitize_cik(str(cik_str))
        except EdgarError:
            continue
        out[ticker] = cik10
    return out


def _parse_submissions_json(
    body: str,
    cik: str,
    *,
    form_types: frozenset[str] | set[str],
    count: int = 20,
    keep_form: bool = False,
) -> list[dict]:
    """Parse a ``data.sec.gov`` submissions doc into filing descriptors.

    The ``filings.recent`` object holds **parallel arrays** (``form``,
    ``accessionNumber``, ``filingDate``, ``primaryDocument``).  We zip them,
    keep rows whose ``form`` is in ``form_types``, newest-first (EDGAR already
    orders ``recent`` newest-first), and take ``count``.

    Returns dicts with ``cik``, ``accession``, ``filed_at``, ``report_date``,
    ``primary_document`` (and ``form`` when ``keep_form``).  ``report_date``
    is always present; it is ``""`` when the ``reportDate`` array is absent or
    the entry is empty (e.g. Form-4 rows).  Tolerant of a malformed body /
    missing arrays (returns ``[]``).
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    # Hostile/empty body: anything that is not a JSON object can't carry the
    # expected `filings.recent` shape — bail cleanly.
    if not isinstance(data, dict):
        return []

    filings = data.get("filings")
    filings = filings if isinstance(filings, dict) else {}
    recent = filings.get("recent")
    recent = recent if isinstance(recent, dict) else {}
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    primaries = recent.get("primaryDocument") or []
    # reportDate is optional in submissions JSON (absent for many form types).
    # We treat a missing/null array the same as an array of empty strings so
    # existing Form-4 and SC13 callers require no changes — they get
    # report_date="" and ignore it.
    report_dates_raw = recent.get("reportDate") or []
    if not isinstance(report_dates_raw, list):
        report_dates_raw = []
    if not (
        isinstance(forms, list)
        and isinstance(accessions, list)
        and isinstance(dates, list)
        and isinstance(primaries, list)
    ):
        return []

    out: list[dict] = []
    n = min(len(forms), len(accessions), len(dates), len(primaries))
    for i in range(n):
        if forms[i] not in form_types:
            continue
        report_date = report_dates_raw[i] if i < len(report_dates_raw) else ""
        row = {
            "cik": cik,
            "accession": accessions[i],
            "filed_at": dates[i],
            "report_date": report_date if isinstance(report_date, str) else "",
            "primary_document": primaries[i],
        }
        if keep_form:
            row["form"] = forms[i]
        out.append(row)
        if len(out) >= count:
            break
    return out
