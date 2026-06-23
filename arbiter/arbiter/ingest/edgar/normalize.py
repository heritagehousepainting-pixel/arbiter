"""RawFiling TypedDict and business-rule normalization for Form 4 filings.

Rules applied here
------------------
1. ``is_10b5_1`` detection: set True when the filing XML carries a
   ``<planName>`` element or ``<footnoteId>`` text / ``<footnote>`` body
   containing the string "10b5-1" or "10b5_1" (case-insensitive).
2. 10b5-1 trades are **excluded** from the output list (filtered out).
3. Transaction codes:
   - "P" (open-market purchase) → ``txn_type = "P"``
   - "S" (open-market sale)     → ``txn_type = "S"``
   - All others (A option exercise, G gift, M conversion, …) → excluded.
4. ``is_amendment`` is True when the ``<documentType>`` is ``4/A``.
5. ``price`` / ``amount_low`` / ``amount_high`` may be ``None`` when EDGAR
   does not disclose the price.  The normalizer preserves ``None`` rather than
   coercing to 0 so downstream detection can distinguish "unknown price" from
   "zero-value trade."
6. ``txn_idx`` is the 0-based position of the transaction within its filing
   (set by the parser).  It is forwarded to the writer so the accession+idx
   pair serves as the per-transaction idempotency key.
"""
from __future__ import annotations

import json
from typing import TypedDict


class RawFiling(TypedDict):
    """Normalized representation of one Form 4 transaction row."""

    source: str               # always "form4"
    ticker: str
    person_id: str            # CIK of the reporting owner
    person_name: str
    filing_ts: str            # tz-aware ISO-8601 (UTC)
    txn_type: str             # "P" or "S"
    txn_idx: int              # 0-based index of this transaction within the filing
    shares: float
    price: float | None       # None when EDGAR omits/zeroes the price
    amount_low: float | None  # None when price is unknown
    amount_high: float | None # None when price is unknown
    is_10b5_1: bool
    is_amendment: bool
    accession: str
    raw_json: str             # json.dumps of the full parsed dict


# Transaction codes we actually care about (open-market only).
_OPEN_MARKET_CODES: frozenset[str] = frozenset({"P", "S"})


def normalize(parsed_filings: list[dict]) -> list[RawFiling]:
    """Apply all business rules and return only keeper filings.

    Parameters
    ----------
    parsed_filings:
        List of dicts as returned by ``parse_form4``.  Each dict has the
        structure produced by the parser (see ``parser.py``).

    Returns
    -------
    List of ``RawFiling`` dicts.  10b5-1 trades and non-open-market codes
    are **excluded**.
    """
    results: list[RawFiling] = []

    for row in parsed_filings:
        # --- filter: open-market buys/sells only -------------------------
        code = row.get("transaction_code", "")
        if code not in _OPEN_MARKET_CODES:
            continue

        # --- 10b5-1 detection -------------------------------------------
        is_10b5_1 = row.get("is_10b5_1", False)

        # --- filter: exclude 10b5-1 plan trades -------------------------
        if is_10b5_1:
            continue

        # Preserve None for price/amounts — do NOT coerce to 0.0.
        # A price of None means "EDGAR did not disclose the price"; 0.0 would
        # look like a zero-value trade and cause detection.py to skip it.
        price = row.get("price")   # already float | None from parser
        amount_low = row.get("amount_low")     # float | None
        amount_high = row.get("amount_high")   # float | None

        filing: RawFiling = {
            "source": "form4",
            "ticker": row["ticker"],
            "person_id": row["person_id"],
            "person_name": row["person_name"],
            "filing_ts": row["filing_ts"],
            "txn_type": code,        # "P" or "S"
            "txn_idx": int(row.get("txn_idx", 0)),
            "shares": float(row.get("shares", 0.0)),
            "price": float(price) if price is not None else None,
            "amount_low": float(amount_low) if amount_low is not None else None,
            "amount_high": float(amount_high) if amount_high is not None else None,
            "is_10b5_1": False,
            "is_amendment": bool(row.get("is_amendment", False)),
            "accession": row["accession"],
            "raw_json": json.dumps(row, default=str),
        }
        results.append(filing)

    return results
