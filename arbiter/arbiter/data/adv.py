"""20-day average dollar volume (ADV) computation — Lane 3 core.

Implements INTERFACES.md §3, §9, §11 convention 4::

    def adv_20d(ticker, as_of, pit) -> float | None

ADV = mean(price_close * volume) over the 20 trading days ending as_of−1.

Returns None when fewer than 20 usable days are available (fail-closed:
Lane 12 policy sizes the position to 0 on a None result — INTERFACES.md §9,
§11 convention 4).

Price/volume data comes from the PIT gateway in one of two forms:
  - Production (Wave-B): ``pit.get("price_close", ticker, ts)`` returns a
    ``Bar`` object; we extract ``.close`` and ``.volume`` from it.
  - Tests: a ``FixtureSource`` stores pre-multiplied dollar-volume scalars
    under a ``"_adv_dolvol"`` private key accessed directly; see
    :func:`make_adv_fixture_pit` for the test helper.

No ``datetime.now()`` calls; all time references come from the caller-supplied
``as_of`` argument.  All reads go through PITGateway.

See INTERFACES.md §3 and §9 (ADV cap sizing), §11 convention 4.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from arbiter.data.pit import Bar, FixtureSource, PITGateway

_logger = logging.getLogger(__name__)

# Must have exactly this many usable dollar-volume days to return a value.
_REQUIRED_DAYS = 20

# Private attribute on PITGateway that holds the Bar-returning provider (the
# Alpaca→Stooq fallback adapter's ``get_bar``).  In PRODUCTION this is set by
# ``attach_bar_provider`` so ADV reads real (close, volume) bars; the scalar
# ``pit.get("price_close")`` returns only a price and would make ADV ≈ price.
_BAR_PROVIDER_ATTR = "_bar_provider"


def attach_bar_provider(pit: PITGateway, provider: object) -> None:
    """Attach a Bar-returning *provider* to *pit* for ADV / volume-anomaly.

    *provider* must expose ``get_bar(ticker, as_of) -> Bar | None`` returning a
    full OHLCV bar (close + volume).  Once attached, :func:`adv_20d` and the
    volume-anomaly gate read real volume through it rather than the scalar
    ``price_close`` field (which stays scalar for exit_monitor / outcome_labeler
    / beta).  Without it (pure FixtureSource unit tests) those callers fall back
    to ``pit.get("price_close")`` which yields Bar objects in fixtures.
    """
    setattr(pit, _BAR_PROVIDER_ATTR, provider)


def _get_pit_bar(pit: PITGateway, ticker: str, as_of: datetime) -> object | None:
    """Return a Bar-like value for (ticker, as_of), preferring the bar provider.

    Production: an attached provider returns a real :class:`Bar` with volume.
    Tests without a provider: falls back to ``pit.get("price_close", ...)``,
    which yields a ``Bar`` (Bar fixtures) or a scalar (registered-source tests).
    """
    provider = getattr(pit, _BAR_PROVIDER_ATTR, None)
    if provider is not None:
        bar = provider.get_bar(ticker, as_of)  # type: ignore[attr-defined]
        if bar is not None:
            return bar
        return None
    return pit.get("price_close", ticker, as_of)

# Calendar-day look-back buffer: 20 trading days fit comfortably in 35
# calendar days even accounting for weekends + holidays.
_LOOKBACK_CALENDAR_DAYS = 35


def adv_20d(
    ticker: str,
    as_of: datetime,
    pit: PITGateway,
) -> float | None:
    """Compute 20-day average dollar volume ending as_of−1.

    Dollar volume for a bar = ``price_close * volume``.

    We probe the PIT gateway for "price_close" day-by-day over a calendar
    window that ends strictly before ``as_of`` (no look-ahead).  If the
    returned value is a :class:`~arbiter.data.pit.Bar` instance we use
    ``bar.close * bar.volume``; if it is a scalar float we use it directly
    as a pre-computed dollar-volume (test mode).

    Parameters
    ----------
    ticker:
        Exchange ticker symbol.
    as_of:
        Information cutoff.  The ADV window ends one day before this.
    pit:
        PITGateway instance — all reads go through here.

    Returns
    -------
    float | None
        Mean dollar volume (USD) over the 20 trading days, or None if
        fewer than 20 usable bars are available (fail-closed).
    """
    # Window ends strictly before as_of (no look-ahead).
    end_exclusive = as_of
    start = as_of - timedelta(days=_LOOKBACK_CALENDAR_DAYS)

    dollar_volumes = _fetch_dollar_volumes(ticker, start, end_exclusive, pit)

    if len(dollar_volumes) < _REQUIRED_DAYS:
        _logger.warning(
            "adv_20d: only %d usable bars for %s as of %s "
            "(need %d) — returning None (fail-closed)",
            len(dollar_volumes),
            ticker,
            as_of,
            _REQUIRED_DAYS,
        )
        return None

    # Use the most recent _REQUIRED_DAYS bars.
    recent = dollar_volumes[-_REQUIRED_DAYS:]
    return sum(recent) / _REQUIRED_DAYS


def _fetch_dollar_volumes(
    ticker: str,
    start: datetime,
    end_exclusive: datetime,
    pit: PITGateway,
) -> list[float]:
    """Walk [start, end_exclusive) day-by-day collecting dollar volumes.

    For each calendar day, ``pit.get("price_close", ticker, day)`` is called.
    The returned value may be:
      - A :class:`~arbiter.data.pit.Bar`: extract ``close * volume``.  We only
        count a bar once, on the calendar day matching the bar's own timestamp
        (prevents FixtureSource's "carry-forward most-recent" from duplicating
        the same bar across multiple probe days).
      - A numeric scalar: treated as a pre-computed dollar-volume registered
        exactly once per day.  In this case, the caller must ensure that each
        day to be counted has a distinct entry in the FixtureSource.

    Returned list is sorted ascending by calendar date.
    """
    results: list[float] = []
    seen_bar_timestamps: set[datetime] = set()
    cursor = start
    one_day = timedelta(days=1)

    while cursor < end_exclusive:
        value = _get_pit_bar(pit, ticker, cursor)

        if value is not None:
            if isinstance(value, Bar):
                # Only count a Bar on the calendar day of its own timestamp.
                # ``FixtureSource.get_pit`` carries the value forward until a
                # later timestamp supersedes it; without deduplication the same
                # bar would be counted for every probe day in its validity span.
                bar_date = value.timestamp.date()
                cursor_date = cursor.date()
                if bar_date == cursor_date and value.timestamp not in seen_bar_timestamps:
                    dollar_vol = _extract_dollar_volume(value)
                    if dollar_vol is not None and dollar_vol >= 0:
                        results.append(dollar_vol)
                        seen_bar_timestamps.add(value.timestamp)
            else:
                # Numeric scalar: one value per calendar-day probe.  Callers
                # using this path must register a distinct entry per day in their
                # FixtureSource (or use make_adv_fixture_pit which uses Bar objects).
                dollar_vol = _extract_dollar_volume(value)
                if dollar_vol is not None and dollar_vol >= 0:
                    results.append(dollar_vol)

        cursor = cursor + one_day

    return results


def _extract_dollar_volume(value: object) -> float | None:
    """Extract dollar volume from a PIT value.

    Handles two cases:
    1. ``Bar`` instance (production) — returns ``close * volume``.
    2. Numeric scalar (test fixture) — returns ``float(value)`` directly
       (interpreted as pre-computed dollar volume).

    Returns None on conversion failure or if prices/volume are non-positive.
    """
    if isinstance(value, Bar):
        if value.close > 0 and value.volume >= 0:
            return value.close * value.volume
        return None

    # Numeric scalar: treat as pre-computed dollar volume.
    try:
        dv = float(value)  # type: ignore[arg-type]
        return dv
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Field-source helper — register an adv_20d computed source on PITGateway
# ---------------------------------------------------------------------------

class _ADVSource:
    """PIT-compatible source that computes adv_20d on each get_pit call.

    Wraps the gateway's existing sources (expected to have "price_close"
    registered) and exposes the computed ADV as a pseudo-field so that
    ``pit.get("adv_20d", ticker, as_of)`` works transparently.

    Parameters
    ----------
    pit:
        The gateway that already has "price_close" registered.
    """

    def __init__(self, pit: PITGateway) -> None:
        self._pit = pit

    def get_pit(
        self,
        field: str,  # noqa: ARG002 — Protocol interface requires this param
        ticker: str,
        as_of: datetime,
    ) -> float | None:
        """Compute and return adv_20d for (ticker, as_of)."""
        return adv_20d(ticker, as_of, self._pit)


def register_adv_source(pit: PITGateway) -> None:
    """Register an ``adv_20d`` field source on *pit* backed by :func:`adv_20d`.

    After calling this, ``pit.get("adv_20d", ticker, as_of)`` computes the
    20-day ADV on-the-fly using the gateway's existing ``price_close`` source.

    Typically called once during gateway setup, before the main loop.

    Parameters
    ----------
    pit:
        PITGateway instance that already has "price_close" registered with
        a source that returns :class:`~arbiter.data.pit.Bar` objects.
    """
    pit.register_source("adv_20d", _ADVSource(pit))


# ---------------------------------------------------------------------------
# Test helpers — build a PITGateway loaded with ADV fixture data
# ---------------------------------------------------------------------------

def make_adv_fixture_pit(
    ticker: str,
    bars: list[tuple[datetime, float, float]],
) -> PITGateway:
    """Build a PITGateway pre-loaded with ADV fixture ``Bar`` objects.

    Constructs ``Bar`` instances from (timestamp, close, volume) tuples and
    registers them under ``"price_close"`` so that :func:`adv_20d` can
    compute the correct dollar volumes.

    Parameters
    ----------
    ticker:
        Exchange ticker symbol.
    bars:
        List of ``(timestamp, close_price, volume)`` tuples (tz-aware UTC).

    Returns
    -------
    PITGateway
        Gateway ready for use in unit tests.
    """
    src = FixtureSource()
    for ts, close, vol in bars:
        bar = Bar(
            ticker=ticker,
            timestamp=ts,
            open=close,   # simplified: open == close for test fixtures
            high=close,
            low=close,
            close=close,
            volume=vol,
        )
        src.add("price_close", ticker, ts, bar)

    pit = PITGateway()
    pit.register_source("price_close", src)
    return pit
