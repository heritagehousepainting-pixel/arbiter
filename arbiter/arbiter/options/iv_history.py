"""IV history store and IV-rank computation.

Design
------
Alpaca's ``indicative`` snapshot provides *current* IV but no 52-week history.
We accumulate daily ATM-IV snapshots in ``option_iv_history`` ourselves so that
a proper IV-rank gate (IVR < 0.40, config: ``option_ivr_max``) is computable by
P2 from data we have collected.

For P1 cold-start (before history accumulates), ``iv_rank()`` falls back to
``realized_vol_proxy()``.

Call pattern (engine cycle)
---------------------------
1. ``record_iv_snapshot(conn, client, underlying, as_of)`` — once per
   underlying per full cycle; writes one row to ``option_iv_history``.
2. ``iv_rank(conn, underlying)`` — returns IVR ∈ [0, 1] if ≥ 30 days of
   history exist, else None (→ fall back to realized_vol_proxy).
3. ``realized_vol_proxy(conn, underlying, *, window_days)`` — always
   computable from equity bar data; used as cold-start IV-cheapness proxy.
"""
from __future__ import annotations

import datetime
import logging
import math
import sqlite3
from typing import Optional

from arbiter.db.helpers import generate_ulid, insert_row
from arbiter.options.alpaca_options_client import AlpacaOptionsClient
from arbiter.options.types import IVHistoryRow, OptionSide

log = logging.getLogger(__name__)

# Minimum number of close prices before realized vol is computable.
_MIN_BARS_FOR_RVOL = 5

# Calendar-day buffer when querying for bars (covers weekends + holidays).
_CALENDAR_BUFFER_FACTOR = 2


def record_iv_snapshot(
    conn: sqlite3.Connection,
    client: AlpacaOptionsClient,
    underlying: str,
    as_of: str,
    *,
    min_dte: int = 60,
) -> Optional[str]:
    """Fetch the current ATM IV for ``underlying`` and persist to ``option_iv_history``.

    Selects the nearest-ATM contract with DTE ≥ ``min_dte`` using
    ``client.fetch_chain()``, then writes one ``IVHistoryRow`` via
    ``insert_row(conn, "option_iv_history", row.to_dict())``.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open arbiter DB connection.
    client : AlpacaOptionsClient
        Initialised options data client.
    underlying : str
        Equity ticker to snapshot.
    as_of : str
        Tz-aware UTC ISO timestamp for the snapshot (injected by engine clock).
    min_dte : int
        Minimum days-to-expiry for the ATM proxy contract (default 60).

    Returns
    -------
    str | None
        ULID of the inserted row, or None when no usable contract found.
    """
    now_date = _parse_date_from_iso(as_of)
    if now_date is None:
        log.warning("iv_history.record_iv_snapshot: cannot parse as_of=%s", as_of)
        return None

    min_expiry = now_date + datetime.timedelta(days=min_dte)
    # Cap max expiry at 2 years to avoid illiquid far-dated contracts.
    max_expiry = now_date + datetime.timedelta(days=min_dte + 365)

    # Fetch calls near ATM (we use calls as the ATM IV proxy — convention).
    try:
        contracts = client.fetch_chain(
            underlying,
            min_expiry=min_expiry,
            max_expiry=max_expiry,
            side=OptionSide.CALL,
            limit=100,
        )
    except Exception:  # noqa: BLE001
        log.warning(
            "iv_history.record_iv_snapshot: fetch_chain failed underlying=%s",
            underlying,
        )
        return None

    # Filter to contracts with non-null IV.
    usable = [c for c in contracts if c.iv is not None]
    if not usable:
        log.debug(
            "iv_history.record_iv_snapshot: no usable IV for underlying=%s",
            underlying,
        )
        return None

    # Find the contract nearest ATM: minimise |strike - current_price|.
    # We don't have the live price here so use the lowest delta > 0 as a proxy
    # for "nearest ATM from the available set" — or fall back to selecting by
    # smallest absolute delta distance from 0.5 (ATM call delta ≈ 0.5).
    # If deltas are all None, pick the contract with the lowest-magnitude strike
    # relative to the median strike in the chain.
    atm_contract = _select_atm_contract(usable)
    if atm_contract is None or atm_contract.iv is None:
        return None

    row = IVHistoryRow(
        id=generate_ulid(),
        underlying=underlying,
        as_of=as_of,
        atm_iv=atm_contract.iv,
        occ_symbol=atm_contract.occ_symbol,
        created_at=as_of,
    )
    try:
        row_id = insert_row(conn, "option_iv_history", row.to_dict())
    except Exception:  # noqa: BLE001
        log.warning(
            "iv_history.record_iv_snapshot: insert_row failed underlying=%s",
            underlying,
        )
        return None

    log.debug(
        "iv_history.record_iv_snapshot: wrote iv=%.4f underlying=%s occ=%s",
        atm_contract.iv,
        underlying,
        atm_contract.occ_symbol,
    )
    return row_id


def iv_rank(
    conn: sqlite3.Connection,
    underlying: str,
    *,
    as_of: Optional[datetime.date] = None,
    lookback_days: int = 252,
    min_history_days: int = 30,
) -> Optional[float]:
    """Return the IV rank of ``underlying`` ∈ [0, 1], or None if insufficient data.

    IV rank = (current_iv - min_iv_in_window) / (max_iv_in_window - min_iv_in_window)

    Queries ``option_iv_history`` for rows in the past ``lookback_days`` days.
    Returns None when fewer than ``min_history_days`` rows exist (cold-start
    condition; callers should fall back to ``realized_vol_proxy``).

    Parameters
    ----------
    conn : sqlite3.Connection
        Open arbiter DB connection.
    underlying : str
        Equity ticker.
    as_of : datetime.date | None
        Reference date for the lookback window (defaults to today).
    lookback_days : int
        Number of calendar days to look back (default 252 ~ 1 trading year).
    min_history_days : int
        Minimum number of daily rows required before returning a rank
        (default 30; returns None below this threshold).

    Returns
    -------
    float | None
        IV rank ∈ [0, 1] or None when insufficient history.
    """
    ref_date = as_of or _latest_iv_as_of(conn, underlying)
    if ref_date is None:
        return None
    window_start = ref_date - datetime.timedelta(days=lookback_days)

    # Query all ATM-IV rows in the lookback window, ordered newest-first.
    try:
        rows = conn.execute(
            """
            SELECT atm_iv
            FROM option_iv_history
            WHERE underlying = ?
              AND as_of >= ?
              AND as_of <= ?
            ORDER BY as_of DESC
            """,
            (underlying, window_start.isoformat(), ref_date.isoformat() + "T23:59:59Z"),
        ).fetchall()
    except Exception:  # noqa: BLE001
        log.warning("iv_history.iv_rank: query failed underlying=%s", underlying)
        return None

    if len(rows) < min_history_days:
        log.debug(
            "iv_history.iv_rank: cold-start underlying=%s rows=%d need=%d",
            underlying,
            len(rows),
            min_history_days,
        )
        return None

    iv_values = [float(r[0]) for r in rows if r[0] is not None]
    if not iv_values:
        return None

    current_iv = iv_values[0]  # newest first
    min_iv = min(iv_values)
    max_iv = max(iv_values)

    iv_range = max_iv - min_iv
    if iv_range == 0.0:
        # All IVs are identical — rank is undefined; return 0.5 (neutral).
        return 0.5

    rank = (current_iv - min_iv) / iv_range
    # Clamp to [0, 1] defensively (floating-point edge cases).
    return max(0.0, min(1.0, rank))


def realized_vol_proxy(
    conn: sqlite3.Connection,
    underlying: str,
    *,
    window_days: int = 30,
    as_of: Optional[datetime.date] = None,
) -> Optional[float]:
    """Return realized volatility (annualised) as an IV-cheapness proxy.

    Uses the ``option_iv_history`` close-price column when bars aren't in a
    dedicated table.  In this codebase the equity bar data lives in
    ``AlpacaPriceSource`` (a live HTTP source) rather than in SQLite — so this
    function queries the ``option_iv_history`` table itself as a secondary
    source, or returns None gracefully when the DB has no usable price data.

    Concretely: if the repo ever adds a ``bars`` or ``equity_prices`` SQLite
    table this function can be extended to query it.  For now it performs a
    best-effort search of the existing DB schema for a close-price column,
    and returns None (never raises) when the data is unavailable.

    Algorithm
    ---------
    1. Query the last ``window_days`` calendar-days of ATM-IV rows for
       ``underlying`` from ``option_iv_history``.
    2. Use ``atm_iv`` as a noisy log-return proxy (delta of IV levels).
       **OR** — if a ``bars`` / ``equity_prices`` table exists with a
       ``close`` column — use those closes for proper log-return computation.
    3. Return ``stdev(log_returns) × sqrt(252)``.
    4. Return None if fewer than ``_MIN_BARS_FOR_RVOL`` observations.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open arbiter DB connection.
    underlying : str
        Equity ticker.
    window_days : int
        Rolling window in calendar days (default 30).
    as_of : datetime.date | None
        Reference end date (defaults to today).

    Returns
    -------
    float | None
        Annualised realized volatility, or None if insufficient data.
    """
    ref_date = as_of or _latest_iv_as_of(conn, underlying)
    if ref_date is None:
        return None
    window_start = ref_date - datetime.timedelta(days=window_days * _CALENDAR_BUFFER_FACTOR)

    # --- Try a dedicated equity price table first (future-proof) ---
    closes = _query_equity_closes(conn, underlying, window_start, ref_date)

    # --- Fall back to ATM-IV as a noisy proxy when no closes available ---
    if len(closes) < _MIN_BARS_FOR_RVOL:
        closes = _query_iv_as_proxy(conn, underlying, window_start, ref_date)

    if len(closes) < _MIN_BARS_FOR_RVOL:
        log.debug(
            "iv_history.realized_vol_proxy: insufficient data underlying=%s closes=%d",
            underlying,
            len(closes),
        )
        return None

    # Compute log returns and annualised stdev.
    return _annualised_stdev(closes)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _select_atm_contract(contracts: list) -> object | None:
    """Select the contract nearest ATM from a list with non-null IV.

    Prefers contracts with delta closest to 0.5 (ATM call delta).
    When all deltas are None, picks the median strike.
    """
    from arbiter.options.types import OptionContract

    usable: list[OptionContract] = [c for c in contracts if c.iv is not None]
    if not usable:
        return None

    # Prefer contracts with delta data — sort by |delta - 0.5|.
    with_delta = [c for c in usable if c.delta is not None]
    if with_delta:
        return min(with_delta, key=lambda c: abs(c.delta - 0.5))  # type: ignore[operator]

    # Fallback: pick median strike from the available set.
    strikes = sorted(c.strike for c in usable)
    median_strike = strikes[len(strikes) // 2]
    return min(usable, key=lambda c: abs(c.strike - median_strike))


def _parse_date_from_iso(iso_ts: str) -> datetime.date | None:
    """Extract the date part from a tz-aware ISO timestamp string."""
    try:
        # Handle both "2026-06-25" and "2026-06-25T12:00:00Z" forms.
        date_part = iso_ts[:10]
        return datetime.date.fromisoformat(date_part)
    except (ValueError, TypeError):
        return None


def _latest_iv_as_of(conn: sqlite3.Connection, underlying: str) -> datetime.date | None:
    """Most-recent recorded snapshot date for ``underlying`` (data-driven "now").

    Used as the reference date when a caller omits ``as_of`` — the system never
    reads wall-clock time outside the injected clock (no-lookahead rule). Returns
    None when there is no history (→ the IV functions return None / cold-start).
    """
    try:
        row = conn.execute(
            "SELECT MAX(as_of) FROM option_iv_history WHERE underlying = ?",
            (underlying,),
        ).fetchone()
    except sqlite3.Error:
        return None
    return _parse_date_from_iso(row[0]) if row and row[0] else None


def _query_equity_closes(
    conn: sqlite3.Connection,
    underlying: str,
    window_start: datetime.date,
    ref_date: datetime.date,
) -> list[float]:
    """Query a ``bars`` or ``equity_prices`` table if it exists.

    Returns closes sorted ascending by date, or [] if no such table exists.
    """
    # Check for table names the repo might use for equity bars.
    _CANDIDATE_TABLES = [
        ("bars", "close", "ticker", "timestamp"),
        ("equity_prices", "close", "ticker", "date"),
        ("equity_bars", "close", "ticker", "timestamp"),
    ]
    try:
        existing_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    except Exception:  # noqa: BLE001
        return []

    for table, close_col, ticker_col, date_col in _CANDIDATE_TABLES:
        if table not in existing_tables:
            continue
        try:
            rows = conn.execute(
                f"""
                SELECT {close_col}
                FROM {table}
                WHERE {ticker_col} = ?
                  AND {date_col} >= ?
                  AND {date_col} <= ?
                ORDER BY {date_col} ASC
                """,  # noqa: S608
                (underlying, window_start.isoformat(), ref_date.isoformat() + "T23:59:59Z"),
            ).fetchall()
            closes = [float(r[0]) for r in rows if r[0] is not None and float(r[0]) > 0]
            if closes:
                return closes
        except Exception:  # noqa: BLE001
            continue

    return []


def _query_iv_as_proxy(
    conn: sqlite3.Connection,
    underlying: str,
    window_start: datetime.date,
    ref_date: datetime.date,
) -> list[float]:
    """Use stored ATM-IV levels as a noisy close-price proxy for rvol.

    This is a P1 cold-start fallback only: we treat the IV series as a
    synthetic price series and compute its own "realized vol", which is a
    second-order measure but better than returning None when the gate needs
    a signal.
    """
    try:
        rows = conn.execute(
            """
            SELECT atm_iv
            FROM option_iv_history
            WHERE underlying = ?
              AND as_of >= ?
              AND as_of <= ?
            ORDER BY as_of ASC
            """,
            (underlying, window_start.isoformat(), ref_date.isoformat() + "T23:59:59Z"),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return []

    return [float(r[0]) for r in rows if r[0] is not None and float(r[0]) > 0]


def _annualised_stdev(closes: list[float]) -> float | None:
    """Compute annualised stdev of log returns from a list of close prices.

    Requires ≥ 2 values (to compute at least one log return).
    Returns None on any numeric error.
    """
    if len(closes) < 2:
        return None
    try:
        log_returns = [
            math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
            if closes[i] > 0 and closes[i - 1] > 0
        ]
        if len(log_returns) < _MIN_BARS_FOR_RVOL - 1:
            return None
        n = len(log_returns)
        mean = sum(log_returns) / n
        variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
        return math.sqrt(variance) * math.sqrt(252)
    except (ZeroDivisionError, ValueError, OverflowError):
        return None
