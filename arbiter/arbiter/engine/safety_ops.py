"""Safety / risk-seeding / breaker / exit-monitor helpers (extracted from Engine).

Free functions taking the ``Engine`` instance as their first argument.  The
corresponding ``Engine`` methods are thin wrappers delegating here; behaviour
and the private method surface are unchanged.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from arbiter.contract.opinion import Opinion
from arbiter.contract.seams import Idea
from arbiter.data.sectors import sector_for
from arbiter.orchestrator.cycle import CycleResult
from arbiter.policy.book import RiskBook
from arbiter.safety import is_trading_allowed  # noqa: F401  (kept for parity of imports)
from arbiter.safety.alerting import AutoPauseSentinel
from arbiter.engine.advisors import _us_market_open

if TYPE_CHECKING:
    from arbiter.engine._engine import Engine

log = structlog.get_logger(__name__)


def run_exit_monitor(
    engine: "Engine", now: datetime, opinions: list[Opinion]
) -> list[str]:
    """Run the exit/sell monitor for the cycle (sub-project #2).

    Builds the per-ticker signed stance from the in-cycle opinions (for the
    conviction-reversal trigger) and delegates to the monitor.  Fail-safe:
    any error is logged and the cycle continues (a missed exit for one
    cycle is safer than aborting the whole run).
    """
    from arbiter.execution import exit_monitor  # noqa: PLC0415

    # Per-ticker signed stance = mean of fresh opinions' stance_score for the
    # ticker this cycle (MVP fusion is bucket-pooled, so we derive a clean
    # ticker-specific stance directly from the current opinions — spec 2b).
    stance_sum: dict[str, float] = {}
    stance_n: dict[str, int] = {}
    for op in opinions:
        stance_sum[op.ticker] = stance_sum.get(op.ticker, 0.0) + float(op.stance_score)
        stance_n[op.ticker] = stance_n.get(op.ticker, 0) + 1
    stance_by_ticker = {
        t: stance_sum[t] / stance_n[t] for t in stance_sum if stance_n[t] > 0
    }

    def _advisor_id_for(idea: Idea) -> str:
        return "A1.insider" if idea.horizon_days >= 180 else "A1.congress"

    try:
        return exit_monitor.run_exit_monitor(
            engine.conn,
            engine.executor,
            engine.pit,
            engine.clock,
            stance_by_ticker=stance_by_ticker,
            advisor_id_for=_advisor_id_for,
            advisor_confidence_for=None,
            breaker=engine.breaker,
            audit_path=engine.config.audit_path,
            current_price_provider=engine.current_price_provider,
            metrics=engine._metrics,
        )
    except Exception as exc:  # noqa: BLE001
        from arbiter.execution.alpaca_adapter import BrokerError  # noqa: PLC0415

        # A broker-fatal SELL rejection must auto-pause (same posture as buys).
        if isinstance(exc, BrokerError):
            fire_critical_alert(
                engine,
                message=f"Broker-fatal error during exit SELL — auto-pausing engine: {exc}",
                ctx={"error": str(exc), "as_of": now.isoformat()},
                as_of=now,
            )
        else:
            log.error("engine.run_exit_monitor.failed", error=str(exc))
        return []


def fire_critical_alert(
    engine: "Engine", message: str, ctx: dict, as_of: datetime
) -> None:
    """Fire a critical alert and latch ``engine.paused`` if sentinel returned.

    Exceptions inside the alerting call are caught and logged — a bug in
    alerting must never crash the cycle (fail-safe, INTERFACES §11.4).
    """
    try:
        sentinel = engine.alerting.alert("critical", message, ctx, as_of=as_of)
    except Exception as exc:  # noqa: BLE001
        log.error("engine.alerting.error", error=str(exc), message=message)
        # Still pause on the side of caution: a broken alerting path should not
        # silently allow trading to continue past a critical condition.
        sentinel = AutoPauseSentinel(message=message)

    if isinstance(sentinel, AutoPauseSentinel):
        log.warning("engine.auto_paused", reason=sentinel.message)
        engine.paused = True
        # C4: persist the pause durably so an auto-relaunching daemon does
        # not silently resume trading after a fatal condition.
        engine._persist_paused(True, reason=sentinel.message, now=as_of)


def market_is_open(engine: "Engine", now: datetime) -> bool:
    """True if the US equity market is open at *now* (calendar-aware).

    Uses the injected ``MarketCalendar`` when present (DST/holiday/early-close
    aware, #3); otherwise the coarse ``_us_market_open`` heuristic.
    """
    if engine.market_calendar is not None:
        try:
            return engine.market_calendar.session(now).is_open
        except Exception as exc:  # noqa: BLE001
            log.warning("engine.market_calendar_failed", error=str(exc))
    return _us_market_open(now)


def safety_gate(engine: "Engine", now: datetime) -> "CycleResult | None":
    """Shared safety gate for ``run_cycle`` AND ``run_fast_iteration`` (C5).

    Checks, in order, the three critical conditions that halt trading:
      (0) ``paused`` latch (in-memory; restored from durable store on build);
      (b) kill switch halted → auto-pause + return;
      (a) any circuit breaker tripped → auto-pause + return.

    Returns a short-circuit ``CycleResult`` (with ``paused_by_alert`` set)
    when trading must NOT proceed, or ``None`` when the gate is clear.  Both
    entrypoints must call this so they cannot drift.
    """
    # Auto-pause short-circuit — checked FIRST.  Persists until resume().
    if engine.paused:
        log.warning("engine.safety_gate.paused", as_of=now.isoformat())
        _r = CycleResult(ideas_processed=0)
        _r.paused_by_alert = True  # type: ignore[attr-defined]
        return _r

    # Critical condition (b): kill switch halted (broker-side; survives death).
    # LIVE always consults + fails closed; paper-sim only when a URL is set.
    if (engine.config.live_trading or engine.config.kill_switch_url) and engine.kill_switch.is_halted(as_of=now):
        log.warning("engine.safety_gate.kill_switch_halted", as_of=now.isoformat())
        fire_critical_alert(
            engine,
            message="Kill switch reports halted — auto-pausing engine",
            ctx={"as_of": now.isoformat()},
            as_of=now,
        )
        _r = CycleResult(ideas_processed=0)
        _r.paused_by_alert = engine.paused  # type: ignore[attr-defined]
        return _r

    # Critical condition (a): any circuit breaker already tripped → pause.
    tripped = engine.breaker.any_tripped(engine.conn)
    if tripped:
        fire_critical_alert(
            engine,
            message=f"Circuit breaker(s) tripped — auto-pausing engine: {tripped}",
            ctx={"tripped_breakers": tripped, "as_of": now.isoformat()},
            as_of=now,
        )
        _r = CycleResult(ideas_processed=0)
        _r.paused_by_alert = engine.paused  # type: ignore[attr-defined]
        return _r

    return None


def position_market_value(engine: "Engine", ticker: str, snap, now: datetime) -> float:
    """USD GROSS market value of a held position (|shares| × current price).

    Prefers the live current-price provider (alpaca_paper intraday), then a
    PIT close, and finally the position's own ``avg_price`` so the value is
    never silently zero on a missing quote.  All in USD notional.

    Uses ``abs(shares)`` so a SHORT (negative shares) contributes its EXPOSURE
    magnitude to gross/open-count/limits rather than REDUCING measured gross —
    a signed value would let shorts hide exposure and over-allocate.
    """
    price: float | None = None
    if engine.current_price_provider is not None:
        try:
            price = engine.current_price_provider.current_price(ticker)
        except Exception:  # noqa: BLE001
            price = None
    if price is None:
        try:
            pit_close = engine.pit.get("price_close", ticker, now)
            price = float(pit_close) if pit_close is not None else None
        except Exception:  # noqa: BLE001
            price = None
    if price is None or price <= 0:
        price = float(snap.avg_price)
    return abs(float(snap.shares)) * float(price)


def seed_risk_book(engine: "Engine", now: datetime) -> RiskBook:
    """Build a ``RiskBook`` from the CURRENT held book as notional USD (A2).

    Seeds ``{ticker: usd_market_value}`` from the active executor's
    positions so ``decide()`` sees real open-count / gross / sector
    exposure on the FIRST order of the cycle (previously the book was empty
    and the three book-aware caps never bound — A2 P0).  The engine folds
    each successful submit's notional into the book mid-cycle.
    """
    held: dict[str, float] = {}
    try:
        positions = engine.executor.get_positions()
    except Exception as exc:  # noqa: BLE001
        log.warning("engine.risk_book.positions_failed", error=str(exc))
        return RiskBook(held={}, sector_for=sector_for)
    for ticker, snap in positions.items():
        held[ticker] = position_market_value(engine, ticker, snap, now)
    return RiskBook(held=held, sector_for=sector_for)


def run_divergence_reconcile(engine: "Engine", now: datetime) -> None:
    """Run the position reconciler and surface any divergence (D3 P0).

    Diagnostic only — it never mutates state.  LOCAL_ONLY / BROKER_ONLY /
    QTY_MISMATCH divergences (orphan positions, manual broker edits, lost
    responses) are logged at error level and, when alerting is available,
    raised as an alert so they are surfaced for human review rather than
    invisible.  Fail-safe: any error is logged and the cycle continues.
    """
    from arbiter.execution import reconciler  # noqa: PLC0415

    try:
        result = reconciler.reconcile(
            engine.conn,
            engine.executor,
            as_of=now,
            audit_path=engine.config.audit_path,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("engine.reconcile_divergence.failed", error=str(exc))
        return

    if result.clean:
        return

    for d in result.divergences:
        log.error(
            "engine.reconcile_divergence.found",
            kind=d.kind,
            ticker=d.ticker,
            local_qty=d.local_qty,
            broker_qty=d.broker_qty,
            detail=d.detail,
        )
    try:
        kinds = ", ".join(sorted({d.kind for d in result.divergences}))
        tickers = ", ".join(sorted({d.ticker for d in result.divergences}))
        engine.alerting.alert(
            "warning",
            f"Reconciler found {len(result.divergences)} ledger/broker "
            f"divergence(s) [{kinds}] on {tickers} — review for orphan positions",
            {
                "divergences": [
                    {
                        "kind": d.kind,
                        "ticker": d.ticker,
                        "local_qty": d.local_qty,
                        "broker_qty": d.broker_qty,
                    }
                    for d in result.divergences
                ],
                "as_of": now.isoformat(),
            },
            as_of=now,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("engine.reconcile_divergence.alert_failed", error=str(exc))


def check_portfolio_breakers(engine: "Engine", account, now: datetime) -> None:
    """Feed live P&L into the daily-loss / per-position breakers (A4 P1).

    These two §3.9 breakers had NO production caller — they could never
    trip.  Wire them here: compute the daily portfolio P&L fraction and the
    worst per-position intraday P&L fraction from the data already loaded
    this cycle and hand them to the breaker ``check_*`` helpers, which latch
    durably.  A trip raises ``BreakerTrippedError`` (caught here); the
    latched breaker then halts the NEXT cycle's ``_safety_gate`` (and this
    cycle's gate callable already consults ``any_tripped``).  Fail-safe.
    """
    from arbiter.safety.breakers import BreakerTrippedError  # noqa: PLC0415

    # Daily-loss breaker: daily_pl as a fraction of equity (or last_equity).
    equity = getattr(account, "equity", None)
    daily_pl = getattr(account, "daily_pl", None)
    if equity and equity > 0 and daily_pl is not None:
        try:
            engine.breaker.check_daily_loss(
                float(daily_pl) / float(equity),
                engine.conn,
                engine.clock,
                audit_path=engine.config.audit_path,
            )
        except BreakerTrippedError as exc:
            log.warning("engine.breaker.daily_loss_tripped", reason=str(exc))
        except Exception as exc:  # noqa: BLE001
            log.error("engine.breaker.daily_loss_failed", error=str(exc))

    # Per-position breaker: worst held-name intraday P&L vs avg cost.
    try:
        positions = engine.executor.get_positions()
    except Exception as exc:  # noqa: BLE001
        log.warning("engine.breaker.positions_failed", error=str(exc))
        return
    for ticker, snap in positions.items():
        avg = float(snap.avg_price)
        if avg <= 0:
            continue
        current = None
        if engine.current_price_provider is not None:
            try:
                current = engine.current_price_provider.current_price(ticker)
            except Exception:  # noqa: BLE001
                current = None
        if current is None:
            continue  # no live price → fail-closed (no spurious trip)
        # P&L fraction FROM THE POSITION'S PERSPECTIVE: a long gains as price
        # rises, a SHORT gains as price falls.  Flip the sign for shorts
        # (snap.shares < 0) so a LOSING short (price up) yields a NEGATIVE pct
        # and can trip the per-position intraday-loss breaker (long unchanged).
        raw_pct = (float(current) - avg) / avg
        position_pct = raw_pct if float(snap.shares) >= 0 else -raw_pct
        try:
            engine.breaker.check_per_position(
                position_pct,
                engine.conn,
                engine.clock,
                audit_path=engine.config.audit_path,
            )
        except BreakerTrippedError as exc:
            log.warning(
                "engine.breaker.per_position_tripped",
                ticker=ticker,
                reason=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("engine.breaker.per_position_failed", error=str(exc))
