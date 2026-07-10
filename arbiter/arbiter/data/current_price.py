"""Intraday current-price provider — sub-project #3 (Decision 1, THE CRUX).

This module gives the LIVE exit monitor a CURRENT (intraday) price for its
stop-loss comparison, **distinct** from the daily ``price_open``/``price_close``
used by entries, outcome labeling, and backtests.

PIT-purity boundary (binding constraint, INTERFACES §3/§11.1)
------------------------------------------------------------
``CurrentPriceProvider`` is **NOT** a PIT field and is **NEVER** registered with
``PITGateway``.  The accessor signature is ``current_price(ticker) -> float | None``
with **no ``as_of`` parameter** — that omission makes it structurally impossible to
misuse as a historical PIT read.  This module reads the wall clock ONLY through
the sanctioned Lane-3 ``Clock`` abstraction, and ONLY to timestamp a feed-outage
alert (never to select data), so the no-look-ahead AST lint stays clean.
``build_engine`` injects ``NullCurrentPriceProvider`` for ``sim`` mode AND
for EVERY backtest (the clock-type gate, amendment C0), so a backtest can never see
a live "now" price.

Multi-symbol batching (amendment C1)
------------------------------------
``AlpacaCurrentPriceSource`` reads ALL held tickers in a single
``GET /v2/stocks/trades/latest?symbols=A,B,C`` (feed=iex) call per iteration via
``current_prices(tickers)`` so cadence cost is independent of position count.  The
single-ticker ``current_price(ticker)`` delegates to the batch path.

Feed-outage hardening (2026-07-10 incident)
-------------------------------------------
``ALPACA_DATA_FEED=sip`` on a non-SIP-entitled account made every latest-trades
call 403 for 8 trading days — the exit monitor got empty prices and stop-losses
were SILENTLY blind, with only a ``log.warning`` as evidence.  Two durable fixes:

1. **Automatic iex fallback** — when the configured feed *errors* (HTTP 4xx/5xx,
   network) and coverage is incomplete, the batch is retried ONCE on ``feed=iex``
   (the free-plan-safe feed).  No retry loop: if the primary already IS iex there
   is no second attempt.
2. **Real outage alert, no cry-wolf** — if every attempted feed ERRORED and zero
   prices came back, that is a broken feed (stop-losses blind) and a *critical*
   alert fires through the existing ``arbiter.safety.alerting.Alerting`` channel
   (audit + ntfy webhook), latched once per outage episode.  An HTTP-200 response
   with no recent trades is the NORMAL closed-market shape — the monitor falls
   back to the daily PIT close by design — and never alerts.
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Protocol, runtime_checkable

import structlog

from arbiter.config import Config
from arbiter.data.clock import Clock

log = structlog.get_logger(__name__)

# The free-plan-safe feed every Alpaca account is entitled to.  Used as the
# automatic retry target when the configured feed errors out.
_FALLBACK_FEED = "iex"


@runtime_checkable
class CurrentPriceProvider(Protocol):
    """A source of the CURRENT (intraday) price for a ticker.

    There is deliberately **no ``as_of``** parameter: a current-price read is
    legitimately "now" and must never be reachable as a historical PIT read.
    Returns ``None`` when no current price is available (closed market, stale,
    unknown ticker) → the exit monitor fails closed (no spurious stop fire).
    """

    def current_price(self, ticker: str) -> float | None: ...

    def current_prices(self, tickers: Iterable[str]) -> dict[str, float]: ...


class NullCurrentPriceProvider:
    """Always returns ``None`` — the default for ``sim`` mode and ALL backtests.

    With this provider the exit monitor falls back to the daily PIT close exactly
    as it does today, so sim/backtest behaviour is unchanged.
    """

    def current_price(self, ticker: str) -> float | None:  # noqa: D102
        return None

    def current_prices(self, tickers: Iterable[str]) -> dict[str, float]:  # noqa: D102
        return {}


def _default_http_get(url: str, headers: dict) -> Any:
    """Real HTTP GET using httpx.  Tests inject a fake instead."""
    import httpx  # lazy — not needed in the offline test path

    resp = httpx.get(url, headers=headers, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


class AlpacaCurrentPriceSource:
    """Live current-price provider backed by the Alpaca DATA API.

    Uses the MULTI-symbol latest-trades endpoint
    ``GET /v2/stocks/trades/latest?symbols=A,B,C&feed=<ALPACA_DATA_FEED|iex>`` so
    ALL held tickers are fetched in ONE call per iteration (amendment C1).  Falls
    back to the latest-quote mid for any symbol with no recent trade.

    The HTTP call goes through an injectable ``http_get(url, headers)`` callable
    (default a thin ``httpx`` shim) so pytest injects a fake and never hits the
    network.  This is the DATA host (``data.alpaca.markets``), not the trading
    host — it adds NO live trading endpoint (paper-only floor preserved).

    Feed-failure hardening (see module docstring): a configured feed that ERRORS
    is retried once on ``feed=iex``; a total outage (every attempted feed errored,
    zero prices) fires a critical alert through the injectable ``alerting`` seam
    (default: a lazily-built ``arbiter.safety.alerting.Alerting``).  The alert is
    latched — one page per outage episode, re-armed on the first non-outage read.
    """

    def __init__(
        self,
        config: Config,
        *,
        http_get: Any = None,
        alerting: Any = None,
        clock: Clock | None = None,
    ) -> None:
        self._config = config
        self._base_url = config.alpaca_data_base_url.rstrip("/")
        self._headers = {
            "APCA-API-KEY-ID": config.alpaca_api_key,
            "APCA-API-SECRET-KEY": config.alpaca_secret_key,
            "Accept": "application/json",
        }
        self._timeout = config.alpaca_timeout
        # Free plan only allows feed=iex; omit defaults to sip (paid) → 403.
        self._feed = os.getenv("ALPACA_DATA_FEED", "iex")
        self._http_get = http_get if http_get is not None else _default_http_get
        # ``alerting`` duck-types Alerting.alert(tier, message, ctx, *, as_of);
        # None → build the real Alerting lazily on first outage (avoids the
        # import/audit machinery entirely on the happy path and in most tests).
        self._alerting = alerting
        self._clock = clock if clock is not None else Clock()
        # Outage latch: True once the critical alert fired for the CURRENT
        # outage episode; re-armed by the next non-outage read so a broken feed
        # pages the phone once, not every monitor iteration.
        self._outage_alerted = False

    # ------------------------------------------------------------------
    # Provider protocol
    # ------------------------------------------------------------------

    def current_price(self, ticker: str) -> float | None:
        prices = self.current_prices([ticker])
        return prices.get(ticker)

    def current_prices(self, tickers: Iterable[str]) -> dict[str, float]:
        """Return {ticker: last_trade_price} for all tickers in ONE batch call.

        Symbols with no recent trade fall back to the latest-quote mid in a
        second batch call.  If the configured feed ERRORS (403 entitlement,
        5xx, network) and coverage is incomplete, the missing symbols are
        retried ONCE on ``feed=iex``.  Any remaining failure yields a
        partial/empty dict (the monitor then fails closed / falls back to
        daily PIT) — and a TOTAL failure (all attempted feeds errored, zero
        prices) escalates to a critical alert.  An error-free empty result
        (HTTP 200, no recent trades — the normal closed-market shape) never
        alerts.
        """
        symbols = sorted({t for t in tickers if t})
        if not symbols:
            return {}

        out, primary_errored = self._fetch_feed(symbols, self._feed)

        fallback_attempted = False
        fallback_errored = False
        if (
            primary_errored
            and len(out) < len(symbols)
            and self._feed.lower() != _FALLBACK_FEED
        ):
            fallback_attempted = True
            log.warning(
                "current_price.feed_fallback",
                primary_feed=self._feed,
                fallback_feed=_FALLBACK_FEED,
            )
            fb_prices, fallback_errored = self._fetch_feed(
                [s for s in symbols if s not in out], _FALLBACK_FEED
            )
            out.update(fb_prices)

        total_failure = (
            not out
            and primary_errored
            and (fallback_errored or not fallback_attempted)
        )
        if total_failure:
            self._escalate_outage(symbols, fallback_attempted=fallback_attempted)
        else:
            # Any non-outage read (prices, or a clean 200-but-empty closed
            # market) re-arms the latch so the NEXT outage pages again.
            self._outage_alerted = False

        return out

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_feed(
        self, symbols: list[str], feed: str
    ) -> tuple[dict[str, float], bool]:
        """One trades→quotes pass against ``feed``.

        Returns ``(prices, errored)`` where ``errored`` is True iff an HTTP
        call RAISED (403/5xx/network).  A clean 200 with no trades/quotes is
        ``errored=False`` — that distinction is what separates "feed broken,
        stop-losses blind" (alert) from "market closed" (designed fail-closed
        path, no alert).
        """
        out: dict[str, float] = {}
        errored = False
        try:
            data = self._http_get(
                f"{self._base_url}/v2/stocks/trades/latest"
                f"?symbols={','.join(symbols)}&feed={feed}",
                self._headers,
            )
            trades = (data or {}).get("trades", {}) or {}
            for sym, trade in trades.items():
                px = trade.get("p") if isinstance(trade, dict) else None
                if px is not None and float(px) > 0:
                    out[sym] = float(px)
        except Exception as exc:  # noqa: BLE001
            errored = True
            log.warning(
                "current_price.latest_trades_failed", feed=feed, error=str(exc)
            )

        # Fall back to the quote mid for any symbol the trades call missed.
        missing = [s for s in symbols if s not in out]
        if missing:
            try:
                data = self._http_get(
                    f"{self._base_url}/v2/stocks/quotes/latest"
                    f"?symbols={','.join(missing)}&feed={feed}",
                    self._headers,
                )
                quotes = (data or {}).get("quotes", {}) or {}
                for sym, quote in quotes.items():
                    if not isinstance(quote, dict):
                        continue
                    bid = quote.get("bp")
                    ask = quote.get("ap")
                    if bid and ask and float(bid) > 0 and float(ask) > 0:
                        out[sym] = (float(bid) + float(ask)) / 2.0
            except Exception as exc:  # noqa: BLE001
                errored = True
                log.warning(
                    "current_price.latest_quotes_failed", feed=feed, error=str(exc)
                )

        return out, errored

    def _escalate_outage(
        self, symbols: list[str], *, fallback_attempted: bool
    ) -> None:
        """Every attempted feed ERRORED and zero prices came back → the feed is
        broken and stop-losses are blind.  Page once per outage episode through
        the existing tiered alerting channel (audit + ntfy webhook).
        """
        log.error(
            "current_price.feed_outage",
            primary_feed=self._feed,
            fallback_attempted=fallback_attempted,
            symbols=symbols,
            already_alerted=self._outage_alerted,
        )
        if self._outage_alerted:
            return  # one page per episode; re-armed by the next healthy read
        self._outage_alerted = True
        try:
            alerting = self._alerting
            if alerting is None:
                from arbiter.safety.alerting import Alerting  # lazy — outage path only

                alerting = Alerting(
                    config=self._config, audit_path=self._config.audit_path
                )
                self._alerting = alerting
            alerting.alert(
                "critical",
                "current-price feed outage — all feeds failed; "
                "exit-monitor stop-losses are BLIND (falling back to daily close)",
                {
                    "code": "current_price.feed_outage",
                    "primary_feed": self._feed,
                    "fallback_feed": _FALLBACK_FEED,
                    "fallback_attempted": fallback_attempted,
                    "symbols": symbols,
                },
                as_of=self._clock.now(),
            )
        except Exception as exc:  # noqa: BLE001 — alerting must never break pricing
            log.error("current_price.outage_alert_failed", error=str(exc))
