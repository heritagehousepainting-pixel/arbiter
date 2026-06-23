# arbiter/arbiter/ingest/edgar/form13f_normalize.py
"""Store 13F holdings snapshots and compute quarter-over-quarter deltas.

A delta becomes a form13f RawFiling row (txn_type 'P' bullish / 'S' bearish).
First filing for a manager -> top-K conviction snapshot (new positions for the
K most-concentrated holdings).  Outright SHARE holdings only (puts/calls stored
but never produce deltas).  Noise floors and PIT (filing_ts = filing_date) per
the spec.  Unresolvable CUSIPs are dropped (never stored, never traded).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Callable

import structlog

from arbiter.config import Config
from arbiter.db.helpers import generate_ulid
from arbiter.ingest.edgar.cusip_resolver import resolve_cusip
from arbiter.ingest.edgar.normalize import RawFiling

log = structlog.get_logger(__name__)


def store_holdings(
    conn: sqlite3.Connection,
    person_id: str,
    accession: str,
    filing_date: str,
    report_date: str,
    holdings: list[dict],
    *,
    asset_lookup: Callable[[], dict],
    now_iso: str,
) -> int:
    """Resolve CUSIPs, insert resolvable outright-share rows into form13f_holdings.

    Idempotent (INSERT OR IGNORE on UNIQUE(person_id, accession, cusip, put_call)).
    Returns count of rows actually inserted (not skipped by UNIQUE constraint).
    Unresolvable CUSIPs are dropped and not counted.
    """
    stored = 0
    for h in holdings:
        put_call = h.get("put_call")
        if put_call:
            # Options: store for completeness, but we never produce deltas for them.
            ticker = None
        else:
            ticker = resolve_cusip(
                conn,
                h["cusip"],
                h.get("issuer_name", ""),
                asset_lookup=asset_lookup,
                now_iso=now_iso,
            )
            if ticker is None:
                # Unresolved outright-share holding -> drop entirely, never trade.
                log.debug("form13f.cusip_unresolved", cusip=h.get("cusip"),
                          issuer=h.get("issuer_name"))
                continue

        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO form13f_holdings "
                "(id, person_id, accession, filing_date, report_date, cusip, ticker, "
                " issuer_name, value_usd, shares, put_call, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    generate_ulid(),
                    person_id,
                    accession,
                    filing_date,
                    report_date,
                    h["cusip"],
                    ticker,
                    h.get("issuer_name"),
                    float(h.get("value_usd", 0)),
                    float(h.get("shares", 0)),
                    put_call,
                    now_iso,
                ),
            )
            # rowcount=1 on actual insert, 0 when UNIQUE row already exists.
            if cur.rowcount and cur.rowcount > 0:
                stored += 1
        except sqlite3.IntegrityError:
            pass

    conn.commit()
    return stored


# ---------------------------------------------------------------------------
# Delta engine helpers
# ---------------------------------------------------------------------------

def _book_total(rows: list[sqlite3.Row]) -> float:
    """Return total book value; guard against divide-by-zero."""
    return sum(r["value_usd"] for r in rows) or 1.0


def _raw(
    person_id: str,
    ticker: str,
    filing_date: str,
    txn_type: str,
    accession: str,
    meta: dict,
) -> RawFiling:
    """Build a form13f RawFiling dict.  PIT: filing_ts is always the FILING date."""
    return {
        "source": "form13f",
        "ticker": ticker,
        "person_id": person_id,
        "person_name": "",      # people row already exists upstream; leave empty here
        "filing_ts": filing_date,  # PIT: never the report_date
        "txn_type": txn_type,
        "txn_idx": 0,
        "shares": float(meta.get("shares", 0.0)),
        "price": None,
        "amount_low": None,
        "amount_high": None,
        "is_10b5_1": False,
        "is_amendment": False,
        "accession": accession,
        "raw_json": json.dumps(meta, default=str),
    }


def _finalize_txn_idx(out: list[RawFiling]) -> list[RawFiling]:
    """Assign a UNIQUE, STABLE txn_idx to each delta of a filing.

    All deltas from one 13F share an ``accession``; ``write_filing`` dedups by
    ``(accession, txn_idx)``, so without a distinct txn_idx every ticker from a
    single filing would collapse to ONE row.  Sorting by ticker makes the index
    deterministic across re-ingests, preserving idempotency.
    """
    out.sort(key=lambda r: r["ticker"])
    for i, r in enumerate(out):
        r["txn_idx"] = i
    return out


def compute_deltas(
    conn: sqlite3.Connection,
    person_id: str,
    report_date: str,
    *,
    config: Config,
) -> list[RawFiling]:
    """Diff this report_date snapshot vs the prior quarter and return RawFiling rows.

    First filing (no prior quarter) -> top-K conviction snapshot as new 'P' rows.
    Outright shares only (put_call IS NULL).  Noise floors applied.
    """
    cur_rows = conn.execute(
        "SELECT * FROM form13f_holdings "
        "WHERE person_id=? AND report_date=? AND put_call IS NULL",
        (person_id, report_date),
    ).fetchall()
    if not cur_rows:
        return []

    accession = cur_rows[0]["accession"]
    filing_date = cur_rows[0]["filing_date"]
    book = _book_total(cur_rows)

    min_usd = config.form13f_min_position_usd
    min_frac = config.form13f_min_book_fraction
    min_delta = config.form13f_min_delta_fraction

    def passes_floor(value_usd: float) -> bool:
        return value_usd >= min_usd and (value_usd / book) >= min_frac

    # Find the most recent prior report_date for this manager.
    prior_rd_row = conn.execute(
        "SELECT report_date FROM form13f_holdings "
        "WHERE person_id=? AND report_date<? "
        "ORDER BY report_date DESC LIMIT 1",
        (person_id, report_date),
    ).fetchone()

    out: list[RawFiling] = []

    if prior_rd_row is None:
        # FIRST filing -> emit top-K most-concentrated floor-passing holdings as new 'P'.
        ranked = sorted(
            [r for r in cur_rows if passes_floor(r["value_usd"])],
            key=lambda r: r["value_usd"],
            reverse=True,
        )
        for r in ranked[: config.form13f_first_filing_top_k]:
            out.append(
                _raw(
                    person_id, r["ticker"], filing_date, "P", accession,
                    {
                        "reason": "first_filing_topk",
                        "value_usd": r["value_usd"],
                        "book_fraction": r["value_usd"] / book,
                        "shares": r["shares"],
                        "report_date": report_date,
                    },
                )
            )
        return _finalize_txn_idx(out)

    # Subsequent filing: diff against prior quarter.
    prior_rows = conn.execute(
        "SELECT * FROM form13f_holdings "
        "WHERE person_id=? AND report_date=? AND put_call IS NULL",
        (person_id, prior_rd_row["report_date"]),
    ).fetchall()

    prior: dict[str, sqlite3.Row] = {r["ticker"]: r for r in prior_rows}
    now_map: dict[str, sqlite3.Row] = {r["ticker"]: r for r in cur_rows}

    all_tickers = set(now_map) | set(prior)
    for t in all_tickers:
        if t is None:
            continue

        p = prior.get(t)
        n = now_map.get(t)
        p_sh = float(p["shares"]) if p else 0.0
        n_sh = float(n["shares"]) if n else 0.0

        # Use the current row's value for floor check; fall back to prior row.
        value_for_floor = float((n or p)["value_usd"])
        if not passes_floor(value_for_floor):
            continue

        if p_sh == 0 and n_sh > 0:          # brand new position
            txn = "P"
            reason = "new"
        elif p_sh > 0 and n_sh == 0:         # fully exited
            txn = "S"
            reason = "exit"
        elif p_sh > 0:
            change = (n_sh - p_sh) / p_sh
            if change >= min_delta:
                txn = "P"
                reason = "add"
            elif change <= -min_delta:
                txn = "S"
                reason = "trim"
            else:
                continue                     # flat / tiny nibble -> no signal
        else:
            continue

        out.append(
            _raw(
                person_id, t, filing_date, txn, accession,
                {
                    "reason": reason,
                    "value_usd": value_for_floor,
                    "book_fraction": value_for_floor / book,
                    "shares": n_sh,
                    "report_date": report_date,
                },
            )
        )

    return _finalize_txn_idx(out)
