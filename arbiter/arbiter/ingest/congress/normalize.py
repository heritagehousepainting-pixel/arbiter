"""L4 — normalize: Transaction → RawFiling for the Congress PTR pipeline.

Backward-compat note
--------------------
The old ``runner.py`` (and its tests) call ``normalize(parsed_records)`` where
``parsed_records`` is a list of dicts produced by ``parser.parse_disclosures``.
The new ``to_raw_filings(transactions)`` takes ``Transaction`` dataclass objects
from L3 (ptr_pdf.parse_ptr).  Both signatures are exported here so the runner
keeps working unchanged while the new PDF pipeline uses ``to_raw_filings``.


Key contract rules (REBUILD_PLAN.md + INTERFACES.md §4.3)
----------------------------------------------------------
1.  source = "congress".
2.  filing_ts = the PUBLIC-availability (DISCLOSURE) date at UTC midnight as a
    tz-aware ISO-8601 string.  NOT the transaction date.
    For the House that is the Clerk receipt date (``clerk_receipt_date``, from
    the index FilingDate) — the per-row PDF notification_date is when the MEMBER
    was notified and can precede public availability, so using it is look-ahead.
    When ``clerk_receipt_date`` is None (Senate: the report header date already
    IS the public date) filing_ts falls back to notification_date.
    The ~45-day disclosure lag places Congress in the MEDIUM horizon bucket.
3.  txn_type: keep "P" (purchase) and "S" (sale) only.
    DROP "E" (exchange — no clear directional signal).
4.  DROP transactions where ticker is None (no tradeable signal).
5.  DROP non-equity instruments by asset_type tag (GS, PS, HN, CS, CO, OL,
    RP, AB, CT) and by CUSIP-shaped ticker (9-char alphanumeric with ≥1 digit,
    or token starting with a digit that is longer than 5 chars).
6.  DROP transactions where notification_date < txn_date (data-entry error) or
    where the disclosure lag exceeds 365 days.
7.  amount_low / amount_high taken verbatim from Transaction range.
    NEVER midpoint-imputed (INTERFACES.md §4.3).
8.  shares = None, price = None — Congress does not disclose these.
9.  is_10b5_1 = False always (no equivalent in disclosure law).
10. is_amendment: read from txn.is_amendment when present (Senate PTR amendments
    carry True; House always False via dataclass default False).
11. person_id = None — the identity layer resolves this later.
12. accession = f"H-{doc_id}-{i}" where i is the INPUT ENUMERATE POSITION
    (stable regardless of which earlier transactions were dropped).
13. txn_idx = i (same input-position index) — invariant to filter changes.
14. raw_json = JSON dump of the source Transaction as a dict.
15. Defensive: a Transaction missing a required field is skipped + logged,
    never fatal.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

# ---------------------------------------------------------------------------
# Re-export RawFiling so importers can do:
#   from arbiter.ingest.congress.normalize import RawFiling
# The edgar shape is the authoritative definition; we mirror it here with
# congress-specific typing notes where values always differ.
# ---------------------------------------------------------------------------
from typing import TypedDict


class RawFiling(TypedDict):
    """One Congress PTR transaction normalized to the shared RawFiling shape.

    Shape is intentionally identical to ``arbiter.ingest.edgar.normalize.RawFiling``
    so that ``write_filing`` works unchanged for both sources.
    """

    source: str             # always "congress"
    ticker: str
    person_id: None         # resolved by the identity layer; always None here
    person_name: str
    filing_ts: str          # tz-aware ISO-8601 UTC, disclosure (notification) date
    txn_type: str           # "P" or "S"
    txn_idx: int            # stable INPUT ENUMERATE position (not post-filter count)
    shares: None            # Congress does not disclose share count
    price: None             # Congress does not disclose per-share price
    amount_low: float       # lower bound of the disclosed amount bracket
    amount_high: float      # upper bound of the disclosed amount bracket
    is_10b5_1: bool         # always False
    is_amendment: bool      # always False at this layer
    accession: str          # synthetic: f"H-{doc_id}-{i}" (i = input position)
    raw_json: str           # json.dumps of the source Transaction fields


# ---------------------------------------------------------------------------
# Transaction dataclass — mirrors ptr_pdf.Transaction contract exactly so this
# module can be built and tested before Layer 3 (ptr_pdf.py) is authored.
# When ptr_pdf.py exists, import from there instead of redefining here.
# ---------------------------------------------------------------------------
try:
    from arbiter.ingest.congress.ptr_pdf import Transaction  # type: ignore[import]
except ModuleNotFoundError:
    # ptr_pdf.py has not been authored yet; define the contract locally so that
    # tests and the normalize layer work standalone.  Field names + types are
    # frozen by REBUILD_PLAN.md — do not diverge.
    @dataclass(frozen=True)
    class Transaction:  # type: ignore[no-redef]
        doc_id: str
        chamber: str
        member_name: str          # "Mark Alford" (Hon. already stripped by parser)
        owner: str                # "SP" | "DC" | "JT" | "SELF"
        asset_name: str
        ticker: str | None        # None if no tradeable ticker found
        asset_type: str | None    # "ST", "OP", etc.
        txn_type: str             # "P" | "S" | "E"
        is_partial: bool
        txn_date: date
        notification_date: date   # DISCLOSURE date; this is filing_ts
        amount_low: float
        amount_high: float
        is_amendment: bool = False  # Senate: True for amendment PTR reports
        clerk_receipt_date: date | None = None  # House Clerk receipt date (public-availability)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)

_KEEP_TXN_TYPES: frozenset[str] = frozenset({"P", "S"})

# Asset types that are definitively non-equity; drop even if a ticker is present.
# GS  = Government Securities (e.g. Treasuries)
# PS  = Partnership / LP units
# HN  = Hedge fund / private fund
# CS  = Corporate debt / notes
# CO  = Commodities
# OL  = Options on debt / index
# RP  = Real property
# AB  = Asset-backed securities
# CT  = Certificates of deposit / trust receipts
# MS  = Municipal Security (Senate eFD; no tradeable equity signal)
_DROP_ASSET_TYPES: frozenset[str] = frozenset(
    {"GS", "PS", "HN", "CS", "CO", "OL", "RP", "AB", "CT", "MS"}
)

# CUSIP-shape regex: exactly 9 uppercase alphanumeric characters containing ≥1 digit.
_CUSIP_RE = re.compile(r"^[0-9A-Z]{9}$")

# A real equity ticker is 1–5 uppercase letters optionally followed by a single
# dot/dash and 1–2 more letters (e.g. BRK.B, BF-B).
_VALID_TICKER_RE = re.compile(r"^[A-Z]{1,5}([.\-][A-Z]{1,2})?$")

_REQUIRED_FIELDS: tuple[str, ...] = (
    "doc_id",
    "member_name",
    "txn_type",
    "txn_date",
    "notification_date",
    "amount_low",
    "amount_high",
)

# Maximum plausible notification lag in days (>365 → likely data-entry error).
_MAX_LAG_DAYS: int = 365


def _notification_date_to_filing_ts(d: date) -> str:
    """Convert a ``date`` to a tz-aware UTC ISO-8601 string at midnight.

    e.g. date(2026, 1, 15) → "2026-01-15T00:00:00+00:00"
    """
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat()


def _txn_to_dict(txn: Transaction) -> dict:
    """Convert a Transaction to a plain dict for raw_json serialisation."""
    try:
        return asdict(txn)
    except TypeError:
        # Fallback for non-dataclass objects (e.g. plain dicts in tests)
        return dict(txn) if hasattr(txn, "items") else txn.__dict__


def amendment_referent(txn: Transaction) -> str | None:
    """Return a stable referent identifying the ORIGINAL filing an amendment corrects.

    [C3] Senate PTR amendments must supersede ONLY the original report they
    amend — not every same-``(ticker, person)`` filing in history (which would
    discard independent same-ticker trades). The Senate eFD search rows do NOT
    expose the original report's UUID, so the only reliable linkage available is
    ``(filer, disclosure_date)``: an amendment "for MM/DD/YYYY" corrects the
    original report filed by the same senator for that same disclosure date.

    Returns a string like ``"congress:John Boozman:2026-06-16"`` for amendments,
    or ``None`` for non-amendments (which never supersede). A downstream writer
    should scope amendment supersession to filings carrying the SAME referent so
    an unrelated same-ticker original survives.
    """
    if not bool(getattr(txn, "is_amendment", False)):
        return None
    person = str(getattr(txn, "member_name", "")).strip()
    disclosure = getattr(txn, "notification_date", None)
    if not person or disclosure is None:
        return None
    return f"congress:{person}:{disclosure.isoformat()}"


def _is_cusip_shaped(ticker: str) -> bool:
    """Return True if *ticker* looks like a CUSIP rather than an equity symbol.

    Two rules (OR):
    1. Exactly 9 uppercase alphanumeric chars with ≥1 digit  (classic CUSIP shape).
    2. Starts with a digit and is longer than 5 chars (catches numeric-prefixed IDs).
    """
    if _CUSIP_RE.match(ticker) and any(c.isdigit() for c in ticker):
        return True
    if ticker and ticker[0].isdigit() and len(ticker) > 5:
        return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def to_raw_filings(
    transactions: list[Transaction],
    *,
    chamber_prefix: str | None = None,
) -> list[RawFiling]:
    """Map each ``Transaction`` to a ``RawFiling``, applying filter rules.

    Parameters
    ----------
    transactions:
        List of ``Transaction`` objects produced by ``parse_ptr``
        (``arbiter.ingest.congress.ptr_pdf``) or ``senate.fetch_senate_ptrs``.
    chamber_prefix:
        Prefix for synthetic accession keys.  ``"H"`` for House (default),
        ``"S"`` for Senate.  When ``None``, inferred from ``txn.chamber``
        (``"senate"`` → ``"S"``, anything else → ``"H"``).
        House callers that do not pass this argument are unaffected.

    Returns
    -------
    A list of ``RawFiling`` dicts.  The ordering within a single doc_id is
    preserved.

    Filtering rules (applied in order)
    ------------------------------------
    - Missing required fields → skipped + logged.
    - ``asset_type`` in DROP set (GS/PS/HN/…) → dropped (non-equity instrument).
    - ``ticker is None`` → dropped; no tradeable signal.
    - ``ticker`` is CUSIP-shaped → dropped (non-equity instrument leaking through).
    - ``txn_type not in {"P","S"}`` → dropped (E = exchange has no clear direction).
    - ``amount_high < amount_low`` → skipped + logged.
    - ``notification_date < txn_date`` OR lag > 365 days → skipped + logged (corrupt dates).

    Stability guarantee
    --------------------
    ``txn_idx`` and the ``accession`` key use the INPUT ENUMERATE POSITION ``i``
    (the loop variable in ``enumerate(transactions)``), NOT a post-filter counter.
    This means re-running with a different filter set does not change the
    ``accession`` of any surviving transaction, eliminating double-write risk.
    """
    results: list[RawFiling] = []

    for i, txn in enumerate(transactions):
        # --- defensive: check required fields are present ------------------
        missing: list[str] = []
        for f in _REQUIRED_FIELDS:
            try:
                val = getattr(txn, f)
                if val is None and f not in ("ticker",):
                    missing.append(f)
            except AttributeError:
                missing.append(f)
        if missing:
            log.warning(
                "congress.normalize: skipping transaction %d — missing required fields: %s",
                i,
                missing,
            )
            continue

        # --- filter: asset-type drop set (non-equity instruments) ----------
        asset_type = getattr(txn, "asset_type", None)
        if asset_type is not None and asset_type in _DROP_ASSET_TYPES:
            log.debug(
                "congress.normalize: skipping transaction %d (doc_id=%s) — "
                "non-equity asset_type=%r",
                i,
                getattr(txn, "doc_id", "?"),
                asset_type,
            )
            continue

        # --- filter: must have a tradeable ticker ---------------------------
        if txn.ticker is None:
            log.debug(
                "congress.normalize: skipping transaction %d (doc_id=%s) — ticker is None",
                i,
                getattr(txn, "doc_id", "?"),
            )
            continue

        # --- filter: CUSIP-shaped ticker guard ------------------------------
        ticker_str = str(txn.ticker)
        if _is_cusip_shaped(ticker_str):
            log.warning(
                "congress.normalize: skipping transaction %d (doc_id=%s) — "
                "ticker %r looks like a CUSIP (non-equity)",
                i,
                getattr(txn, "doc_id", "?"),
                ticker_str,
            )
            continue

        # --- filter: only keep purchases and sales -------------------------
        if txn.txn_type not in _KEEP_TXN_TYPES:
            log.debug(
                "congress.normalize: skipping transaction %d (doc_id=%s) — txn_type=%r",
                i,
                getattr(txn, "doc_id", "?"),
                txn.txn_type,
            )
            continue

        # --- defensive: amount range sanity check --------------------------
        try:
            amount_low = float(txn.amount_low)
            amount_high = float(txn.amount_high)
        except (TypeError, ValueError) as exc:
            log.warning(
                "congress.normalize: skipping transaction %d — cannot parse amounts: %s",
                i,
                exc,
            )
            continue

        if amount_high < amount_low:
            log.warning(
                "congress.normalize: skipping transaction %d (doc_id=%s) — "
                "amount_high (%s) < amount_low (%s)",
                i,
                getattr(txn, "doc_id", "?"),
                amount_high,
                amount_low,
            )
            continue

        # --- filter: notification_date / txn_date sanity -------------------
        txn_date = txn.txn_date
        notification_date = txn.notification_date
        try:
            lag_days = (notification_date - txn_date).days
        except (TypeError, AttributeError) as exc:
            log.warning(
                "congress.normalize: skipping transaction %d — cannot compute date lag: %s",
                i,
                exc,
            )
            continue

        if lag_days < 0:
            log.warning(
                "congress.normalize: skipping transaction %d (doc_id=%s) — "
                "notification_date %s is BEFORE txn_date %s (lag=%d days)",
                i,
                getattr(txn, "doc_id", "?"),
                notification_date,
                txn_date,
                lag_days,
            )
            continue

        if lag_days > _MAX_LAG_DAYS:
            log.warning(
                "congress.normalize: skipping transaction %d (doc_id=%s) — "
                "implausible disclosure lag of %d days (max %d)",
                i,
                getattr(txn, "doc_id", "?"),
                lag_days,
                _MAX_LAG_DAYS,
            )
            continue

        # --- choose the "information available" date for filing_ts ----------
        # INTERFACES.md §3 line 106: "Congress = disclosure date" — the date the
        # filing became PUBLICLY available. For the House that is the Clerk
        # receipt date (index FilingDate), which is >= the per-row notification
        # date; using the earlier notification date would be look-ahead. When no
        # clerk date is carried (Senate: the report <h1> date already IS the
        # public date) we fall back to notification_date. We also never go
        # earlier than notification_date (defensive max).
        clerk_date = getattr(txn, "clerk_receipt_date", None)
        if clerk_date is not None and clerk_date >= notification_date:
            info_date = clerk_date
        elif clerk_date is not None:
            # Clerk date earlier than notification (corrupt/odd) — keep the later
            # of the two so we never disclose before info was public.
            info_date = notification_date
        else:
            info_date = notification_date

        # --- convert the chosen disclosure date to tz-aware UTC ISO string --
        try:
            filing_ts = _notification_date_to_filing_ts(info_date)
        except (AttributeError, TypeError, ValueError) as exc:
            log.warning(
                "congress.normalize: skipping transaction %d — bad disclosure date: %s",
                i,
                exc,
            )
            continue

        # --- synthetic accession (stable: uses INPUT position i) -----------
        doc_id = str(txn.doc_id)
        if chamber_prefix is not None:
            prefix = chamber_prefix
        else:
            prefix = "S" if getattr(txn, "chamber", "house") == "senate" else "H"
        accession = f"{prefix}-{doc_id}-{i}"

        # --- build RawFiling -----------------------------------------------
        # is_amendment: read from the Transaction when present (Senate PTRs
        # carry this flag per-report; House always False via the dataclass default).
        txn_is_amendment: bool = bool(getattr(txn, "is_amendment", False))

        raw_dict = _txn_to_dict(txn)
        # [C3] Carry the amendment referent so a writer can scope supersession to
        # the ORIGINAL report (same filer + disclosure date) instead of sweeping
        # all same-ticker history. Embedded in raw_json so the shared RawFiling
        # key-set (matched against edgar) is unchanged. None for non-amendments.
        referent = amendment_referent(txn)
        if referent is not None:
            raw_dict["amends_referent"] = referent

        filing: RawFiling = {
            "source": "congress",
            "ticker": ticker_str,
            "person_id": None,
            "person_name": str(txn.member_name),
            "filing_ts": filing_ts,
            "txn_type": txn.txn_type,
            "txn_idx": i,
            "shares": None,
            "price": None,
            "amount_low": amount_low,
            "amount_high": amount_high,
            "is_10b5_1": False,
            "is_amendment": txn_is_amendment,
            "accession": accession,
            "raw_json": json.dumps(raw_dict, default=str),
        }
        results.append(filing)

    return results


# ---------------------------------------------------------------------------
# Backward-compat shim — used by runner.py via the OLD JSON-dict pipeline
# ---------------------------------------------------------------------------

def normalize(parsed_records: list[dict]) -> list[dict]:
    """Compat shim: old-style parsed-dict records → RawFiling dicts.

    This is the normalize function imported by ``runner.py`` as
    ``_congress_normalize``.  It accepts dicts from ``parser.parse_disclosures``
    (the old JSON pipeline) and maps them to the same ``RawFiling`` shape so
    the runner writes correctly without needing a code change.

    The NEW pipeline uses ``to_raw_filings(transactions)`` directly.
    """
    results: list[dict] = []
    for i, rec in enumerate(parsed_records):
        txn_type = rec.get("txn_type", "")
        if txn_type not in _KEEP_TXN_TYPES:
            continue
        ticker = str(rec.get("ticker", "")).strip()
        if not ticker:
            continue
        try:
            amount_low = float(rec.get("amount_low", 0.0))
            amount_high = float(rec.get("amount_high", 0.0))
        except (TypeError, ValueError):
            continue
        if amount_high < amount_low:
            continue
        filing_ts = str(rec.get("filing_ts", ""))
        person_id = str(rec.get("person_id", ""))
        # Generate a deterministic accession for the old-path records
        accession = rec.get("accession") or f"CONG-{person_id}-{i}"
        result: dict = {
            "source": "congress",
            "ticker": ticker,
            "person_id": person_id,
            "person_name": str(rec.get("person_name", "")),
            "filing_ts": filing_ts,
            "txn_type": txn_type,
            "txn_idx": i,
            "shares": None,
            "price": None,
            "amount_low": amount_low,
            "amount_high": amount_high,
            "is_10b5_1": False,
            "is_amendment": bool(rec.get("is_amendment", False)),
            "accession": accession,
            "raw_json": json.dumps(rec.get("_raw", rec), default=str),
        }
        results.append(result)
    return results
