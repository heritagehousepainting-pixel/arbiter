"""Exit / sell monitor — sub-project #2.

Once per cycle the monitor inspects every open position and fires a full-exit
SELL when one of three triggers is met:

  * **stop-loss**     — current PIT close breaches the stop level.  The stop is
                        recomputed LIVE, in memory, each cycle from the broker
                        ``avg_price`` × the bucket stop-fraction (amendment B0).
                        The stored phantom ``exits_json.stop_loss`` is IGNORED.
  * **horizon-expiry**— ``now.date() >= entry_date + horizon_days`` (from the
                        owning order row's bucket).
  * **conviction-reversal** — a FRESH opposite-signed opinion for the held
                        ticker exists THIS cycle (per-ticker stance; absence of
                        a contrary opinion is NOT a reversal).

Trigger priority when several fire the same cycle: stop-loss > reversal >
horizon (the most urgent / most negative cause wins).

Sizing (amendment B3): the SELL qty is the held share count, NOT a notional —
``submit_order(..., presized_shares=shares, is_exit=True)`` skips the A0 divide
and routes idempotency to a local-ledger-only check.  Sell-side slippage (B1)
biases the limit DOWN.

Close-out (amendment B4): on a confirmed SELL fill the owning idea transitions
MONITORED → OUTCOME_READY → CLOSED and the outcome is labeled with the REAL
exit price/date and the trigger-mapped ``label_kind`` (stop → ``early_exit``,
reversal → ``reversal``, horizon → ``normal``).  For SimExecutor the sell fills
synchronously (close + label same cycle); for AlpacaAdapter a pending sell is
closed on the reconcile that confirms the fill.

All timestamps come from the injected clock; all prices via PIT (no
look-ahead).  No ``datetime.now()`` calls.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Callable

import structlog

from arbiter.contract.seams import Idea, PaperOrder
from arbiter.db.audit import audit as _audit
from arbiter.execution.idempotency import dedup_hash
from arbiter.execution.submit import submit_order
from arbiter.policy.exits import _STOP_LOSS_BY_BUCKET
from arbiter.shared.executor import Executor, PositionSnapshot
from arbiter.types import HorizonBucket, IdeaState, OrderSide

if TYPE_CHECKING:
    from arbiter.data.clock import Clock
    from arbiter.data.current_price import CurrentPriceProvider
    from arbiter.data.pit import PITGateway
    from arbiter.safety.breakers import CircuitBreaker

log = structlog.get_logger(__name__)


# Map a fired trigger to the outcome label_kind.
_LABEL_KIND_BY_REASON: dict[str, str] = {
    "stop_loss": "early_exit",
    "reversal": "reversal",
    "horizon": "normal",
}


@dataclass(frozen=True)
class ExitDecision:
    """A fired exit trigger for one position."""

    reason: str  # "stop_loss" | "reversal" | "horizon"

    @property
    def label_kind(self) -> str:
        return _LABEL_KIND_BY_REASON[self.reason]


# ---------------------------------------------------------------------------
# Pure helpers (no I/O — unit-tested directly)
# ---------------------------------------------------------------------------

def recompute_stop(
    avg_price: float, bucket: HorizonBucket, *, is_short: bool = False
) -> float:
    """Recompute the LIVE stop-loss level from the real cost basis (B0).

    For a LONG: ``stop = avg_price × (1 − bucket_stop_fraction)`` — a long loses
    as price falls, so the stop sits BELOW the entry.  For a SHORT: ``stop =
    avg_price × (1 + bucket_stop_fraction)`` — a short loses as price RISES, so
    the stop sits ABOVE the entry (the mirror).  Deterministic and idempotent
    (inputs are stable), so it never ratchets.
    """
    frac = _STOP_LOSS_BY_BUCKET[bucket]
    if is_short:
        return avg_price * (1.0 + frac)
    return avg_price * (1.0 - frac)


def evaluate_triggers(
    *,
    avg_price: float,
    bucket: HorizonBucket,
    horizon_expiry: date,
    current_price: float | None,
    current_stance: float | None,
    now: datetime,
    reversal_threshold: float = 0.0,
    is_short: bool = False,
) -> ExitDecision | None:
    """Decide whether to exit a position this cycle (pure).

    Handles both LONG and SHORT positions (``is_short``).  For a SHORT the
    directional triggers are mirrored: the stop fires when price RISES through
    ``avg_price × (1 + frac)`` (a short loses as price rises), and the reversal
    fires on a fresh BULLISH opinion (``stance >= +reversal_threshold``).  The
    horizon trigger is side-independent.

    Parameters
    ----------
    avg_price:
        Broker cost basis — the true entry price (B0 source of truth).
    bucket:
        The position's horizon bucket (drives the stop fraction).
    horizon_expiry:
        Calendar date on/after which the horizon trigger fires.
    current_price:
        Current PIT close (or open fallback).  ``None`` → stop cannot be
        evaluated this cycle (fail closed against spurious sells).
    current_stance:
        Signed stance of a FRESH opinion for the held ticker this cycle, or
        ``None`` when no fresh opinion exists (→ no reversal).
    now:
        Injected clock value.
    reversal_threshold:
        Reversal fires when the fresh opinion flips AGAINST the position: for a
        long ``current_stance <= -reversal_threshold``, for a short
        ``current_stance >= +reversal_threshold`` (default 0.0 → any
        opposite-signed fresh opinion).
    is_short:
        True when the held position is a SHORT (mirrors the stop + reversal
        direction).

    Returns
    -------
    ExitDecision | None
        The fired trigger (priority stop > reversal > horizon), or None.
    """
    # Priority 1: stop-loss (recomputed live from avg_price).  A long stops on a
    # fall THROUGH the level; a short stops on a rise THROUGH the (mirrored) level.
    if current_price is not None:
        stop_level = recompute_stop(avg_price, bucket, is_short=is_short)
        if (current_price >= stop_level) if is_short else (current_price <= stop_level):
            return ExitDecision(reason="stop_loss")

    # Priority 2: conviction-reversal — a FRESH opinion flipping AGAINST the
    # position.  Long: a bearish (negative) stance; short: a bullish (positive) one.
    if current_stance is not None:
        if is_short:
            reversed_ = current_stance >= reversal_threshold and current_stance > 0.0
        else:
            reversed_ = current_stance <= -reversal_threshold and current_stance < 0.0
        if reversed_:
            return ExitDecision(reason="reversal")

    # Priority 3: horizon-expiry (deterministic, data-free).
    if now.date() >= horizon_expiry:
        return ExitDecision(reason="horizon")

    return None


def build_exit_order(
    *,
    position: PositionSnapshot,
    owning_order_row: sqlite3.Row | dict,
    exits: dict,
    now: datetime,
    nonce: str = "",
) -> PaperOrder:
    """Construct a full-exit ``PaperOrder`` for the held share count (B3).

    For a LONG position the exit is a SELL; for a SHORT position it is a
    **BUY-to-cover** (a SELL would only enlarge the short).  The side is derived
    from the sign of ``position.shares`` and the qty is ``abs(shares)``.

    The exit order carries the SAME ``horizon_bucket`` / ``entry_date`` /
    ``advisor_signature`` as the owning opening order so its dedup_hash is stable
    and reproducible (and differs from the opener because ``side`` is in the hash).

    ``nonce`` is appended to the advisor_signature for partial-residual sweeps
    (B4) so a subsequent cycle's re-cover of the remaining shares is a DISTINCT
    order (otherwise the persisted ``partial`` row would block it).
    """
    from arbiter.db.helpers import generate_ulid  # noqa: PLC0415

    advisor_signature = str(owning_order_row["advisor_signature"])
    if nonce:
        advisor_signature = f"{advisor_signature}|exit:{nonce}"

    bucket = HorizonBucket(owning_order_row["horizon_bucket"])
    entry_date = date.fromisoformat(str(owning_order_row["entry_date"]))

    # Short → cover with a BUY; long → exit with a SELL.
    exit_side = OrderSide.BUY if position.shares < 0 else OrderSide.SELL

    order = PaperOrder(
        order_id=generate_ulid(),
        dedup_hash="",  # computed below; PaperOrder is frozen so build then set
        ticker=position.ticker,
        side=exit_side,
        qty=abs(float(position.shares)),  # informational — presized_shares is authoritative
        horizon_bucket=bucket,
        entry_date=entry_date,
        advisor_signature=advisor_signature,
        exits=exits,
    )
    # PaperOrder is frozen; recreate with the computed dedup_hash.
    import dataclasses  # noqa: PLC0415

    return dataclasses.replace(order, dedup_hash=dedup_hash(order))


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def is_exit_order(order_row: sqlite3.Row | dict) -> bool:
    """True when *order_row* is an EXIT order (long-exit SELL / short-cover BUY).

    Exit orders are the only orders that carry ``exit_label_kind`` in their
    ``exits_json`` (the exit monitor stamps it; the entry path's
    ``compute_exits`` never does).  This is the side-agnostic discriminator
    between an EXIT order and an OPENING order — needed once shorts exist,
    because a short OPENS with a SELL and an exit SELL is otherwise
    indistinguishable by side alone.
    """
    try:
        exits = json.loads(order_row["exits_json"])
    except Exception:  # noqa: BLE001
        return False
    return isinstance(exits, dict) and "exit_label_kind" in exits


def _latest_opening_order_for(
    conn: sqlite3.Connection, ticker: str, *, is_short: bool
) -> sqlite3.Row | None:
    """Return the most-recent filled/partial OPENING order row for *ticker*.

    The opening side is BUY for a long and SELL for a short.  EXIT orders
    (those carrying ``exit_label_kind`` — a long-exit SELL or a short-cover BUY)
    are SKIPPED so an exit is never mistaken for the opener (matters once a
    ticker has both opening and exit rows on the same side over its history).

    Carries ``exits_json``, ``entry_date``, ``horizon_bucket``,
    ``advisor_signature`` and (when present) ``idea_id``.
    """
    opening_side = OrderSide.SELL if is_short else OrderSide.BUY
    rows = conn.execute(
        "SELECT * FROM orders "
        "WHERE ticker = ? AND side = ? AND status IN ('filled', 'partial') "
        "ORDER BY created_at DESC",
        (ticker, opening_side.value),
    ).fetchall()
    for row in rows:
        if not is_exit_order(row):
            return row
    return None


def _resolve_idea_id(
    conn: sqlite3.Connection,
    order_row: sqlite3.Row | dict,
) -> str | None:
    """Resolve the owning MONITORED idea_id for an order row.

    Prefers the order row's ``idea_id`` (B5) when set; falls back to the
    (ticker, horizon_bucket) join used elsewhere in the engine for legacy
    NULL rows.
    """
    # B5: exact link by idea_id when available.
    oid = None
    try:
        oid = order_row["idea_id"]
    except (KeyError, IndexError):
        oid = None
    if oid:
        row = conn.execute(
            "SELECT idea_id, state FROM ideas WHERE idea_id = ? AND is_superseded = 0",
            (oid,),
        ).fetchone()
        if row is not None:
            return row["idea_id"]

    # Fallback: (ticker, bucket) join (one-live-bucket-per-held-ticker invariant).
    row = conn.execute(
        "SELECT idea_id FROM ideas "
        "WHERE is_superseded = 0 AND dedupe_key_ticker = ? AND dedupe_key_bucket = ? "
        "AND state = ? ORDER BY created_at DESC LIMIT 1",
        (order_row["ticker"], order_row["horizon_bucket"], IdeaState.MONITORED.value),
    ).fetchone()
    return row["idea_id"] if row is not None else None


def close_idea_on_sell_fill(
    conn: sqlite3.Connection,
    *,
    order_row: sqlite3.Row | dict,
    exit_price: float | None,
    exit_as_of: datetime,
    label_kind: str,
    pit: "PITGateway",
    advisor_id_for: Callable[[Idea], str],
    advisor_confidence_for: Callable[[Idea], float] | None = None,
    audit_path: str | None = None,
    metrics=None,
) -> str | None:
    """Drive idea MONITORED → OUTCOME_READY → CLOSED and store the outcome (B4).

    Shared by the in-cycle monitor (sim, synchronous) and the engine's
    pending-SELL reconcile path (alpaca_paper).

    ``exit_price`` is the REAL SELL avg fill price; when ``None`` (mid-partial
    broker quirk) we fall back to the PIT close for ``exit_as_of`` with a
    logged note.  If no price can be found the close-out is skipped (the SELL
    row stays for next-cycle reconciliation).

    If the labeler raises ``LookupError`` (a required PIT bar — entry open, SPY
    open/close, or beta — is not yet available), the close-out is skipped and
    the idea is LEFT MONITORED with its SELL row intact (nothing is persisted)
    so a LATER cycle can retry the label once the bars are present.  This never
    transitions to CLOSED with no outcome and never crashes the monitor.

    Returns the stored outcome id, or None when the close-out was skipped.
    """
    from arbiter.evaluation import attribution  # noqa: PLC0415
    from arbiter.orchestrator import idea_store  # noqa: PLC0415

    idea_id = _resolve_idea_id(conn, order_row)
    if idea_id is None:
        log.info(
            "exit_monitor.no_idea_for_sell",
            ticker=order_row["ticker"],
            bucket=order_row["horizon_bucket"],
        )
        return None

    ideas = idea_store.load_ideas_by_state(conn, {IdeaState.MONITORED})
    idea = next((i for i in ideas if i.idea_id == idea_id), None)
    if idea is None:
        # Not MONITORED (already closed / superseded) — nothing to do.
        log.info("exit_monitor.idea_not_monitored", idea_id=idea_id)
        return None

    # Guard a None exit price — fall back to PIT close, else leave pending.
    effective_exit_price = exit_price
    if effective_exit_price is None:
        px = pit.get("price_close", idea.ticker, exit_as_of)
        if px is None:
            log.warning(
                "exit_monitor.no_exit_price_skip",
                idea_id=idea_id,
                ticker=idea.ticker,
                as_of=exit_as_of.isoformat(),
            )
            return None
        effective_exit_price = float(px)
        log.warning(
            "exit_monitor.exit_price_fallback_pit_close",
            idea_id=idea_id,
            ticker=idea.ticker,
            fallback_price=effective_exit_price,
        )

    # Fan out per-advisor outcomes FIRST (uses the REAL exit price — no PIT close
    # read for the EXIT, but the labeler still reads PIT for entry-open / SPY /
    # beta to compute alpha).  Any of those bars can be missing → LookupError.
    # On the sim path the SELL has ALREADY executed (position closed, P&L booked,
    # SELL row persisted) by the time we get here, so a raise here must NOT
    # strand the idea: we leave it MONITORED with its SELL row intact and persist
    # NOTHING, so a LATER cycle's close-out retry can label + close it once the
    # PIT bars are available.  Mirrors orchestrator/outcome_runner.run_outcome_sweep.
    # ``resolve_advisor_outcomes`` recovers the persisted opinions for the idea
    # and writes ONE outcome per contributing advisor (#5a, D2); a per-(idea,
    # advisor) existence guard makes the close-out / retry idempotent.  The flip
    # to CLOSED happens AFTER the fan-out loop completes for all linked advisors.
    try:
        oids = attribution.resolve_advisor_outcomes(
            conn,
            idea,
            pit=pit,
            cutoff_as_of=exit_as_of,
            exit_price=effective_exit_price,
            exit_as_of=exit_as_of,
            label_kind=label_kind,
            audit_path=audit_path,
            metrics=metrics,
            fallback_advisor_id_for=advisor_id_for,
            fallback_advisor_confidence_for=advisor_confidence_for,
        )
    except LookupError as exc:
        log.warning(
            "exit_monitor.label_lookup_error_retry_later",
            idea_id=idea_id,
            ticker=idea.ticker,
            as_of=exit_as_of.isoformat(),
            error=str(exc),
        )
        return None

    if not oids:
        # Nothing written this attempt (no opinion + no fallback) — leave
        # MONITORED for a later retry rather than closing with no outcome.
        log.warning(
            "exit_monitor.no_outcome_written_skip",
            idea_id=idea_id, ticker=idea.ticker,
        )
        return None

    # Legal FSM path MONITORED → OUTCOME_READY → CLOSED (flip after fan-out).
    idea_store.update_idea_state(
        conn, idea_id, IdeaState.OUTCOME_READY,
        updated_state_at=exit_as_of, audit_path=audit_path,
    )
    idea_store.update_idea_state(
        conn, idea_id, IdeaState.CLOSED,
        updated_state_at=exit_as_of, audit_path=audit_path,
    )

    oid = oids[0]
    _audit(
        "exit_monitor.closed",
        {
            "idea_id": idea_id,
            "ticker": idea.ticker,
            "label_kind": label_kind,
            "exit_price": effective_exit_price,
            "outcome_id": oid,
            "outcome_ids": oids,
        },
        ts=exit_as_of.isoformat(),
        audit_path=audit_path,
    )
    log.info(
        "exit_monitor.closed",
        idea_id=idea_id, ticker=idea.ticker, label_kind=label_kind,
        exit_price=effective_exit_price, outcome_count=len(oids),
    )
    return oid


# ---------------------------------------------------------------------------
# Close-out retry sweep
# ---------------------------------------------------------------------------

def _retry_stranded_closeouts(
    conn: sqlite3.Connection,
    *,
    pit: "PITGateway",
    now: datetime,
    advisor_id_for: Callable[[Idea], str],
    advisor_confidence_for: Callable[[Idea], float] | None,
    audit_path: str | None,
    metrics=None,
) -> list[str]:
    """Re-attempt close-out for ideas whose SELL filled but never got labeled.

    A SELL can fill (position closed, P&L booked, ``filled`` row persisted) yet
    ``close_idea_on_sell_fill`` skip the label on a transient ``LookupError``
    (PIT entry-open / SPY / beta bar not yet available).  That leaves the idea
    MONITORED with a ``filled`` SELL row and NO outcome — and since the position
    is already gone it never reappears in the positions loop, and the engine
    reconcile only re-processes ``pending`` rows.  Without this sweep the idea
    would be stranded forever.

    For each MONITORED idea that has a ``filled`` SELL row and still has
    attribution WORK to do, re-run the close-out using the SELL row's recorded
    fill price and ``exit_label_kind``.  Once the PIT bars are available the
    label + close succeeds; if they are still missing the close-out simply skips
    again and is retried next cycle.

    Selection (E0 — STRICT-SUBSET, not ``NOT EXISTS (any outcome)``).  With
    per-advisor fan-out, a PARTIAL write (advisor 1 stored, crash before advisor
    2) leaves the idea with ≥1 outcome row — ``NOT EXISTS`` would then NEVER
    re-select it, stranding advisor 2 AND leaving the idea stuck MONITORED.  We
    instead re-select a MONITORED idea whenever its STORED-advisor set is a
    strict subset of its LINKED-opinion-advisor set (the resolver still has work),
    plus the no-opinion case (zero linked opinions AND zero outcomes → resolve
    via the proxy fallback).  ``resolve_advisor_outcomes`` is idempotent per
    (idea, advisor), so re-running it for a partially-written idea writes only the
    missing advisor(s) and then flips CLOSED.
    """
    from arbiter.evaluation import attribution  # noqa: PLC0415

    closed: list[str] = []

    # All MONITORED ideas whose EXIT order is filled.  An exit order is a
    # long-exit SELL OR a short-cover BUY — both carry ``exit_label_kind`` and
    # have a NULL ``idea_id`` (submit_order does not stamp it), so we match the
    # exit to its idea the way the B2 guard does: by (ticker, horizon_bucket).
    # We select filled orders of EITHER side and filter to true exit orders in
    # Python (``is_exit_order``) so a short's OPENING SELL is never swept as an
    # exit; the strict-subset gate below then keeps only ideas with work left.
    rows = conn.execute(
        "SELECT o.* FROM orders o "
        "JOIN ideas i ON i.dedupe_key_ticker = o.ticker "
        "             AND i.dedupe_key_bucket = o.horizon_bucket "
        "WHERE o.status = 'filled' "
        "AND i.is_superseded = 0 AND i.state = ? "
        "ORDER BY o.created_at ASC",
        (IdeaState.MONITORED.value,),
    ).fetchall()

    for sell_row in rows:
        if not is_exit_order(sell_row):
            continue  # an OPENING order (e.g. a short's entry SELL) — not an exit
        # Strict-subset gate (E0): only re-process an idea with remaining work.
        idea_id_for_gate = _resolve_idea_id(conn, sell_row)
        if idea_id_for_gate is not None:
            linked = attribution.linked_opinion_advisors(conn, idea_id_for_gate)
            stored = attribution.stored_outcome_advisors(conn, idea_id_for_gate)
            if linked:
                # Has persisted opinions → work remains iff stored ⊊ linked.
                if not (stored < linked):
                    continue
            else:
                # No persisted opinions → fallback case; work remains only if no
                # outcome has been written yet.
                if stored:
                    continue

        # Recover the trigger's label_kind + the real fill price from the SELL
        # row.  The fill price was persisted as the ledger limit_price (the
        # slippage-adjusted SELL limit at which the sim fills); reuse it so the
        # retry reproduces the original economic exit.
        label_kind = "normal"
        try:
            sell_exits = json.loads(sell_row["exits_json"])
            label_kind = str(sell_exits.get("exit_label_kind", "normal"))
        except Exception:  # noqa: BLE001
            pass

        # Find the owning OPENING order so close-out resolves the idea the same
        # way the in-cycle path does (idea_id link, with (ticker,bucket)
        # fallback).  The exit's side tells us the position side: a SELL exit
        # closed a LONG (opener = BUY); a BUY cover closed a SHORT (opener =
        # SELL).
        was_short = str(sell_row["side"]) == OrderSide.BUY.value
        buy_row = _latest_opening_order_for(conn, sell_row["ticker"], is_short=was_short)
        if buy_row is None:
            continue

        idea_id_before = _resolve_idea_id(conn, buy_row)
        oid = close_idea_on_sell_fill(
            conn,
            order_row=buy_row,
            exit_price=None,  # fall back to PIT close for the exit price
            exit_as_of=now,
            label_kind=label_kind,
            pit=pit,
            advisor_id_for=advisor_id_for,
            advisor_confidence_for=advisor_confidence_for,
            audit_path=audit_path,
            metrics=metrics,
        )
        if oid is not None and idea_id_before:
            log.info(
                "exit_monitor.closeout_retry_succeeded",
                idea_id=idea_id_before, ticker=sell_row["ticker"],
            )
            closed.append(idea_id_before)

    return closed


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_exit_monitor(
    conn: sqlite3.Connection,
    executor: Executor,
    pit: "PITGateway",
    clock: "Clock",
    *,
    stance_by_ticker: dict[str, float],
    advisor_id_for: Callable[[Idea], str],
    advisor_confidence_for: Callable[[Idea], float] | None = None,
    breaker: "CircuitBreaker | None" = None,
    audit_path: str | None = None,
    current_price_provider: "CurrentPriceProvider | None" = None,
    metrics=None,
) -> list[str]:
    """Inspect open positions, fire SELLs on triggers, close-out filled sells.

    Returns the list of closed idea_ids (sim: synchronous; adapter: only those
    whose SELL filled synchronously — pending sells close on the reconcile).

    ``current_price_provider`` (sub-project #3, Decision 1): when supplied and it
    returns a price for the held ticker, the stop-loss comparison uses that LIVE
    "now" price instead of the daily PIT close.  ``None`` (sim/backtest, or a live
    read that failed) falls back to the daily PIT close exactly as before — the
    live price is NEVER persisted as a fill, fed to the labeler, or written to PIT.
    """
    now: datetime = clock.now()

    # Fail closed: if the broker positions read fails, skip the monitor this
    # cycle (a missed protective sell is safer than a wrong sell).
    try:
        positions = executor.get_positions()
    except Exception as exc:  # noqa: BLE001
        log.error("exit_monitor.get_positions_failed", error=str(exc))
        _audit(
            "exit_monitor.get_positions_failed",
            {"error": str(exc)},
            ts=now.isoformat(),
            audit_path=audit_path,
        )
        return []

    # Live current prices for all held names in ONE batch read (amendment C1).
    # Empty in sim/backtest (NullCurrentPriceProvider) or on a failed live read →
    # the per-position ladder falls back to the daily PIT close.
    live_prices: dict[str, float] = {}
    if current_price_provider is not None:
        held = [t for t, p in positions.items() if p.shares != 0]
        try:
            live_prices = current_price_provider.current_prices(held)
        except Exception as exc:  # noqa: BLE001
            log.warning("exit_monitor.current_prices_failed", error=str(exc))
            live_prices = {}

    # First, retry any close-out that a prior cycle's transient LookupError left
    # stranded (SELL filled but idea still MONITORED with no outcome).  The sold
    # position is gone from the positions loop, so this is the only retry path.
    closed: list[str] = _retry_stranded_closeouts(
        conn,
        pit=pit,
        now=now,
        advisor_id_for=advisor_id_for,
        advisor_confidence_for=advisor_confidence_for,
        audit_path=audit_path,
        metrics=metrics,
    )

    for ticker, position in positions.items():
        if position.shares == 0:
            continue
        is_short = position.shares < 0

        order_row = _latest_opening_order_for(conn, ticker, is_short=is_short)
        if order_row is None:
            # Orphan / manually-held position — no lifecycle to drive.  We do
            # NOT fire an exit for a position we cannot tie to an order row.
            log.info("exit_monitor.no_owning_order", ticker=ticker)
            continue

        bucket = HorizonBucket(order_row["horizon_bucket"])
        try:
            stored_exits = json.loads(order_row["exits_json"])
        except Exception:  # noqa: BLE001
            stored_exits = {}

        # B0: horizon_expiry from the order row's entry_date + bucket horizon.
        # The stored stop_loss is phantom and IGNORED; we recompute live.
        from arbiter.policy.exits import compute_exits  # noqa: PLC0415

        entry_date = date.fromisoformat(str(order_row["entry_date"]))
        live_exits = compute_exits(
            bucket=bucket,
            side=OrderSide.SELL if is_short else OrderSide.BUY,
            entry_price=position.avg_price,
            entry_date=entry_date,
        )
        horizon_expiry = live_exits["horizon_expiry"]
        reversal_threshold = float(stored_exits.get("conviction_reversal", 0.0))

        # Stop-check price: prefer the LIVE current price (sub-project #3); fall
        # back to the daily PIT close (price_close → price_open) in sim/backtest
        # or when the live read returned nothing for this ticker.
        current_price = live_prices.get(ticker)
        if current_price is None:
            px = pit.get("price_close", ticker, now)
            if px is None:
                px = pit.get("price_open", ticker, now)
            current_price = float(px) if px is not None else None
        if current_price is None:
            log.info("exit_monitor.no_price", ticker=ticker, as_of=now.isoformat())

        current_stance = stance_by_ticker.get(ticker)

        decision = evaluate_triggers(
            avg_price=position.avg_price,
            bucket=bucket,
            horizon_expiry=horizon_expiry,
            current_price=current_price,
            current_stance=current_stance,
            now=now,
            reversal_threshold=reversal_threshold,
            is_short=is_short,
        )
        if decision is None:
            continue

        log.info(
            "exit_monitor.trigger",
            ticker=ticker, reason=decision.reason,
            label_kind=decision.label_kind, shares=position.shares,
        )

        # Partial-residual sweep: if the latest EXIT order for this
        # (ticker,bucket) is a persisted `partial`, the residual re-exit needs a
        # fresh nonce so the local-ledger check doesn't block it.  The exit side
        # is BUY-to-cover for a short, SELL for a long.
        exit_side = OrderSide.BUY if is_short else OrderSide.SELL
        nonce = ""
        prior_partial = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE ticker = ? AND side = ? "
            "AND horizon_bucket = ? AND status = 'partial'",
            (ticker, exit_side.value, order_row["horizon_bucket"]),
        ).fetchone()
        if prior_partial is not None and prior_partial["c"] > 0:
            nonce = now.date().isoformat()

        # Stamp the trigger's label_kind into the exit row's exits_json so the
        # async reconcile path (pending → fill next cycle) can recover it (B4).
        sell_exits = dict(live_exits)
        sell_exits["exit_label_kind"] = decision.label_kind

        sell_order = build_exit_order(
            position=position,
            owning_order_row=order_row,
            exits=sell_exits,
            now=now,
            nonce=nonce,
        )

        # Use the current price as raw_price; submit_order applies side-correct
        # slippage (B1): a long SELL biases the limit DOWN, a short cover BUY UP.
        sell_raw_price = current_price if current_price is not None else position.avg_price
        spread = 0.01
        spread_val = pit.get("spread", ticker, now)
        if spread_val is not None:
            spread = float(spread_val)

        result = submit_order(
            sell_order,
            executor,
            clock,
            conn=conn,
            spread=spread,
            raw_price=sell_raw_price,
            breaker=breaker,
            audit_path=audit_path,
            presized_shares=abs(int(position.shares)),
            is_exit=True,
        )

        if result.duplicate:
            # A live exit order (long SELL / short cover BUY) is already in
            # flight for this name — leave it for the reconcile path (idempotent
            # across cycles).
            continue

        # On a synchronous fill (sim, or adapter filling immediately) close out
        # NOW.  A pending exit is closed on the next-cycle reconcile (engine).
        if result.filled:
            # The REAL fill price is carried back on the SubmitResult (no longer
            # reaching into the executor's private ``_reports``).  Fall back to
            # the slippage-adjusted SELL limit if the broker reported no price.
            exit_price = (
                result.avg_fill_price
                if result.avg_fill_price is not None
                else sell_raw_price
            )
            # Capture the idea_id BEFORE close-out flips it out of MONITORED
            # (the (ticker,bucket) fallback join filters on MONITORED).
            idea_id_before = _resolve_idea_id(conn, order_row)
            oid = close_idea_on_sell_fill(
                conn,
                order_row=order_row,
                exit_price=exit_price,
                exit_as_of=now,
                label_kind=decision.label_kind,
                pit=pit,
                advisor_id_for=advisor_id_for,
                advisor_confidence_for=advisor_confidence_for,
                audit_path=audit_path,
                metrics=metrics,
            )
            if oid is not None and idea_id_before:
                closed.append(idea_id_before)

    return closed
