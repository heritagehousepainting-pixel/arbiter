"""Order submission for Lane 12b execution.

submit_order(order, executor, clock) -> SubmitResult
    - Slippage-adjusts the fill price via model_slippage.
    - Converts the dollar NOTIONAL (order.qty) into a whole SHARE count using
      the slippage-adjusted limit_price (spec A0); skips 0-share orders.
    - Passes adjusted price as limit_price on the OrderIntent (INTERFACES.md §10b.3).
    - Inserts into orders with dedup_hash UNIQUE (duplicate → skip, idempotent).
    - Persists exits transactionally with the position.
    - Audits every submission and outcome.
    - 1 retry on broker error then halt+alert (delegated to AlpacaAdapter).

Returns a small ``SubmitResult`` so the engine can distinguish
filled / pending / duplicate / zero-share outcomes (spec §4.4, A0).

No datetime.now() — clock is passed in by the caller (INTERFACES.md §11.1).
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from arbiter.contract.seams import PaperOrder
from arbiter.data.slippage import model_slippage
from arbiter.types import OrderSide
from arbiter.db.audit import audit as _audit
from arbiter.db.helpers import generate_ulid, insert_row
from arbiter.execution.idempotency import (
    DuplicateOrderError,
    dedup_hash,
    ensure_not_duplicate,
)
from arbiter.shared.executor import Executor, OrderIntent

if TYPE_CHECKING:
    from arbiter.data.clock import Clock
    from arbiter.safety.breakers import CircuitBreaker

log = structlog.get_logger(__name__)

# Sentinel status strings (kept for back-compat with audit semantics).
_SKIP_SENTINEL = "DUPLICATE_SKIP"
_ZERO_SHARE_SKIP = "ZERO_SHARE_SKIP"


@dataclass(frozen=True)
class SubmitResult:
    """Outcome of a ``submit_order`` call.

    Attributes
    ----------
    order_id:
        The submitted order's ULID, or ``None`` when nothing was placed
        (duplicate or zero-share skip).
    status:
        One of the broker ``OrderStatus`` values ("filled", "pending",
        "partial", "rejected") for a placed order, or one of the sentinel
        strings ``"DUPLICATE_SKIP"`` / ``"ZERO_SHARE_SKIP"`` when skipped.
    duplicate:
        True when the order was skipped as a duplicate (local ledger,
        broker position, or UNIQUE-constraint race).
    zero_share:
        True when the notional rounded to 0 shares and nothing was placed.
    avg_fill_price:
        The broker ``ExecutionReport.avg_fill_price`` for a placed order (the
        REAL fill price), or ``None`` when nothing was placed or the broker did
        not report a fill price.  Lets callers (e.g. the exit monitor's
        synchronous close-out) read the actual fill without reaching into the
        executor's private ``_reports``.
    filled_notional:
        Realized notional USD = ``avg_fill_price × filled_qty`` for a placed
        order (``None`` when nothing was placed or no fill price).  On a FULL
        fill this equals the requested notional; on a PARTIAL it is smaller.
        Callers fold THIS into the risk book, not the requested ``order.qty``
        (``filled_qty`` is in SHARES; the book speaks notional USD — surfacing
        the already-multiplied notional keeps the units honest at the seam).
    """

    order_id: str | None
    status: str
    duplicate: bool = False
    zero_share: bool = False
    avg_fill_price: float | None = None
    filled_notional: float | None = None

    @property
    def filled(self) -> bool:
        """True only on a confirmed broker fill (advance idea → MONITORED)."""
        return self.status == "filled"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _serialize_exits(exits: dict) -> str:
    """Serialize the exits dict to a JSON string, coercing date objects."""
    return json.dumps(exits, default=str)


def _insert_order_row(
    conn: sqlite3.Connection,
    order: PaperOrder,
    dh: str,
    fill_price: float,
    status: str,
    as_of: datetime,
    *,
    qty: float,
) -> None:
    """Insert the order into the local ledger.

    ``qty`` is the SHARE count (already converted from notional by the
    caller per spec A0), so the ledger, reconciliation, and the broker all
    agree in shares.
    """
    insert_row(conn, "orders", {
        "order_id": order.order_id,
        "dedup_hash": dh,
        "ticker": order.ticker,
        "side": order.side.value,
        "qty": qty,
        "horizon_bucket": order.horizon_bucket.value,
        "entry_date": str(order.entry_date),
        "advisor_signature": order.advisor_signature,
        "exits_json": _serialize_exits(order.exits),
        "status": status,
        "created_at": as_of.isoformat(),
    })


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def submit_order(
    order: PaperOrder,
    executor: Executor,
    clock: "Clock",
    *,
    conn: sqlite3.Connection,
    spread: float = 0.01,
    raw_price: float | None = None,
    breaker: "CircuitBreaker | None" = None,
    audit_path: str | None = None,
    presized_shares: int | None = None,
    is_exit: bool = False,
) -> SubmitResult:
    """Submit a PaperOrder through the executor with idempotency guarantees.

    Parameters
    ----------
    order:
        The PaperOrder to submit (from Lane 12 policy).
    executor:
        Active executor (SimExecutor or AlpacaAdapter).
    clock:
        Lane-3 clock; ``clock.now()`` is called once for the as_of timestamp.
    conn:
        Open SQLite connection (orders table must be migrated).
    spread:
        Bid-ask spread in price units; passed to model_slippage.
        Caller should source this from PITGateway.get("spread", ...).
    raw_price:
        Real entry price (e.g. price_open from PITGateway).  If None,
        falls back to ``order.raw_price`` attribute (for test backward-compat).
        A missing/zero price will use 0.0 → a near-zero limit_price, which
        SimExecutor will fill but AlpacaAdapter will reject.  Callers MUST
        supply a real price for live/paper paths (engine.py enforces this).
    breaker:
        Optional CircuitBreaker.  If supplied and the broker returns a
        ``rejected`` status, ``broker_non_200`` is tripped and a
        BrokerHaltError is raised to abort further submissions this cycle.
    audit_path:
        Override the audit log path (for tests).
    presized_shares:
        Exit-monitor B3.  When set, ``order.qty`` is NOT treated as a dollar
        notional: the A0 notional→shares divide is SKIPPED and this whole
        share count is used directly (and persisted as the ledger ``qty``).
        Used for EXIT SELLs which size in shares (the held position qty).
    is_exit:
        Exit-monitor B3.  When True the order is an EXIT SELL: idempotency
        routes to a LOCAL-LEDGER-ONLY check (the broker position-presence
        check would otherwise block every sell, since holding the position is
        the precondition).  Sell-side slippage (B1) is applied so the limit is
        biased DOWN.  A repeated identical SELL (same dedup_hash) stays blocked.

    Returns
    -------
    SubmitResult
        ``order_id`` + ``status`` for a placed order, or a sentinel result
        (``duplicate=True`` / ``zero_share=True``) when nothing was placed.

    Raises
    ------
    BrokerError
        If the broker rejects after 1 retry (bubbles from AlpacaAdapter).
    """
    as_of: datetime = clock.now()
    dh = dedup_hash(order)

    # ------------------------------------------------------------------
    # 1. Idempotency check — local ledger + broker
    # ------------------------------------------------------------------
    try:
        ensure_not_duplicate(order, conn, executor, dh=dh, is_exit=is_exit)
    except DuplicateOrderError as exc:
        log.info("submit_order.skip_duplicate", order_id=order.order_id, reason=str(exc))
        _audit(
            "order.duplicate_skip",
            {"order_id": order.order_id, "dedup_hash": dh, "reason": str(exc)},
            ts=as_of.isoformat(),
            audit_path=audit_path,
        )
        return SubmitResult(order_id=None, status=_SKIP_SENTINEL, duplicate=True)

    # ------------------------------------------------------------------
    # 2. Slippage-adjusted fill price (INTERFACES.md §10b.3)
    #    raw_price must come from PITGateway.get("price_open", ...) via
    #    the caller (engine.py).  The engine enforces fail-closed: if
    #    price_open is None the order is NOT submitted (returns False
    #    before calling submit_order).  The $1.00 stub fallback has been
    #    removed — a missing price here is a bug in the caller.
    # ------------------------------------------------------------------
    effective_raw_price: float
    if raw_price is not None and raw_price > 0.0:
        effective_raw_price = raw_price
    else:
        # Backward-compat: check order attribute (used in some unit tests).
        effective_raw_price = float(getattr(order, "raw_price", 0.0))
    if effective_raw_price <= 0.0:
        # Fail closed: a missing/zero price must NEVER silently fill at ~$0.
        raise ValueError(
            f"submit_order requires a positive raw_price (got {effective_raw_price!r}); "
            "the caller must supply price_open from the PIT gateway."
        )
    # Sell-side slippage (B1) biases the limit DOWN to keep the SELL marketable;
    # the BUY default biases UP.  We use the order's side so exit SELLs get the
    # correct direction even when called without is_exit.
    limit_price = model_slippage(effective_raw_price, spread, side=order.side)

    # ------------------------------------------------------------------
    # 2b. Share sizing.
    #     - Default (entry BUY): order.qty is a DOLLAR NOTIONAL (quarter-Kelly
    #       USD from compute_size), NOT a share count.  Convert at the
    #       slippage-adjusted limit_price and floor to whole shares (spec A0).
    #       Alpaca rejects fractional LIMIT orders, so whole-share rounding is a
    #       correctness requirement, not just a guard.
    #     - Exit SELL (presized_shares set, B3): order.qty is ALREADY a share
    #       count (the held position qty).  SKIP the A0 divide entirely and use
    #       the presized share count directly.
    # ------------------------------------------------------------------
    notional = float(order.qty)
    if presized_shares is not None:
        shares = int(presized_shares)
    else:
        shares = math.floor(notional / limit_price)
    if shares <= 0:
        log.info(
            "submit_order.zero_share_skip",
            order_id=order.order_id,
            ticker=order.ticker,
            notional=notional,
            limit_price=limit_price,
        )
        _audit(
            "order.zero_share_skip",
            {
                "order_id": order.order_id,
                "ticker": order.ticker,
                "notional": notional,
                "limit_price": limit_price,
            },
            ts=as_of.isoformat(),
            audit_path=audit_path,
        )
        return SubmitResult(order_id=None, status=_ZERO_SHARE_SKIP, zero_share=True)

    share_qty = float(shares)

    # ------------------------------------------------------------------
    # 3. Build OrderIntent and place (qty is now a SHARE count)
    # ------------------------------------------------------------------
    intent = OrderIntent(
        order_id=order.order_id,
        ticker=order.ticker,
        side=order.side,
        qty=share_qty,
        limit_price=limit_price,
    )

    log.info(
        "submit_order.placing",
        order_id=order.order_id,
        ticker=order.ticker,
        side=order.side.value,
        notional=notional,
        qty=share_qty,
        limit_price=limit_price,
        executor=executor.name,
    )

    report = executor.place(intent)

    # ------------------------------------------------------------------
    # 3b. Broker rejection → NEVER persist + abort (Finding 4 / D1 P1)
    #     A ``rejected`` report must NEVER persist an order row — breaker or
    #     not.  Persisting a rejected order would poison the dedup slot
    #     (UNIQUE(dedup_hash)) so a later legitimate retry of the same order
    #     would be silently skipped as a "duplicate".  So the not-persisted
    #     guarantee is UNCONDITIONAL on the breaker.
    #
    #     The breaker latch is the only conditional part: if a breaker was
    #     supplied, trip broker_non_200 to halt further submissions this cycle.
    #     SimExecutor never rejects, so this path is live-only in practice.
    # ------------------------------------------------------------------
    if report.status == "rejected":
        log.error(
            "submit_order.broker_rejected",
            order_id=order.order_id,
            ticker=order.ticker,
            reason=report.reject_reason,
        )
        if breaker is not None:
            from arbiter.safety.breakers import BreakerTrippedError  # noqa: PLC0415
            try:
                breaker.check_broker_non_200(
                    status_code=503,  # non-200 sentinel (actual code not available here)
                    endpoint=executor.name,
                    conn=conn,
                    clock=None,
                    audit_path=audit_path,
                )
            except BreakerTrippedError:
                pass  # already latched; the raise below stops further submissions
        # Raise to abort cycle — do NOT persist the rejected order (breaker or not).
        from arbiter.execution.alpaca_adapter import BrokerError  # noqa: PLC0415
        raise BrokerError(
            f"Broker rejected order {order.order_id} for {order.ticker}: {report.reject_reason}"
        )

    # ------------------------------------------------------------------
    # 4. Persist into local ledger (INSERT with UNIQUE dedup_hash)
    #    SQLite UNIQUE constraint is the idempotency backstop.
    # ------------------------------------------------------------------
    status = report.status
    # D4 P2: a ``partial`` fill must persist the ACTUALLY-FILLED qty, not the
    # requested share count — otherwise ledger-based exposure / P&L (which sums
    # the orders.qty column) over-states the position by the unfilled remainder.
    # This matters most for partial SELLs, where the requested qty is the whole
    # held position but only part executed.  A full fill persists share_qty.
    ledger_qty = float(report.filled_qty) if status == "partial" else share_qty
    try:
        _insert_order_row(conn, order, dh, limit_price, status, as_of, qty=ledger_qty)
    except sqlite3.IntegrityError:
        # Rare race: another process/thread inserted between our check and insert.
        log.info(
            "submit_order.integrity_error_skip",
            order_id=order.order_id,
            dedup_hash=dh,
        )
        _audit(
            "order.race_skip",
            {"order_id": order.order_id, "dedup_hash": dh},
            ts=as_of.isoformat(),
            audit_path=audit_path,
        )
        return SubmitResult(order_id=None, status=_SKIP_SENTINEL, duplicate=True)

    # ------------------------------------------------------------------
    # 5. Audit
    # ------------------------------------------------------------------
    _audit(
        "order.submitted",
        {
            "order_id": order.order_id,
            "dedup_hash": dh,
            "ticker": order.ticker,
            "side": order.side.value,
            "qty": share_qty,
            "notional": notional,
            "limit_price": limit_price,
            "status": status,
            "executor": executor.name,
            "filled_qty": report.filled_qty,
            "avg_fill_price": report.avg_fill_price,
        },
        ts=as_of.isoformat(),
        audit_path=audit_path,
    )

    log.info(
        "submit_order.done",
        order_id=order.order_id,
        status=status,
        filled_qty=report.filled_qty,
        avg_fill_price=report.avg_fill_price,
    )

    # Realized notional USD for the fold (avg_fill_price × filled_qty).  Both
    # may be absent on an accepted-but-unfilled ``pending`` → None.
    _filled_notional = (
        float(report.avg_fill_price) * float(report.filled_qty)
        if report.avg_fill_price is not None and report.filled_qty
        else None
    )
    return SubmitResult(
        order_id=order.order_id,
        status=status,
        avg_fill_price=report.avg_fill_price,
        filled_notional=_filled_notional,
    )
