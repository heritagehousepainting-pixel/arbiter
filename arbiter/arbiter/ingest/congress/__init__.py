"""Congress financial-disclosure ingestion adapter — Layer L5 (integration).

Public surface
--------------
- ``CongressClient``         : HTTP client (L1) — fetches index zips + PTR PDFs
- ``fetch_house_ptrs``       : high-level orchestrator — chains L1 → L2 → L3 → L4
- ``fetch_and_normalize_house``: alias for ``fetch_house_ptrs`` (convenience)

For backward-compatibility the following names are importable *from their
submodules* (do NOT re-export them at package level — importing
``arbiter.ingest.congress.normalize`` must return the submodule, not a function):

- ``arbiter.ingest.congress.normalize.normalize``       : old-path compat shim
- ``arbiter.ingest.congress.normalize.to_raw_filings``  : new PTR-pipeline normalizer
- ``arbiter.ingest.congress.parser.parse_disclosures``  : OLD JSON-path parser
- ``arbiter.ingest.congress.ptr_pdf.extract_ptr_text``  : PDF extractor (L3)
- ``arbiter.ingest.congress.ptr_pdf.parse_ptr``         : PTR parser (L3)

Design notes (REBUILD_PLAN.md §5)
-----------------------------------
- ``fetch_house_ptrs`` chains: fetch_house_index → parse_index → filter_ptrs
  → fetch_ptr_pdf → extract_ptr_text → parse_ptr → to_raw_filings.
- Fault-isolated per PTR: one bad filing logs + continues.
- ``limit`` param (default 50) caps fetched PDFs per run to be polite.
- No ``datetime.now()`` anywhere in this file.

IMPORTANT: We intentionally do NOT assign ``normalize = <function>`` at package
level because that would shadow the ``normalize`` *submodule*, breaking any code
that does ``import arbiter.ingest.congress.normalize as mod``.  Python resolves
package attribute lookups before checking ``sys.modules`` for submodule names.
"""
from __future__ import annotations

import dataclasses
import logging

from arbiter.ingest.congress.client import CongressClient
from arbiter.ingest.congress.index import parse_index, filter_ptrs

# Import the new-pipeline types — using explicit submodule references to avoid
# shadowing the submodule attribute on this package.
from arbiter.ingest.congress.ptr_pdf import extract_ptr_text, parse_ptr
from arbiter.ingest.congress.normalize import to_raw_filings, RawFiling
from arbiter.ingest.congress.senate import fetch_senate_ptrs as _fetch_senate_transactions

log = logging.getLogger(__name__)


def fetch_house_ptrs(
    client: CongressClient,
    year: int,
    *,
    limit: int = 50,
) -> list[RawFiling]:
    """Fetch and normalize all House PTR filings for a given year.

    Orchestrates the full L1→L2→L3→L4 pipeline:

    1. ``client.fetch_house_index(year)``  → raw zip bytes  (L1)
    2. ``parse_index(zip_bytes)``          → list[IndexRecord]  (L2)
    3. ``filter_ptrs(electronic_only=True)`` → PTR-only electronic records  (L2)
    4. For each record (up to ``limit``):
       a. ``client.fetch_ptr_pdf(year, doc_id)`` → raw PDF bytes  (L1)
       b. ``extract_ptr_text(pdf_bytes)``        → PtrText  (L3)
       c. ``parse_ptr(ptr_text)``               → list[Transaction]  (L3)
       d. ``to_raw_filings(transactions)``       → list[RawFiling]  (L4)

    One bad PTR logs a WARNING and continues (fault-isolated per filing).

    Parameters
    ----------
    client:
        A ``CongressClient`` instance (real or injected mock for tests).
    year:
        The 4-digit filing year (e.g. 2026).
    limit:
        Maximum number of PTR PDFs to fetch per call.  Defaults to 50.
        Set lower for testing or polite bulk fetches.

    Returns
    -------
    Flat list of ``RawFiling`` dicts from all successfully parsed PTRs.
    """
    # Step 1 + 2: fetch and parse the annual index zip
    try:
        zip_bytes = client.fetch_house_index(year)
    except Exception as exc:
        log.warning("fetch_house_ptrs: failed to fetch index for year=%s: %s", year, exc)
        return []

    try:
        all_records = parse_index(zip_bytes, chamber="house", year=year)
    except Exception as exc:
        log.warning("fetch_house_ptrs: failed to parse index for year=%s: %s", year, exc)
        return []

    # Step 3: keep PTRs, electronic-only (text-extractable PDFs)
    ptr_records = filter_ptrs(all_records, electronic_only=True)

    if not ptr_records:
        log.info("fetch_house_ptrs: no electronic PTR records found for year=%s", year)
        return []

    # Sort by filing_date descending so the limit cap selects the MOST RECENT
    # disclosures rather than the first N alphabetically (the House index is
    # ordered by member last name, so an unsorted slice would always pick
    # A–D members and miss later-alphabet filers entirely).
    ptr_records = sorted(ptr_records, key=lambda r: r.filing_date, reverse=True)

    # Apply the per-run limit (now operating on recency-ordered records)
    ptr_records = ptr_records[:limit]
    log.info(
        "fetch_house_ptrs: processing %d electronic PTR(s) for year=%s (limit=%d)",
        len(ptr_records),
        year,
        limit,
    )

    # Steps 4a–4d: per-PTR fault-isolated pipeline
    all_filings: list[RawFiling] = []
    for record in ptr_records:
        doc_id = record.doc_id
        try:
            pdf_bytes = client.fetch_ptr_pdf(year, doc_id)
        except Exception as exc:
            log.warning(
                "fetch_house_ptrs: skipping doc_id=%s — fetch_ptr_pdf failed: %s",
                doc_id,
                exc,
            )
            continue

        try:
            ptr_text = extract_ptr_text(pdf_bytes, doc_id=doc_id, chamber="house", year=year)
        except Exception as exc:
            log.warning(
                "fetch_house_ptrs: skipping doc_id=%s — extract_ptr_text failed: %s",
                doc_id,
                exc,
            )
            continue

        try:
            transactions = parse_ptr(ptr_text)
        except Exception as exc:
            log.warning(
                "fetch_house_ptrs: skipping doc_id=%s — parse_ptr failed: %s",
                doc_id,
                exc,
            )
            continue

        if not transactions:
            log.debug("fetch_house_ptrs: doc_id=%s yielded no transactions", doc_id)
            continue

        # Stamp the Clerk receipt date (public-availability date) from the index
        # record onto each transaction so filing_ts uses the true "information
        # available" timestamp rather than the earlier per-row PDF notification
        # date (avoids look-ahead). Transaction is frozen → dataclasses.replace.
        transactions = [
            dataclasses.replace(t, clerk_receipt_date=record.filing_date)
            for t in transactions
        ]

        try:
            filings = to_raw_filings(transactions)
        except Exception as exc:
            log.warning(
                "fetch_house_ptrs: skipping doc_id=%s — to_raw_filings failed: %s",
                doc_id,
                exc,
            )
            continue

        log.debug(
            "fetch_house_ptrs: doc_id=%s → %d filing(s)", doc_id, len(filings)
        )
        all_filings.extend(filings)

    log.info(
        "fetch_house_ptrs: year=%s complete — %d total RawFiling(s)", year, len(all_filings)
    )
    return all_filings


def fetch_senate_ptrs(
    client: CongressClient,
    year: int,
    *,
    limit: int = 50,
) -> list[RawFiling]:
    """Fetch and normalize all Senate PTR filings for a given year.

    Orchestrates the Senate flow using the senate module:

    1. ``senate.fetch_senate_ptrs(year)`` → list[Transaction]  (network)
    2. Sort by notification_date descending; cap at ``limit``.
    3. ``to_raw_filings(transactions, chamber_prefix="S")`` → list[RawFiling]

    One bad report logs a WARNING and continues (fault-isolated per filing).
    The ``client`` parameter is accepted for API symmetry with
    ``fetch_house_ptrs`` but the Senate module manages its own HTTP session
    (cookie jar + CSRF required per request).

    Parameters
    ----------
    client:
        A ``CongressClient`` instance (real or injected mock).  Not used
        directly by this function but kept for interface symmetry.
    year:
        The 4-digit filing year (e.g. 2026).
    limit:
        Maximum number of PTRs (reports) to process per call.  Applied
        *before* normalisation so it caps the number of fetched pages.
        Defaults to 50.

    Returns
    -------
    Flat list of ``RawFiling`` dicts from all successfully parsed PTRs.
    """
    try:
        all_transactions = _fetch_senate_transactions(year)
    except Exception as exc:
        log.warning("fetch_senate_ptrs: senate flow failed for year=%s: %s", year, exc)
        return []

    if not all_transactions:
        log.info("fetch_senate_ptrs: no transactions found for year=%s", year)
        return []

    # Group by doc_id (UUID) to apply per-report limit, sorted by notification_date desc
    from collections import defaultdict
    by_uuid: dict[str, list] = defaultdict(list)
    for txn in all_transactions:
        by_uuid[txn.doc_id].append(txn)

    # Sort UUIDs by the notification_date of their first transaction (desc)
    sorted_uuids = sorted(
        by_uuid.keys(),
        key=lambda uid: by_uuid[uid][0].notification_date,
        reverse=True,
    )

    # Cap at limit (per-report, not per-transaction)
    capped_uuids = sorted_uuids[:limit]

    # Write NON-amendments before amendments so an amendment supersedes its
    # same-batch original (the writer only supersedes priors already in the DB,
    # so order matters when both land in one run). Stable sort keeps date-desc
    # order within each group; False (original) sorts before True (amendment).
    capped_uuids.sort(key=lambda uid: bool(by_uuid[uid][0].is_amendment))

    log.info(
        "fetch_senate_ptrs: processing %d PTR report(s) for year=%s (limit=%d)",
        len(capped_uuids),
        year,
        limit,
    )

    all_filings: list[RawFiling] = []
    for uuid in capped_uuids:
        txns = by_uuid[uuid]
        try:
            filings = to_raw_filings(txns, chamber_prefix="S")
        except Exception as exc:
            log.warning(
                "fetch_senate_ptrs: to_raw_filings failed for uuid=%s: %s",
                uuid,
                exc,
            )
            continue

        log.debug("fetch_senate_ptrs: uuid=%s → %d filing(s)", uuid, len(filings))
        all_filings.extend(filings)

    log.info(
        "fetch_senate_ptrs: year=%s complete — %d total RawFiling(s)",
        year,
        len(all_filings),
    )
    return all_filings


# Convenience alias matching the public surface spec in REBUILD_PLAN.md
fetch_and_normalize_house = fetch_house_ptrs

__all__ = [
    "CongressClient",
    "fetch_house_ptrs",
    "fetch_senate_ptrs",
    "fetch_and_normalize_house",
    "to_raw_filings",
    "RawFiling",
]
