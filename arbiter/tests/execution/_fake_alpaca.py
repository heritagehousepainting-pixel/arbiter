"""Reusable in-memory fake Alpaca broker for OFFLINE tests (spec §5).

The real ``AlpacaAdapter`` routes every call through injectable HTTP callables
(``http_post`` / ``http_get`` / ``http_delete``).  ``FakeAlpaca`` provides those
callables backed by an in-memory positions/account/orders store so engine and
adapter tests never touch the network.

It models exactly the endpoints the adapter uses:

    POST   /v2/orders          → echo an order with controllable
                                 filled_qty/filled_avg_price/status; records
                                 client_order_id and REJECTS a duplicate one.
    GET    /v2/positions       → current positions.
    GET    /v2/account         → cash/equity/last_equity/position_count.
    GET    /v2/orders/{id}     → a single order's current status (for get_order).
    DELETE /v2/orders/{id}     → cancel.

Fill behaviour is controllable: by default orders fill immediately; set
``fill_mode="pending"`` to accept-but-not-fill (so the engine keeps the idea
pre-MONITORED), then call ``fill_order(order_id)`` to simulate a later fill that
the next-cycle reconciliation picks up.  ``fill_mode="partial"`` fills half.
"""
from __future__ import annotations

from typing import Any


class DuplicateClientOrderId(Exception):
    """Raised by the fake POST when a client_order_id is reused."""


class FakeAlpaca:
    """In-memory Alpaca stand-in wired into AlpacaAdapter via http_* callables."""

    def __init__(
        self,
        *,
        cash: float = 10_000.0,
        equity: float = 10_000.0,
        last_equity: float = 10_000.0,
        fill_mode: str = "filled",  # "filled" | "pending" | "partial"
    ) -> None:
        self.cash = cash
        self.equity = equity
        self.last_equity = last_equity
        self.fill_mode = fill_mode

        # order_id -> order dict (mirrors Alpaca JSON)
        self.orders: dict[str, dict[str, Any]] = {}
        # symbol -> position dict
        self.positions: dict[str, dict[str, Any]] = {}
        # client_order_ids seen (idempotency at the broker)
        self._client_order_ids: set[str] = set()
        # call counters for assertions
        self.post_count = 0

    # ------------------------------------------------------------------
    # HTTP callables to inject into AlpacaAdapter
    # ------------------------------------------------------------------

    def http_post(self, url: str, headers: dict, json_body: dict) -> dict[str, Any]:
        if url.endswith("/v2/orders"):
            self.last_order_body = json_body  # capture for assertions
            return self._place_order(json_body)
        raise AssertionError(f"FakeAlpaca: unexpected POST {url}")

    def http_get(self, url: str, headers: dict) -> Any:
        if url.endswith("/v2/positions"):
            return list(self.positions.values())
        if url.endswith("/v2/account"):
            return {
                "cash": str(self.cash),
                "buying_power": str(self.cash),
                "equity": str(self.equity),
                "last_equity": str(self.last_equity),
                "position_count": len(self.positions),
            }
        # GET /v2/orders:by_client_order_id?client_order_id={coid}  (status reconcile)
        # Mirrors real Alpaca: status is fetched by CLIENT order id, not the
        # broker UUID. Orders are stored keyed by client_order_id.
        if ":by_client_order_id" in url:
            from urllib.parse import parse_qs, urlparse  # noqa: PLC0415
            coid = parse_qs(urlparse(url).query).get("client_order_id", [""])[0]
            order = self.orders.get(coid)
            if order is None:
                # Real Alpaca returns 404 for an unknown client_order_id.
                raise AssertionError(f"FakeAlpaca: unknown client_order_id {coid}")
            return order
        # GET /v2/orders/{id}  (cancel/lookup by broker id path)
        marker = "/v2/orders/"
        if marker in url:
            order_id = url.rsplit("/", 1)[-1]
            order = self.orders.get(order_id)
            if order is None:
                raise AssertionError(f"FakeAlpaca: unknown order {order_id}")
            return order
        raise AssertionError(f"FakeAlpaca: unexpected GET {url}")

    def http_delete(self, url: str, headers: dict) -> Any:
        marker = "/v2/orders/"
        if marker in url:
            order_id = url.rsplit("/", 1)[-1]
            order = self.orders.get(order_id)
            if order is not None:
                order["status"] = "canceled"
            return {}
        raise AssertionError(f"FakeAlpaca: unexpected DELETE {url}")

    # ------------------------------------------------------------------
    # Test controls
    # ------------------------------------------------------------------

    def fill_order(self, order_id: str, *, avg_price: float | None = None) -> None:
        """Simulate a previously-pending order filling completely."""
        order = self.orders[order_id]
        qty = float(order["qty"])
        price = avg_price if avg_price is not None else float(order.get("limit_price") or 0.0)
        order["filled_qty"] = str(qty)
        order["filled_avg_price"] = str(price)
        order["status"] = "filled"
        self._apply_position(order["symbol"], qty, price)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _place_order(self, body: dict) -> dict[str, Any]:
        self.post_count += 1
        coid = body.get("client_order_id")
        if coid is not None and coid in self._client_order_ids:
            # Alpaca rejects a duplicate client_order_id → adapter sees non-200.
            raise DuplicateClientOrderId(f"duplicate client_order_id {coid}")
        if coid is not None:
            self._client_order_ids.add(coid)

        order_id = coid or f"broker-{self.post_count}"
        qty = float(body["qty"])
        limit_price = float(body.get("limit_price") or 0.0)

        if self.fill_mode == "filled":
            filled_qty = qty
            status = "filled"
        elif self.fill_mode == "partial":
            filled_qty = qty / 2.0
            status = "partially_filled"
        else:  # pending
            filled_qty = 0.0
            status = "accepted"

        order = {
            "id": order_id,
            "client_order_id": coid,
            "symbol": body["symbol"],
            "qty": str(qty),
            "filled_qty": str(filled_qty),
            "filled_avg_price": str(limit_price) if filled_qty > 0 else None,
            "limit_price": str(limit_price),
            "status": status,
        }
        self.orders[order_id] = order

        if filled_qty > 0:
            self._apply_position(body["symbol"], filled_qty, limit_price)

        return order

    def _apply_position(self, symbol: str, qty: float, price: float) -> None:
        pos = self.positions.get(symbol)
        if pos is None:
            self.positions[symbol] = {
                "symbol": symbol,
                "qty": str(qty),
                "avg_entry_price": str(price),
            }
        else:
            old_qty = float(pos["qty"])
            old_price = float(pos["avg_entry_price"])
            new_qty = old_qty + qty
            new_price = (old_qty * old_price + qty * price) / new_qty if new_qty else 0.0
            pos["qty"] = str(new_qty)
            pos["avg_entry_price"] = str(new_price)
