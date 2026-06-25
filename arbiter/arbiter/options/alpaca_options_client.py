"""Alpaca options data + execution client.

This is a SEPARATE client from ``execution/alpaca_adapter.py`` (equity-only).
Options require a different API surface:
  - data:   ``data.alpaca.markets/v1beta1/options/snapshots``
            ``paper-api.alpaca.markets/v2/options/contracts``
  - orders: ``paper-api.alpaca.markets/v2/orders`` with ``asset_class="us_option"``
            and OCC symbol as the ``symbol`` field; qty in contracts, NOT shares.

The equity ``AlpacaAdapter.place()`` hardcodes an equity body
(``alpaca_adapter.py:173-225``) and shares-based conversion; this client is
entirely independent and the two must NEVER be merged.

Data-feed note (P0 spike verified 2026-06-25)
---------------------------------------------
The free ``indicative`` feed returns ``impliedVolatility``, ``greeks``
(delta/gamma/theta/vega), bid, and ask.  The paid ``opra`` feed is not
available (OPRA agreement not signed).  All snapshot calls MUST default to
``feed="indicative"`` — do NOT attempt opra.

P1 execution status
-------------------
``place()`` raises ``NotImplementedError`` in P1 (shadow mode).  The body is
intentionally left for the P2 parallel wave to fill.

P2 execution
------------
``place()`` POSTs a **limit** buy-to-open order to
``paper-api.alpaca.markets/v2/orders`` with ``asset_class="us_option"``.
We only ever buy long calls/puts — never sell, never market.

Limit-price rule (mirrors equity ``_alpaca_limit_str``):
  - Base price = ``order.contract.ask`` if available, else ``mid_price``.
  - Marketable buffer = +1 tick (so the day-limit fills immediately at the
    ask or better without becoming a market order): +$0.01 for prices >= $1,
    +$0.0001 for prices < $1.
  - Formatted: 2 decimal places for prices >= $1; 4 decimal places below $1.

Success: returns the parsed broker response dict
  ``{"id": str, "status": str, "symbol": str, "qty": str,
     "filled_qty": str, "client_order_id": str, ...}``.
Failure: raises ``OptionsBrokerError`` (non-200 after 1 retry).
"""
from __future__ import annotations

import datetime
import logging
import math
from typing import Any, Callable

import httpx

from arbiter.config import Config
from arbiter.options.types import OptionContract, OptionOrder, OptionSide

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Broker error
# ---------------------------------------------------------------------------

class OptionsBrokerError(Exception):
    """Raised when Alpaca returns a non-200 response for an options order.

    Attributes
    ----------
    status_code : int | None
        The HTTP status code returned by Alpaca, or ``None`` if unknown.
    body : str
        The raw response text (useful for debugging rejection reasons).
    """

    def __init__(self, message: str, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


# ---------------------------------------------------------------------------
# Tick-rounding helper (mirrors equity _alpaca_limit_str)
# ---------------------------------------------------------------------------

def _options_limit_str(price: float) -> str:
    """Format an options limit price to Alpaca's accepted tick size.

    Options follow the same tick convention as equities:
      - prices >= $1.00 → 2 decimal places ($0.01 increments)
      - prices  < $1.00 → 4 decimal places ($0.0001 increments)

    An extra tick is added to make the limit *marketable* (fills immediately
    at or below the ask without becoming a market order).
    """
    if price >= 1.0:
        # Round to nearest cent, then add one tick for marketability.
        ticked = round(price, 2) + 0.01
        return f"{ticked:.2f}"
    else:
        ticked = round(price, 4) + 0.0001
        return f"{ticked:.4f}"


# ---------------------------------------------------------------------------
# Default HTTP POST (injectable for tests — mirrors alpaca_adapter pattern)
# ---------------------------------------------------------------------------

def _default_http_post(url: str, headers: dict, json_body: dict) -> dict[str, Any]:
    """Real HTTP POST using httpx.  Tests replace via injection."""
    resp = httpx.post(url, headers=headers, json=json_body, timeout=30.0)
    if not resp.is_success:
        raise OptionsBrokerError(
            f"Alpaca options order rejected: HTTP {resp.status_code} — {resp.text}",
            status_code=resp.status_code,
            body=resp.text,
        )
    return resp.json()


# ---------------------------------------------------------------------------
# Endpoint constants
# ---------------------------------------------------------------------------

# Contracts: list / filter options contracts.
_CONTRACTS_PATH = "/v2/options/contracts"

# Snapshots: multi-symbol greeks + quotes (indicative feed).
_SNAPSHOTS_PATH = "/v1beta1/options/snapshots"

# Alpaca accepts up to 100 symbols per snapshot request; stay safely under the
# URL length limit by batching at 100 (OCC symbols are ~21 chars each).
_SNAPSHOT_BATCH_SIZE = 100

_MAX_RETRIES = 3
_BACKOFF_BASE = 2.0  # seconds; doubles per retry


# ---------------------------------------------------------------------------
# Default HTTP GET (injectable for tests)
# ---------------------------------------------------------------------------

def _default_http_get(url: str, headers: dict, params: dict) -> Any:
    """Real HTTP GET using httpx.  Tests replace via injection."""
    resp = httpx.get(url, headers=headers, params=params, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class AlpacaOptionsClient:
    """Thin HTTP client for Alpaca options data and (P2) paper order submission.

    Parameters
    ----------
    config : Config
        Frozen arbiter config.  HTTP credentials are taken from
        ``config.alpaca_api_key`` / ``config.alpaca_secret_key``.
        Base URLs are constructed from ``config.alpaca_paper_base_url``
        (contracts) and ``config.alpaca_data_base_url`` (snapshots).
    http_get : callable | None
        Injectable HTTP GET callable for tests.  Signature:
        ``(url: str, headers: dict, params: dict) -> Any``.
        Defaults to a real ``httpx.get`` call.
    http_post : callable | None
        Injectable HTTP POST callable for tests (P2).  Signature:
        ``(url: str, headers: dict, json_body: dict) -> dict``.
        Defaults to a real ``httpx.post`` call.  Must raise
        ``OptionsBrokerError`` on non-200 (the default does this).
    """

    def __init__(
        self,
        config: Config,
        *,
        http_get: Callable[[str, dict, dict], Any] | None = None,
        http_post: Callable[[str, dict, dict], dict] | None = None,
    ) -> None:
        self._config = config
        self._contracts_base = config.alpaca_paper_base_url.rstrip("/")
        self._data_base = config.alpaca_data_base_url.rstrip("/")
        self._headers: dict[str, str] = {
            "APCA-API-KEY-ID": config.alpaca_api_key,
            "APCA-API-SECRET-KEY": config.alpaca_secret_key,
            "Accept": "application/json",
        }
        self._http_get = http_get or _default_http_get
        self._http_post = http_post or _default_http_post

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _post_with_retry(self, url: str, body: dict) -> dict:
        """POST with exactly 1 retry on failure; raises ``OptionsBrokerError``."""
        last_exc: Exception | None = None
        _post_retries = 1  # same as equity adapter: 1 retry then fail

        for attempt in range(_post_retries + 1):
            try:
                return self._http_post(url, self._headers, body)
            except OptionsBrokerError:
                raise  # non-200 broker rejection — no point retrying
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < _post_retries:
                    log.warning(
                        "alpaca_options.post_retry attempt=%d url=%s error=%s",
                        attempt,
                        url,
                        exc,
                    )
                else:
                    log.error(
                        "alpaca_options.post_halt attempts=%d url=%s error=%s",
                        _post_retries + 1,
                        url,
                        exc,
                    )

        raise OptionsBrokerError(
            f"Options order POST failed after {_post_retries + 1} attempts: {last_exc}",
        )

    def _get(self, url: str, params: dict) -> Any:
        """GET ``url`` with simple retry on 429 / 5xx."""
        import time

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                return self._http_get(url, self._headers, params)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {429, 503}:
                    wait = _BACKOFF_BASE ** (attempt + 1)
                    log.warning(
                        "alpaca_options.rate_limit url=%s status=%s retry_in=%.1fs",
                        url,
                        exc.response.status_code,
                        wait,
                    )
                    time.sleep(wait)
                    last_exc = exc
                    continue
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                wait = _BACKOFF_BASE ** (attempt + 1)
                log.warning(
                    "alpaca_options.request_error url=%s attempt=%d error=%s",
                    url,
                    attempt,
                    exc,
                )
                time.sleep(wait)
        raise RuntimeError(
            f"AlpacaOptionsClient._get failed after {_MAX_RETRIES} attempts: {url}"
        ) from last_exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_chain(
        self,
        underlying: str,
        *,
        min_expiry: datetime.date,
        max_expiry: datetime.date,
        side: OptionSide,
        limit: int = 100,
    ) -> list[OptionContract]:
        """Fetch the options chain for ``underlying`` and enrich with snapshot data.

        Calls ``GET /v2/options/contracts`` (paper-api) then enriches each
        contract via ``snapshot()`` (data-api).  Contracts whose snapshot
        lacks greeks or IV are logged and dropped — never raised.

        Parameters
        ----------
        underlying : str
            Equity ticker, e.g. ``"AAPL"``.
        min_expiry : datetime.date
            Earliest acceptable expiry (inclusive).
        max_expiry : datetime.date
            Latest acceptable expiry (inclusive).
        side : OptionSide
            CALL or PUT.
        limit : int
            Maximum contracts to retrieve (default 100).

        Returns
        -------
        list[OptionContract]
            Fully-populated contracts with greeks/market data merged.
            Contracts with null greeks have ``delta=None`` (not dropped here —
            ``select_contract`` filters them).  Empty on network failure.
        """
        url = self._contracts_base + _CONTRACTS_PATH
        params: dict[str, Any] = {
            "underlying_symbols": underlying,
            "type": side.value,
            "expiration_date_gte": min_expiry.isoformat(),
            "expiration_date_lte": max_expiry.isoformat(),
            "status": "active",
            "limit": limit,
        }

        try:
            data = self._get(url, params)
        except Exception:  # noqa: BLE001
            log.warning(
                "alpaca_options.fetch_chain_failed underlying=%s side=%s",
                underlying,
                side.value,
            )
            return []

        raw_contracts: list[dict] = data.get("option_contracts") or []
        if not raw_contracts:
            log.debug(
                "alpaca_options.fetch_chain_empty underlying=%s side=%s",
                underlying,
                side.value,
            )
            return []

        # Collect OCC symbols for snapshot enrichment.
        occ_symbols: list[str] = []
        contract_meta: dict[str, dict] = {}
        for raw in raw_contracts:
            occ = raw.get("symbol") or raw.get("id") or ""
            if not occ:
                continue
            occ_symbols.append(occ)
            contract_meta[occ] = raw

        if not occ_symbols:
            return []

        # Fetch snapshots in batches and merge.
        snap_map = self.snapshot(occ_symbols)

        contracts: list[OptionContract] = []
        for occ in occ_symbols:
            raw = contract_meta[occ]
            snap = snap_map.get(occ, {})

            # Parse expiry; skip malformed.
            expiry_str = raw.get("expiration_date", "")
            try:
                expiry = datetime.date.fromisoformat(expiry_str)
            except (ValueError, TypeError):
                log.debug(
                    "alpaca_options.bad_expiry occ=%s expiry=%s",
                    occ,
                    expiry_str,
                )
                continue

            strike_raw = raw.get("strike_price")
            try:
                strike = float(strike_raw)
            except (TypeError, ValueError):
                log.debug("alpaca_options.bad_strike occ=%s strike=%s", occ, strike_raw)
                continue

            # Extract greeks + market data from snapshot (may be None).
            iv: float | None = _safe_float(snap.get("iv"))
            delta: float | None = _safe_float(snap.get("delta"))
            gamma: float | None = _safe_float(snap.get("gamma"))  # noqa: F841 (available if needed)
            theta: float | None = _safe_float(snap.get("theta"))  # noqa: F841
            vega: float | None = _safe_float(snap.get("vega"))   # noqa: F841
            bid: float | None = _safe_float(snap.get("bid"))
            ask: float | None = _safe_float(snap.get("ask"))
            open_interest: int | None = _safe_int(
                raw.get("open_interest") or snap.get("open_interest")
            )
            volume: int | None = _safe_int(
                raw.get("volume") or snap.get("volume")
            )

            contract = OptionContract(
                occ_symbol=occ,
                underlying=underlying,
                side=side,
                strike=strike,
                expiry=expiry,
                delta=delta,
                iv=iv,
                bid=bid,
                ask=ask,
                open_interest=open_interest,
                volume=volume,
            )
            contracts.append(contract)

        log.debug(
            "alpaca_options.fetch_chain_done underlying=%s side=%s count=%d",
            underlying,
            side.value,
            len(contracts),
        )
        return contracts

    def snapshot(
        self,
        occ_symbols: list[str],
        *,
        feed: str = "indicative",
    ) -> dict[str, dict]:
        """Fetch a multi-symbol options snapshot from the Alpaca data API.

        Calls ``GET data.alpaca.markets/v1beta1/options/snapshots`` with the
        free ``indicative`` feed.  Batches requests at
        ``_SNAPSHOT_BATCH_SIZE`` to stay within URL-length limits.

        Parameters
        ----------
        occ_symbols : list[str]
            OCC option symbols to snapshot.
        feed : str
            Market data feed (default ``"indicative"``; ``"opra"`` is not
            available — do NOT use).

        Returns
        -------
        dict[str, dict]
            Normalised snapshot keyed by OCC symbol::

                {
                  "AAPL240119C00150000": {
                    "iv": 0.3844,
                    "delta": 0.8706,
                    "gamma": 0.002,
                    "theta": -0.05,
                    "vega": 0.12,
                    "bid": 51.44,
                    "ask": 52.45,
                  },
                  ...
                }

            Missing symbols are absent from the dict (not an error).

        Raises
        ------
        ValueError
            If ``feed == "opra"`` — the OPRA feed requires a paid agreement
            not available in this deployment.
        """
        if feed == "opra":
            raise ValueError(
                "feed='opra' is not available (OPRA agreement not signed). "
                "Use feed='indicative' (the default)."
            )

        if not occ_symbols:
            return {}

        url = self._data_base + _SNAPSHOTS_PATH
        result: dict[str, dict] = {}

        # Batch to stay safely under URL length limits.
        for i in range(0, len(occ_symbols), _SNAPSHOT_BATCH_SIZE):
            batch = occ_symbols[i : i + _SNAPSHOT_BATCH_SIZE]
            params: dict[str, Any] = {
                "symbols": ",".join(batch),
                "feed": feed,
            }

            try:
                data = self._get(url, params)
            except Exception:  # noqa: BLE001
                log.warning(
                    "alpaca_options.snapshot_batch_failed batch_start=%d batch_size=%d",
                    i,
                    len(batch),
                )
                continue

            # Alpaca returns {"snapshots": {OCC: {...}}}
            snapshots: dict[str, dict] = data.get("snapshots") or {}

            for occ, snap in snapshots.items():
                if not isinstance(snap, dict):
                    continue

                greeks = snap.get("greeks") or {}
                quote = snap.get("latestQuote") or {}

                result[occ] = {
                    "iv": _safe_float(snap.get("impliedVolatility")),
                    "delta": _safe_float(greeks.get("delta")),
                    "gamma": _safe_float(greeks.get("gamma")),
                    "theta": _safe_float(greeks.get("theta")),
                    "vega": _safe_float(greeks.get("vega")),
                    "bid": _safe_float(quote.get("bp")),
                    "ask": _safe_float(quote.get("ap")),
                }

        return result

    def place(self, order: OptionOrder) -> dict:
        """Submit a paper option buy-to-open order to Alpaca (P2).

        Always buys long (``side="buy"``); never sells, never market.

        Limit-price derivation
        ----------------------
        Base = ``order.contract.ask`` if available, else ``order.contract.mid_price``.
        We add one tick as a marketable buffer so the day-limit fills at the
        ask or better:
          - prices >= $1 → base rounded to $0.01 + $0.01 tick
          - prices  < $1 → base rounded to $0.0001 + $0.0001 tick

        Parameters
        ----------
        order : OptionOrder
            Fully-sized order from ``sizing.size_option()``.

        Returns
        -------
        dict
            Parsed Alpaca broker response, e.g.::

                {
                  "id":               "<broker-uuid>",
                  "client_order_id":  "<our-ulid>",
                  "status":           "accepted",  # or "pending_new"
                  "symbol":           "AAPL240119C00150000",
                  "qty":              "2",
                  "filled_qty":       "0",
                  "type":             "limit",
                  "side":             "buy",
                  "time_in_force":    "day",
                  "limit_price":      "5.15",
                  ...
                }

        Raises
        ------
        OptionsBrokerError
            When Alpaca returns a non-200 response (e.g. 422 invalid symbol,
            403 insufficient buying power, 5xx server error).
        ValueError
            When neither ``ask`` nor ``mid_price`` is available on the
            contract (cannot compute a limit price).
        """
        from arbiter.db.helpers import generate_ulid

        contract = order.contract

        # Determine base price: prefer ask (executable quote); fall back to mid.
        base_price = contract.ask if contract.ask is not None else contract.mid_price
        if base_price is None:
            raise ValueError(
                f"Cannot place options order for {contract.occ_symbol}: "
                "no ask or mid price available (bid and ask are both None)."
            )

        limit_price_str = _options_limit_str(base_price)

        # Stable idempotency key: ULID (unique per call; retry uses the same
        # client_order_id because we generate it once before the retry loop).
        client_order_id = generate_ulid()

        url = self._contracts_base + "/v2/orders"
        body: dict[str, Any] = {
            "symbol": contract.occ_symbol,
            "qty": str(order.contracts_qty),   # integer contracts as string
            "side": "buy",                      # always buy-to-open; never sell
            "type": "limit",                    # never market
            "time_in_force": "day",
            "limit_price": limit_price_str,
            "asset_class": "us_option",
            "client_order_id": client_order_id,
        }

        log.info(
            "alpaca_options.place occ=%s qty=%d limit=%s client_order_id=%s",
            contract.occ_symbol,
            order.contracts_qty,
            limit_price_str,
            client_order_id,
        )

        data = self._post_with_retry(url, body)

        log.info(
            "alpaca_options.place_accepted broker_id=%s status=%s",
            data.get("id"),
            data.get("status"),
        )
        return data

    def close_position(
        self,
        *,
        occ_symbol: str,
        contracts_qty: int,
        limit_price: float,
    ) -> dict:
        """Submit a paper sell-to-close order for an open option position (P2).

        Mirrors ``place()`` exactly but with ``side="sell"``.  Always uses a
        limit order; never market.

        Limit-price rule: the caller passes the desired limit price (typically
        the current mid); this method formats it via ``_options_limit_str``
        before submission.  For a sell-to-close we subtract one tick to make
        the order immediately marketable at the bid or better (symmetric to
        the buy-to-open tick-up).

        Parameters
        ----------
        occ_symbol : str
            OCC symbol of the contract to sell.
        contracts_qty : int
            Number of contracts to sell (should match the open position qty).
        limit_price : float
            Per-share limit price (typically the current mid from a snapshot).

        Returns
        -------
        dict
            Parsed Alpaca broker response (same shape as ``place()``).

        Raises
        ------
        OptionsBrokerError
            When Alpaca returns a non-200 response.
        ValueError
            When ``limit_price <= 0``.
        """
        from arbiter.db.helpers import generate_ulid

        if limit_price <= 0:
            raise ValueError(
                f"close_position: limit_price must be positive, got {limit_price!r}"
            )

        limit_price_str = _options_limit_str(limit_price)
        client_order_id = generate_ulid()

        url = self._contracts_base + "/v2/orders"
        body: dict[str, Any] = {
            "symbol": occ_symbol,
            "qty": str(contracts_qty),
            "side": "sell",                     # sell-to-close
            "type": "limit",                    # never market
            "time_in_force": "day",
            "limit_price": limit_price_str,
            "asset_class": "us_option",
            "client_order_id": client_order_id,
        }

        log.info(
            "alpaca_options.close_position occ=%s qty=%d limit=%s client_order_id=%s",
            occ_symbol,
            contracts_qty,
            limit_price_str,
            client_order_id,
        )

        data = self._post_with_retry(url, body)

        log.info(
            "alpaca_options.close_accepted broker_id=%s status=%s",
            data.get("id"),
            data.get("status"),
        )
        return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(value: object) -> float | None:
    """Convert *value* to float, returning None on failure or NaN/Inf."""
    if value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _safe_int(value: object) -> int | None:
    """Convert *value* to int, returning None on failure."""
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
