"""Business-rule normalization for Schedule 13D / 13G filings.

Maps the parser rows from ``sc13_parser.parse_sc13`` into the **same**
``RawFiling`` TypedDict used for Form-4 (reused from ``normalize.py``), tagged
with ``source = "form13d"``.

Rules applied here
------------------
1. Drop rows with ``percent_of_class is not None and percent_of_class < 5.0``
   **when not an amendment** — sub-threshold non-amendments are a data error.
   A ``<5%`` *amendment* is kept (it is an exit/reduction → ``txn_type="S"``).
2. ``txn_type = row["transaction_code"]`` (``"P"`` new/increased stake;
   ``"S"`` exit/reduction).
3. ``source = "form13d"``; ``shares = aggregate_amount or 0.0``.
4. ``amount_low`` / ``amount_high`` are **always None** — the schedule does not
   disclose the dollar value of the stake.  Never fabricate (consistent with
   the Form-4 ``None``-preservation rule).
5. ``is_10b5_1`` is always False (not applicable; shape parity).
6. ``is_amendment`` forwarded; ``writer.write_filing`` supersede logic then
   flips prior rows for the same ``(ticker, person_id)``.
7. ``raw_json`` preserves ``schedule`` / ``is_activist`` / ``percent_of_class``
   / ``cusip`` / ``aggregate_amount`` for the engine owner's detector/scorer.
"""
from __future__ import annotations

import json

from arbiter.ingest.edgar.normalize import RawFiling


_REPORTING_THRESHOLD_PCT = 5.0


def normalize_sc13(parsed: list[dict]) -> list[RawFiling]:
    """Apply 13D/13G business rules and return keeper ``RawFiling`` rows.

    Parameters
    ----------
    parsed:
        Rows as returned by ``sc13_parser.parse_sc13``.

    Returns
    -------
    List of ``RawFiling`` dicts with ``source = "form13d"``.
    """
    results: list[RawFiling] = []

    for row in parsed:
        percent = row.get("percent_of_class")
        is_amendment = bool(row.get("is_amendment", False))

        # Drop sub-threshold non-amendments (defensive — SEC threshold is 5%).
        # A sub-threshold *amendment* is a real exit/reduction; keep it.
        if (
            percent is not None
            and percent < _REPORTING_THRESHOLD_PCT
            and not is_amendment
        ):
            continue

        txn_type = row.get("transaction_code", "P")
        aggregate_amount = row.get("aggregate_amount")
        shares = float(aggregate_amount) if aggregate_amount is not None else 0.0

        filing: RawFiling = {
            "source": "form13d",
            "ticker": row["ticker"],
            "person_id": row["person_id"],
            "person_name": row["person_name"],
            "filing_ts": row["filing_ts"],
            "txn_type": txn_type,
            "txn_idx": int(row.get("txn_idx", 0)),
            "shares": shares,
            "price": None,
            "amount_low": None,
            "amount_high": None,
            "is_10b5_1": False,
            "is_amendment": is_amendment,
            "accession": row["accession"],
            "raw_json": json.dumps(row, default=str),
        }
        results.append(filing)

    return results
