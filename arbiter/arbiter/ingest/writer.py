"""Filing writer — Lane 5c.

``write_filing`` persists a normalised RawFiling dict to the ``filings`` table
and emits an audit event.

Business rules (INTERFACES.md §10, §11; spec §4.3)
----------------------------------------------------
- **10b5-1 exclusion**: filings with ``is_10b5_1=True`` are silently skipped;
  they return ``None``.
- **Idempotency per transaction**: if ``accession`` is present the dedup key
  is ``(accession, txn_idx)`` — one Form 4 with multiple
  ``<nonDerivativeTransaction>`` rows produces several ``RawFiling`` dicts
  sharing one accession but with distinct ``txn_idx`` values.  Re-writing the
  exact same ``(accession, txn_idx)`` pair is a no-op; writing a *different*
  ``txn_idx`` from the same accession is NOT a duplicate.  Congress filings
  (no ``txn_idx``) fall back to plain ``accession`` dedup.
- **Amendments** (``is_amendment=True``): the writer supersedes ALL
  non-superseded filings for the same (ticker, person_id) that pre-date this
  one.  Superseding only the most recent prior leaves earlier rows active and
  causes double-counting for multi-amendment chains.
- **Amount ranges**: ``amount_low`` and ``amount_high`` are stored as-is; no
  midpoint is computed (INTERFACES.md §10 comment in schema, spec §4.3).
  Both may be ``None`` when EDGAR did not disclose the price.
- **Insert-only store**: the only permitted UPDATE is the ``is_superseded``
  flag flip inside ``supersede_row`` (INTERFACES.md §11.2).

RawFiling dict schema (from adapter agents)
-------------------------------------------
    source        str          'form4' | 'congress'
    ticker        str
    person_id     str          resolved ULID from identity.resolver
    person_name   str          (not stored; resolved before calling writer)
    filing_ts     str          tz-aware ISO-8601 UTC
    txn_type      str          e.g. 'P', 'S', 'purchase', 'sale'
    txn_idx       int | None   0-based position within filing (Form 4 only)
    shares        float | None
    price         float | None
    amount_low    float | None
    amount_high   float | None
    is_10b5_1     bool
    is_amendment  bool
    accession     str | None
    raw_json      str | None
"""
from __future__ import annotations

import json
import sqlite3
from typing import Callable

import structlog

from arbiter.db.audit import audit
from arbiter.db.helpers import generate_ulid, insert_row, supersede_rows

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Accession index helpers
# ---------------------------------------------------------------------------

# Idempotency lookups intentionally match ANY existing row — superseded or not.
# The UNIQUE index ``idx_filings_accession_txn`` spans superseded rows, so once
# a filing is stored it permanently owns its ``(accession, txn_idx)`` slot. If
# dedup filtered on ``is_superseded = 0``, re-ingesting a filing that an
# amendment later superseded would miss the (superseded) row and attempt a
# duplicate INSERT → ``UNIQUE constraint failed``. Re-ingest must be a no-op
# that returns the existing id without un-superseding it.
_ACCESSION_TXN_QUERY = """
    SELECT id FROM filings
    WHERE accession = ? AND txn_idx = ?
    LIMIT 1
"""

_ACCESSION_QUERY = """
    SELECT id FROM filings
    WHERE accession = ?
    LIMIT 1
"""

_ALL_PRIOR_FILINGS_QUERY = """
    SELECT id FROM filings
    WHERE ticker = ? AND person_id = ? AND is_superseded = 0
          AND filing_ts < ?
    ORDER BY filing_ts DESC
"""

# Amendment variant: use <= so that a same-day original is also superseded.
# The amendment row is not yet inserted when this query runs, so there is no
# self-match risk.  A distinct accession/UUID guards against accidentally
# superseding a row inserted in the same clock-second with a different accession.
_ALL_PRIOR_FILINGS_QUERY_AMENDMENT = """
    SELECT id FROM filings
    WHERE ticker = ? AND person_id = ? AND is_superseded = 0
          AND filing_ts <= ?
          AND (accession IS NULL OR accession != ?)
    ORDER BY filing_ts DESC
"""


def _accession_txn_exists(
    conn: sqlite3.Connection, accession: str, txn_idx: int
) -> str | None:
    """Return existing filing id for *(accession, txn_idx)* pair, else None."""
    row = conn.execute(_ACCESSION_TXN_QUERY, (accession, txn_idx)).fetchone()
    return str(row[0]) if row else None


def _accession_exists(conn: sqlite3.Connection, accession: str) -> str | None:
    """Return the existing filing id if *accession* is already stored, else None.

    Used as fallback for filings that do not carry a ``txn_idx`` (e.g.
    Congress disclosures).
    """
    row = conn.execute(_ACCESSION_QUERY, (accession,)).fetchone()
    return str(row[0]) if row else None


def _find_all_prior_filings(
    conn: sqlite3.Connection,
    ticker: str,
    person_id: str,
    filing_ts: str,
    *,
    amendment_accession: str | None = None,
) -> list[str]:
    """Return ids of ALL non-superseded filings before (or on) filing_ts for (ticker, person_id).

    When ``amendment_accession`` is provided (amendment path), the query uses
    ``filing_ts <=`` so that a same-day original is included.  The amendment's
    own accession is excluded to prevent self-supersede.

    For non-amendment calls (``amendment_accession=None``), uses strict ``<``
    (unchanged behaviour — Form-4 and other non-amendment flows are unaffected).

    Returns them newest-first so the caller can supersede in order.
    """
    if amendment_accession is not None:
        rows = conn.execute(
            _ALL_PRIOR_FILINGS_QUERY_AMENDMENT,
            (ticker, person_id, filing_ts, amendment_accession),
        ).fetchall()
    else:
        rows = conn.execute(
            _ALL_PRIOR_FILINGS_QUERY, (ticker, person_id, filing_ts)
        ).fetchall()
    return [str(row[0]) for row in rows]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_filing(
    conn: sqlite3.Connection,
    raw: dict,
    clock: Callable[[], str],
) -> str | None:
    """Persist *raw* to the ``filings`` table and return the filing id.

    Args:
        conn:  Open SQLite connection with 001_core + 008_identity migrations.
        raw:   Normalised RawFiling dict (see module docstring for schema).
        clock: Callable returning an ISO timestamp string — never datetime.now.

    Returns:
        The ULID of the written (or pre-existing) filing row, or ``None`` if
        the filing was excluded (10b5-1 rule).
    """
    # --- 10b5-1 exclusion ---
    if raw.get("is_10b5_1"):
        log.debug("write_filing.skipped_10b5_1", ticker=raw.get("ticker"))
        return None

    ticker: str = raw["ticker"]
    person_id: str = raw["person_id"]
    filing_ts: str = raw["filing_ts"]
    accession: str | None = raw.get("accession")
    txn_idx: int | None = raw.get("txn_idx")  # 0-based position within filing; set for BOTH Form 4 AND Congress (H-{doc_id}-{txn_idx} accession scheme)

    # --- Idempotency: per-transaction dedup key ---
    # Form 4 filings: dedup on (accession, txn_idx) because one accession may
    # contain multiple transactions.  Congress filings: dedup on accession only.
    if accession:
        if txn_idx is not None:
            existing_id = _accession_txn_exists(conn, accession, txn_idx)
        else:
            existing_id = _accession_exists(conn, accession)

        if existing_id:
            log.debug(
                "write_filing.duplicate",
                accession=accession,
                txn_idx=txn_idx,
                existing_id=existing_id,
            )
            return existing_id

    # Build the row dict for the filings table.
    row: dict = {
        "source": raw["source"],
        "ticker": ticker,
        "person_id": person_id,
        "filing_ts": filing_ts,
        "txn_type": raw["txn_type"],
        "amount_low": raw.get("amount_low"),
        "amount_high": raw.get("amount_high"),
        "is_10b5_1": 0,  # excluded above; always 0 here
        "is_amendment": 1 if raw.get("is_amendment") else 0,
        "raw_json": raw.get("raw_json"),
        "created_at": clock(),
    }
    if accession is not None:
        row["accession"] = accession
    if txn_idx is not None:
        row["txn_idx"] = txn_idx

    # Optional numeric fields (Form 4 has shares/price; Congress may not).
    for optional in ("shares", "price"):
        val = raw.get(optional)
        if val is not None:
            row[optional] = val

    # --- Amendment: supersede ALL prior non-superseded filings ---
    # Superseding only the most recent prior leaves earlier rows active, which
    # causes double-counting for multi-amendment chains (P1 audit finding).
    # Uses filing_ts <= so same-day originals are also caught (Senate PTR
    # amendments often share the same disclosure date as their original).
    if raw.get("is_amendment"):
        prior_ids = _find_all_prior_filings(
            conn, ticker, person_id, filing_ts,
            amendment_accession=accession,
        )
        if prior_ids:
            # Supersede ALL prior non-superseded filings for this (ticker,
            # person_id) atomically: one correcting row inserted (supersedes_id
            # = most-recent prior) + every prior flipped is_superseded=1 in a
            # single transaction. A crash can't leave any old filing active.
            new_id: str | None = supersede_rows(conn, "filings", prior_ids, row)
            audit(
                "write_filing.amendment_supersede",
                {
                    "new_id": new_id,
                    "superseded_ids": prior_ids,
                    "ticker": ticker,
                    "person_id": person_id,
                    "accession": accession,
                },
                ts=clock(),
            )
            log.info(
                "write_filing.amendment_supersede",
                new_id=new_id,
                superseded_count=len(prior_ids),
                ticker=ticker,
            )
            return new_id

    # --- Normal insert ---
    filing_id = insert_row(conn, "filings", row)
    audit(
        "write_filing.insert",
        {
            "filing_id": filing_id,
            "ticker": ticker,
            "person_id": person_id,
            "accession": accession,
            "is_amendment": bool(raw.get("is_amendment")),
        },
        ts=clock(),
    )
    log.info(
        "write_filing.insert",
        filing_id=filing_id,
        ticker=ticker,
        person_id=person_id,
    )
    return filing_id
