"""Congress disclosure parser — Lane 5b.

Converts raw dicts from ``CongressClient`` into a normalized intermediate
representation suitable for ``normalize.py``.

Key responsibilities
--------------------
1. Map House and Senate raw dict fields to a common intermediate schema.
2. Parse ``filing_date`` (disclosure date) into an ISO-8601 tz-aware string.
3. Detect buy vs. sell from ``transaction_type``.
4. Map ``amount`` bracket strings to ``(amount_low, amount_high)`` tuples.
5. Detect amendments (``doc_type`` == "amendment" or ``amended`` flag).

Amount brackets
---------------
Congress reporters disclose amounts as ordinal ranges, NOT exact figures.
The brackets used in both STOCK Act (House and Senate) disclosures are:

    "$1 - $1,000"            →  (1.0,      1_000.0)
    "$1,001 - $15,000"       →  (1_001.0,  15_000.0)
    "$15,001 - $50,000"      →  (15_001.0, 50_000.0)
    "$50,001 - $100,000"     →  (50_001.0, 100_000.0)
    "$100,001 - $250,000"    →  (100_001.0, 250_000.0)
    "$250,001 - $500,000"    →  (250_001.0, 500_000.0)
    "$500,001 - $1,000,000"  →  (500_001.0, 1_000_000.0)
    "$1,000,001 - $5,000,000"→  (1_000_001.0, 5_000_000.0)
    "$5,000,001 - $25,000,000"→ (5_000_001.0, 25_000_000.0)
    "$25,000,001 - $50,000,000"→(25_000_001.0, 50_000_000.0)
    "Over $50,000,000"       →  (50_000_001.0, None)  — no cap; high = 50_000_001.0

CRITICAL: ``normalize.py`` NEVER midpoint-imputes (INTERFACES.md §4.3).
The parser stores raw low/high; the caller decides how to handle None high.

``filing_ts`` semantics
-----------------------
``filing_ts`` is the **disclosure date** (i.e. the date the member filed the
disclosure with the House or Senate).  It is NOT the transaction date.
Congress has a ~45-day disclosure window.  Per INTERFACES.md §3:
    "Per-source ``as_of``: … Congress = disclosure date".

All datetimes are set to midnight UTC (00:00:00Z) on the disclosure date
because the House/Senate APIs return date-only strings.

Design constraint
-----------------
No ``datetime.now()`` here.  All timestamps are parsed from the data itself.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Amount bracket table
# ---------------------------------------------------------------------------
# Each entry: (bracket_label_pattern, amount_low, amount_high)
# Pattern is applied case-insensitively to the raw "amount" string.
# The table is ordered from most-specific (largest numbers) to least-specific
# to avoid false matches on overlapping patterns (e.g. "50,000" in two rows).

_AMOUNT_BRACKETS: list[tuple[str, float, float]] = [
    # Over $50M — no defined upper bound; we use the lower bound as high too
    # (explicitly surfaced as a non-None pair so downstream can range-check).
    ("over.*50,000,000",          50_000_001.0, 50_000_001.0),  # sentinel: no cap
    # $25M – $50M
    ("25,000,001.*50,000,000",    25_000_001.0, 50_000_000.0),
    # $5M – $25M
    ("5,000,001.*25,000,000",      5_000_001.0, 25_000_000.0),
    # $1M – $5M
    ("1,000,001.*5,000,000",       1_000_001.0,  5_000_000.0),
    # $500K – $1M
    ("500,001.*1,000,000",           500_001.0,  1_000_000.0),
    # $250K – $500K
    ("250,001.*500,000",             250_001.0,    500_000.0),
    # $100K – $250K
    ("100,001.*250,000",             100_001.0,    250_000.0),
    # $50K – $100K
    ("50,001.*100,000",               50_001.0,    100_000.0),
    # $15K – $50K
    ("15,001.*50,000",                15_001.0,     50_000.0),
    # $1,001 – $15K
    ("1,001.*15,000",                  1_001.0,     15_000.0),
    # $1 – $1,000
    ("\\$1\\s*[-–]\\s*\\$1,000",          1.0,      1_000.0),
]

# Compiled once at module import
_COMPILED_BRACKETS: list[tuple[re.Pattern[str], float, float]] = [
    (re.compile(pattern, re.IGNORECASE), low, high)
    for pattern, low, high in _AMOUNT_BRACKETS
]


def parse_amount_bracket(raw: str) -> tuple[float, float]:
    """Map a raw disclosure amount string to (amount_low, amount_high).

    Parameters
    ----------
    raw:
        The raw amount string from the disclosure, e.g. "$15,001 - $50,000".

    Returns
    -------
    (amount_low, amount_high) floats.

    Raises
    ------
    ValueError
        If ``raw`` does not match any known bracket.
    """
    for pattern, low, high in _COMPILED_BRACKETS:
        if pattern.search(raw):
            return low, high
    raise ValueError(f"Unknown Congress amount bracket: {raw!r}")


# ---------------------------------------------------------------------------
# Transaction type mapping
# ---------------------------------------------------------------------------

def _parse_txn_type(raw: str) -> str | None:
    """Normalise a raw transaction_type string to "P" or "S", or None to skip.

    Handles both House and Senate field values.
    """
    normalised = raw.strip().upper()
    # Purchase variants
    if normalised in {"PURCHASE", "P", "BUY", "BOUGHT"}:
        return "P"
    # Sale variants (full sale and partial sale both map to "S")
    if normalised in {
        "SALE",
        "SALE (FULL)",
        "SALE (PARTIAL)",
        "S",
        "SELL",
        "SOLD",
    }:
        return "S"
    return None  # unknown / exercise / gift / etc. → caller should skip


# ---------------------------------------------------------------------------
# Date parsing helper
# ---------------------------------------------------------------------------

def _parse_disclosure_date(raw: str) -> str:
    """Parse a disclosure date string to a tz-aware ISO-8601 UTC timestamp.

    The House/Senate APIs return date-only strings in various formats:
    - "MM/DD/YYYY"   (House)
    - "YYYY-MM-DD"   (Senate)

    Returns a UTC midnight ISO-8601 string, e.g. "2024-03-15T00:00:00+00:00".

    Raises
    ------
    ValueError
        If the date string cannot be parsed.
    """
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse disclosure date: {raw!r}")


# ---------------------------------------------------------------------------
# House record parser
# ---------------------------------------------------------------------------

def _parse_house_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a single House disclosure record into the intermediate schema.

    Returns ``None`` for records that should be skipped (unknown txn type,
    missing required fields, or non-equity asset classes).
    """
    # House fields (vary slightly by year / API version).
    # IMPORTANT: only read from explicit ticker fields — do NOT fall back to
    # asset_description, which contains free-text company names (not symbols).
    ticker = (
        record.get("ticker")
        or record.get("Ticker")
        or ""
    ).strip().upper()

    if not ticker or ticker in {"N/A", "NONE", "--"}:
        return None  # non-equity or missing ticker

    raw_txn = (
        record.get("transaction_type")
        or record.get("TransactionType")
        or record.get("type", "")
    )
    txn_type = _parse_txn_type(str(raw_txn))
    if txn_type is None:
        return None  # skip non-buy/sell (options exercises, gifts, etc.)

    raw_amount = (
        record.get("amount")
        or record.get("Amount")
        or record.get("amount_disclosed", "")
    )
    try:
        amount_low, amount_high = parse_amount_bracket(str(raw_amount))
    except ValueError:
        logger.warning("house: skipping unknown amount bracket %r", raw_amount)
        return None

    raw_date = (
        record.get("filing_date")
        or record.get("FilingDate")
        or record.get("disclosure_date")
        or record.get("date", "")
    )
    try:
        filing_ts = _parse_disclosure_date(str(raw_date))
    except ValueError:
        logger.warning("house: skipping bad disclosure date %r", raw_date)
        return None

    # Member identity
    name = (
        record.get("name")
        or record.get("Name")
        or record.get("member_name", "unknown")
    ).strip()
    bioguide = (
        record.get("bioguide_id")
        or record.get("BioguideID")
        or record.get("member_id", "")
    ).strip()
    person_id = f"house:{bioguide}" if bioguide else f"house:{name.lower().replace(' ', '_')}"

    is_amendment = bool(
        record.get("amended")
        or record.get("Amended")
        or str(record.get("doc_type", "")).lower() == "amendment"
    )

    return {
        "source_chamber": "house",
        "ticker": ticker,
        "person_id": person_id,
        "person_name": name,
        "filing_ts": filing_ts,
        "txn_type": txn_type,
        "amount_low": amount_low,
        "amount_high": amount_high,
        "is_10b5_1": False,
        "is_amendment": is_amendment,
        # pass through raw for raw_json
        "_raw": record,
    }


# ---------------------------------------------------------------------------
# Senate record parser
# ---------------------------------------------------------------------------

def _parse_senate_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a single Senate eFTDS disclosure record into the intermediate schema.

    Senate eFTDS records have a ``_source`` payload (already unwrapped by the
    client) with fields like ``first_name``, ``last_name``, ``transaction_date``,
    ``asset_description``, ``transaction_type``, ``amount``.

    Returns ``None`` for records to skip.
    """
    # Only use explicit ticker fields — do NOT fall back to asset_description.
    ticker = (
        record.get("ticker")
        or ""
    ).strip().upper()

    if not ticker or ticker in {"N/A", "NONE", "--"}:
        return None

    raw_txn = str(record.get("transaction_type") or record.get("type", ""))
    txn_type = _parse_txn_type(raw_txn)
    if txn_type is None:
        return None

    raw_amount = str(record.get("amount") or record.get("amount_disclosed", ""))
    try:
        amount_low, amount_high = parse_amount_bracket(raw_amount)
    except ValueError:
        logger.warning("senate: skipping unknown amount bracket %r", raw_amount)
        return None

    # Senate disclosure date (the date the senator filed)
    raw_date = str(
        record.get("disclosure_date")
        or record.get("filing_date")
        or record.get("date_received")
        or record.get("date", "")
    )
    try:
        filing_ts = _parse_disclosure_date(raw_date)
    except ValueError:
        logger.warning("senate: skipping bad disclosure date %r", raw_date)
        return None

    first = str(record.get("first_name") or "").strip()
    last = str(record.get("last_name") or "").strip()
    name = (
        f"{first} {last}".strip()
        or str(record.get("name") or "unknown").strip()
    )

    bioguide = str(record.get("bioguide_id") or record.get("senator_id", "")).strip()
    person_id = f"senate:{bioguide}" if bioguide else f"senate:{name.lower().replace(' ', '_')}"

    is_amendment = bool(
        record.get("amendment_id")
        or str(record.get("doc_type", "")).lower() == "amendment"
    )

    return {
        "source_chamber": "senate",
        "ticker": ticker,
        "person_id": person_id,
        "person_name": name,
        "filing_ts": filing_ts,
        "txn_type": txn_type,
        "amount_low": amount_low,
        "amount_high": amount_high,
        "is_10b5_1": False,
        "is_amendment": is_amendment,
        "_raw": record,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_disclosures(
    records: list[dict[str, Any]],
    *,
    chamber: str = "house",
) -> list[dict[str, Any]]:
    """Parse a list of raw disclosure records into the intermediate schema.

    Parameters
    ----------
    records:
        Raw dicts as returned by ``CongressClient.fetch_house`` or
        ``CongressClient.fetch_senate``.
    chamber:
        ``"house"`` (default) or ``"senate"``.  Controls which field-mapping
        logic is applied.

    Returns
    -------
    List of intermediate dicts (one per valid transaction).  Records with
    unknown transaction types, missing tickers, or unrecognised amount
    brackets are silently dropped (logged at WARNING level).
    """
    parse_fn = _parse_house_record if chamber == "house" else _parse_senate_record
    parsed: list[dict[str, Any]] = []
    for record in records:
        result = parse_fn(record)
        if result is not None:
            parsed.append(result)
    return parsed
