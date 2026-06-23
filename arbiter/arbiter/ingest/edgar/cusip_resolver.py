# arbiter/arbiter/ingest/edgar/cusip_resolver.py
"""Resolve a 13F CUSIP to a tradeable US-equity ticker, safety-first.

Order: cusip_map cache -> megacap seed -> exact issuer-name match against the
Alpaca tradeable US-equity asset list.  Anything not resolved with high
confidence is DROPPED (returns None) and never traded.  Confident resolutions
are cached in cusip_map so the map grows.
"""
from __future__ import annotations
import sqlite3
from typing import Callable
import structlog
from arbiter.db.helpers import generate_ulid  # noqa: F401  (ULID not needed; cusip is PK)

log = structlog.get_logger(__name__)

_TRUST = 0.9  # min confidence trusted for trading

# Hand-seeded megacap CUSIP -> ticker (verified). Extend as needed.
_SEED: dict[str, str] = {
    "67066G104": "NVDA",  # NVIDIA
    "037833100": "AAPL",  # Apple
    "023135106": "AMZN",  # Amazon
    "594918104": "MSFT",  # Microsoft
    "88160R101": "TSLA",  # Tesla
    "02079K305": "GOOGL", # Alphabet A
    "30303M102": "META",  # Meta
}

def _cache_get(conn: sqlite3.Connection, cusip: str) -> str | None:
    row = conn.execute(
        "SELECT ticker, confidence FROM cusip_map WHERE cusip = ?", (cusip,)
    ).fetchone()
    if row and row["confidence"] >= _TRUST:
        return row["ticker"]
    return None

def _cache_put(conn, cusip, ticker, issuer_name, source, confidence, now_iso):
    conn.execute(  # insert-only-ok — cusip_map is a resolution CACHE, not trade/ledger state
        "INSERT OR REPLACE INTO cusip_map "
        "(cusip, ticker, issuer_name, source, confidence, resolved_at) "
        "VALUES (?,?,?,?,?,?)",
        (cusip, ticker, issuer_name, source, confidence, now_iso),
    )
    conn.commit()

def resolve_cusip(
    conn: sqlite3.Connection,
    cusip: str,
    issuer_name: str,
    *,
    asset_lookup: Callable[[], dict[str, str]],
    now_iso: str,
) -> str | None:
    cusip = (cusip or "").strip().upper()
    if not cusip:
        return None
    # 1. cache
    cached = _cache_get(conn, cusip)
    if cached:
        return cached
    # 2. seed
    if cusip in _SEED:
        t = _SEED[cusip]
        _cache_put(conn, cusip, t, issuer_name, "seed", 1.0, now_iso)
        return t
    # 3. exact issuer-name match against tradeable assets
    name = (issuer_name or "").strip().upper()
    if name:
        assets = asset_lookup() or {}
        t = assets.get(name)
        if t:
            _cache_put(conn, cusip, t, issuer_name, "alpaca_name", 0.9, now_iso)
            return t
    log.info("cusip.unresolved", cusip=cusip, issuer=issuer_name)
    return None  # drop, never guess
