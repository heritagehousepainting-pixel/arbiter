"""Intraday current-price provider — sub-project #3 (Decision 1, THE CRUX).

This module gives the LIVE exit monitor a CURRENT (intraday) price for its
stop-loss comparison, **distinct** from the daily ``price_open``/``price_close``
used by entries, outcome labeling, and backtests.

PIT-purity boundary (binding constraint, INTERFACES §3/§11.1)
------------------------------------------------------------
``CurrentPriceProvider`` is **NOT** a PIT field and is **NEVER** registered with
``PITGateway``.  The accessor signature is ``current_price(ticker) -> float | None``
with **no ``as_of`` parameter** — that omission makes it structurally impossible to
misuse as a historical PIT read.  This module makes no wall-clock read and no
latest-style PIT lookup, so the no-look-ahead AST lint stays clean.
``build_engine`` injects ``NullCurrentPriceProvider`` for ``sim`` mode AND
for EVERY backtest (the clock-type gate, amendment C0), so a backtest can never see
a live "now" price.

Multi-symbol batching (amendment C1)
------------------------------------
``AlpacaCurrentPriceSource`` reads ALL held tickers in a single
``GET /v2/stocks/trades/latest?symbols=A,B,C`` (feed=iex) call per iteration via
``current_prices(tickers)`` so cadence cost is independent of position count.  The
single-ticker ``current_price(ticker)`` delegates to the batch path.
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Protocol, runtime_checkable

import structlog

from arbiter.config import Config

log = structlog.get_logger(__name__)


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
    """

    def __init__(self, config: Config, *, http_get: Any = None) -> None:
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

    # ------------------------------------------------------------------
    # Provider protocol
    # ------------------------------------------------------------------

    def current_price(self, ticker: str) -> float | None:
        prices = self.current_prices([ticker])
        return prices.get(ticker)

    def current_prices(self, tickers: Iterable[str]) -> dict[str, float]:
        """Return {ticker: last_trade_price} for all tickers in ONE batch call.

        Symbols with no recent trade fall back to the latest-quote mid in a
        second batch call.  Any failure is logged and yields a partial/empty
        dict (the monitor then fails closed / falls back to daily PIT).
        """
        symbols = sorted({t for t in tickers if t})
        if not symbols:
            return {}

        out: dict[str, float] = {}
        try:
            data = self._http_get(
                f"{self._base_url}/v2/stocks/trades/latest"
                f"?symbols={','.join(symbols)}&feed={self._feed}",
                self._headers,
            )
            trades = (data or {}).get("trades", {}) or {}
            for sym, trade in trades.items():
                px = trade.get("p") if isinstance(trade, dict) else None
                if px is not None and float(px) > 0:
                    out[sym] = float(px)
        except Exception as exc:  # noqa: BLE001
            log.warning("current_price.latest_trades_failed", error=str(exc))

        # Fall back to the quote mid for any symbol the trades call missed.
        missing = [s for s in symbols if s not in out]
        if missing:
            try:
                data = self._http_get(
                    f"{self._base_url}/v2/stocks/quotes/latest"
                    f"?symbols={','.join(missing)}&feed={self._feed}",
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
                log.warning("current_price.latest_quotes_failed", error=str(exc))

        return out
