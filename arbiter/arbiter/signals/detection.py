"""Signal detection for A1 (Form 4 + Congress) — Lane 6.

Detects opportunistic buying signals from ``filings`` table rows.

Signal types
------------
- ``cluster_buy``         : ≥2 distinct insiders buying the same ticker within
                            a configurable window (default 30 days).  This is
                            the primary "edge" signal per the design doc §1.
- ``single_insider_buy``  : A single insider making a high-conviction purchase
                            (large % of holdings/net worth, no 10b5-1).
- ``congress_sector``     : A Congress member buying within a sector window.

Design rules (INTERFACES.md §11)
---------------------------------
- No ``datetime.now()``.  Callers always pass ``as_of``.
- Double-checks ``is_10b5_1 = 0`` even though upstream normalize.py already
  filters; defense-in-depth per spec §2.
- Only reads ``filings`` rows from the DB; never imports the ingest lane.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum


# ---------------------------------------------------------------------------
# Enums & constants
# ---------------------------------------------------------------------------

class SignalType(str, Enum):
    CLUSTER_BUY = "cluster_buy"
    SINGLE_INSIDER_BUY = "single_insider_buy"
    CONGRESS_SECTOR = "congress_sector"
    ACTIVIST_STAKE = "activist_stake"
    FUND_HOLDING = "fund_holding"


# Minimum number of distinct insiders for a cluster signal.
_CLUSTER_MIN_PEOPLE: int = 2

# Default cluster window in calendar days.
_CLUSTER_WINDOW_DAYS: int = 30

# Minimum conviction fraction for single-insider signal.
# (amount_low / assumed net-worth proxy; without real net-worth data we use
# a relative "large purchase" threshold based on amount alone.)
_SINGLE_MIN_AMOUNT_LOW: float = 100_000.0  # $100k minimum open-market purchase

# Hard cap on conviction for 13F fund-holding signals (13F data is stale — up
# to 45 days after quarter-end before filing deadline).
_FUND_MAX_CONVICTION: float = 0.7


# ---------------------------------------------------------------------------
# Signal dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Signal:
    """A detected trading signal.

    Attributes
    ----------
    signal_type:
        One of the :class:`SignalType` values.
    ticker:
        Target equity ticker.
    source:
        ``"form4"``, ``"congress"``, or ``"form13d"``.
    person_ids:
        Sorted tuple of person_id strings (insiders / members) who compose
        this signal.  Single-person signals have a one-element tuple.
    filing_ids:
        Sorted tuple of filing ULIDs that underlie this signal.  Used to
        compute ``source_fingerprint`` in :mod:`emit`.
    window_start:
        Earliest ``filing_ts`` in the detection window (tz-aware UTC).
    window_end:
        Latest ``filing_ts`` in the detection window (tz-aware UTC).
    conviction_score:
        Raw conviction in ``[0.0, 1.0]``.  For cluster buys this scales with
        number of participants; for single buys it scales with purchase size.
    meta:
        Optional dict of additional detection metadata (persisted as JSON).
    as_of:
        Information timestamp (tz-aware UTC).  Set by the caller to the
        latest filing_ts in the window (no look-ahead).
    """

    signal_type: SignalType
    ticker: str
    source: str  # "form4" | "congress" | "form13d"
    person_ids: tuple[str, ...]
    filing_ids: tuple[str, ...]
    window_start: datetime
    window_end: datetime
    conviction_score: float
    meta: dict = field(default_factory=dict, hash=False, compare=False)
    # Required keyword field: the caller MUST pass the information timestamp
    # (latest filing_ts in the window). No wall-clock default — that would be a
    # look-ahead hazard and violates the no-`datetime.now()` convention.
    as_of: datetime = field(kw_only=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts_str: str) -> datetime:
    """Parse a tz-aware ISO datetime string from the DB."""
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        # defensive: treat naive timestamps stored as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _cluster_conviction(n_buyers: int, max_buyers: int = 10) -> float:
    """Scale conviction by number of distinct buyers, capped at 1.0."""
    base = min(n_buyers / max_buyers, 1.0)
    # At 2 buyers → 0.20; at 5 buyers → 0.50; at 10+ → 1.0
    return round(base, 4)


def _single_conviction(amount_low: float) -> float:
    """Scale conviction by purchase size (log-normalized)."""
    import math
    if amount_low <= 0:
        return 0.0
    # log10: $100k→5, $1M→6, $10M→7.  Normalize to [0,1] with max at $10M.
    log_val = math.log10(amount_low)
    return round(min(max((log_val - 5.0) / 2.0, 0.0), 1.0), 4)


# ---------------------------------------------------------------------------
# Main detection function
# ---------------------------------------------------------------------------

def detect_signals(
    conn: sqlite3.Connection,
    as_of: datetime,
    *,
    ticker: str | None = None,
    cluster_window_days: int = _CLUSTER_WINDOW_DAYS,
    cluster_min_people: int = _CLUSTER_MIN_PEOPLE,
    single_min_amount: float = _SINGLE_MIN_AMOUNT_LOW,
) -> list[Signal]:
    """Detect trading signals from ``filings`` table rows.

    Reads filings up to ``as_of`` (no look-ahead).  Returns a list of
    :class:`Signal` objects ordered by ``window_end`` descending.

    Parameters
    ----------
    conn:
        Open SQLite connection.
    as_of:
        Information timestamp upper bound.  Only filings with
        ``filing_ts <= as_of`` are considered.
    ticker:
        If provided, restrict detection to this ticker.
    cluster_window_days:
        Rolling window in which multiple insider buys must fall to count
        as a cluster.
    cluster_min_people:
        Minimum distinct insiders required for a cluster signal.
    single_min_amount:
        Minimum ``amount_low`` (USD) for a single-insider signal.

    Returns
    -------
    List of :class:`Signal` objects (may be empty).
    """
    as_of_str = as_of.isoformat()

    # Build base WHERE clause — always exclude 10b5-1 (defense-in-depth).
    base_where = "is_superseded = 0 AND is_10b5_1 = 0 AND filing_ts <= ?"
    params_base: list = [as_of_str]

    if ticker:
        base_where += " AND ticker = ?"
        params_base.append(ticker)

    # -----------------------------------------------------------------------
    # 1. Fetch Form 4 purchase rows.
    # -----------------------------------------------------------------------
    form4_sql = (
        "SELECT id, ticker, person_id, filing_ts, txn_type, amount_low, amount_high "
        f"FROM filings WHERE {base_where} AND source = 'form4' AND txn_type = 'P' "
        "ORDER BY filing_ts ASC"
    )
    form4_rows = conn.execute(form4_sql, params_base).fetchall()

    # -----------------------------------------------------------------------
    # 2. Fetch Congress purchase rows.
    # -----------------------------------------------------------------------
    congress_sql = (
        "SELECT id, ticker, person_id, filing_ts, txn_type, amount_low, amount_high "
        f"FROM filings WHERE {base_where} AND source = 'congress' AND txn_type = 'P' "
        "ORDER BY filing_ts ASC"
    )
    congress_rows = conn.execute(congress_sql, params_base).fetchall()

    # -----------------------------------------------------------------------
    # 2b. Fetch Schedule 13D/13G (activist / passive stake) rows.
    # -----------------------------------------------------------------------
    # NB: unlike form4/congress (P-only), activist rows include BOTH 'P'
    # (acquire / increase) AND 'S' (exit) — a 13D/G exit is a valid bearish
    # signal.  ``raw_json`` is selected so the detector can read
    # schedule/percent_of_class/is_activist (the form4/congress SELECTs omit it).
    sc13_sql = (
        "SELECT id, ticker, person_id, filing_ts, txn_type, amount_low, amount_high, raw_json "
        f"FROM filings WHERE {base_where} AND source = 'form13d' AND txn_type IN ('P','S') "
        "ORDER BY filing_ts ASC"
    )
    sc13_rows = conn.execute(sc13_sql, params_base).fetchall()

    # -----------------------------------------------------------------------
    # 2c. Fetch Form 13F fund-manager delta rows.
    # -----------------------------------------------------------------------
    # 13F rows include both 'P' (new/increased position) and 'S' (reduced/exited).
    # ``raw_json`` carries ``reason``/``book_fraction``/``value_usd`` written by
    # the delta engine (form13f_normalize.py).
    form13f_sql = (
        "SELECT id, ticker, person_id, filing_ts, txn_type, amount_low, amount_high, raw_json "
        f"FROM filings WHERE {base_where} AND source = 'form13f' AND txn_type IN ('P','S') "
        "ORDER BY filing_ts ASC"
    )
    form13f_rows = conn.execute(form13f_sql, params_base).fetchall()

    signals: list[Signal] = []

    # -----------------------------------------------------------------------
    # 3. Cluster buy detection (Form 4).
    # -----------------------------------------------------------------------
    # Group rows by ticker, then slide a window looking for ≥2 distinct buyers.
    signals.extend(
        _detect_cluster_buys(
            form4_rows,
            source="form4",
            window_days=cluster_window_days,
            min_people=cluster_min_people,
            as_of=as_of,
        )
    )

    # -----------------------------------------------------------------------
    # 4. Single-insider buy detection (Form 4).
    # -----------------------------------------------------------------------
    signals.extend(
        _detect_single_insider(
            form4_rows,
            source="form4",
            min_amount=single_min_amount,
            as_of=as_of,
        )
    )

    # -----------------------------------------------------------------------
    # 5. Congress sector detection.
    # -----------------------------------------------------------------------
    signals.extend(
        _detect_congress(
            congress_rows,
            window_days=cluster_window_days,
            min_people=cluster_min_people,
            as_of=as_of,
        )
    )

    # -----------------------------------------------------------------------
    # 6. Activist / passive stake detection (Schedule 13D / 13G).
    # -----------------------------------------------------------------------
    signals.extend(_detect_activist_stake(sc13_rows, as_of=as_of))

    # -----------------------------------------------------------------------
    # 7. Fund-manager holding delta detection (Form 13F).
    # -----------------------------------------------------------------------
    signals.extend(_detect_fund_holdings(form13f_rows, as_of=as_of))

    # Sort by window_end descending (most recent first).
    signals.sort(key=lambda s: s.window_end, reverse=True)
    return signals


# ---------------------------------------------------------------------------
# Sub-detectors
# ---------------------------------------------------------------------------

def _detect_cluster_buys(
    rows: list,
    *,
    source: str,
    window_days: int,
    min_people: int,
    as_of: datetime,
) -> list[Signal]:
    """Detect cluster buy signals from a list of DB filing rows."""
    # Group by ticker.
    by_ticker: dict[str, list] = {}
    for row in rows:
        by_ticker.setdefault(row["ticker"], []).append(row)

    results: list[Signal] = []

    for ticker_sym, ticker_rows in by_ticker.items():
        # Sort by filing_ts ascending (already sorted from SQL, but be safe).
        ticker_rows_sorted = sorted(ticker_rows, key=lambda r: r["filing_ts"])

        # Sliding window: for each row i find all rows j where
        # filing_ts[j] - filing_ts[i] <= window_days.
        n = len(ticker_rows_sorted)
        for i in range(n):
            ts_i = _parse_ts(ticker_rows_sorted[i]["filing_ts"])
            window_end_ts = ts_i + timedelta(days=window_days)

            # Collect all rows within the window starting at i.
            window = [ticker_rows_sorted[i]]
            for j in range(i + 1, n):
                ts_j = _parse_ts(ticker_rows_sorted[j]["filing_ts"])
                if ts_j <= window_end_ts:
                    window.append(ticker_rows_sorted[j])
                else:
                    break

            # Check distinct people threshold.
            people_in_window = set(r["person_id"] for r in window)
            if len(people_in_window) < min_people:
                continue

            # Build signal.
            latest_ts = _parse_ts(window[-1]["filing_ts"])
            if latest_ts > as_of:
                continue  # no look-ahead

            filing_ids = tuple(sorted(r["id"] for r in window))
            person_ids = tuple(sorted(people_in_window))

            # Dedup: skip if we already emitted a signal with these exact filings.
            if any(
                s.filing_ids == filing_ids and s.signal_type == SignalType.CLUSTER_BUY
                for s in results
            ):
                continue

            conviction = _cluster_conviction(len(people_in_window))

            results.append(
                Signal(
                    signal_type=SignalType.CLUSTER_BUY,
                    ticker=ticker_sym,
                    source=source,
                    person_ids=person_ids,
                    filing_ids=filing_ids,
                    window_start=ts_i,
                    window_end=latest_ts,
                    conviction_score=conviction,
                    meta={
                        "n_buyers": len(people_in_window),
                        "window_days": window_days,
                    },
                    as_of=as_of,
                )
            )

    return results


def _detect_single_insider(
    rows: list,
    *,
    source: str,
    min_amount: float,
    as_of: datetime,
) -> list[Signal]:
    """Detect single-insider high-conviction buys."""
    results: list[Signal] = []

    for row in rows:
        amount_low = float(row["amount_low"] or 0.0)
        if amount_low < min_amount:
            continue

        ts = _parse_ts(row["filing_ts"])
        if ts > as_of:
            continue  # no look-ahead

        conviction = _single_conviction(amount_low)
        results.append(
            Signal(
                signal_type=SignalType.SINGLE_INSIDER_BUY,
                ticker=row["ticker"],
                source=source,
                person_ids=(row["person_id"],),
                filing_ids=(row["id"],),
                window_start=ts,
                window_end=ts,
                conviction_score=conviction,
                meta={"amount_low": amount_low},
                as_of=as_of,
            )
        )

    return results


def _detect_congress(
    rows: list,
    *,
    window_days: int,
    min_people: int,
    as_of: datetime,
) -> list[Signal]:
    """Detect Congress sector buy signals (≥min_people members buying same ticker)."""
    # Reuse cluster logic on congress rows (same ticker grouping).
    return _detect_cluster_buys(
        rows,
        source="congress",
        window_days=window_days,
        min_people=min_people,
        as_of=as_of,
    )


def _detect_fund_holdings(rows: list, *, as_of: datetime) -> list[Signal]:
    """One signal per 13F delta row.

    Conviction = event-cleanliness base + concentration boost, hard-capped at
    ``_FUND_MAX_CONVICTION`` (13F data is stale — can be up to 45 days after
    quarter-end).  ``txn_type`` carried in ``meta`` so :mod:`emit` can set the
    stance SIGN without re-querying.  No look-ahead: rows with
    ``filing_ts > as_of`` are dropped.
    """
    results: list[Signal] = []
    for row in rows:
        ts = _parse_ts(row["filing_ts"])
        if ts > as_of:
            continue  # no look-ahead
        meta = json.loads(row["raw_json"]) if row["raw_json"] else {}
        reason = meta.get("reason", "add")
        book_frac = float(meta.get("book_fraction") or 0.0)
        # clean new/exit are the strongest; add/trim a notch lower
        base = 0.45 if reason in ("new", "exit", "first_filing_topk") else 0.30
        boost = min(book_frac / 0.10, 1.0) * 0.25      # 10%+ of book => full boost
        conviction = round(min(base + boost, _FUND_MAX_CONVICTION), 4)
        results.append(
            Signal(
                signal_type=SignalType.FUND_HOLDING,
                ticker=row["ticker"],
                source="form13f",
                person_ids=(row["person_id"],),
                filing_ids=(row["id"],),
                window_start=ts,
                window_end=ts,
                conviction_score=conviction,
                meta={
                    "txn_type": row["txn_type"],
                    "reason": reason,
                    "book_fraction": book_frac,
                },
                as_of=as_of,
            )
        )
    return results


def _detect_activist_stake(rows: list, *, as_of: datetime) -> list[Signal]:
    """Detect activist / passive stake signals from Schedule 13D/13G rows.

    A single 13D/13G filing is itself a signal (no clustering): a large
    beneficial-ownership disclosure is high-conviction on its own.  Conviction
    is keyed by ``schedule`` (13D activist > 13G passive) and boosted by the
    disclosed ``percent_of_class``.  The ``txn_type`` ('P' acquire / 'S' exit)
    is carried in ``meta`` so :mod:`emit` can set the stance SIGN without
    re-querying.  No look-ahead: rows with ``filing_ts > as_of`` are dropped.
    """
    results: list[Signal] = []
    for row in rows:
        ts = _parse_ts(row["filing_ts"])
        if ts > as_of:
            continue  # no look-ahead
        meta = json.loads(row["raw_json"]) if row["raw_json"] else {}
        pct = meta.get("percent_of_class")
        is_activist = bool(meta.get("is_activist", False))
        schedule = meta.get("schedule", "13G")
        # base conviction by schedule; sign carried via txn_type downstream in emit.
        base = 0.70 if is_activist else 0.35
        boost = min((pct or 0.0) / 50.0, 0.30)
        conviction = round(min(base + boost, 1.0), 4)
        results.append(
            Signal(
                signal_type=SignalType.ACTIVIST_STAKE,
                ticker=row["ticker"],
                source="form13d",
                person_ids=(row["person_id"],),
                filing_ids=(row["id"],),
                window_start=ts,
                window_end=ts,
                conviction_score=conviction,
                meta={
                    "schedule": schedule,
                    "percent_of_class": pct,
                    "is_activist": is_activist,
                    "txn_type": row["txn_type"],
                },
                as_of=as_of,
            )
        )
    return results
