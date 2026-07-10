"""Alpaca live/paper adapter for Lane 12b execution.

Selected ONLY when ``executor_backend == "alpaca_paper"`` AND both
``alpaca_api_key`` and ``alpaca_secret_key`` are non-empty (INTERFACES.md §9).
``live_trading`` does NOT select this adapter (it is reserved for a future
live path); see ``build_executor()``.

In all other cases ``build_executor()`` returns a ``SimExecutor``.

Network is MOCKED in tests — no live HTTP calls are made by this module
directly; they go through the ``_http_post`` helper which tests replace.

Retry / halt contract (INTERFACES.md §9):
    On a broker non-200 response: 1 retry then halt+alert.
    The ``HaltSignal`` exception bubbles to ``submit_order`` which is
    responsible for triggering the alert.

Method names (INTERFACES.md §10b.2):
    place, cancel, get_positions, get_account.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from arbiter.config import Config
from arbiter.shared.executor import (
    AccountSnapshot,
    ExecutionReport,
    Executor,
    OrderIntent,
    PositionSnapshot,
)
from arbiter.shared.sim_executor import SimExecutor
from arbiter.types import OrderSide

log = structlog.get_logger(__name__)

_MAX_RETRIES = 1  # 1 retry then halt


def _alpaca_limit_str(price: float) -> str:
    """Format a limit price to Alpaca's accepted tick size.

    Alpaca rejects sub-penny prices with HTTP 422: limit prices for equities
    >= $1.00 must be in $0.01 increments; below $1.00, $0.0001 increments.
    The slippage-adjusted price is a raw float (many decimals), so we quantize
    here at the broker boundary.
    """
    return f"{price:.2f}" if price >= 1.0 else f"{price:.4f}"


# ---------------------------------------------------------------------------
# Halt signal (re-exported from idempotency to avoid circular imports)
# ---------------------------------------------------------------------------

class BrokerError(Exception):
    """Raised when the broker returns a non-200 after max retries."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Rejection classification (2026-07-10 SPCX incident)
# ---------------------------------------------------------------------------
#
# A broker non-200 on ORDER SUBMISSION is either:
#   * SYMBOL-LEVEL — a per-order 4xx caused by an invalid/untradable/unknown
#     asset (422 unprocessable, 404 asset-not-found, "asset ... not tradable").
#     Skipping that ONE order and continuing the cycle is safe; halting all
#     trading over it is not (one bad news-advisor ticker paused the engine).
#   * SYSTEMIC — auth (401), account-level (403), rate-limit (429), 5xx,
#     timeouts/connectivity.  Continuing is unsafe: keep the broker-fatal
#     auto-pause.
#
# Fail-safe default: anything unrecognized classifies as SYSTEMIC.

_SYMBOL_REJECTION_STATUSES: frozenset[int] = frozenset({404, 422})

# A 422 caused by a DUPLICATE client_order_id (lost-response retry) is NOT a
# symbol problem — the first POST likely succeeded at the broker, so skipping
# would strand an untracked live order.  Force these back to broker-fatal so
# the existing halt + reconcile posture applies.
_SYSTEMIC_OVERRIDE_PATTERNS: tuple[str, ...] = (
    "client_order_id",
    "must be unique",
    "duplicate",
)

# Reject-text fragments that mark a symbol-level rejection when no HTTP status
# code survived the seam (e.g. a transport that raises without a .response).
_SYMBOL_REJECTION_PATTERNS: tuple[str, ...] = (
    "not tradable",
    "not tradeable",
    "asset not found",
    "asset is not found",
    "symbol not found",
    "invalid symbol",
    "asset is not active",
    "could not find asset",
)

# Targeted HTTP-code extraction from a stringified error.  Deliberately narrow
# so "Broker non-200" can never parse as code 200:
#   [HTTP 422]   — stamped by place() into ExecutionReport.reject_reason;
#   error '422   — httpx's HTTPStatusError message style;
#   HTTP 503     — plain "HTTP <code>" phrasing (test fakes / transports).
_HTTP_CODE_RE = re.compile(r"(?:\[http (\d{3})\]|error '(\d{3})|http[ /:](\d{3}))")


def is_symbol_rejection(message: str, status_code: int | None = None) -> bool:
    """True when a broker order rejection is a PER-ORDER symbol-level 4xx.

    Callers treat True as non-fatal (skip the order, blacklist the ticker for
    the session, continue the cycle) and False as broker-fatal (existing
    auto-pause).  Classification order:

    1. Systemic overrides (duplicate client_order_id wording) → False.
    2. ``status_code`` — explicit, or parsed from *message* via the narrow
       ``_HTTP_CODE_RE`` patterns — 404/422 → True, any other code → False.
    3. No code at all: symbol-naming reject text ("not tradable", …) → True.
    4. Anything else (timeouts, connectivity, unknown text) → False.
    """
    msg = (message or "").lower()
    if any(p in msg for p in _SYSTEMIC_OVERRIDE_PATTERNS):
        return False
    if status_code is None:
        match = _HTTP_CODE_RE.search(msg)
        if match is not None:
            status_code = int(next(g for g in match.groups() if g))
    if status_code is not None:
        return status_code in _SYMBOL_REJECTION_STATUSES
    return any(p in msg for p in _SYMBOL_REJECTION_PATTERNS)


# ---------------------------------------------------------------------------
# HTTP helper (injectable for tests)
# ---------------------------------------------------------------------------

def _default_http_post(url: str, headers: dict, json_body: dict) -> dict[str, Any]:
    """Real HTTP POST using httpx.  Tests replace this via monkeypatching."""
    import httpx  # imported lazily — not available in all test environments

    resp = httpx.post(url, headers=headers, json=json_body, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def _default_http_get(url: str, headers: dict) -> Any:
    """Real HTTP GET using httpx."""
    import httpx

    resp = httpx.get(url, headers=headers, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def _default_http_delete(url: str, headers: dict) -> Any:
    """Real HTTP DELETE using httpx."""
    import httpx

    resp = httpx.delete(url, headers=headers, timeout=30.0)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


# ---------------------------------------------------------------------------
# Alpaca adapter
# ---------------------------------------------------------------------------

@dataclass
class AlpacaAdapter(Executor):
    """Thin wrapper around Alpaca's paper/live order API.

    Parameters
    ----------
    config:
        Frozen Config with Alpaca credentials and URLs.
    http_post:
        Replaceable HTTP POST callable (for test mocking).
    http_get:
        Replaceable HTTP GET callable (for test mocking).
    http_delete:
        Replaceable HTTP DELETE callable (for test mocking).
    """

    name: str = field(default="alpaca_paper", init=False)

    config: Config
    http_post: Any = field(default=_default_http_post)
    http_get: Any = field(default=_default_http_get)
    http_delete: Any = field(default=_default_http_delete)
    # Per-instance cache for is_fractionable (asset flag is static).
    _fractionable_cache: dict[str, bool] = field(
        default_factory=dict, init=False, repr=False
    )

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.config.alpaca_api_key,
            "APCA-API-SECRET-KEY": self.config.alpaca_secret_key,
            "Content-Type": "application/json",
        }

    def _base(self) -> str:
        return self.config.alpaca_paper_base_url.rstrip("/")

    def _post_with_retry(self, url: str, body: dict) -> dict:
        """POST with exactly 1 retry on non-200; raises BrokerError on failure."""
        headers = self._headers()
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES + 1):  # 0 and 1
            try:
                result = self.http_post(url, headers, body)
                return result
            except Exception as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    log.warning(
                        "alpaca_adapter.retry",
                        attempt=attempt,
                        url=url,
                        error=str(exc),
                    )

        # Recover the HTTP status + response body when the transport exposes
        # them (httpx.HTTPStatusError carries .response).  The body is what
        # names the actual failure ("asset SPCX is not tradable") — without it
        # the halt log said nothing about WHY (2026-07-10 SPCX incident).
        resp = getattr(last_exc, "response", None)
        status_code = getattr(resp, "status_code", None) if resp is not None else None
        body_text = ""
        if resp is not None:
            try:
                body_text = str(getattr(resp, "text", "") or "")[:500]
            except Exception:  # noqa: BLE001 — body recovery is best-effort
                body_text = ""
        detail = f"{last_exc}" + (f" | {body_text}" if body_text else "")
        message = f"Broker non-200 after {_MAX_RETRIES + 1} attempts: {detail}"

        # Log honestly: a symbol-level 4xx is handled NON-fatally upstream
        # (submit_order skips the order; the engine blacklists the ticker), so
        # only a genuinely systemic failure logs the ``halt`` event.
        if is_symbol_rejection(message, status_code=status_code):
            log.warning(
                "alpaca_adapter.order_rejected",
                attempts=_MAX_RETRIES + 1,
                url=url,
                status_code=status_code,
                error=detail,
            )
        else:
            log.error(
                "alpaca_adapter.halt",
                attempts=_MAX_RETRIES + 1,
                url=url,
                status_code=status_code,
                error=detail,
            )

        raise BrokerError(message, status_code=status_code)

    # ------------------------------------------------------------------
    # Executor interface (INTERFACES.md §10b.2)
    # ------------------------------------------------------------------

    def place(self, intent: OrderIntent) -> ExecutionReport:
        """Submit an equity order to Alpaca."""
        url = f"{self._base()}/v2/orders"
        body: dict[str, Any] = {
            "symbol": intent.ticker,
            "qty": str(intent.qty),
            "side": intent.side.value.lower(),
            "type": "limit" if intent.limit_price is not None else "market",
            "time_in_force": "day",
            # client_order_id makes the single retry idempotent at the broker:
            # Alpaca rejects a duplicate client_order_id, so a lost-response
            # retry cannot create a second order (spec §4.5).
            "client_order_id": intent.order_id,
        }
        if intent.limit_price is not None:
            # Alpaca rejects sub-penny limit prices (422). Quantize to the
            # accepted tick: $0.01 for prices >= $1, $0.0001 below $1.
            body["limit_price"] = _alpaca_limit_str(intent.limit_price)

        try:
            data = self._post_with_retry(url, body)
        except BrokerError as exc:
            # Stamp the HTTP status into the reject_reason so downstream
            # classification (submit_order → is_symbol_rejection) survives the
            # exception→report seam, where only this string is carried.
            reject_reason = (
                f"[HTTP {exc.status_code}] {exc}" if exc.status_code is not None else str(exc)
            )
            return ExecutionReport(
                order_id=intent.order_id,
                ticker=intent.ticker,
                side=intent.side,
                status="rejected",
                filled_qty=0.0,
                avg_fill_price=None,
                gross_notional=0.0,
                realized_pl=None,
                reject_reason=reject_reason,
                executor=self.name,
                paper_only=True,  # structurally paper-only: adapter only ever hits the paper endpoint (§2, §4.1)
            )

        fill_qty = float(data.get("filled_qty", 0.0))
        avg_price_raw = data.get("filled_avg_price")
        avg_price = float(avg_price_raw) if avg_price_raw else intent.limit_price

        return ExecutionReport(
            order_id=intent.order_id,
            ticker=intent.ticker,
            side=intent.side,
            status="filled" if fill_qty > 0 else "pending",
            filled_qty=fill_qty,
            avg_fill_price=avg_price,
            gross_notional=(avg_price or 0.0) * fill_qty,
            realized_pl=None,
            reject_reason="",
            executor=self.name,
            paper_only=True,  # structurally paper-only: adapter only ever hits the paper endpoint (§2, §4.1)
        )

    def cancel(self, order_id: str) -> ExecutionReport:
        """Cancel a pending order at Alpaca."""
        url = f"{self._base()}/v2/orders/{order_id}"
        headers = self._headers()
        try:
            self.http_delete(url, headers)
            status: str = "cancelled"
        except Exception as exc:
            log.warning("alpaca_adapter.cancel_failed", order_id=order_id, error=str(exc))
            status = "rejected"

        return ExecutionReport(
            order_id=order_id,
            ticker="",
            side=OrderSide.BUY,
            status=status,  # type: ignore[arg-type]
            filled_qty=0.0,
            avg_fill_price=None,
            gross_notional=0.0,
            realized_pl=None,
            reject_reason="" if status == "cancelled" else "cancel failed",
            executor=self.name,
            paper_only=True,  # structurally paper-only: adapter only ever hits the paper endpoint (§2, §4.1)
        )

    def get_order(self, order_id: str) -> ExecutionReport:
        """Fetch a single order's current status from Alpaca (A1).

        Hits ``GET /v2/orders/{order_id}`` and maps the broker order into an
        ExecutionReport.  Used by the engine's pending→filled reconciliation:
        a position can exist without telling us *which* pending idea filled,
        so reconciling by order id is the correct primitive.

        On any HTTP error a ``status="pending"`` report is returned (treat as
        not-yet-known rather than asserting a fill) — the adapter is paper-only.

        NOTE: ``order_id`` is OUR id, which we set as the Alpaca
        ``client_order_id`` at placement. Alpaca's ``GET /v2/orders/{id}`` takes
        the broker's own UUID, so we must look up by client id instead
        (``GET /v2/orders:by_client_order_id``), or every reconcile 422s.
        """
        url = f"{self._base()}/v2/orders:by_client_order_id?client_order_id={order_id}"
        headers = self._headers()
        try:
            data = self.http_get(url, headers)
        except Exception as exc:  # noqa: BLE001
            log.warning("alpaca_adapter.get_order_failed", order_id=order_id, error=str(exc))
            return ExecutionReport(
                order_id=order_id,
                ticker="",
                side=OrderSide.BUY,
                status="pending",
                filled_qty=0.0,
                avg_fill_price=None,
                gross_notional=0.0,
                realized_pl=None,
                reject_reason="",
                executor=self.name,
                paper_only=True,
            )

        broker_status = str(data.get("status", "") or "")
        fill_qty = float(data.get("filled_qty", 0.0) or 0.0)
        avg_price_raw = data.get("filled_avg_price")
        avg_price = float(avg_price_raw) if avg_price_raw else None

        # Map Alpaca order status → ExecutionReport status.
        # Alpaca: new/accepted/pending_new/partially_filled/filled/canceled/rejected/expired
        if broker_status in ("rejected",):
            status = "rejected"
        elif broker_status in ("canceled", "expired"):
            status = "cancelled"
        elif fill_qty > 0.0:
            # Distinguish full vs partial using requested qty when available.
            req_qty_raw = data.get("qty")
            req_qty = float(req_qty_raw) if req_qty_raw else fill_qty
            status = "filled" if fill_qty >= req_qty else "partial"
        else:
            status = "pending"

        return ExecutionReport(
            order_id=order_id,
            ticker=str(data.get("symbol", "") or ""),
            side=OrderSide.BUY,
            status=status,  # type: ignore[arg-type]
            filled_qty=fill_qty,
            avg_fill_price=avg_price,
            gross_notional=(avg_price or 0.0) * fill_qty,
            realized_pl=None,
            reject_reason=str(data.get("reject_reason", "") or ""),
            executor=self.name,
            paper_only=True,
        )

    def get_positions(self) -> dict[str, PositionSnapshot]:
        """Return open positions from Alpaca."""
        url = f"{self._base()}/v2/positions"
        headers = self._headers()
        try:
            data = self.http_get(url, headers)
        except Exception as exc:
            log.warning("alpaca_adapter.get_positions_failed", error=str(exc))
            return {}

        positions: dict[str, PositionSnapshot] = {}
        for pos in data:
            ticker = pos.get("symbol", "")
            if ticker:
                positions[ticker] = PositionSnapshot(
                    ticker=ticker,
                    shares=float(pos.get("qty", 0.0)),
                    avg_price=float(pos.get("avg_entry_price", 0.0)),
                )
        return positions

    def is_fractionable(self, ticker: str) -> bool:
        """Whether Alpaca marks the asset fractionable (Tier-2 #4).

        Consulted by ``submit_order`` before the fractional-share fallback.
        Cached per adapter instance (asset fractionability is static).
        Fail-closed: on any fetch/parse error return False WITHOUT caching, so
        the caller degrades to the legacy zero-share skip instead of risking a
        422 rejection that would trip the broker_non_200 breaker.
        """
        cached = self._fractionable_cache.get(ticker)
        if cached is not None:
            return cached
        url = f"{self._base()}/v2/assets/{ticker}"
        try:
            data = self.http_get(url, self._headers())
            result = bool(data.get("fractionable", False))
        except Exception as exc:
            log.warning(
                "alpaca_adapter.is_fractionable_failed", ticker=ticker, error=str(exc)
            )
            return False
        self._fractionable_cache[ticker] = result
        return result

    def get_account(self) -> AccountSnapshot:
        """Return account snapshot from Alpaca."""
        url = f"{self._base()}/v2/account"
        headers = self._headers()
        try:
            data = self.http_get(url, headers)
        except Exception as exc:
            log.warning("alpaca_adapter.get_account_failed", error=str(exc))
            return AccountSnapshot(
                cash=0.0,
                buying_power=0.0,
                realized_pl=0.0,
                daily_pl=0.0,
                open_positions=0,
                paper_only=True,  # structurally paper-only: adapter only ever hits the paper endpoint (§2, §4.1)
            )

        # open_positions: Alpaca provides "position_count" or we fall back to
        # len(get_positions()).  "position_market_value" is a dollar value, NOT
        # a count — using it as an int would produce a wildly wrong number.
        position_count_raw = data.get("position_count")
        if position_count_raw is not None:
            open_positions = int(position_count_raw)
        else:
            open_positions = len(self.get_positions())

        # realized_pl: Alpaca's /v2/account has no direct realized_pl field.
        # "regt_buying_power" is buying power, not realized P&L.  We record
        # 0.0 here; a separate P&L endpoint or local ledger is needed for this.
        return AccountSnapshot(
            cash=float(data.get("cash", 0.0)),
            buying_power=float(data.get("buying_power", 0.0)),
            realized_pl=0.0,  # Alpaca /v2/account has no direct realized_pl field
            daily_pl=float(data.get("equity", 0.0)) - float(data.get("last_equity", 0.0)),
            open_positions=open_positions,
            paper_only=True,  # structurally paper-only: adapter only ever hits the paper endpoint (§2, §4.1)
            equity=float(data.get("equity", 0.0)),
        )


# ---------------------------------------------------------------------------
# Factory — executor selection (INTERFACES.md §9, §10b.2)
# ---------------------------------------------------------------------------

def build_executor(config: Config, **adapter_kwargs: Any) -> Executor:
    """Return the appropriate executor for the given config.

    Selection rule (INTERFACES.md §9, spec §4.1):
        executor_backend == "alpaca_paper" AND both Alpaca keys present
            → AlpacaAdapter (paper endpoint only — §2)
        otherwise → SimExecutor (fail-closed default)

    ``live_trading`` is NOT consulted here: it is reserved for a future
    real-money path that does not exist yet and stays false.  The adapter is
    structurally paper-only regardless.

    Parameters
    ----------
    config:
        Frozen runtime config.
    **adapter_kwargs:
        Optional overrides passed to AlpacaAdapter (e.g. http_post for tests).

    Returns
    -------
    Executor
        Either an AlpacaAdapter or a SimExecutor instance.
    """
    if (
        config.executor_backend == "alpaca_paper"
        and config.alpaca_api_key
        and config.alpaca_secret_key
    ):
        log.info("build_executor.alpaca", backend=config.executor_backend)
        return AlpacaAdapter(config=config, **adapter_kwargs)

    log.info("build_executor.sim", backend=config.executor_backend)
    return SimExecutor()
