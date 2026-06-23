"""Point-in-time (PIT) gateway — Lane 3 core.

The ONLY sanctioned read path for price, filing, news, and trust data.
No ``get_latest()``.  Every call passes an explicit ``as_of`` timestamp;
the gateway NEVER returns data timestamped after ``as_of`` (no look-ahead).

See INTERFACES.md §3 and design spec §4.2.

Public surface
--------------
Bar             — OHLCV bar dataclass.
PriceSource     — Protocol that Alpaca/Stooq clients implement.
FixtureSource   — In-memory source for unit tests (no network).
PITGateway      — The central read interface.

Wave-B network clients (Alpaca, Stooq) plug in via::

    gateway.register_source("price_close", AlpacaPriceSource(...))
    gateway.register_source("price_open",  AlpacaPriceSource(...))
    ...
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


# ---------------------------------------------------------------------------
# Bar dataclass
# ---------------------------------------------------------------------------

@dataclass
class Bar:
    """Single OHLCV price bar.

    Attributes
    ----------
    ticker:
        Exchange ticker symbol.
    timestamp:
        Bar close (or open, depending on convention) timestamp — tz-aware UTC.
    open:
        Opening price.
    high:
        High price.
    low:
        Low price.
    close:
        Closing price.
    volume:
        Number of shares traded.
    """

    ticker: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


# ---------------------------------------------------------------------------
# PriceSource Protocol — implemented by Alpaca / Stooq clients (Wave-B)
# ---------------------------------------------------------------------------

class PriceSource(Protocol):
    """Protocol that price-data clients must implement.

    Wave-B agents build Alpaca and Stooq clients that implement this.
    Tests use ``FixtureSource`` instead (no network calls in unit tests).
    """

    def bars(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        """Return OHLCV bars for ``ticker`` in [start, end).

        Parameters
        ----------
        ticker:
            Exchange ticker symbol.
        start:
            Inclusive start timestamp (tz-aware UTC).
        end:
            Exclusive end timestamp (tz-aware UTC).

        Returns
        -------
        list[Bar]
            Bars whose timestamps fall in [start, end), sorted ascending.
            Returns an empty list when no data is available.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# FixtureSource — in-memory source for unit tests
# ---------------------------------------------------------------------------

class FixtureSource:
    """In-memory PIT data source for tests.

    Stores (field, ticker, timestamp, value) tuples.  ``get()`` returns the
    value for the most-recent entry whose timestamp ≤ as_of.  Entries with
    timestamps > as_of are NEVER returned (strict look-ahead guard).

    Usage::

        src = FixtureSource()
        src.add("price_close", "AAPL", ts, 150.0)
        gateway.register_source("price_close", src)
    """

    def __init__(self) -> None:
        # Stores list of (timestamp, value) per (field, ticker)
        self._data: dict[tuple[str, str], list[tuple[datetime, object]]] = {}
        self._lock = threading.Lock()

    def add(
        self,
        field: str,
        ticker: str,
        timestamp: datetime,
        value: object,
    ) -> None:
        """Add a data point.

        Parameters
        ----------
        field:
            PIT field name (e.g. "price_close").
        ticker:
            Exchange ticker symbol.
        timestamp:
            Information timestamp (tz-aware UTC) when this value became known.
        value:
            The data value (float, dict, str, etc.).
        """
        key = (field, ticker)
        with self._lock:
            if key not in self._data:
                self._data[key] = []
            self._data[key].append((timestamp, value))
            # Keep sorted ascending by timestamp for binary-search correctness.
            self._data[key].sort(key=lambda x: x[0])

    def get_pit(
        self,
        field: str,
        ticker: str,
        as_of: datetime,
    ) -> object | None:
        """Return the most-recent value for (field, ticker) with timestamp ≤ as_of.

        Returns None if no such value exists (look-ahead guard: values with
        timestamps > as_of are never returned).
        """
        key = (field, ticker)
        with self._lock:
            entries = self._data.get(key, [])

        # Walk backwards to find latest entry whose timestamp ≤ as_of.
        for ts, value in reversed(entries):
            if ts <= as_of:
                return value
        return None


# ---------------------------------------------------------------------------
# PITGateway — the central PIT read interface
# ---------------------------------------------------------------------------

_SUPPORTED_FIELDS = frozenset({
    "price_open",
    "price_close",
    "adv_20d",
    "beta_252d",
    "spread",
    "filing",
    "news",
    "trust",
})


class PITGateway:
    """Point-in-time data gateway.

    The ONLY way to read price/filing/news/trust data.  All reads pass
    an explicit ``as_of`` timestamp; data with timestamps after ``as_of``
    is NEVER returned.

    Wave-B network clients plug in via ``register_source()``.
    Tests use ``FixtureSource`` (no network calls).

    Supported fields (INTERFACES.md §3):
        "price_open", "price_close", "adv_20d", "beta_252d",
        "spread", "filing", "news", "trust"

    Per-source as_of semantics (from design spec §4.2):
        - Form 4 / filing → filing timestamp
        - Congress        → disclosure date
        - price (exec)    → next-day open
        - news            → publish timestamp
        - beta            → 252-day window ending as_of−1
    """

    def __init__(self) -> None:
        # Maps field name → FixtureSource (or any object with get_pit method)
        self._sources: dict[str, object] = {}
        self._lock = threading.Lock()

    def register_source(self, field: str, source: object) -> None:
        """Register a data source for a specific field.

        Parameters
        ----------
        field:
            PIT field name (must be in the supported fields set).
        source:
            Object with a ``get_pit(field, ticker, as_of)`` method.
            For price data, may also implement ``PriceSource.bars()``.

        Raises
        ------
        ValueError
            If ``field`` is not a supported PIT field.
        """
        if field not in _SUPPORTED_FIELDS:
            raise ValueError(
                f"Unknown PIT field {field!r}. "
                f"Supported fields: {sorted(_SUPPORTED_FIELDS)}"
            )
        with self._lock:
            self._sources[field] = source

    def get(
        self,
        field: str,
        ticker: str,
        as_of: datetime,
    ) -> object | None:
        """Return the PIT value for (field, ticker) as known at as_of.

        Returns None in two cases:
        1. No source is registered for this field.
        2. No data exists for this ticker/field with timestamp ≤ as_of
           (i.e. the value was not yet known as of as_of).

        NEVER returns a value whose information timestamp is after as_of.
        This is the structural look-ahead guard.

        Parameters
        ----------
        field:
            PIT field name (e.g. "price_close", "beta_252d").
        ticker:
            Exchange ticker symbol.
        as_of:
            Information timestamp cutoff (tz-aware UTC).

        Returns
        -------
        object | None
            The data value, or None if not available as of as_of.
        """
        if field not in _SUPPORTED_FIELDS:
            # Unknown field — return None rather than raising, per spec.
            return None

        with self._lock:
            source = self._sources.get(field)

        if source is None:
            return None

        return source.get_pit(field, ticker, as_of)  # type: ignore[attr-defined]
