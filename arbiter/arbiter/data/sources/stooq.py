"""Stooq CSV price source — Lane 3 Wave-B fallback client.

Implements ``PriceSource`` (INTERFACES.md §3).

Stooq provides free daily OHLCV data via a CSV download URL:

    https://stooq.com/q/d/l/?s={ticker}&d1={YYYYMMDD}&d2={YYYYMMDD}&i=d

This is the **backup / delisted-coverage** source.  It is NOT yfinance.

As-of semantics
---------------
Same contract as ``AlpacaPriceSource``: bars whose timestamps are ≥ end are
dropped before returning.

Survivorship
------------
Delisted tickers are passed through.  Stooq may return an empty CSV or
a CSV with the single header row "No data"; both map to an empty list.

Network
-------
Uses ``requests`` (simple GET).  No authentication required.

Spec references
---------------
INTERFACES.md §3, §11 convention 1.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone

import requests

from arbiter.data.pit import Bar

log = logging.getLogger(__name__)

_STOOQ_URL = "https://stooq.com/q/d/l/"
_DATE_FMT = "%Y%m%d"
_BAR_DATE_FMT = "%Y-%m-%d"

# Stooq CSV columns (lowercase after normalisation).
_COL_DATE = "date"
_COL_OPEN = "open"
_COL_HIGH = "high"
_COL_LOW = "low"
_COL_CLOSE = "close"
_COL_VOLUME = "volume"


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_stooq_symbol(ticker: str) -> str:
    """Map a US exchange ticker to the Stooq symbol format.

    Stooq requires a market suffix (``AAPL`` → ``AAPL.US``).  The bare ticker
    handed in from production returns "bars not found", which silently kills
    the Alpaca→Stooq fallback (audit C4).  We append ``.US`` only when the
    symbol carries no exchange suffix, so callers that already pass
    ``"AAPL.US"`` (or any ``X.Y`` form) are left untouched.

    Examples
    --------
    >>> _to_stooq_symbol("AAPL")
    'AAPL.US'
    >>> _to_stooq_symbol("AAPL.US")
    'AAPL.US'
    >>> _to_stooq_symbol("brk.b")  # already dotted — pass through
    'brk.b'
    """
    if "." in ticker:
        return ticker
    return f"{ticker}.US"


def _parse_stooq_csv(ticker: str, csv_text: str, end_utc: datetime) -> list[Bar]:
    """Parse Stooq CSV text into a list of ``Bar`` objects.

    Bars at or after *end_utc* are excluded (look-ahead guard).
    Rows with missing or non-numeric prices are skipped.
    """
    bars: list[Bar] = []
    reader = csv.DictReader(io.StringIO(csv_text))

    if reader.fieldnames is None:
        return []

    # Normalise header names to lowercase and strip whitespace.
    fieldnames_lower = [f.strip().lower() for f in reader.fieldnames]

    required = {_COL_DATE, _COL_OPEN, _COL_HIGH, _COL_LOW, _COL_CLOSE, _COL_VOLUME}
    if not required.issubset(set(fieldnames_lower)):
        log.warning(
            "stooq_csv_unexpected_headers ticker=%s headers=%s",
            ticker,
            reader.fieldnames,
        )
        return []

    # Build a mapping from normalised column name → original column name.
    col_map = {
        lower: orig
        for lower, orig in zip(fieldnames_lower, reader.fieldnames)
    }

    for row in reader:
        try:
            date_str: str = row[col_map[_COL_DATE]].strip()
            if not date_str or date_str.lower() == "no data":
                continue

            # Stooq date format: YYYY-MM-DD
            bar_date = datetime.strptime(date_str, _BAR_DATE_FMT).replace(
                tzinfo=timezone.utc
            )

            # Look-ahead guard: skip bars at or after end.
            if bar_date >= end_utc:
                continue

            open_p = float(row[col_map[_COL_OPEN]].strip())
            high_p = float(row[col_map[_COL_HIGH]].strip())
            low_p = float(row[col_map[_COL_LOW]].strip())
            close_p = float(row[col_map[_COL_CLOSE]].strip())
            volume = float(row[col_map[_COL_VOLUME]].strip())

            bars.append(
                Bar(
                    ticker=ticker,
                    timestamp=bar_date,
                    open=open_p,
                    high=high_p,
                    low=low_p,
                    close=close_p,
                    volume=volume,
                )
            )
        except (ValueError, KeyError) as exc:
            log.debug("stooq_csv_row_skip ticker=%s error=%s", ticker, exc)
            continue

    bars.sort(key=lambda b: b.timestamp)
    return bars


class StooqPriceSource:
    """Stooq CSV daily price source.

    Parameters
    ----------
    timeout:
        HTTP request timeout in seconds (default 20).
    session:
        Optional ``requests.Session`` for injection (used in tests).

    Usage::

        src = StooqPriceSource()
        bars = src.bars("AAPL.US", start, end)
    """

    def __init__(
        self,
        timeout: float = 20.0,
        session: requests.Session | None = None,
    ) -> None:
        self._timeout = timeout
        self._session = session or requests.Session()

    def bars(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        """Return daily OHLCV bars for *ticker* in [start, end).

        Parameters
        ----------
        ticker:
            Stooq ticker symbol (e.g. ``"AAPL.US"``).  Delisted tickers
            are passed through; an empty list is returned when Stooq has
            no data.
        start:
            Inclusive start (tz-aware UTC).
        end:
            Exclusive end (tz-aware UTC).  Bars at or after this timestamp
            are dropped.

        Returns
        -------
        list[Bar]
            Bars sorted ascending by timestamp.

        Raises
        ------
        requests.HTTPError
            On unexpected HTTP errors (5xx).
        """
        start_utc = _to_utc(start)
        end_utc = _to_utc(end)

        # Map bare ticker → Stooq symbol (AAPL → AAPL.US).  Without this Stooq
        # returns "bars not found" and the fallback is dead (audit C4).  The
        # parsed Bars keep the ORIGINAL ticker so downstream PIT keying matches.
        stooq_symbol = _to_stooq_symbol(ticker)

        params = {
            "s": stooq_symbol,
            "d1": start_utc.strftime(_DATE_FMT),
            "d2": end_utc.strftime(_DATE_FMT),
            "i": "d",
        }

        try:
            resp = self._session.get(
                _STOOQ_URL,
                params=params,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            log.warning(
                "stooq_bars_request_error ticker=%s symbol=%s error=%s",
                ticker,
                stooq_symbol,
                exc,
            )
            return []

        # 404 or empty response → graceful degradation.
        if resp.status_code == 404:
            log.info("stooq_bars_not_found ticker=%s", ticker)
            return []

        resp.raise_for_status()

        csv_text = resp.text.strip()
        if not csv_text:
            return []

        result = _parse_stooq_csv(ticker, csv_text, end_utc)
        log.debug(
            "stooq_bars_fetched ticker=%s count=%d start=%s end=%s",
            ticker,
            len(result),
            start_utc.isoformat(),
            end_utc.isoformat(),
        )
        return result

    # ------------------------------------------------------------------
    # PITGateway adapter
    # ------------------------------------------------------------------

    def get_pit(
        self,
        field: str,
        ticker: str,
        as_of: datetime,
    ) -> object | None:
        """Return scalar value for *field* at *as_of* using recent bars.

        Adapter that allows ``StooqPriceSource`` to be registered with
        ``PITGateway.register_source(field, source)``.

        Supported fields: ``price_open``, ``price_close``, ``spread``.
        (``adv_20d`` is handled by the spec-compliant ``adv.py`` path via
        ``register_adv_source()`` — not here.)

        Returns ``None`` if no bars exist as of *as_of*.
        """
        from datetime import timedelta

        as_of_utc = _to_utc(as_of)

        # For open/close/spread, fetch just the last few days.
        # adv_20d is no longer handled here — routed via adv.py instead.
        window_start = as_of_utc - timedelta(days=5)

        # Use as_of + 1 day as the exclusive end so that daily bars
        # timestamped at as_of midnight (T00:00:00Z) are included by
        # bars()'s strict ``timestamp < end`` guard, while bars strictly
        # after as_of are excluded by the downstream eligibility filter.
        # Do NOT use ``as_of + 1 second`` — that is fragile and would
        # permit a refactor to accidentally drop the eligibility guard.
        window_end = as_of_utc + timedelta(days=1)

        fetched = self.bars(ticker, window_start, window_end)
        if not fetched:
            return None

        # Latest bar at or before as_of (explicit PIT eligibility guard).
        eligible = [b for b in fetched if b.timestamp <= as_of_utc]
        if not eligible:
            return None

        latest = eligible[-1]

        if field == "price_close":
            return latest.close
        if field == "price_open":
            return latest.open
        if field == "spread":
            if latest.close > 0:
                return (latest.high - latest.low) / latest.close
            return None

        return None

    def get_bar(
        self,
        ticker: str,
        as_of: datetime,
    ) -> Bar | None:
        """Return the latest full OHLCV ``Bar`` at or before *as_of*.

        Mirror of :meth:`AlpacaPriceSource.get_bar` — returns the whole bar so
        ADV / volume-anomaly callers get ``close`` AND ``volume``.  Enforces the
        ``timestamp <= as_of`` eligibility guard (no look-ahead).
        """
        from datetime import timedelta

        as_of_utc = _to_utc(as_of)
        window_start = as_of_utc - timedelta(days=5)
        window_end = as_of_utc + timedelta(days=1)

        fetched = self.bars(ticker, window_start, window_end)
        if not fetched:
            return None

        eligible = [b for b in fetched if b.timestamp <= as_of_utc]
        if not eligible:
            return None
        return eligible[-1]
