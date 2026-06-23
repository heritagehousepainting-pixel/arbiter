"""Senate eFD ingestion — Layer S (HTTP + HTML parsing, returns list[Transaction]).

Flow:
  1. GET /search/home/ → capture csrftoken cookie + csrfmiddlewaretoken form field.
  2. POST /search/home/ with prohibition_agreement=1 → sets sessionid cookie.
  3. POST /search/report/data/ with DataTables payload → JSON list of PTR links.
  4. Filter: electronic (/ptr/) only; skip paper (/paper/).
  5. GET /search/view/ptr/{uuid}/ → parse HTML table → list[Transaction].

Public API:
  fetch_senate_ptrs(year, http_client=None) -> list[Transaction]
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date
from html.parser import HTMLParser

import httpx

from arbiter.ingest.congress.ptr_pdf import Transaction

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SENATE_BASE_URL = "https://efdsearch.senate.gov"
SENATE_HOME_URL = f"{SENATE_BASE_URL}/search/home/"
SENATE_SEARCH_URL = f"{SENATE_BASE_URL}/search/report/data/"
SENATE_PTR_URL = f"{SENATE_BASE_URL}/search/view/ptr/{{uuid}}/"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

_OWNER_MAP: dict[str, str] = {
    "self": "SELF",
    "joint": "JT",
    "spouse": "SP",
    "child": "DC",
    "dependent child": "DC",
}

_ASSET_TYPE_MAP: dict[str, str] = {
    "stock": "ST",
    "other securities": "OT",
    "municipal security": "MS",
    "corporate bond": "CS",
    "government security": "GS",
    "hedge fund": "HN",
    "real property": "RP",
}

# Regex for extracting CSRF token from hidden form field
_CSRF_FORM_RE = re.compile(
    r'<input[^>]+name=["\']csrfmiddlewaretoken["\'][^>]+value=["\']([^"\']+)["\']'
    r'|'
    r'<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']csrfmiddlewaretoken["\']',
    re.IGNORECASE,
)

# Regex for extracting UUID and type (ptr/paper) from report link HTML
_LINK_RE = re.compile(r'/search/view/(ptr|paper)/([a-f0-9-]{36})/', re.IGNORECASE)

# Regex for parsing amount range "$1,001 - $15,000"
_AMOUNT_RE = re.compile(r'\$\s*([\d,]+)\s*-\s*\$\s*([\d,]+)')

# Regex for extracting notification date from h1 "for MM/DD/YYYY"
_H1_DATE_RE = re.compile(r'for\s+(\d{2}/\d{2}/\d{4})', re.IGNORECASE)

# Regex for extracting ticker from <a> tag
_TICKER_A_RE = re.compile(r'<a\b[^>]*>([^<]+)</a>', re.IGNORECASE)

# A real equity ticker: 1-5 uppercase letters, optionally one dot/dash + 1-2 more
# letters (e.g. BRK.B, BF-B). Anything else (e.g. "N/A", "--", asset-name
# fragments, CUSIPs) is NOT a tradeable symbol and must be dropped so we never
# fabricate a signal from a non-ticker cell.
_VALID_TICKER_RE = re.compile(r'^[A-Z]{1,5}([.\-][A-Z]{1,2})?$')


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SenateEFDUnavailable(Exception):
    """Raised when the Senate eFD site cannot be reached or rejects our session."""


# ---------------------------------------------------------------------------
# HTML table parser (lightweight, avoids BS4 dependency)
# ---------------------------------------------------------------------------

class _TableRowParser(HTMLParser):
    """Collect <tr>/<td> cell text and raw inner-HTML from the PTR table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_tbody = False
        self._in_tr = False
        self._in_td = False
        self._td_depth = 0
        self._current_row_cells: list[str] = []
        self._current_cell_text = ""
        self._current_cell_html = ""
        self.rows: list[list[str]] = []           # text per cell per row
        self.rows_html: list[list[str]] = []      # raw inner HTML per cell per row (for ticker)
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag == "tbody":
            self._in_tbody = True
        elif tag == "tr" and self._in_tbody:
            self._in_tr = True
            self._current_row_cells = []
            self._current_row_html_cells: list[str] = []
        elif tag == "td" and self._in_tr:
            self._in_td = True
            self._td_depth = 0
            self._current_cell_text = ""
            self._current_cell_html = ""
        elif self._in_td:
            # rebuild inner HTML for this tag
            attr_str = ""
            for name, val in attrs:
                if val is not None:
                    attr_str += f' {name}="{val}"'
                else:
                    attr_str += f' {name}'
            self._current_cell_html += f"<{tag}{attr_str}>"
            self._td_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "tbody":
            self._in_tbody = False
        elif tag == "tr" and self._in_tr:
            self._in_tr = False
            if self._current_row_cells:
                self.rows.append(self._current_row_cells)
                self.rows_html.append(self._current_row_html_cells)
        elif tag == "td" and self._in_td:
            self._in_td = False
            self._current_row_cells.append(self._current_cell_text.strip())
            self._current_row_html_cells.append(self._current_cell_html)
        elif self._in_td and self._td_depth > 0:
            self._current_cell_html += f"</{tag}>"
            self._td_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_td:
            self._current_cell_text += data
            self._current_cell_html += data


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _do_agreement_flow(client: httpx.Client) -> str:
    """Steps 1-2: GET home → POST agreement → return current csrf cookie value.

    Returns the csrftoken cookie value (used as X-CSRFToken on subsequent requests).
    Raises SenateEFDUnavailable if either step fails.
    """
    # Step 1: GET home page to obtain CSRF tokens
    try:
        resp = client.get(
            SENATE_HOME_URL,
            headers={
                "User-Agent": BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
    except httpx.RequestError as exc:
        raise SenateEFDUnavailable(f"Senate eFD GET home failed: {exc}") from exc

    if resp.status_code != 200:
        raise SenateEFDUnavailable(
            f"Senate eFD GET home returned {resp.status_code}"
        )

    home_html = resp.text

    # Extract csrfmiddlewaretoken from the form body
    m = _CSRF_FORM_RE.search(home_html)
    if not m:
        raise SenateEFDUnavailable(
            "Could not find csrfmiddlewaretoken in Senate eFD home page"
        )
    form_csrf = m.group(1) or m.group(2)

    # Get csrftoken cookie
    csrf_cookie = client.cookies.get("csrftoken", domain="efdsearch.senate.gov")
    if not csrf_cookie:
        # Try without domain restriction
        csrf_cookie = dict(client.cookies).get("csrftoken", "")
    if not csrf_cookie:
        raise SenateEFDUnavailable(
            "No csrftoken cookie set after Senate eFD GET home"
        )

    # Step 2: POST agreement
    try:
        resp2 = client.post(
            SENATE_HOME_URL,
            data={
                "prohibition_agreement": "1",
                "csrfmiddlewaretoken": form_csrf,
            },
            headers={
                "User-Agent": BROWSER_UA,
                "Referer": SENATE_HOME_URL,
                "X-CSRFToken": csrf_cookie,
                "Origin": SENATE_BASE_URL,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    except httpx.RequestError as exc:
        raise SenateEFDUnavailable(f"Senate eFD POST agreement failed: {exc}") from exc

    if resp2.status_code not in (200, 302):
        raise SenateEFDUnavailable(
            f"Senate eFD POST agreement returned {resp2.status_code}"
        )

    # Refresh csrf cookie (may have rotated)
    csrf_cookie_new = client.cookies.get("csrftoken", domain="efdsearch.senate.gov")
    if not csrf_cookie_new:
        csrf_cookie_new = dict(client.cookies).get("csrftoken", csrf_cookie)

    log.debug("senate: agreement flow complete, sessionid cookie set")
    return csrf_cookie_new or csrf_cookie


def _search_ptrs(client: httpx.Client, csrf: str, year: int) -> list[dict]:
    """Step 3: paginated POST to /search/report/data/ — returns list of raw row dicts.

    Each dict has keys: first_name, last_name, uuid, is_paper, is_amendment, date_filed.
    """
    page_size = 100
    start = 0
    all_rows: list[dict] = []

    while True:
        payload = {
            "draw": "1",
            "start": str(start),
            "length": str(page_size),
            "search[value]": "",
            "search[regex]": "false",
            "order[0][column]": "4",
            "order[0][dir]": "desc",
            "report_types": "[11]",
            "filer_types": "[1]",
            "submitted_start_date": f"01/01/{year} 00:00:00",
            "submitted_end_date": f"12/31/{year} 23:59:59",
            "candidate_state": "",
            "senator_state": "",
            "office_id": "",
            "first_name": "",
            "last_name": "",
        }

        try:
            resp = client.post(
                SENATE_SEARCH_URL,
                data=payload,
                headers={
                    "User-Agent": BROWSER_UA,
                    "X-CSRFToken": csrf,
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Referer": f"{SENATE_BASE_URL}/search/",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
            )
        except httpx.RequestError as exc:
            raise SenateEFDUnavailable(
                f"Senate eFD POST search failed: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise SenateEFDUnavailable(
                f"Senate eFD search returned {resp.status_code}"
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise SenateEFDUnavailable(
                f"Senate eFD search response is not JSON: {exc}"
            ) from exc

        records_total = data.get("recordsTotal", 0)
        rows = data.get("data", [])

        for row in rows:
            if len(row) < 5:
                log.debug("senate: skipping malformed search row: %r", row)
                continue
            first_name = str(row[0]).strip()
            last_name = str(row[1]).strip()
            link_html = str(row[3])
            date_filed_str = str(row[4]).strip()

            m = _LINK_RE.search(link_html)
            if not m:
                log.debug("senate: skipping row with unrecognised link: %r", link_html)
                continue

            link_type = m.group(1).lower()  # "ptr" or "paper"
            uuid = m.group(2)
            is_paper = link_type == "paper"
            is_amendment = bool(re.search(r"Amendment", link_html, re.IGNORECASE))

            all_rows.append(
                {
                    "first_name": first_name,
                    "last_name": last_name,
                    "uuid": uuid,
                    "is_paper": is_paper,
                    "is_amendment": is_amendment,
                    "date_filed": date_filed_str,
                }
            )

        if start + page_size >= records_total:
            break
        start += page_size

    return all_rows


def _parse_filer_name(h2_text: str) -> str:
    """Strip honorific titles and parenthetical from h2 filer text.

    e.g. "The Honorable John\\n                Boozman\\n                \\n                (Boozman, John)"
    → "John Boozman"
    """
    # Remove parenthetical (Last, First) suffix
    text = re.sub(r'\([^)]+\)', '', h2_text)
    # Remove honorific prefixes
    for prefix in ("The Honorable ", "Hon. ", "Mr. ", "Ms. ", "Dr. ", " Jr.", " Sr.", " II", " III"):
        text = text.replace(prefix, " ")
    # Collapse whitespace
    return " ".join(text.split())


def _parse_notification_date(h1_text: str) -> date:
    """Extract filing date from h1 text '...for MM/DD/YYYY'."""
    m = _H1_DATE_RE.search(h1_text)
    if not m:
        raise ValueError(f"Cannot find date in h1 text: {h1_text!r}")
    return _parse_date(m.group(1))


def _parse_date(date_str: str) -> date:
    """Parse MM/DD/YYYY → date."""
    parts = date_str.strip().split("/")
    if len(parts) != 3:
        raise ValueError(f"Unrecognised date format: {date_str!r}")
    return date(int(parts[2]), int(parts[0]), int(parts[1]))


def _parse_owner(cell: str) -> str:
    """Map owner cell text to owner code."""
    key = cell.strip().lower()
    return _OWNER_MAP.get(key, "SELF")


def _parse_asset_type(cell: str) -> str | None:
    """Map asset type cell text to two-letter code.

    Returns None for unknown types (treated as OT downstream).
    """
    key = cell.strip().lower()
    return _ASSET_TYPE_MAP.get(key, "OT")


def _parse_ticker(cell_text: str, cell_html: str) -> str | None:
    """Extract a VALID equity ticker from the cell, else None.

    Checks the inner HTML for an <a> tag first; falls back to plain text.
    Returns None if the cell is empty, '--', or does not match the equity
    ticker shape (``_VALID_TICKER_RE``).  This drops non-tickers such as
    ``N/A``, ``None``, and asset-name fragments so they never survive as a
    fabricated trading signal downstream.
    """
    # Try to extract from <a> tag in HTML
    m = _TICKER_A_RE.search(cell_html)
    if m:
        ticker = m.group(1).strip()
    else:
        ticker = cell_text.strip()

    if not ticker or ticker == "--":
        return None

    # Normalise to upper-case and validate against the equity ticker shape.
    candidate = ticker.upper()
    if not _VALID_TICKER_RE.match(candidate):
        log.debug("senate: dropping non-ticker cell value %r", ticker)
        return None
    return candidate


def _parse_amount(cell: str) -> tuple[float, float]:
    """Parse '$1,001 - $15,000' → (1001.0, 15000.0)."""
    m = _AMOUNT_RE.search(cell)
    if not m:
        raise ValueError(f"Cannot parse amount: {cell!r}")
    low = float(m.group(1).replace(",", ""))
    high = float(m.group(2).replace(",", ""))
    return low, high


def _parse_txn_type(cell: str) -> tuple[str, bool]:
    """Parse transaction type cell → (txn_type_code, is_partial).

    Returns ``("", False)`` for an UNKNOWN/ambiguous type. We must NOT default
    an unrecognised type to ``"S"`` (sale): a mislabelled purchase or an
    ambiguous row silently booked as a sale is a fabricated directional signal.
    The caller treats an empty code as ambiguous and skips the row.
    """
    text = cell.strip()
    if text == "Purchase":
        return "P", False
    if text in ("Sale (Full)", "Sale"):
        return "S", False
    if text == "Sale (Partial)":
        return "S", True
    if text == "Exchange":
        return "E", False
    # Unknown / ambiguous — do NOT guess a direction.
    log.warning("senate: unrecognised txn_type %r — marking ambiguous (row skipped)", text)
    return "", False


_REDIRECT_PAGE_RE = re.compile(
    r'<title>\s*eFD:\s*(Home|Find\s+Reports)\s*</title>', re.IGNORECASE
)
_PTR_H1_RE = re.compile(
    r'<h1[^>]*>\s*Periodic\s+Transaction\s+Report', re.IGNORECASE
)


def _looks_like_redirect_page(html: str) -> bool:
    """Return True if *html* is the agreement/home page rather than a PTR page.

    Detects the session-expiry redirect: the site returns 200 with the
    "eFD: Home" or "eFD: Find Reports" title page instead of the PTR content.
    """
    if _REDIRECT_PAGE_RE.search(html):
        return True
    # Also catch pages that lack the PTR <h1> entirely (belt-and-suspenders).
    if not _PTR_H1_RE.search(html):
        h1_m = re.search(r'<h1[^>]*>\s*(.*?)\s*</h1>', html, re.DOTALL | re.IGNORECASE)
        if not h1_m:
            return True
    return False


def _parse_ptr_page(
    html: str,
    uuid: str,
    *,
    is_amendment: bool = False,
) -> list[Transaction]:
    """Parse a PTR HTML page → list[Transaction].

    Pure function — takes the HTML string and the report UUID.
    Extracts filer metadata from the page header and transactions from the table.
    Bad rows are logged and skipped; never raises.

    Parameters
    ----------
    html:
        Full HTML of the PTR page.
    uuid:
        The PTR report UUID (used as doc_id on every Transaction and in log messages).
    is_amendment:
        When True, every Transaction produced by this report will have
        ``is_amendment=True``.  Threaded in from the search-result row flag.
    """
    # --- Extract notification_date from <h1> --------------------------------
    h1_m = re.search(
        r'<h1[^>]*>\s*(.*?)\s*</h1>', html, re.DOTALL | re.IGNORECASE
    )
    if not h1_m:
        log.warning("senate: no <h1> found in PTR page uuid=%s", uuid)
        return []

    try:
        notification_date = _parse_notification_date(h1_m.group(1))
    except ValueError as exc:
        log.warning("senate: cannot parse notification_date uuid=%s: %s", uuid, exc)
        return []

    # --- Extract member_name from <h2 class="filedReport"> ------------------
    h2_m = re.search(
        r'<h2[^>]*class=["\'][^"\']*filedReport[^"\']*["\'][^>]*>\s*(.*?)\s*</h2>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if not h2_m:
        log.warning("senate: no <h2 class='filedReport'> found in PTR page uuid=%s", uuid)
        return []

    member_name = _parse_filer_name(h2_m.group(1))
    if not member_name:
        log.warning("senate: empty member_name after stripping uuid=%s", uuid)
        return []

    # --- Parse transaction table -------------------------------------------
    parser = _TableRowParser()
    parser.feed(html)

    transactions: list[Transaction] = []

    for i, (cells, cells_html) in enumerate(zip(parser.rows, parser.rows_html)):
        if len(cells) < 9:
            log.debug(
                "senate: skipping short row (got %d cells, need 9) uuid=%s row_idx=%d",
                len(cells),
                uuid,
                i,
            )
            continue

        try:
            # col 1: txn_date
            txn_date = _parse_date(cells[1])

            # col 2: owner
            owner = _parse_owner(cells[2])

            # col 3: ticker (uses inner HTML for <a> tag)
            ticker = _parse_ticker(cells[3], cells_html[3] if cells_html else "")

            # col 4: asset_name
            asset_name = cells[4].strip()

            # col 5: asset_type
            asset_type = _parse_asset_type(cells[5])

            # col 6: txn_type
            txn_type, is_partial = _parse_txn_type(cells[6])
            if not txn_type:
                # Ambiguous / unrecognised type — skip rather than mis-book it
                # as a sale (fabricated directional signal).
                log.warning(
                    "senate: skipping ambiguous txn_type row uuid=%s row_idx=%d (cell=%r)",
                    uuid,
                    i,
                    cells[6],
                )
                continue

            # col 7: amount
            amount_low, amount_high = _parse_amount(cells[7])

            txn = Transaction(
                doc_id=uuid,
                chamber="senate",
                member_name=member_name,
                owner=owner,
                asset_name=asset_name,
                ticker=ticker,
                asset_type=asset_type,
                txn_type=txn_type,
                is_partial=is_partial,
                txn_date=txn_date,
                notification_date=notification_date,
                amount_low=amount_low,
                amount_high=amount_high,
                is_amendment=is_amendment,
            )
            transactions.append(txn)

        except Exception as exc:
            log.warning(
                "senate: skipping row %d in uuid=%s: %s",
                i,
                uuid,
                exc,
            )
            continue

    return transactions


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_senate_ptrs(
    year: int,
    http_client: httpx.Client | None = None,
) -> list[Transaction]:
    """Fetch all Senate PTR transactions for a given year.

    Orchestrates the full flow:
      1. Agreement / CSRF (steps 1-2)
      2. Paginated search (step 3)
      3. Filter electronic PTRs (step 4)
      4. Fetch and parse each PTR HTML page (step 5)

    Parameters
    ----------
    year:
        4-digit filing year (e.g. 2026).
    http_client:
        Optional pre-configured ``httpx.Client``.  When omitted, a real client
        with a browser-like User-Agent and cookie jar is created.

    Returns
    -------
    Flat list of ``Transaction`` objects from all successfully parsed PTRs.
    Paper PTRs are skipped.  Bad reports log a WARNING and continue.

    Raises
    ------
    SenateEFDUnavailable
        If the agreement flow or initial search request fails.
    """
    own_client = http_client is None
    if own_client:
        client = httpx.Client(
            timeout=30.0,
            headers={"User-Agent": BROWSER_UA},
            follow_redirects=True,
        )
    else:
        client = http_client

    try:
        # Step 1-2: agreement flow
        csrf = _do_agreement_flow(client)

        # Step 3: paginated search
        rows = _search_ptrs(client, csrf, year)

        log.info(
            "senate: search complete — %d total rows for year=%s", len(rows), year
        )

        all_transactions: list[Transaction] = []

        for row in rows:
            uuid = row["uuid"]
            first = row["first_name"]
            last = row["last_name"]
            date_filed = row["date_filed"]

            if row["is_paper"]:
                log.debug(
                    "senate: skipping paper PTR %s for %s %s (filed %s)",
                    uuid,
                    first,
                    last,
                    date_filed,
                )
                continue

            is_amendment = row["is_amendment"]

            # Step 5: fetch PTR HTML page (with one session-expiry retry)
            ptr_url = SENATE_PTR_URL.format(uuid=uuid)
            try:
                resp = client.get(
                    ptr_url,
                    headers={
                        "User-Agent": BROWSER_UA,
                        "Referer": f"{SENATE_BASE_URL}/search/",
                        "X-CSRFToken": csrf,
                    },
                )
            except httpx.RequestError as exc:
                log.warning(
                    "senate: HTTP error fetching PTR uuid=%s for %s %s: %s",
                    uuid,
                    first,
                    last,
                    exc,
                )
                continue

            if resp.status_code != 200:
                log.warning(
                    "senate: PTR page returned %d for uuid=%s (%s %s)",
                    resp.status_code,
                    uuid,
                    first,
                    last,
                )
                continue

            # Fix 2: detect session-expiry redirect (200 but wrong page content).
            # The site returns the agreement/home page when the session has expired.
            # Re-run the agreement flow ONCE and retry the page fetch.
            if _looks_like_redirect_page(resp.text):
                log.warning(
                    "senate: session expired while fetching uuid=%s — re-authenticating",
                    uuid,
                )
                try:
                    csrf = _do_agreement_flow(client)
                    resp = client.get(
                        ptr_url,
                        headers={
                            "User-Agent": BROWSER_UA,
                            "Referer": f"{SENATE_BASE_URL}/search/",
                            "X-CSRFToken": csrf,
                        },
                    )
                except (httpx.RequestError, SenateEFDUnavailable) as exc:
                    log.warning(
                        "senate: re-auth failed for uuid=%s (%s %s): %s — skipping",
                        uuid,
                        first,
                        last,
                        exc,
                    )
                    continue

                if resp.status_code != 200 or _looks_like_redirect_page(resp.text):
                    log.warning(
                        "senate: PTR page uuid=%s still redirecting after re-auth "
                        "(%s %s) — skipping",
                        uuid,
                        first,
                        last,
                    )
                    continue

            # Parse transactions from page
            try:
                txns = _parse_ptr_page(resp.text, uuid, is_amendment=is_amendment)
            except Exception as exc:
                log.warning(
                    "senate: failed to parse PTR page uuid=%s for %s %s: %s",
                    uuid,
                    first,
                    last,
                    exc,
                )
                continue

            log.debug(
                "senate: uuid=%s (%s %s) → %d transaction(s)",
                uuid,
                first,
                last,
                len(txns),
            )
            all_transactions.extend(txns)

            # Polite delay between PTR page fetches
            time.sleep(0.5)

        log.info(
            "senate: year=%s complete — %d total Transaction(s)",
            year,
            len(all_transactions),
        )
        return all_transactions

    finally:
        if own_client:
            client.close()
