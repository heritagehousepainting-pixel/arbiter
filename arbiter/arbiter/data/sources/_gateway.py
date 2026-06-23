"""Gateway builder — assembles PITGateway with Alpaca primary + Stooq fallback.

The ``build_price_gateway`` helper is the Wave-C wiring point: the orchestrator
calls it once at startup and passes the resulting ``PITGateway`` to all
downstream components that need PIT price reads.

Fallback semantics
------------------
``_FallbackPriceAdapter`` wraps ``(primary, fallback)`` and tries primary first.
If the primary returns ``None`` OR raises any exception, it silently falls back
to the secondary.  If both fail (return ``None`` or raise), the adapter returns
``None`` (fail-closed → downstream sizes 0, per INTERFACES.md §11 convention 4).

No look-ahead is introduced by the fallback layer because each underlying source
already enforces the as-of guard independently.
"""
from __future__ import annotations

import logging

from arbiter.config import Config
from arbiter.data.adv import attach_bar_provider, register_adv_source
from arbiter.data.pit import PITGateway
from arbiter.data.sources.alpaca import AlpacaPriceSource
from arbiter.data.sources.stooq import StooqPriceSource

log = logging.getLogger(__name__)


class _FallbackPriceAdapter:
    """Wraps a primary and fallback source; exposes ``get_pit`` for PITGateway.

    Tries *primary* first.  Falls back to *secondary* when primary raises or
    returns ``None``.  If both fail, returns ``None``.
    """

    def __init__(
        self,
        primary: AlpacaPriceSource | StooqPriceSource,
        secondary: AlpacaPriceSource | StooqPriceSource,
    ) -> None:
        self._primary = primary
        self._secondary = secondary

    def get_pit(
        self,
        field: str,
        ticker: str,
        as_of: object,  # datetime, passed through
    ) -> object | None:
        # --- Try primary (Alpaca) ---
        try:
            value = self._primary.get_pit(field, ticker, as_of)  # type: ignore[arg-type]
        except Exception as exc:
            log.warning(
                "price_primary_failed_falling_back field=%s ticker=%s error=%s",
                field,
                ticker,
                exc,
            )
            value = None

        if value is not None:
            return value

        # --- Fall back to secondary (Stooq) ---
        log.debug(
            "price_fallback_to_secondary field=%s ticker=%s",
            field,
            ticker,
        )
        try:
            return self._secondary.get_pit(field, ticker, as_of)  # type: ignore[arg-type]
        except Exception as exc:
            log.warning(
                "price_secondary_failed field=%s ticker=%s error=%s",
                field,
                ticker,
                exc,
            )
            return None

    def get_bar(
        self,
        ticker: str,
        as_of: object,  # datetime, passed through
    ) -> object | None:
        """Return a full OHLCV ``Bar`` (close + volume) at or before *as_of*.

        Tries the primary (Alpaca) then falls back to the secondary (Stooq),
        same fail-soft contract as :meth:`get_pit`.  This is the accessor ADV
        and the volume-anomaly breaker use so they get **real volume** rather
        than the scalar ``close`` returned by ``get_pit("price_close")``.

        Both-down vs no-data distinction
        --------------------------------
        When *both* sources raise (an outage), we emit a distinct
        ``price_both_sources_down`` WARNING so an outage is not silently
        indistinguishable from a ticker that legitimately has no bars (which
        returns ``None`` quietly).  Either way the return is ``None``
        (fail-closed), but the log signal makes the outage observable.
        """
        primary_errored = False
        # --- Try primary (Alpaca) ---
        try:
            bar = self._primary.get_bar(ticker, as_of)  # type: ignore[arg-type]
        except Exception as exc:
            primary_errored = True
            log.warning(
                "price_primary_bar_failed_falling_back ticker=%s error=%s",
                ticker,
                exc,
            )
            bar = None

        if bar is not None:
            return bar

        # --- Fall back to secondary (Stooq) ---
        try:
            secondary_bar = self._secondary.get_bar(ticker, as_of)  # type: ignore[arg-type]
        except Exception as exc:
            # Both sources raised → this is an OUTAGE, distinct from "no data".
            if primary_errored:
                log.warning(
                    "price_both_sources_down ticker=%s as_of=%s — "
                    "outage, not absence of data (fail-closed → None)",
                    ticker,
                    as_of,
                )
            else:
                log.warning(
                    "price_secondary_bar_failed ticker=%s error=%s",
                    ticker,
                    exc,
                )
            return None

        return secondary_bar


def build_price_gateway(config: Config) -> PITGateway:
    """Build and return a ``PITGateway`` wired with Alpaca + Stooq sources.

    Registers the ``_FallbackPriceAdapter`` for raw price fields:
    ``price_open``, ``price_close``, ``spread``.

    ``adv_20d`` is registered separately via :func:`arbiter.data.adv.register_adv_source`
    so that it goes through the spec-compliant path (window ending ``as_of−1``,
    20-bar minimum, 35-calendar-day lookback) rather than through the adapter.

    Alpaca is primary for raw price fields; Stooq is the fallback (covers
    delisted tickers and periods where Alpaca has no data).

    Wave-C wiring
    -------------
    The orchestrator (lane L13) calls this function once at startup::

        config = load_config()
        pit = build_price_gateway(config)
        # pit is passed to advisors, sizing, beta, slippage helpers.

    Parameters
    ----------
    config:
        Loaded ``Config`` instance (provides Alpaca credentials, URL, timeout).

    Returns
    -------
    PITGateway
        Fully wired gateway.  ``pit.get(field, ticker, as_of)`` returns
        ``None`` if both sources fail (fail-closed).
    """
    alpaca = AlpacaPriceSource(config)
    stooq = StooqPriceSource(timeout=config.alpaca_timeout)

    adapter = _FallbackPriceAdapter(primary=alpaca, secondary=stooq)

    pit = PITGateway()
    # Register raw price fields through the Alpaca-primary / Stooq-fallback adapter.
    for field in ("price_open", "price_close", "spread"):
        pit.register_source(field, adapter)

    # Attach the Bar-returning accessor (close + volume) onto the gateway so
    # ADV and the volume-anomaly breaker can read REAL volume.  ``get_pit`` was
    # left scalar deliberately (exit_monitor / outcome_labeler / beta depend on
    # the scalar close), so the dollar-volume / volume-anomaly path uses this
    # provider instead.  See arbiter.data.adv.attach_bar_provider.
    attach_bar_provider(pit, adapter)

    # adv_20d must go through the spec-compliant path (adv.py) which:
    #   - ends the window at as_of−1 (no look-ahead on the current bar),
    #   - requires exactly 20 usable trading-day bars (fail-closed on thin data),
    #   - uses a 35-calendar-day lookback (not 30).
    # The bar provider (attached above) supplies close*volume per day.
    register_adv_source(pit)

    log.info(
        "price_gateway_built fields=%s primary=alpaca fallback=stooq adv_via=adv.py",
        ["price_open", "price_close", "spread", "adv_20d"],
    )
    return pit
