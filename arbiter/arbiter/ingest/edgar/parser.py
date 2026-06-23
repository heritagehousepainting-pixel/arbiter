"""Form 4 XML parser — produces intermediate dicts fed into ``normalize``.

Each Form 4 XML may contain multiple ``<nonDerivativeTransaction>`` elements
(and optionally ``<derivativeTransaction>`` elements, which we skip).

Output per transaction row
--------------------------
{
    "ticker":             str,
    "person_id":          str,
    "person_name":        str,
    "filing_ts":          str,   # tz-aware ISO-8601 UTC
    "transaction_code":   str,   # single char, e.g. "P", "S", "A", "G", …
    "shares":             float,
    "price":              float,
    "amount_low":         float,
    "amount_high":        float,
    "is_10b5_1":          bool,
    "is_amendment":       bool,
    "accession":          str,
    # --- full raw extract (written into raw_json by normalizer) ---
    ... all parsed fields for audit trail
}

10b5-1 detection heuristic
---------------------------
1. ``<planName>`` element present and non-empty → True.
2. Any ``<footnote>`` text contains "10b5" (case-insensitive) → True.
3. Any ``<footnoteId>`` whose corresponding footnote matches (2) → True
   (we scan all footnotes globally because the link is by id).

Amendment detection
-------------------
``<documentType>4/A</documentType>`` → ``is_amendment = True``.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree as ET


# Regex to normalise 10b5-1 mentions regardless of spacing/punctuation
_10B5_RE = re.compile(r"10b5[-_]?1", re.IGNORECASE)


def _text(el: ET.Element | None, default: str = "") -> str:
    if el is None:
        return default
    return (el.text or "").strip()


def _float(el: ET.Element | None, default: float = 0.0) -> float:
    raw = _text(el)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _float_or_none(el: ET.Element | None) -> float | None:
    """Return the element value as float, or None if missing/empty/zero."""
    raw = _text(el)
    if not raw:
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    # Treat a literal 0 price as missing — EDGAR uses 0 as a sentinel when
    # the price is not disclosed (e.g. gifts, grants).
    return val if val != 0.0 else None


def _parse_filing_ts(period_of_report: str, filing_date: str) -> str:
    """Return a tz-aware ISO-8601 UTC timestamp string.

    Prefer ``periodOfReport`` (the actual transaction date).  Fall back to
    ``filingDate``.  EDGAR dates are ``YYYY-MM-DD``; we anchor them to
    midnight UTC because intra-day precision is not available in Form 4.
    """
    for raw in (period_of_report, filing_date):
        raw = raw.strip()
        if not raw:
            continue
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue
    raise ValueError(
        f"Cannot parse filing timestamp from period='{period_of_report}'"
        f" filing='{filing_date}'"
    )


def _detect_10b5_1(root: ET.Element) -> bool:
    """Return True if any signal in the document indicates a 10b5-1 plan."""
    # 1. <planName> element anywhere
    for el in root.iter("planName"):
        if _text(el):
            return True

    # 2. Collect all footnote bodies
    footnote_texts: list[str] = []
    for fn in root.iter("footnote"):
        t = _text(fn)
        if t:
            footnote_texts.append(t)

    if any(_10B5_RE.search(t) for t in footnote_texts):
        return True

    # 3. <footnoteId> — the footnote link is by id value; we already checked
    #    all footnote bodies above, so no separate lookup needed.

    return False


def parse_form4(
    xml_text: str,
    ticker: str,
    accession: str,
) -> list[dict[str, Any]]:
    """Parse a Form 4 XML document into a list of raw transaction dicts.

    Parameters
    ----------
    xml_text:
        Raw XML string fetched from EDGAR.
    ticker:
        Issuer ticker symbol (not present in the XML itself; must be supplied
        by the caller from the EDGAR company search results).
    accession:
        EDGAR accession number, e.g. ``"0001234567-26-000001"``.

    Returns
    -------
    A list of dicts, one per ``<nonDerivativeTransaction>`` row.  Derivative
    transactions (options, warrants, …) are intentionally skipped — they
    require separate handling and are excluded per §4.3 data-integrity rules.

    Hostile / malformed / non-XML / empty input never raises — it yields ``[]``.
    A document that parses but lacks a usable filing date also yields ``[]``
    (we cannot date the transaction, so it is unusable).
    """
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    # --- document-level fields -------------------------------------------
    doc_type = _text(root.find(".//documentType"))
    is_amendment = doc_type.upper() in {"4/A", "FORM 4/A"}

    period_of_report = _text(root.find(".//periodOfReport"))
    filing_date = _text(root.find(".//filingDate"))

    try:
        filing_ts = _parse_filing_ts(period_of_report, filing_date)
    except ValueError:
        # Document parsed but carries no usable date — cannot date the
        # transaction, so it is unusable. Skip rather than crash.
        return []

    # Reporting owner (may be multiple; take the first)
    owner_el = root.find(".//reportingOwner")
    person_id = ""
    person_name = ""
    if owner_el is not None:
        person_id = _text(owner_el.find(".//rptOwnerCik"))
        person_name = _text(owner_el.find(".//rptOwnerName"))

    # --- 10b5-1 detection (document-level; applies to ALL rows) ----------
    is_10b5_1 = _detect_10b5_1(root)

    # --- per-transaction rows --------------------------------------------
    rows: list[dict[str, Any]] = []

    for txn_idx, txn in enumerate(root.iter("nonDerivativeTransaction")):
        code_el = txn.find(".//transactionCode")
        transaction_code = _text(code_el)

        shares_el = txn.find(".//transactionShares/value")
        price_el = txn.find(".//transactionPricePerShare/value")

        # Value-range handling: EDGAR sometimes gives a range
        amount_el = txn.find(".//transactionAcquiredDisposedCode/value")

        shares = _float(shares_el)
        # Use _float_or_none so a missing/zero price yields None rather than 0,
        # which would silently zero out both amount fields and cause detection.py
        # to drop the filing entirely (P0 silent data-loss bug).
        price: float | None = _float_or_none(price_el)

        # acquired (A) or disposed (D) — cross-check against txn code
        acq_disp = _text(amount_el)

        # Amount range: collapse to a point value when price is known;
        # set both to None when price is unknown so downstream layers can
        # distinguish "zero-value trade" from "price not disclosed."
        if price is not None:
            amount = shares * price
            amount_low: float | None = amount
            amount_high: float | None = amount
        else:
            amount_low = None
            amount_high = None

        row: dict[str, Any] = {
            "ticker": ticker,
            "person_id": person_id,
            "person_name": person_name,
            "filing_ts": filing_ts,
            "transaction_code": transaction_code,
            "acq_disp": acq_disp,
            "txn_idx": txn_idx,         # position within this filing (0-based)
            "shares": shares,
            "price": price,             # None when EDGAR omits/zeroes the price
            "amount_low": amount_low,   # None when price is unknown
            "amount_high": amount_high, # None when price is unknown
            "is_10b5_1": is_10b5_1,
            "is_amendment": is_amendment,
            "accession": accession,
        }
        rows.append(row)

    return rows
