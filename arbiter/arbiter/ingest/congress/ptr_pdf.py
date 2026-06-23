"""L3 – PTR PDF extraction and parsing.

extract_ptr_text : pdf_bytes -> PtrText   (pdfplumber)
parse_ptr        : PtrText  -> list[Transaction]
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from datetime import date

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Frozen contracts (per REBUILD_PLAN.md)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PtrText:
    raw_text: str
    is_electronic: bool
    doc_id: str = ""
    chamber: str = "house"
    year: int = 0


@dataclass(frozen=True)
class Transaction:
    doc_id: str
    chamber: str
    member_name: str       # "Mark Alford" (strip "Hon. ")
    owner: str             # "SP" | "DC" | "JT" | "SELF"
    asset_name: str
    ticker: str | None     # None if no ticker found
    asset_type: str | None # "ST", "OP", "OT", etc.
    txn_type: str          # "P" | "S" | "E"
    is_partial: bool
    txn_date: date
    notification_date: date
    amount_low: float
    amount_high: float
    is_amendment: bool = False  # Senate: True when the PTR is an amendment filing
    # Clerk receipt / public-availability date (House index FilingDate). This is
    # the date the disclosure became PUBLICLY available and is the correct
    # "information available" timestamp (INTERFACES.md §3 line 106: "Congress =
    # disclosure date"). The PDF's per-row notification_date is when the MEMBER
    # was notified and can PRECEDE public availability -> using it for filing_ts
    # is look-ahead. When None (e.g. Senate, where the report <h1> date already
    # IS the public date) filing_ts falls back to notification_date.
    clerk_receipt_date: date | None = None


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_ptr_text(
    pdf_bytes: bytes,
    *,
    doc_id: str = "",
    chamber: str = "house",
    year: int = 0,
) -> PtrText:
    """Extract raw text from a PTR PDF via pdfplumber.

    If the PDF is scanned / image-only (little or no extractable text),
    returns PtrText(raw_text="", is_electronic=False).  Never raises.
    """
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        log.error("pdfplumber not installed – cannot extract PTR text")
        return PtrText(raw_text="", is_electronic=False, doc_id=doc_id, chamber=chamber, year=year)

    try:
        pages: list[str] = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                txt = page.extract_text() or ""
                pages.append(txt)
        raw = "\n".join(pages)
        # Strip null bytes that pdfplumber can produce from some electronic PDFs.
        # These corrupt skip-line detection and amount-range regex matching.
        raw = raw.replace("\x00", "")
    except Exception as exc:
        log.warning("pdfplumber failed for doc_id=%s: %s", doc_id, exc)
        return PtrText(raw_text="", is_electronic=False, doc_id=doc_id, chamber=chamber, year=year)

    # Heuristic: if we got meaningful text (>100 chars) it's electronic
    is_electronic = len(raw.strip()) > 100
    if not is_electronic:
        raw = ""
    return PtrText(raw_text=raw, is_electronic=is_electronic, doc_id=doc_id, chamber=chamber, year=year)


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

def _clean_text(text: str) -> str:
    """Strip NUL bytes and other PDF artefacts; collapse runs of spaces."""
    # Remove NUL / zero-width characters embedded by PDF character spacing
    text = text.replace("\x00", "")
    # Collapse multiple spaces (but preserve newlines)
    text = re.sub(r"[ \t]+", " ", text)
    return text


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# Lines to skip outright (page headers + sub-line prefixes)
# NOTE: patterns are matched against lines that have already had NUL bytes
# stripped, so "P        T           R" becomes "P T R" etc.
_SKIP_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^ID\s+Owner\s+Asset\s+Transaction\s+Date", re.I),
    re.compile(r"^Type\s+Date\s+Gains\s*>", re.I),
    re.compile(r"^\$200\??$"),
    re.compile(r"^F\s+S\s*:"),
    re.compile(r"^S\s+O\s*:"),
    re.compile(r"^D\s*:"),
    re.compile(r"^L\s*:"),
    re.compile(r"^Filing ID"),
    re.compile(r"^P\s+T\s+R"),            # "P T R" title header
    re.compile(r"^Clerk of the House"),
    re.compile(r"^F\s+I\b"),              # "F I" filer info header
    re.compile(r"^Status:"),
    re.compile(r"^State/District:"),
    re.compile(r"^T\s*$"),                # lone "T" table separator
    re.compile(r"^\*\s+For the complete"),
    re.compile(r"^I\s+V\s+D"),            # investment vehicle description header
    re.compile(r"^I\s+P\s+O"),            # investment performance options header
    re.compile(r"^Yes\s+No$"),
    re.compile(r"^C\s+S\b"),              # certification section header
    re.compile(r"^I CERTIFY"),
    re.compile(r"^Digitally Signed"),
    # Investment vehicle descriptions
    re.compile(r"^R\.W\.\s+Allen"),
    re.compile(r"^Putnam Investments"),
    # Location sub-lines: "L : US" or "L : Augusta/ Richmond, GA"
    re.compile(r"^L\s*:"),
]

# Date: two consecutive MM/DD/YYYY
_TWO_DATES = re.compile(r"(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})")

# Dollar amount: $N,NNN  (the comma-separated number after $)
_DOLLAR_PAT = re.compile(r"\$([\d,]+)")

# Contiguous amount pair: $N,NNN - $N,NNN
_AMOUNT_BOTH = re.compile(r"\$\s*([\d,]+)\s*-\s*\$\s*([\d,]+)")

# Owner prefix
_OWNER_PREFIX = re.compile(r"^(SP|DC|JT)\s+(.+)$", re.S)

# Ticker: last (TICKER) group immediately before a [TAG]
# The \)*  allows for double-closing-paren PDF artifacts: (91282CJR3)) [GS]
_TICKER_TAG_PAT = re.compile(r"\(([^)]+)\)\)*\s*\[([A-Z]{2,3})\]")

# Secondary ticker extraction patterns (used only when parenthesised pattern finds nothing)
# (a) Exchange-prefixed: "NYSEARCA: DIA", "NYSE: XOM", "NASDAQ: AAPL", "BATS: SCHB"
_EXCHANGE_TICKER_PAT = re.compile(
    r"\b(?:NYSEARCA|NYSE|NASDAQ|BATS)\s*:\s*([A-Z][A-Z0-9.]{0,8})\b"
)
# (b) Bare 2-5 uppercase letters immediately before a [TAG] — conservative, no common words
_BARE_TICKER_BEFORE_TAG = re.compile(r"\b([A-Z]{2,5})\s*\[([A-Z]{2,3})\]")


def _parse_amount(s: str) -> float:
    return float(s.replace(",", ""))


def _parse_date(s: str) -> date:
    m, d, y = s.split("/")
    return date(int(y), int(m), int(d))


def _is_skip(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    for pat in _SKIP_PATTERNS:
        if pat.search(stripped):
            return True
    return False


# ---------------------------------------------------------------------------
# Core parser
# ---------------------------------------------------------------------------

def parse_ptr(ptr: PtrText) -> list[Transaction]:
    """Parse a PtrText into a list of Transaction objects.

    Skips rows it cannot parse; logs a warning for each skip.
    Returns [] for empty / scanned PTRs.
    """
    if not ptr.raw_text.strip():
        return []

    # Normalise: strip NUL bytes and extra spaces
    text = _clean_text(ptr.raw_text)

    member_name = _extract_member_name(text)
    chunks = _split_into_chunks(text)
    transactions: list[Transaction] = []

    for chunk in chunks:
        try:
            txn = _parse_chunk(chunk, ptr.doc_id, ptr.chamber, member_name)
            if txn is not None:
                transactions.append(txn)
        except Exception as exc:
            log.warning(
                "Skipping unparseable chunk (doc_id=%s): %s | chunk=%r",
                ptr.doc_id, exc, chunk,
            )

    return transactions


def _extract_member_name(text: str) -> str:
    m = re.search(r"Name:\s*Hon\.\s*(.+)", text)
    if m:
        return m.group(1).strip()
    return ""


# ---------------------------------------------------------------------------
# Chunking strategy
#
# Observed wrapping patterns in the real fixtures:
#
# Pattern A (amount splits, ticker on continuation):
#   "SP Ferguson Enterprises Inc. Common P 12/12/2025 01/06/2026 $15,001 -"
#   "Stock (FERG) [ST] $50,000"
#
# Pattern B (tag wraps to next line):
#   "SP Netflix, Inc. - Common Stock (NFLX) S 12/12/2025 01/06/2026 $1,001 - $15,000"
#   "[ST]"
#
# Pattern C (asset name wraps, ticker on continuation):
#   "Amazon.com, Inc. - Common Stock S (partial) 03/16/2026 03/16/2026 $1,001 - $15,000"
#   "(AMZN) [ST]"
#
# Pattern D (ticker and tag on continuation, page header in between):
#   "Berkshire Hathaway Inc. New S (partial) 03/16/2026 03/16/2026 $1,001 - $15,000"
#   "Common Stock (BRK.B) [ST]"   ← appears AFTER a page-header block
#
# Strategy: lines with two dates (the "anchor") start a new chunk.  All
# subsequent non-anchor, non-skip lines before the next anchor are continuations
# of the same transaction.
# ---------------------------------------------------------------------------

def _split_into_chunks(text: str) -> list[str]:
    """Return one string per transaction block."""
    raw_lines = text.splitlines()

    # Separate name line from transaction lines
    name_line: str = ""
    content_lines: list[str] = []
    for ln in raw_lines:
        stripped = ln.strip()
        if not stripped:
            continue
        if stripped.startswith("Name:"):
            name_line = stripped
        elif not _is_skip(stripped):
            content_lines.append(stripped)

    # Group into chunks: each chunk starts with an anchor line (two dates).
    chunks: list[list[str]] = []
    current: list[str] = []

    for ln in content_lines:
        if _TWO_DATES.search(ln):
            # New anchor → flush previous chunk
            if current:
                chunks.append(current)
            current = [ln]
        else:
            # Continuation of current chunk (ticker/tag/amount wrap)
            current.append(ln)

    if current:
        chunks.append(current)

    result: list[str] = []
    for chunk_lines in chunks:
        joined = " ".join(chunk_lines)
        # Only keep chunks that actually have two dates
        if _TWO_DATES.search(joined):
            result.append(joined)

    return result


# ---------------------------------------------------------------------------
# Chunk parser
# ---------------------------------------------------------------------------

def _extract_amounts(text: str) -> tuple[float, float] | None:
    """Extract (amount_low, amount_high) from text.

    Handles both:
    - contiguous: "$1,001 - $15,000"
    - separated:  "$15,001 - Stock (FERG) [ST] $50,000"
      (any text between the dash and the high amount)
    """
    # First try the clean contiguous pattern
    m = _AMOUNT_BOTH.search(text)
    if m:
        return _parse_amount(m.group(1)), _parse_amount(m.group(2))

    # Fall back: find all dollar amounts and use first two
    dollars = _DOLLAR_PAT.findall(text)
    if len(dollars) >= 2:
        return _parse_amount(dollars[0]), _parse_amount(dollars[1])

    return None


def _parse_chunk(chunk: str, doc_id: str, chamber: str, member_name: str) -> Transaction | None:
    """Parse one joined transaction chunk string into a Transaction, or None."""

    # ---- 1. Owner prefix ----
    owner = "SELF"
    body = chunk.strip()
    m_owner = _OWNER_PREFIX.match(body)
    if m_owner:
        owner = m_owner.group(1)
        body = m_owner.group(2).strip()

    # ---- 2. Locate the two dates ----
    m_dates = _TWO_DATES.search(body)
    if not m_dates:
        return None
    txn_date = _parse_date(m_dates.group(1))
    notification_date = _parse_date(m_dates.group(2))
    date_start = m_dates.start()
    date_end = m_dates.end()

    # ---- 3. Pre-date portion (asset + type) ----
    pre_date = body[:date_start].strip()

    # ---- 4. Post-date portion (amount + possible ticker/tag overflow) ----
    post_date = body[date_end:].strip()

    # ---- 5. Transaction type ----
    m_partial = re.search(r"\bS\s*\(partial\)", pre_date)
    if m_partial:
        txn_type = "S"
        is_partial = True
        type_start = m_partial.start()
        type_end = m_partial.end()
    else:
        # Find the LAST standalone P/S/E in pre_date.
        # Use a negative lookbehind/lookahead to avoid matching mid-word.
        # Allow S after spaces but not after letters.
        m_type = None
        for m in re.finditer(r"(?<![A-Za-z0-9(])([PSE])(?![A-Za-z0-9&/.])", pre_date):
            m_type = m
        if m_type is None:
            log.warning("No type token found in chunk: %r", chunk)
            return None
        txn_type = m_type.group(1)
        is_partial = False
        type_start = m_type.start()
        type_end = m_type.end()

    # ---- 6. Asset raw (everything before the type token) ----
    asset_raw = pre_date[:type_start].strip()

    # ---- 7. Ticker + asset_type ----
    # The ticker/tag appear in various wrapping combinations:
    #
    #   (a) Adjacent in asset_raw:    "...(NFLX) [ST]" before type token
    #   (b) (TICKER) in asset_raw, [TAG] in post_date after the amount
    #       e.g. "...(NFLX) S <dates> $1,001 - $15,000 [ST]"
    #   (c) Both in post_date:        "(AMZN) [ST]" or "(FERG) [ST] $50,000"
    #   (d) [TAG] in asset_raw before type token (ETF short names):
    #       "Invesco QQQ [OT] S (partial)..." → asset_raw="Invesco QQQ [OT]"
    #   (e) NYSEARCA:/NYSE:/etc. prefix in post_date:
    #       "...NYSEARCA: DIA [OT]" → ticker="DIA", asset_type="OT"
    #   (f) Double-paren PDF artifact: "(91282CJR3)) [GS]" — handled by _TICKER_TAG_PAT
    #       which allows trailing extra closing parens via \)*
    #
    # Strategy:
    #   1. Try adjacent (TICKER)[)]*[TAG] pair in full search space (handles a, c, f).
    #   2. Fall back: find last parenthesised (TICKER) in asset_raw + [TAG] anywhere.
    #   3. [P1] If still no asset_type/ticker: search asset_raw for a trailing [TAG],
    #      extract it, and strip it from asset_name.  Then apply secondary ticker
    #      extraction (exchange-prefixed or bare token before [TAG]) on the full space.
    pre_type_post = pre_date[type_end:]   # text between type token and dates (usually empty)
    search_space = asset_raw + " " + pre_type_post + " " + post_date

    ticker: str | None = None
    asset_type: str | None = None

    # Attempt 1: adjacent (TICKER)[)]*[TAG] pair — also handles double-paren artifacts
    ticker_matches = list(_TICKER_TAG_PAT.finditer(search_space))
    if ticker_matches:
        last = ticker_matches[-1]
        ticker = last.group(1).strip()
        asset_type = last.group(2).strip()
        ticker = ticker.replace("/", ".")
        asset_raw = _TICKER_TAG_PAT.sub("", asset_raw).strip()
    else:
        # Attempt 2: (UPPERCASE_TICKER) in asset_raw + [TAG] anywhere
        # Match parenthesised all-caps tokens (ticker symbols)
        ticker_candidates = re.findall(r"\(([A-Z][A-Z0-9./]{0,9})\)", asset_raw)
        if ticker_candidates:
            raw_ticker = ticker_candidates[-1]
            ticker = raw_ticker.replace("/", ".")
            # Remove the "(TICKER)" from asset_raw
            asset_raw = re.sub(
                r"\(" + re.escape(raw_ticker) + r"\)", "", asset_raw
            ).strip()

        # [TAG] in post_date or between type token and dates
        tag_search = pre_type_post + " " + post_date
        m_tag = re.search(r"\[([A-Z]{2,3})\]", tag_search)
        if m_tag:
            asset_type = m_tag.group(1)

    # ---- 7b. [P1] + [P3] Fallback: [TAG] and/or bare ticker in asset_raw ----
    # If asset_type is still None, search asset_raw for an embedded [TAG] token.
    # Crucially: extract the bare-token ticker BEFORE stripping the [TAG], because
    # the bare-ticker pattern "QQQ [OT]" requires the [TAG] still to be present.
    if asset_type is None:
        m_tag_in_raw = re.search(r"\[([A-Z]{2,3})\]", asset_raw)
        if m_tag_in_raw:
            asset_type = m_tag_in_raw.group(1)

            # [P3] While the [TAG] is still present in asset_raw, try bare ticker:
            # "Invesco QQQ [OT]" → bare token immediately before [OT]
            if ticker is None:
                m_bare = _BARE_TICKER_BEFORE_TAG.search(asset_raw)
                if m_bare:
                    candidate = m_bare.group(1)
                    tag = m_bare.group(2)
                    # Avoid mistaking the tag token itself for a ticker
                    if candidate != tag:
                        ticker = candidate

            # Now strip the [TAG] from asset_raw so it does not leak into asset_name
            asset_raw = (
                asset_raw[: m_tag_in_raw.start()] + asset_raw[m_tag_in_raw.end() :]
            ).strip()

    # ---- 7c. [P3] Secondary ticker extraction — only when no parenthesised ticker found ----
    # Used for ETF rows where the ticker appears without parens.
    if ticker is None:
        # (a) Exchange-prefixed: "NYSEARCA: DIA", "NYSE: XOM", "NASDAQ: AAPL"
        # Search post_date first (continuation lines), then asset_raw
        for search_str in (post_date, asset_raw):
            m_exch = _EXCHANGE_TICKER_PAT.search(search_str)
            if m_exch:
                ticker = m_exch.group(1)
                break

    # ---- 8. Amount extraction ----
    amounts = _extract_amounts(post_date)
    if amounts is None:
        # Last resort: search entire body
        amounts = _extract_amounts(body)
    if amounts is None:
        log.warning("No amount found in chunk (doc_id=%s): %r", doc_id, chunk)
        return None
    amount_low, amount_high = amounts

    # ---- 9. Clean asset name ----
    asset_name = re.sub(r"\s+", " ", asset_raw).strip().rstrip("-").strip()

    return Transaction(
        doc_id=doc_id,
        chamber=chamber,
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
    )
