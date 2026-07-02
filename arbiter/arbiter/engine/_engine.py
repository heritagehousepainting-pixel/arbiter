"""Composition root — Wave C.

``build_engine(config, *, conn, pit, clock) -> Engine`` wires every lane
into a runnable paper-sim cycle.  All dependency injection is done here;
lane modules never import each other directly.

Paper-only guarantee
--------------------
The executor is selected by ``config.executor_backend``: a real (paper)
``AlpacaAdapter`` is used only when ``executor_backend == "alpaca_paper"``
AND both Alpaca keys are present; otherwise the in-memory ``SimExecutor`` is
used.  ``config.live_trading`` does NOT select the executor (it is reserved
for a future live path and asserted to require keys); no live-money trading
endpoint exists in the package.

Clock wiring
------------
After construction the audit module-level clock hook and the MetricsWriter
``recorded_at`` are both wired to ``clock.now().isoformat()`` so the
``"NO_CLOCK"`` / ``"CLOCK_NOT_WIRED"`` sentinels are replaced.

Advisor map (A1 only for MVP)
------------------------------
``advisor_map = {"A1.insider": fn, "A1.congress": fn}``

Each advisor function:
  1. Calls ``detect_signals(conn, as_of, ...)`` for its source.
  2. Scores each signal with ``score_signal(signal, as_of)``.
  3. Emits the best opinion via ``emit_opinion(signal, as_of, score_bundle)``.
  4. Returns ``Opinion | None`` (abstention = None, not raised).

A2 (MiroFish) and A3 (tips) are shadow/absent in the MVP and are NOT wired.

Cycle contract
--------------
``Engine.run_cycle(as_of)`` drives one full cycle:
  ingest ideas → gather opinions → fuse (equal-weight) → decide → submit.

The cycle is idempotent: re-running with the same as_of produces no
duplicate orders (dedup_hash UNIQUE constraint in the DB).

Auto-pause (critical alerting — §3.9)
--------------------------------------
``Engine.paused`` latches True when a critical alert fires.  A paused engine
short-circuits ``run_cycle`` immediately — no opinions gathered, no orders
submitted.  The pause persists across cycles until ``resume()`` is called
(admin action only).  Critical conditions wired:

  a. Any circuit breaker is tripped at the start of the cycle.
  b. The kill switch reports halted.
  c. A ``BrokerError`` is raised by ``submit_order`` during order submission.

The ``Alerting`` instance is constructed in ``build_engine()`` and injected
into ``Engine`` (injectable for tests via ``alerting=`` kwarg).  An exception
inside the alerting call must not crash the cycle (fail-safe).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

import arbiter.db.audit as _audit_mod
from arbiter.config import Config, load_config
from arbiter.contract.opinion import Opinion
from arbiter.contract.seams import FusionOutput, Idea, PaperOrder
from arbiter.data.clock import BacktestClock, Clock
from arbiter.data.current_price import (
    AlpacaCurrentPriceSource,
    CurrentPriceProvider,
    NullCurrentPriceProvider,
)
from arbiter.data.pit import PITGateway
from arbiter.runtime.market_calendar import (
    AlpacaMarketCalendar,
    MarketCalendar,
    OfflineMarketCalendar,
)
from arbiter.data.sources import build_price_gateway
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.execution.alpaca_adapter import AlpacaAdapter, build_executor
from arbiter.execution.submit import submit_order
from arbiter.calibration.calibrator import Calibrator
from arbiter.fusion.engine import fuse as _fuse
from arbiter.trust.ledger import TrustLedger
from arbiter.metrics import MetricsWriter
from arbiter.orchestrator.cycle import run_cycle, CycleResult
from arbiter.orchestrator.idea import make_idea
from arbiter.policy.book import RiskBook
from arbiter.policy.decision import decide as _decide
from arbiter.safety import Alerting, CircuitBreaker, KillSwitch, is_trading_allowed
from arbiter.shared.executor import Executor
from arbiter.shared.sim_executor import SimExecutor
from arbiter.signals.detection import detect_signals
from arbiter.signals.leaderboard import render_leaderboard
from arbiter.types import HorizonBucket, IdeaState

from arbiter.engine.advisors import (
    _build_a1_activist_fn,
    _build_a1_congress_fn,
    _build_a1_fund_fn,
    _build_a1_insider_fn,
    _build_a1_sell_fn,
    _build_a2_mirofish_fn,
    _us_market_open,
)
from arbiter.engine import learning as _learning
from arbiter.engine import reconcile as _reconcile
from arbiter.engine import safety_ops as _safety_ops
from arbiter.signals.detection import SignalType as _SignalType

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Engine dataclass
# ---------------------------------------------------------------------------

@dataclass
class Engine:
    """Wired arbiter engine.  All lanes are composed here.

    Construct via ``build_engine()``; do not instantiate directly.

    Attributes
    ----------
    paused:
        When True the engine has been auto-paused by a critical alert.
        ``run_cycle`` short-circuits immediately and returns a
        ``CycleResult(paused_by_alert=True)`` without submitting any orders.
        Cleared only by calling ``resume()`` (admin action).
    alerting:
        Tiered alerting instance.  Fires ``AutoPauseSentinel`` on critical
        conditions and sets ``paused = True``.  Injected for testability.
    """

    config: Config
    conn: sqlite3.Connection
    pit: PITGateway
    clock: Clock
    executor: Executor
    breaker: CircuitBreaker
    kill_switch: KillSwitch
    advisor_map: dict[str, Callable[[], Opinion | None]]
    alerting: Alerting
    _metrics: MetricsWriter = field(repr=False)
    current_price_provider: "CurrentPriceProvider | None" = field(default=None)
    market_calendar: "MarketCalendar | None" = field(default=None)
    paused: bool = field(default=False)
    # A2 (MiroFish) — per-idea, list-valued advisor channel.  Distinct from the
    # single-opinion ``advisor_map`` (A2 takes an Idea and returns 0..N
    # Opinions).  ``None`` or a noop-returning fn when MIROFISH_ENDPOINT unset.
    a2_mirofish_fn: "Callable[[Idea], list[Opinion]] | None" = field(default=None)

    # ------------------------------------------------------------------
    # Learning loop (sub-project #4) — long-lived stateful members.
    # ``ledger`` carries last_update_at/outcomes_at_last_update so the
    # should_update gate works across cycles; ``calibrators`` are per-advisor;
    # ``_learning_cache`` holds the last (ledger_bundle, MultiAdvisorCalibrator,
    # cap_reasons) so a no-new-outcome LIVE cycle reuses them.  In BACKTEST mode
    # the cache is NEVER used (D2 — recompute each step for PIT correctness).
    ledger: "TrustLedger | None" = field(default=None)
    calibrators: "dict[str, Calibrator]" = field(default_factory=dict)
    _learning_cache: "tuple | None" = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resume(self) -> None:
        """Clear the auto-pause flag (admin action only).

        After calling ``resume()`` the engine will process cycles normally
        again.  The caller is responsible for ensuring the underlying critical
        condition has been resolved before resuming.
        """
        self.paused = False
        self._persist_paused(False, reason="", now=self.clock.now())
        log.info("engine.resumed")

    def _persist_paused(self, paused: bool, *, reason: str, now: datetime) -> None:
        """Durably persist the pause flag (C4) — fail-safe, never crashes."""
        try:
            self.conn.execute(
                "INSERT INTO engine_state (id, paused, reason, updated_at) "
                "VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET paused = excluded.paused, "
                "reason = excluded.reason, updated_at = excluded.updated_at",
                (1 if paused else 0, reason, now.isoformat()),
            )
            self.conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.error("engine.persist_paused_failed", error=str(exc))

    def restore_persisted_pause(self) -> None:
        """Restore ``self.paused`` from the durable store on build/daemon start (C4)."""
        try:
            row = self.conn.execute(
                "SELECT paused, reason FROM engine_state WHERE id = 1"
            ).fetchone()
        except Exception as exc:  # noqa: BLE001
            log.error("engine.restore_paused_failed", error=str(exc))
            return
        if row is not None and row["paused"]:
            self.paused = True
            log.warning(
                "engine.paused_restored_from_store",
                reason=row["reason"],
                detail="durable pause flag restored — engine will not trade until resume()",
            )

    def _reconcile_pending_orders(self, now: datetime) -> None:
        """Promote pending broker orders that have since filled (alpaca_paper).

        Delegates to :func:`arbiter.engine.reconcile.reconcile_pending_orders`.
        """
        _reconcile.reconcile_pending_orders(self, now)

    def _advance_buy_idea(self, row, order_id: str, now: datetime, idea_store) -> None:
        """Advance a filled BUY's owning idea → MONITORED (D2 P1)."""
        _reconcile.advance_buy_idea(self, row, order_id, now, idea_store)

    def _mark_order_terminal(self, order_row, report, now: datetime) -> None:
        """Map a terminal broker state to a terminal LOCAL order status (C2)."""
        _reconcile.mark_order_terminal(self, order_row, report, now)

    def _close_out_filled_sell(self, order_row, report, now: datetime) -> None:
        """Close out a filled/partial SELL by driving the idea lifecycle (B4)."""
        _reconcile.close_out_filled_sell(self, order_row, report, now)

    @staticmethod
    def _sell_label_kind(order_row) -> str:
        """Recover the exit label_kind recorded on a SELL order row."""
        return _reconcile.sell_label_kind(order_row)

    def _run_exit_monitor(self, now: datetime, opinions: list[Opinion]) -> list[str]:
        """Run the exit/sell monitor for the cycle (sub-project #2)."""
        return _safety_ops.run_exit_monitor(self, now, opinions)

    def _fire_critical_alert(self, message: str, ctx: dict, as_of: datetime) -> None:
        """Fire a critical alert and latch ``self.paused`` if sentinel returned."""
        _safety_ops.fire_critical_alert(self, message, ctx, as_of)

    def _market_is_open(self, now: datetime) -> bool:
        """True if the US equity market is open at *now* (calendar-aware)."""
        return _safety_ops.market_is_open(self, now)

    def _safety_gate(self, now: datetime) -> CycleResult | None:
        """Shared safety gate for ``run_cycle`` AND ``run_fast_iteration`` (C5)."""
        return _safety_ops.safety_gate(self, now)

    # ------------------------------------------------------------------
    # Risk-book seeding (A2) — make the book-aware caps actually bind.
    # ------------------------------------------------------------------

    def _position_market_value(self, ticker: str, snap, now: datetime) -> float:
        """USD market value of a held position (shares × current price)."""
        return _safety_ops.position_market_value(self, ticker, snap, now)

    def _seed_risk_book(self, now: datetime) -> RiskBook:
        """Build a ``RiskBook`` from the CURRENT held book as notional USD (A2)."""
        return _safety_ops.seed_risk_book(self, now)

    # ------------------------------------------------------------------
    # Divergence reconciler (D3) — surface orphan / drifted positions.
    # ------------------------------------------------------------------

    def _run_divergence_reconcile(self, now: datetime) -> None:
        """Run the position reconciler and surface any divergence (D3 P0)."""
        _safety_ops.run_divergence_reconcile(self, now)

    # ------------------------------------------------------------------
    # Portfolio circuit breakers (A4) — daily-loss + per-position.
    # ------------------------------------------------------------------

    def _check_portfolio_breakers(self, account, now: datetime) -> None:
        """Feed live P&L into the daily-loss / per-position breakers (A4 P1)."""
        _safety_ops.check_portfolio_breakers(self, account, now)

    def run_fast_iteration(self, now: datetime | None = None) -> CycleResult:
        """Run a CHEAP, FREQUENT intraday iteration (sub-project #3, Decision 4).

        Runs ONLY: the shared safety gate → reconcile pending broker orders
        (alpaca_paper) → account / A2 fail-closed read → the exit monitor with
        NO fresh opinions (so stop-loss against the LIVE current price and
        horizon-expiry fire, while conviction-reversal is INERT — absence of a
        fresh opinion is not a reversal, #2 Dec 2b).

        Does NOT ingest, gather opinions, generate entries, snapshot, or run the
        full outcome sweep.  A closeout performed here still writes a complete,
        non-duplicated outcome row via ``close_idea_on_sell_fill`` (C5).
        """
        if now is not None and isinstance(self.clock, BacktestClock):
            self.clock.set_as_of(now)
        now = self.clock.now()

        gate = self._safety_gate(now)
        if gate is not None:
            return gate

        _is_adapter = isinstance(self.executor, AlpacaAdapter)
        if _is_adapter:
            self._reconcile_pending_orders(now)

        # A2 — fail closed on a broker account-read failure (no sim phantom).
        if _is_adapter:
            account = self.executor.get_account()
            _equity = getattr(account, "equity", None)
            if _equity is None or _equity <= 0:
                log.critical(
                    "engine.run_fast_iteration.broker_equity_unavailable",
                    equity=_equity,
                    as_of=now.isoformat(),
                )
                return CycleResult(ideas_processed=0)

        # Exit monitor with NO fresh opinions → reversal inert; stop + horizon fire.
        sold = self._run_exit_monitor(now, [])

        # F3 P0 — snapshot the SimExecutor after a fast-iteration SELL.  The
        # exit monitor synchronously fills sim stop-loss / horizon SELLs (the
        # in-memory broker mutates AND the durable ledger closes the idea), but
        # the only other snapshot site is the END of run_cycle.  Without this a
        # crash + KeepAlive relaunch reseeds the STALE snapshot and resurrects
        # the just-closed position (cash/realized_pl roll back too).  Snapshot in
        # the SAME iteration as the ledger mutation so the durable snapshot can
        # never lag a durable ledger close.
        if sold and isinstance(self.executor, SimExecutor):
            from arbiter.execution import position_store  # noqa: PLC0415
            try:
                position_store.snapshot_executor(
                    self.conn, self.executor, as_of=now
                )
            except Exception as exc:  # noqa: BLE001
                log.error("engine.run_fast_iteration.snapshot_failed", error=str(exc))

        log.info("engine.run_fast_iteration.done", as_of=now.isoformat(), paused=self.paused)
        return CycleResult(ideas_processed=0)

    def _build_learning_inputs(self, now: datetime):
        """Build the (WeightBundle, calibrator) handed to ``fuse`` (sub-project #4)."""
        return _learning.build_learning_inputs(self, now)

    def _persist_cycle_opinions(
        self, now: datetime, valid_opinions: list[Opinion], ideas: list[Idea]
    ) -> None:
        """Persist each non-abstain opinion linked to its idea (#5a, D1)."""
        _learning.persist_cycle_opinions(self, now, valid_opinions, ideas)

    def _eligible_by_advisor(
        self, outcomes_by_advisor: dict
    ) -> dict[str, list[str]]:
        """v1 eligible-idea roster (D4): the set of idea_ids the advisor produced an
        outcome on (coverage ≈ 1.0).  A real roster (incl. abstained ideas) needs
        Lane-13 idea→advisor eligibility — out of scope for #4 (R2)."""
        return _learning.eligible_by_advisor(self, outcomes_by_advisor)

    def _gather_a3_opinions(self, tickers: list[str] | None = None) -> list[Opinion]:
        """Gather corroborated A3 (news) opinions for this cycle (fail-closed).

        Delegates to ``arbiter.adapters.a3.gather_a3_opinions``.  ``tickers``
        selects the sweep set (Tier-3 #12 catalyst gate); ``None`` falls back
        to the full default watchlist (legacy behavior).  The adapter
        self-gates: it returns ``[]`` when ``finnhub_api_key`` is unset and
        under a ``BacktestClock`` (no network / no look-ahead), and never
        raises.  We still wrap in a guard so any unexpected A3 failure can
        never abort the trading cycle.
        """
        try:
            from arbiter.adapters.a3 import gather_a3_opinions  # noqa: PLC0415

            if tickers is None:
                from arbiter.ingest.runner import _DEFAULT_WATCHLIST  # noqa: PLC0415

                tickers = list(_DEFAULT_WATCHLIST)
            if not tickers:
                return []
            return gather_a3_opinions(self.conn, self.clock, self.config, tickers)
        except Exception as exc:  # noqa: BLE001
            log.warning("engine.a3.gather_failed", error=str(exc))
            return []

    def _gather_a4_opinions(self) -> list[Opinion]:
        """Gather A4.macro opinions from persisted findings (fail-closed)."""
        try:
            from arbiter.adapters.a4 import gather_a4_opinions  # noqa: PLC0415
            return gather_a4_opinions(self.conn, self.clock, self.config)
        except Exception as exc:  # noqa: BLE001
            log.warning("engine.a4.gather_failed", error=str(exc))
            return []

    def run_cycle(self, as_of: datetime | None = None) -> CycleResult:
        """Run one full decision cycle.

        Parameters
        ----------
        as_of:
            Information timestamp.  Defaults to ``clock.now()``.

        Returns
        -------
        CycleResult
            Statistics for the cycle.  When the engine is paused by a critical
            alert, ``CycleResult.paused_by_alert`` is True and no orders are
            submitted.
        """
        if as_of is not None and isinstance(self.clock, BacktestClock):
            self.clock.set_as_of(as_of)

        now: datetime = self.clock.now()

        # Shared safety gate (C5): paused → kill-switch → breaker, each early-return.
        _gate = self._safety_gate(now)
        if _gate is not None:
            return _gate

        # ------------------------------------------------------------------
        # Reconcile pending broker orders FIRST (alpaca_paper mode only).
        # Async fills from a prior cycle are promoted pending→filled here and
        # the corresponding idea is advanced → MONITORED (spec §4.4 + A1).
        # ------------------------------------------------------------------
        _is_adapter = isinstance(self.executor, AlpacaAdapter)
        if _is_adapter:
            self._reconcile_pending_orders(now)
            # D3 P0 — surface ledger/broker divergences (orphan positions, manual
            # broker edits, lost-response fills) that the pending-order promotion
            # path is structurally blind to.  Diagnostic only; routes to alert/log.
            self._run_divergence_reconcile(now)

        account = self.executor.get_account()

        # A4 P1 — feed live P&L into the daily-loss / per-position breakers so
        # they can actually latch (they had no production caller before).  A trip
        # latches durably; the gate callable below + the next cycle's safety gate
        # consult ``any_tripped`` and halt.  Runs in BOTH sim and adapter modes.
        self._check_portfolio_breakers(account, now)

        # ------------------------------------------------------------------
        # A2 — fail CLOSED on a broker account-read failure.  The AlpacaAdapter
        # returns zeros on a /v2/account exception; we must NOT fall back to the
        # 100_000.0 sim phantom on a real (paper) account.  Run no orders.
        # ------------------------------------------------------------------
        if _is_adapter:
            _equity = getattr(account, "equity", None)
            if _equity is None or _equity <= 0:
                log.critical(
                    "engine.run_cycle.broker_equity_unavailable",
                    equity=_equity,
                    as_of=now.isoformat(),
                )
                # Page the operator: a broker account-read failure is a real
                # outage, not just a quiet no-op. Trading is already blocked
                # (0-order return); this surfaces it to the alert webhook.
                self._fire_critical_alert(
                    message=(
                        "Broker account read failed (equity unavailable) — "
                        "skipping cycle, no orders. Check Alpaca connectivity."
                    ),
                    ctx={"equity": _equity, "as_of": now.isoformat()},
                    as_of=now,
                )
                return CycleResult(ideas_processed=0)

        # A4 — warn if we are submitting while the US market is closed: day limit
        # orders placed off-hours expire unfilled and only reconcile as no-fill.
        # Prefer the injected MarketCalendar (#3, DST/holiday/early-close aware);
        # fall back to the coarse heuristic when no calendar is wired.
        if _is_adapter and not self._market_is_open(now):
            log.warning(
                "engine.run_cycle.market_closed",
                as_of=now.isoformat(),
                detail="submitting day limit orders while US market closed",
            )

        # Lazy imports (house style) for the Phase-2 persistence lanes.
        from arbiter.execution import position_store  # noqa: PLC0415
        from arbiter.orchestrator import idea_store, outcome_runner  # noqa: PLC0415
        from arbiter.orchestrator.scheduler import run_named_advisors_parallel  # noqa: PLC0415

        # ------------------------------------------------------------------
        # Gather opinions ONCE up front so they feed BOTH the exit monitor's
        # conviction-reversal check and the entry path (no second advisor pass).
        # ------------------------------------------------------------------
        raw_opinions = run_named_advisors_parallel(self.advisor_map, timeout_seconds=30.0)
        valid_opinions: list[Opinion] = [op for op in raw_opinions.values() if op is not None]
        live_advisor_count: int = len(valid_opinions)

        # ------------------------------------------------------------------
        # Exit / sell monitor (sub-project #2) — runs BEFORE new entries so we
        # derisk and free buying power first, and BEFORE any no-signal/no-idea
        # early return so protective stops/horizon exits ALWAYS run while a
        # position is held.  Consumes the in-cycle opinions for the reversal
        # trigger.  Paused/kill-switched/breaker-tripped engines never reach
        # here (those gates returned early above) — paused = no autonomous sells.
        # ------------------------------------------------------------------
        self._run_exit_monitor(now, valid_opinions)

        # Load active (non-terminal) ideas from the durable store for cross-run
        # dedupe — the orchestrator cycle uses these to skip (ticker, bucket)
        # ideas that are already live from a prior run.
        active_ideas = idea_store.load_active_ideas(self.conn)

        # Build the set of currently-held tickers.  A held name normally blocks
        # a fresh idea ("don't double-buy") — UNLESS the add-on gate passes
        # (Tier-2 #5, 2026-07-02): per-name cap headroom remains AND we haven't
        # already opened/added the name today.  The ORDER side is checked at
        # submit time (an add must match the held side; a mismatch still hits
        # the broker dedup exactly as before).
        held_positions = self.executor.get_positions()
        held_tickers: set[str] = set(held_positions.keys())
        _addon_name_cap = self.config.max_position_pct * float(account.equity)
        _addon_min_notional = 25.0  # no dust adds

        def _addon_ok(ticker: str) -> bool:
            """True when a HELD ticker may take a fresh add-on idea."""
            snap = held_positions.get(ticker)
            if snap is None:
                return False
            held_notional = abs(snap.shares * snap.avg_price)
            headroom = _addon_name_cap - held_notional
            if headroom < _addon_min_notional:
                return False
            # Daily cooldown: at most one opening order (non-exit) per ticker
            # per day, regardless of advisor set.
            row = self.conn.execute(
                "SELECT COUNT(*) AS c FROM orders WHERE ticker = ? "
                "AND entry_date = ? "
                "AND json_extract(exits_json, '$.exit_label_kind') IS NULL",
                (ticker, now.date().isoformat()),
            ).fetchone()
            if int(row["c"] if "c" in row.keys() else row[0]) > 0:
                return False
            _audit_mod.audit(
                "engine.run_cycle.addon_candidate",
                {
                    "ticker": ticker,
                    "held_notional": held_notional,
                    "name_cap_headroom": headroom,
                },
                ts=now.isoformat(),
                audit_path=self.config.audit_path,
            )
            return True

        # Detect what signals exist to build ideas — BEFORE the A3 gather so
        # the catalyst gate below can see this cycle's fresh filing tickers.
        signals = detect_signals(self.conn, now)

        # A3 (news) — gather corroborated free-news opinions up front so they can
        # spawn their OWN short-horizon ideas (A3 has no filing in detect_signals,
        # so without this it could never trade or earn trust).  Self-gating in
        # adapters.a3: returns [] without FINNHUB_API_KEY and under a BacktestClock
        # (no network / no look-ahead); never raises.
        #
        # Tier-3 #12 (2026-07-02): the sweep is CATALYST-GATED — only tickers
        # that are held, carry a fresh filing signal this cycle, or have an
        # active idea.  The previous full-watchlist sweep (138 names) took
        # 30+ min per full cycle under Finnhub's free-tier rate limit and
        # starved the daemon's stop-checks meanwhile.  Escape hatch: set
        # ``a3_catalyst_only = False`` on config to restore the full sweep.
        if getattr(self.config, "a3_catalyst_only", True):
            catalyst: set[str] = set(held_tickers)
            catalyst.update(s.ticker for s in signals)
            catalyst.update(i.ticker for i in active_ideas)
            log.info(
                "engine.a3.catalyst_gate",
                n_catalyst=len(catalyst),
                as_of=now.isoformat(),
            )
            a3_opinions = self._gather_a3_opinions(sorted(catalyst))
        else:
            a3_opinions = self._gather_a3_opinions()
        a4_opinions = self._gather_a4_opinions()
        if not signals and not a3_opinions and not a4_opinions:
            log.info("engine.run_cycle.no_signals", as_of=now.isoformat())
            return CycleResult(ideas_processed=0)

        # Build one Idea per distinct (ticker, source) combination (MVP heuristic).
        seen_tickers: set[str] = set()
        ideas: list[Idea] = []
        for sig in signals:
            if sig.ticker in seen_tickers:
                continue
            # A held ticker blocks a fresh idea UNLESS the add-on gate passes.
            if sig.ticker in held_tickers and not _addon_ok(sig.ticker):
                log.info(
                    "engine.run_cycle.skip_held_ticker",
                    ticker=sig.ticker,
                    as_of=now.isoformat(),
                )
                continue
            seen_tickers.add(sig.ticker)
            # Horizon: form4/form13d → 180d (LONG); congress → 90d (MEDIUM).
            # form13d MUST map to 180 so the 180-day A1.activist opinion links
            # to its idea by typed (ticker, HorizonBucket) in
            # _persist_cycle_opinions (a 90-day mismatch would orphan it).
            # Tier-3 #9: SELL-cluster signals emit 90d MEDIUM opinions
            # regardless of source — the idea horizon MUST match or the
            # opinion orphans (same linkage rule).
            if sig.signal_type in (_SignalType.CLUSTER_SELL, _SignalType.CONGRESS_SELL):
                horizon = 90
            else:
                horizon = 180 if sig.source in ("form4", "form13d", "form13f") else 90
            idea = make_idea(
                ticker=sig.ticker,
                thesis=f"{sig.signal_type.value} on {sig.ticker}",
                horizon_days=horizon,
                as_of=now,
            )
            ideas.append(idea)

        # A3 (news) idea-spawning: build a SHORT-horizon idea per corroborated
        # news ticker we don't already hold / haven't built this cycle, then
        # append the A3 opinion so it persists, fuses (probationary EQUAL_FLOOR
        # weight until it graduates on real outcomes), and links to its idea by
        # the (ticker, SHORT bucket) typed key.  horizon_days=7 ⇒ SHORT, matching
        # the opinion's own bucket so it never orphans (the attribution bug we
        # caught in audit).  A3 sizes SMALL and is governed by the learning loop.
        for op in a3_opinions:
            if op.ticker in held_tickers and not _addon_ok(op.ticker):
                continue
            if op.ticker not in seen_tickers:
                seen_tickers.add(op.ticker)
                ideas.append(make_idea(
                    ticker=op.ticker,
                    thesis=f"news on {op.ticker}",
                    horizon_days=op.horizon_days,
                    as_of=now,
                ))
            valid_opinions.append(op)
            live_advisor_count += 1

        for op in a4_opinions:
            if op.ticker in held_tickers and not _addon_ok(op.ticker):
                # Skip macro opinions on held tickers without add-on headroom;
                # A4 otherwise spawns short-horizon ideas like any advisor.
                continue
            if op.ticker not in seen_tickers:
                seen_tickers.add(op.ticker)
                ideas.append(make_idea(
                    ticker=op.ticker,
                    thesis=f"macro on {op.ticker}",
                    horizon_days=op.horizon_days,
                    as_of=now,
                ))
            valid_opinions.append(op)
            live_advisor_count += 1

        if not ideas:
            return CycleResult(ideas_processed=0)

        # ------------------------------------------------------------------
        # A2 (MiroFish) — list-valued, idea-specific advisor.  Runs AFTER ideas
        # are built (it analyzes a SPECIFIC idea) and BEFORE persistence/fusion,
        # extending the single-opinion A1 pool.  No-op when MIROFISH_ENDPOINT is
        # unset (the builder short-circuits to []).  Appending to
        # ``valid_opinions`` is the single choke point that feeds persistence,
        # the replay map (→ run_cycle → fusion), all with zero further wiring.
        # The exit monitor already ran above (A1-only this wave — A2 is
        # shadow/weight-0 and does not yet inform live exits/reversals).
        # ------------------------------------------------------------------
        if self.a2_mirofish_fn is not None:
            for idea in ideas:
                a2_ops = self.a2_mirofish_fn(idea)  # 0..N opinions, never raises
                for op in a2_ops:
                    valid_opinions.append(op)
                    live_advisor_count += 1  # A2 opinions count toward the live quorum

        # ------------------------------------------------------------------
        # Persist gathered opinions linked to the idea they informed (#5a, D1).
        # Link = op.ticker == idea.ticker AND HorizonBucket(op.horizon_days) ==
        # HorizonBucket(idea.dedupe_key[1]) — typed HorizonBucket equality on both
        # sides (E3; a str-vs-enum mismatch would silently link nothing).  An
        # opinion matching no idea this cycle (held/deduped ticker — the NORMAL
        # case on source-overlapping tickers, E3) is still persisted with
        # idea_id=NULL for audit completeness.  Persisted at the decision ``now``
        # (clock-injected; backtest stamps the replay date — PIT-clean).  Persist
        # errors are surfaced/counted, NOT swallowed (E1).
        self._persist_cycle_opinions(now, valid_opinions, ideas)

        # Build bound callables for the cycle.
        # Learning step (sub-project #4): replace the hardcoded equal-weight +
        # passthrough with the trust-ledger-derived bundle + real calibrator.
        # Reads outcomes resolved STRICTLY BEFORE ``now`` (D0); the end-of-cycle
        # outcome sweep below writes NEW outcomes AFTER this — that ordering is
        # the no-look-ahead guarantee (R6: do not move the sweep earlier).
        advisor_ids = list(self.advisor_map.keys())
        weight_bundle, calibrator = self._build_learning_inputs(now)

        # Quorum fix: if 0 live advisors, return HALTED immediately (no orders).
        if live_advisor_count == 0:
            log.warning(
                "engine.run_cycle.no_live_advisors",
                as_of=now.isoformat(),
                total_advisors=len(advisor_ids),
            )
            return CycleResult(ideas_processed=len(ideas), opinions_gathered=len(advisor_ids), opinions_null=len(advisor_ids))

        def _bound_fuse(opinions: list[Opinion], bucket: HorizonBucket) -> FusionOutput:
            bucket_outputs = _fuse(opinions, weight_bundle, calibrator)
            # Return the FusionOutput for the requested bucket, or a zero-conviction one.
            if bucket in bucket_outputs:
                return bucket_outputs[bucket]
            return FusionOutput(
                bucket=bucket,
                conviction=0.0,
                dispersion=0.0,
                effective_n=0.0,
                n_opinions=0,
                advisor_contributions={},
                vetoes=[],
                cold_start=True,
            )

        def _gate_callable(acct: object, n_advisors: int) -> object:
            breaker_provider = lambda: self.breaker.any_tripped(self.conn)  # noqa: E731
            return is_trading_allowed(
                acct,
                live_advisor_count=n_advisors,
                breaker_provider=breaker_provider,
            )

        def _adv_provider(ticker: str, ts: datetime) -> float | None:
            val = self.pit.get("adv_20d", ticker, ts)
            return float(val) if val is not None else None

        # A2 — seed the risk book from the CURRENT held book as notional USD so
        # the open-count / gross / sector caps bind on the FIRST order.  A
        # one-element list lets the submit closure swap in a NEW (immutable) book
        # after each successful submit so subsequent decides see the freshly
        # committed exposure — and a rejected/failed order consumes NO headroom.
        _book: list[RiskBook] = [self._seed_risk_book(now)]

        # A2 — portfolio_equity is the broker/sim equity (USD).  The A2 gate above
        # already returned a zero-order cycle for a falsy/zero adapter equity, and
        # sim equity is always real (cash + positions), so the dead 100_000.0
        # phantom-equity fallback is removed (it would size against $100k if the
        # gate were ever reordered — D3 P3).
        _equity_usd = float(account.equity)

        def _bound_decide(fusion: FusionOutput, idea: Idea) -> PaperOrder | None:
            orders = _decide(
                ticker=idea.ticker,
                bucket_outputs={fusion.bucket: fusion},
                account=account,
                gate=_gate_callable,
                adv_provider=_adv_provider,
                clock=self.clock,
                config=self.config,
                portfolio_equity=_equity_usd,
                live_advisor_count=live_advisor_count,  # actual live count this cycle
                **_book[0].as_decide_kwargs(idea.ticker),  # A2: book-aware caps bind
            )
            return orders[0] if orders else None

        _audit_path = self.config.audit_path

        # Use a mutable container so the closure can signal a broker-fatal event
        # back to run_cycle without raising (the orchestrator cycle.py catches
        # BrokerError from submit; we need to fire the alert in engine scope).
        _broker_fatal: list[str] = []

        def _bound_submit(order: PaperOrder) -> bool:
            # Fetch real entry price — fail-closed if unavailable.
            price_val = self.pit.get("price_open", order.ticker, now)
            if price_val is None:
                log.warning(
                    "engine.run_cycle.no_price_skip",
                    ticker=order.ticker,
                    as_of=now.isoformat(),
                )
                return False
            raw_price = float(price_val)

            spread = 0.01  # default fallback spread
            spread_val = self.pit.get("spread", order.ticker, now)
            if spread_val is not None:
                spread = float(spread_val)

            # Tier-2 #5: an order on a HELD ticker whose side MATCHES the held
            # side is an add-on (long+BUY / short+SELL) → skip the broker
            # position-dedup.  A MISMATCHED side (e.g. non-exit SELL on a held
            # long would net the position at Alpaca and corrupt the idea
            # lifecycle) keeps is_addon=False and is blocked by the broker
            # dedup exactly as before.
            _held_snap = held_positions.get(order.ticker)
            _is_addon = (
                _held_snap is not None
                and _held_snap.shares != 0.0
                and (_held_snap.shares > 0.0) == (order.side.value == "BUY")
            )

            # Critical condition (c): broker-fatal error → fire alert + re-raise so
            # cycle.py's BrokerError name-check can halt remaining submissions.
            from arbiter.execution.alpaca_adapter import BrokerError  # noqa: PLC0415
            try:
                sub_result = submit_order(
                    order,
                    self.executor,
                    self.clock,
                    conn=self.conn,
                    spread=spread,
                    raw_price=raw_price,
                    breaker=self.breaker,
                    audit_path=_audit_path,
                    is_addon=_is_addon,
                    allow_fractional=self.config.allow_fractional,
                )
            except BrokerError as exc:
                _broker_fatal.append(str(exc))
                self._fire_critical_alert(
                    message=f"Broker-fatal error during order submission — auto-pausing engine: {exc}",
                    ctx={"order_id": order.order_id, "ticker": order.ticker, "error": str(exc), "as_of": now.isoformat()},
                    as_of=now,
                )
                raise  # re-raise so orchestrator cycle.py can break its submission loop

            # B5: link the persisted order row to its owning idea by idea_id.
            # We match the live idea for (ticker, bucket) — at submit time it is
            # FINAL_DECIDED — so the sell/close-out path can resolve the idea
            # exactly instead of relying on the (ticker, bucket) join.
            if sub_result.order_id is not None:
                try:
                    idea_row = self.conn.execute(
                        "SELECT idea_id FROM ideas WHERE is_superseded = 0 "
                        "AND dedupe_key_ticker = ? AND dedupe_key_bucket = ? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (order.ticker, order.horizon_bucket.value),
                    ).fetchone()
                    if idea_row is not None:
                        self.conn.execute(
                            "UPDATE orders SET idea_id = ? WHERE order_id = ?",
                            (idea_row["idea_id"], sub_result.order_id),
                        )
                        self.conn.commit()
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "engine.run_cycle.idea_id_link_failed",
                        order_id=sub_result.order_id, error=str(exc),
                    )

            # A2 — fold this order's notional USD into the running book ONLY on a
            # confirmed submit (filled, or accepted-pending), so subsequent
            # decides in this cycle see the committed gross/sector/count.  A
            # rejected/failed submit (handled above by re-raise / early return)
            # consumes NO headroom.  ``order.qty`` is the notional dollar amount
            # in this entry path (the same unit the book tracks).
            if sub_result.order_id is not None:
                # Fold REALIZED notional on a PARTIAL fill (the book must not
                # over-count headroom consumed by the unfilled remainder);
                # requested notional on a full/pending fill (exact intended
                # exposure, no float drift; pending is re-seeded next cycle).
                if sub_result.status == "partial" and sub_result.filled_notional is not None:
                    _book[0] = _book[0].add(order.ticker, sub_result.filled_notional)
                else:
                    _book[0] = _book[0].add(order.ticker, float(order.qty))

            # Advance the idea → MONITORED ONLY on a confirmed broker fill
            # (spec §4.4).  A `pending` (accepted-but-unfilled) order, a
            # duplicate, or a zero-share skip must NOT advance the idea: the
            # order row is persisted `pending` and the NEXT cycle's
            # reconciliation promotes it on a real fill.  For SimExecutor this
            # is a no-op (place always returns "filled" synchronously).
            return sub_result.filled

        # Re-use pre-gathered opinions by passing them into a pre-seeded advisor map
        # that returns already-fetched opinions (avoids a second round of advisor calls).
        _cached_opinions: dict[str, Opinion | None] = dict(raw_opinions)

        def _opinion_provider_map() -> dict[str, Callable[[], Opinion | None]]:
            """Wrap cached opinions as zero-arg callables for run_cycle.

            A1: one slot per advisor (None when abstained), preserving today's
            keys exactly.  A2: one SYNTHETIC slot per opinion so a LIST survives
            the single-opinion ``{id: ()->Opinion|None}`` map — a plain
            advisor_id key would collapse N A2 opinions into one.  The synthetic
            key (with ``i`` for uniqueness) only affects dict keying in
            ``run_named_advisors_parallel``; fusion groups by
            ``op.horizon_bucket`` NOT advisor_id, and ``op.advisor_id`` stays
            ``"A2.mirofish"`` for weight resolution — so same-bucket A2 opinions
            still fuse together correctly.
            """
            m: dict[str, Callable[[], Opinion | None]] = {}
            for aid, op in _cached_opinions.items():
                m[aid] = (lambda op=op: op)
            for i, op in enumerate(valid_opinions):
                if op is not None and op.advisor_id == "A2.mirofish":
                    m[f"A2.mirofish#{i}:{op.horizon_bucket.value}"] = (lambda op=op: op)
            return m

        def _on_new_idea(idea: Idea) -> None:
            idea_store.persist_new_idea(self.conn, idea, created_at=self.clock.now())

        def _on_transition(idea: Idea, new_state: IdeaState) -> None:
            idea_store.update_idea_state(
                self.conn,
                idea.idea_id,
                new_state,
                updated_state_at=self.clock.now(),
                audit_path=self.config.audit_path,
            )

        # Options expression overlay (P1 shadow / P2 paper).  Strict no-op when
        # config.options_mode == "off" (express_option early-returns).  Runs
        # AFTER equity handling per idea; never folds delta into the live book in
        # shadow mode (would change equity caps for later ideas this cycle).
        _options_on = self.config.options_mode != "off"

        def _open_options_premium() -> float:
            """Aggregate premium of OPEN paper option positions (sleeve usage)."""
            try:
                return float(self.conn.execute(
                    "SELECT COALESCE(SUM(p.entry_premium), 0) FROM option_positions p "
                    "LEFT JOIN option_outcomes o "
                    "  ON o.idea_id = p.idea_id AND o.occ_symbol = p.occ_symbol "
                    "WHERE o.id IS NULL"
                ).fetchone()[0])
            except Exception:  # table absent / any error → treat as empty sleeve
                return 0.0

        def _bound_express(fusion: FusionOutput, idea: Idea) -> None:
            from arbiter.options.express import express_option  # noqa: PLC0415

            express_option(
                self.conn,
                idea,
                fusion,
                config=self.config,
                book_container=_book,
                clock=self.clock,
                portfolio_equity=_equity_usd,
                open_options_premium=_open_options_premium(),
                current_price_provider=self.current_price_provider,
            )

        # Manage open PAPER option positions (premium-stop / horizon / reversal)
        # BEFORE entries, so any closed position frees sleeve budget this cycle.
        if _options_on and self.config.options_mode == "paper":
            try:
                from arbiter.options.alpaca_options_client import (  # noqa: PLC0415
                    AlpacaOptionsClient,
                )
                from arbiter.options.manage import (  # noqa: PLC0415
                    manage_option_positions,
                )

                _closed = manage_option_positions(
                    self.conn,
                    AlpacaOptionsClient(self.config),
                    config=self.config,
                    clock=now.isoformat(),
                    current_conviction_for=None,
                )
                if _closed:
                    log.info("options.manage.closed", count=len(_closed))
            except Exception as exc:  # never disrupt the equity cycle
                log.warning("options.manage.failed", error=str(exc))

        result = run_cycle(
            ideas=ideas,
            advisor_map=_opinion_provider_map(),
            fuse=_bound_fuse,
            decide=_bound_decide,
            submit=_bound_submit,
            clock=self.clock,
            active_ideas=active_ideas,
            on_new_idea=_on_new_idea,
            on_transition=_on_transition,
            express=_bound_express if _options_on else None,
        )

        # If a broker-fatal event occurred during the cycle, mark the result.
        if _broker_fatal and self.paused:
            result.paused_by_alert = True  # type: ignore[attr-defined]

        ts = now.isoformat()
        self._metrics.record(
            "cycle_complete",
            {
                "ideas_processed": result.ideas_processed,
                "orders_submitted": result.orders_submitted,
                "opinions_gathered": result.opinions_gathered,
            },
            recorded_at=ts,
        )

        # ------------------------------------------------------------------
        # Phase-2 persistence: snapshot positions + run the outcome sweep.
        # Both are wrapped so a failure logs but never aborts the run.
        # ------------------------------------------------------------------
        if isinstance(self.executor, SimExecutor):
            try:
                position_store.snapshot_executor(
                    self.conn, self.executor, as_of=self.clock.now()
                )
            except Exception as exc:  # noqa: BLE001
                log.error("engine.run_cycle.snapshot_failed", error=str(exc))

        # Real attribution (#5a): the outcome sweep fans out one outcome per
        # contributing advisor, recovered from the opinions persisted for the
        # idea above.  ``_advisor_id_for`` (the old horizon proxy) is retired to
        # the LAST-RESORT FALLBACK only — consulted by resolve_advisor_outcomes
        # iff an idea has NO recoverable opinion (legacy / orphan close), where it
        # writes one neutral (stance 0.0) outcome and increments
        # ``attribution.fallback_proxy``.  It is ~1:1 for the 2-advisor MVP
        # (insider=180d, congress=90d) but is no longer the primary attribution.
        def _advisor_id_for(idea: Idea) -> str:
            return "A1.insider" if idea.horizon_days >= 180 else "A1.congress"

        try:
            outcome_runner.run_outcome_sweep(
                self.conn,
                pit=self.pit,
                clock=self.clock,
                advisor_id_for=_advisor_id_for,
                advisor_confidence_for=None,
                audit_path=self.config.audit_path,
                metrics=self._metrics,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("engine.run_cycle.outcome_sweep_failed", error=str(exc))

        # Tier-2 #8 — counterfactually label decided-but-never-executed ideas
        # whose horizon elapsed, then ABANDON them (unblocks the ticker and
        # feeds the learning loop the advisor's falsifiable call).
        try:
            outcome_runner.run_unexecuted_sweep(
                self.conn,
                pit=self.pit,
                clock=self.clock,
                advisor_id_for=_advisor_id_for,
                advisor_confidence_for=None,
                audit_path=self.config.audit_path,
                metrics=self._metrics,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("engine.run_cycle.unexecuted_sweep_failed", error=str(exc))

        log.info(
            "engine.run_cycle.done",
            ideas_processed=result.ideas_processed,
            orders_submitted=result.orders_submitted,
            paused=self.paused,
        )
        return result

    def leaderboard(self, as_of: datetime | None = None) -> str:
        """Render the A1 signal leaderboard as a formatted string."""
        ts = as_of if as_of is not None else self.clock.now()
        return render_leaderboard(ts, plain=True)

    def status(self) -> dict:
        """Return engine status as a dict for CLI / health checks."""
        tripped = self.breaker.any_tripped(self.conn)
        account = self.executor.get_account()
        is_sim = isinstance(self.executor, SimExecutor)
        # open_positions source of truth is mode-aware (spec §4.3):
        #  - sim: the durable Phase-2 snapshot count (survives process restarts);
        #  - alpaca_paper: the broker itself (len(get_positions())) — the broker
        #    is the durable store, the sim snapshot is meaningless there.
        if is_sim:
            from arbiter.execution import position_store  # noqa: PLC0415
            open_positions = position_store.open_position_count(self.conn)
        else:
            open_positions = len(self.executor.get_positions())

        status_dict = {
            "live_trading": self.config.live_trading,
            "executor_backend": self.config.executor_backend,
            "executor": self.executor.name,
            "is_sim": is_sim,
            "tripped_breakers": tripped,
            "open_positions": open_positions,
            "advisor_count": len(self.advisor_map),
            "advisors": list(self.advisor_map.keys()),
            "account_equity": getattr(account, "equity", None),
            "account_cash": account.cash,
            "paused": self.paused,
        }
        # realized_pl is NOT available from /v2/account for the adapter
        # (hardcoded 0.0) — do not present it as truth in alpaca_paper mode.
        # daily_pl (equity - last_equity) IS real and may be surfaced.
        if is_sim:
            status_dict["realized_pl"] = account.realized_pl
        else:
            status_dict["realized_pl"] = None  # unavailable from broker /v2/account
            status_dict["daily_pl"] = account.daily_pl
        return status_dict


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_engine(
    config: Config | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    pit: PITGateway | None = None,
    clock: Clock | None = None,
    kill_switch: KillSwitch | None = None,
    alerting: Alerting | None = None,
) -> Engine:
    """Construct and wire the full arbiter engine.

    Parameters
    ----------
    config:
        Frozen Config.  If None, loads from ``config/arbiter.toml`` + env.
    conn:
        Explicit SQLite connection (for tests; defaults to Config.db_path).
    pit:
        Explicit PITGateway (for tests; defaults to ``build_price_gateway``).
    clock:
        Explicit Clock (for tests; defaults to live ``Clock()``).
    kill_switch:
        Explicit KillSwitch (for tests; defaults to ``KillSwitch(config=config)``).
    alerting:
        Explicit Alerting instance (for tests; defaults to ``Alerting(config)``).

    Returns
    -------
    Engine
        Fully wired engine ready to call ``run_cycle()``.

    Raises
    ------
    AssertionError
        If ``config.live_trading`` is True but no Alpaca keys are present
        (paper-only guarantee for MVP).
    """
    if config is None:
        config = load_config()

    # Paper-only assertion (INTERFACES.md §9).
    if config.live_trading:
        assert (
            config.alpaca_api_key and config.alpaca_secret_key
        ), "LIVE_TRADING=true requires alpaca_api_key and alpaca_secret_key"

    # -- Clock ----------------------------------------------------------------
    if clock is None:
        clock = Clock()

    # -- Database -------------------------------------------------------------
    if conn is None:
        conn = get_connection(config.db_path)
    run_migrations(conn, applied_at=clock.now().isoformat())

    # -- Wire clock into audit module -----------------------------------------
    _audit_mod._clock = lambda: clock.now().isoformat()

    # -- Metrics writer -------------------------------------------------------
    metrics = MetricsWriter(config.metrics_path)

    # -- PITGateway -----------------------------------------------------------
    if pit is None:
        pit = build_price_gateway(config)

    # -- Executor (sim or paper-broker; structurally paper-only — §2) ---------
    # Resolve ``build_executor`` through the ``arbiter.engine`` PACKAGE namespace
    # (not the local import) so tests that ``monkeypatch.setattr(
    # "arbiter.engine.build_executor", ...)`` still swap the executor after the
    # H1 refactor moved ``build_engine`` into ``arbiter.engine._engine``.
    import arbiter.engine as _engine_pkg  # noqa: PLC0415
    executor = _engine_pkg.build_executor(config)
    # Defensive type check: only the two known executors are valid here.
    from arbiter.execution.alpaca_adapter import AlpacaAdapter  # noqa: PLC0415
    assert isinstance(executor, (SimExecutor, AlpacaAdapter)), (
        f"Unexpected executor type {type(executor)}"
    )
    # Seed the in-memory broker from the durable snapshot ONLY for SimExecutor —
    # a fresh process restores prior cash/positions (Phase-2 continuity, WP-D).
    # The AlpacaAdapter talks to a broker that is itself durable, so no seed.
    if isinstance(executor, SimExecutor):
        from arbiter.execution import position_store  # noqa: PLC0415
        position_store.seed_executor(conn, executor)

    # -- Circuit breaker ------------------------------------------------------
    breaker = CircuitBreaker()

    # -- Kill switch (broker-side; fail-closed when URL configured but unreachable) --
    if kill_switch is None:
        kill_switch = KillSwitch(config=config)

    # -- Alerting (tiered; critical → AutoPauseSentinel) ----------------------
    if alerting is None:
        alerting = Alerting(config=config, audit_path=config.audit_path)

    # -- Advisor map (A1 only; A2/A3 absent) ----------------------------------
    # Advisors receive db_path (not the shared conn) so each invocation can
    # open its own thread-local SQLite connection (SQLite objects must be used
    # in the same thread that created them).
    advisor_map: dict[str, Callable[[], Opinion | None]] = {
        "A1.insider": _build_a1_insider_fn(config.db_path, pit, clock),
        "A1.congress": _build_a1_congress_fn(config.db_path, pit, clock),
        "A1.activist": _build_a1_activist_fn(config.db_path, pit, clock),
        "A1.fund": _build_a1_fund_fn(config.db_path, pit, clock),
        # Tier-3 #9 — the bearish disclosure legs (cluster sells).  Separate
        # advisor ids so the learning loop scores sell-signal quality on its
        # own track record; probationary EQUAL_FLOOR like every new advisor.
        "A1.insider_sell": _build_a1_sell_fn(
            config.db_path, pit, clock, signal_type=_SignalType.CLUSTER_SELL
        ),
        "A1.congress_sell": _build_a1_sell_fn(
            config.db_path, pit, clock, signal_type=_SignalType.CONGRESS_SELL
        ),
    }

    # -- A2 (MiroFish) channel — per-idea, list-valued (NOT in advisor_map) ---
    # Configured-or-noop: inert when MIROFISH_ENDPOINT is unset.  ``breaker=None``
    # for now — the A2 breaker callback is a ``Callable[[], None]`` distinct from
    # the ``CircuitBreaker`` object; A2 stays shadow/weight-0 until the live
    # MiroFish wave wires the circuit breaker to it.
    a2_mirofish_fn = _build_a2_mirofish_fn(config.db_path, clock, breaker=None)

    # -- Current-price provider (sub-project #3, amendment C0) ----------------
    # GATE ON CLOCK TYPE, not just the backend: a BACKTEST run in the operator's
    # shell can carry EXECUTOR_BACKEND=alpaca_paper, but it uses a BacktestClock
    # and must NEVER see a live "now" price.  Inject the live source ONLY when
    # the backend is alpaca_paper AND the clock is the live Clock; Null otherwise.
    current_price_provider: CurrentPriceProvider
    market_calendar: MarketCalendar
    _is_backtest = isinstance(clock, BacktestClock)
    if config.executor_backend == "alpaca_paper" and not _is_backtest:
        current_price_provider = AlpacaCurrentPriceSource(config)
        market_calendar = AlpacaMarketCalendar(config)
    else:
        current_price_provider = NullCurrentPriceProvider()
        market_calendar = OfflineMarketCalendar()

    log.info(
        "engine.built",
        live_trading=config.live_trading,
        executor=executor.name,
        advisors=list(advisor_map.keys()),
        current_price_provider=type(current_price_provider).__name__,
        market_calendar=type(market_calendar).__name__,
    )

    engine = Engine(
        config=config,
        conn=conn,
        pit=pit,
        clock=clock,
        executor=executor,
        breaker=breaker,
        kill_switch=kill_switch,
        advisor_map=advisor_map,
        alerting=alerting,
        _metrics=metrics,
        current_price_provider=current_price_provider,
        market_calendar=market_calendar,
        a2_mirofish_fn=a2_mirofish_fn,
    )

    # C4: restore a durably-persisted pause so an auto-relaunched daemon does not
    # silently resume trading after a non-breaker auto-pause.
    engine.restore_persisted_pause()

    # F3 P2 — reconcile-on-start (alpaca_paper).  After a crash + KeepAlive
    # relaunch an in-flight order or a broker/ledger divergence is otherwise
    # invisible until the next scheduled cycle.  Square the local ledger against
    # the broker EAGERLY at build time: promote any pending order that has since
    # filled, then run the diagnostic divergence reconcile to surface orphans.
    # Fail-safe: a recovery error must never block the engine from starting.
    if isinstance(executor, AlpacaAdapter):
        try:
            _now = clock.now()
            engine._reconcile_pending_orders(_now)
            engine._run_divergence_reconcile(_now)
        except Exception as exc:  # noqa: BLE001
            log.error("engine.reconcile_on_start.failed", error=str(exc))

    return engine
