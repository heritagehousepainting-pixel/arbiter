"""Order-reconciliation helpers (extracted from the Engine god-object).

Free functions that take the ``Engine`` instance as their first argument.  The
``Engine`` methods (``_reconcile_pending_orders`` etc.) are thin wrappers that
delegate here, so behaviour and the private method surface are unchanged.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import structlog

import arbiter.db.audit as _audit_mod
from arbiter.contract.seams import Idea
from arbiter.types import IdeaState

if TYPE_CHECKING:
    from arbiter.engine._engine import Engine

log = structlog.get_logger(__name__)


def reconcile_pending_orders(engine: "Engine", now: datetime) -> None:
    """Promote pending broker orders that have since filled (alpaca_paper).

    Spec §4.4 + A1: for each local order with ``status='pending'`` we ask
    the broker for the order's current state via ``get_order(order_id)``
    (preferred over position-presence, which cannot tie a fill to a
    *specific* pending idea).  On a confirmed fill we:
      - update the order row ``pending → filled`` (or ``partial``);
      - advance the matching idea → MONITORED via the idea store;
      - audit the promotion.
    The reconciler stays diagnostic; the engine owns the state mutation.

    Fail-safe: a broker/DB error for one order is logged and skipped — it
    must never abort the cycle.
    """
    from arbiter.orchestrator import idea_store  # noqa: PLC0415

    try:
        # D2 P1 — keep `partial` rows in the working set so the residual fill
        # is re-reconciled to completion next cycle (a `partial` row was
        # previously terminal-by-selection and the remainder was dropped).
        rows = engine.conn.execute(
            "SELECT * FROM orders WHERE status IN ('pending', 'partial')"
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.error("engine.reconcile_pending.query_failed", error=str(exc))
        return

    for row in rows:
        order_id = row["order_id"]
        try:
            report = engine.executor.get_order(order_id)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "engine.reconcile_pending.get_order_failed",
                order_id=order_id,
                error=str(exc),
            )
            continue

        # C2: map terminal broker states (expired/canceled/rejected — e.g. a
        # `day` order unfilled at close) to a TERMINAL local order status so
        # the row is no longer selected as `pending` and re-queried forever.
        # An expired/canceled/rejected BUY does NOT advance its idea (stays
        # pre-MONITORED); an expired SELL leaves the idea MONITORED for a
        # later re-attempt.  We do not close-out or label on a terminal state.
        if report.status in ("rejected", "cancelled"):
            mark_order_terminal(engine, row, report, now)
            continue

        if report.status not in ("filled", "partial"):
            continue  # still pending — leave as-is

        new_status = report.status  # "filled" or "partial"
        try:
            if new_status == "partial":
                # D3 P2 / D2 P1 — persist the ACTUALLY-FILLED qty on a partial
                # so the local ledger agrees with the broker in shares (§0 A1
                # invariant) and the reconciler is not a perpetual
                # QTY_MISMATCH generator.  The row stays `partial` and is
                # re-selected next cycle to reconcile any remainder.
                engine.conn.execute(
                    "UPDATE orders SET status = ?, qty = ? WHERE order_id = ?",
                    (new_status, float(report.filled_qty), order_id),
                )
            else:
                engine.conn.execute(
                    "UPDATE orders SET status = ? WHERE order_id = ?",
                    (new_status, order_id),
                )
            engine.conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.error(
                "engine.reconcile_pending.update_failed",
                order_id=order_id,
                error=str(exc),
            )
            continue

        # Branch on whether the order is an EXIT or an OPENING order, NOT its
        # side (a SHORT opens with a SELL and covers with a BUY, so side alone
        # is ambiguous).  Exit orders carry ``exit_label_kind`` in exits_json;
        # opening orders never do (``is_exit_order``).  A filled EXIT (long-exit
        # SELL / short-cover BUY) drives the close-out lifecycle (B4); a filled
        # OPENING order (long BUY / short SELL) advances its idea → MONITORED.
        from arbiter.execution.exit_monitor import is_exit_order  # noqa: PLC0415

        if is_exit_order(row):
            close_out_filled_sell(engine, row, report, now)
        elif new_status == "filled":
            # D2 P1 — advance the opening idea ONLY on a FULL fill (a `partial`
            # leaves shares outstanding and is re-reconciled next cycle), and
            # resolve the owning idea by ``orders.idea_id`` (migration 023)
            # when present — the (ticker, bucket) join is fragile and can
            # advance the WRONG idea if two ideas ever share the same
            # (ticker, bucket).  Fall back to the join only for legacy NULL
            # rows.
            advance_buy_idea(engine, row, order_id, now, idea_store)

        side = str(row["side"])

        log.info(
            "engine.reconcile_pending.promoted",
            order_id=order_id,
            ticker=row["ticker"],
            side=side,
            new_status=new_status,
            filled_qty=report.filled_qty,
        )
        _audit_mod.audit(
            "order.reconciled_fill",
            {
                "order_id": order_id,
                "ticker": row["ticker"],
                "side": side,
                "new_status": new_status,
                "filled_qty": report.filled_qty,
                "avg_fill_price": report.avg_fill_price,
            },
            ts=now.isoformat(),
            audit_path=engine.config.audit_path,
        )


def advance_buy_idea(
    engine: "Engine", row, order_id: str, now: datetime, idea_store
) -> None:
    """Advance a filled BUY's owning idea → MONITORED (D2 P1).

    Resolves the idea by ``orders.idea_id`` (migration 023) when the order
    row carries it — tying the fill to its EXACT owning idea — and only
    falls back to the legacy ``(ticker, horizon_bucket)`` join for rows with
    a NULL ``idea_id``.  The state-guard (advance only if not already
    MONITORED) backstops double-advance either way.  Fail-safe.
    """
    try:
        idea_id = row["idea_id"] if "idea_id" in row.keys() else None
        idea_row = None
        if idea_id is not None:
            idea_row = engine.conn.execute(
                "SELECT idea_id, state FROM ideas WHERE idea_id = ?",
                (idea_id,),
            ).fetchone()
        if idea_row is None:
            # Legacy NULL idea_id (or a missing link) → (ticker, bucket) join.
            idea_row = engine.conn.execute(
                "SELECT idea_id, state FROM ideas "
                "WHERE is_superseded = 0 AND dedupe_key_ticker = ? "
                "AND dedupe_key_bucket = ? AND state != ? "
                "ORDER BY created_at DESC LIMIT 1",
                (row["ticker"], row["horizon_bucket"], IdeaState.MONITORED.value),
            ).fetchone()
        if idea_row is None:
            # D2 P3 — a filled BUY reconciled but resolved no advanceable
            # idea (superseded/closed/missing).  Log rather than silently no-op.
            log.warning(
                "engine.reconcile_pending.no_advanceable_idea",
                order_id=order_id,
                ticker=row["ticker"],
            )
            return
        if str(idea_row["state"]) == IdeaState.MONITORED.value:
            return  # already advanced — idempotent
        idea_store.update_idea_state(
            engine.conn,
            idea_row["idea_id"],
            IdeaState.MONITORED,
            updated_state_at=now,
            audit_path=engine.config.audit_path,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "engine.reconcile_pending.idea_advance_failed",
            order_id=order_id,
            error=str(exc),
        )


def mark_order_terminal(engine: "Engine", order_row, report, now: datetime) -> None:
    """Map a terminal broker state to a terminal LOCAL order status (C2).

    ``rejected``/``cancelled``/``expired`` orders are written to a terminal
    local status (``rejected`` → ``rejected``; ``cancelled`` → ``expired``)
    so they are never re-selected as ``pending``.  No idea advance, no
    close-out, no outcome.  Each transition is audited.
    """
    order_id = order_row["order_id"]
    # adapter.get_order maps broker expired→"cancelled"; record it as the
    # terminal local status "expired" (cancelled day orders at close), and a
    # broker "rejected" as "rejected".
    local_status = "rejected" if report.status == "rejected" else "expired"
    try:
        engine.conn.execute(
            "UPDATE orders SET status = ? WHERE order_id = ?",
            (local_status, order_id),
        )
        engine.conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.error(
            "engine.reconcile_pending.terminal_update_failed",
            order_id=order_id,
            error=str(exc),
        )
        return

    log.info(
        "engine.reconcile_pending.terminal",
        order_id=order_id,
        ticker=order_row["ticker"],
        side=str(order_row["side"]),
        broker_status=report.status,
        local_status=local_status,
    )
    _audit_mod.audit(
        "order.reconciled_terminal",
        {
            "order_id": order_id,
            "ticker": order_row["ticker"],
            "side": str(order_row["side"]),
            "broker_status": report.status,
            "local_status": local_status,
        },
        ts=now.isoformat(),
        audit_path=engine.config.audit_path,
    )


def close_out_filled_sell(engine: "Engine", order_row, report, now: datetime) -> None:
    """Close out a filled/partial SELL by driving the idea lifecycle (B4).

    A FULL fill closes the idea (MONITORED → OUTCOME_READY → CLOSED) and
    labels the outcome with the real exit price + the SELL row's recorded
    ``label_kind``.  A PARTIAL fill leaves the idea MONITORED so the next
    cycle re-sells the residual (with a fresh dedup nonce).
    """
    from arbiter.execution import exit_monitor  # noqa: PLC0415

    # A partial SELL is not a close — leave the idea MONITORED for the
    # residual sweep next cycle.
    if report.status == "partial":
        log.info(
            "engine.reconcile_pending.sell_partial",
            order_id=order_row["order_id"],
            ticker=order_row["ticker"],
            filled_qty=report.filled_qty,
        )
        return

    label_kind = sell_label_kind(order_row)

    def _advisor_id_for(idea: Idea) -> str:
        return "A1.insider" if idea.horizon_days >= 180 else "A1.congress"

    try:
        exit_monitor.close_idea_on_sell_fill(
            engine.conn,
            order_row=order_row,
            exit_price=report.avg_fill_price,
            exit_as_of=now,
            label_kind=label_kind,
            pit=engine.pit,
            advisor_id_for=_advisor_id_for,
            advisor_confidence_for=None,
            audit_path=engine.config.audit_path,
            metrics=engine._metrics,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "engine.reconcile_pending.sell_close_failed",
            order_id=order_row["order_id"],
            error=str(exc),
        )


def sell_label_kind(order_row) -> str:
    """Recover the exit label_kind recorded on a SELL order row.

    The monitor records the trigger reason in the SELL row's exits_json
    under ``exit_label_kind``; default to ``normal`` if absent.
    """
    import json as _json  # noqa: PLC0415

    try:
        exits = _json.loads(order_row["exits_json"])
        return str(exits.get("exit_label_kind", "normal"))
    except Exception:  # noqa: BLE001
        return "normal"
