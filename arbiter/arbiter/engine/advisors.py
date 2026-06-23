"""Advisor-builder helpers + the market-hours heuristic (extracted from engine).

These are the module-level free functions that ``build_engine`` uses to wire the
A1 single-opinion advisors and the A2 (MiroFish) per-idea channel, plus the
coarse ``_us_market_open`` heuristic.  Behaviour is byte-for-byte identical to
the original ``arbiter.engine`` module; only the location changed.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import structlog

from arbiter.contract.opinion import Opinion
from arbiter.contract.seams import Idea
from arbiter.data.clock import BacktestClock, Clock
from arbiter.data.pit import PITGateway
from arbiter.db.connection import get_connection
from arbiter.signals.detection import detect_signals
from arbiter.signals.emit import emit_opinion
from arbiter.signals.scoring import score_signal

log = structlog.get_logger(__name__)


def _us_market_open(now: datetime) -> bool:
    """Heuristic US equity market-hours check (A4 — warning only).

    Regular session is Mon–Fri 09:30–16:00 US/Eastern.  We approximate using
    the UTC offset for Eastern (EST = UTC-5).  This is intentionally a coarse
    heuristic: it only drives a log warning (off-hours day limit orders simply
    expire unfilled) and deliberately does NOT account for DST or holidays —
    a real market-hours scheduler is out-of-scope (#3).
    """
    from datetime import timezone, timedelta  # noqa: PLC0415

    eastern = now.astimezone(timezone(timedelta(hours=-5)))
    if eastern.weekday() >= 5:  # Saturday/Sunday
        return False
    minutes = eastern.hour * 60 + eastern.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


# ---------------------------------------------------------------------------
# Internal advisor builder helpers
# ---------------------------------------------------------------------------

def _build_a1_insider_fn(
    db_path: str,
    pit: PITGateway,
    clock: Clock,
) -> Callable[[], Opinion | None]:
    """Return a zero-arg callable that produces Opinion | None for A1.insider.

    Each call opens a fresh SQLite connection so the advisor is safe to run
    in a background thread (SQLite connections are not thread-shareable).
    """

    def _fn() -> Opinion | None:
        as_of: datetime = clock.now()
        # Open a fresh connection per invocation — thread-safe pattern.
        thread_conn = get_connection(db_path)
        try:
            signals = detect_signals(thread_conn, as_of, cluster_min_people=2)
            # Filter to form4 signals only.
            form4_signals = [s for s in signals if s.source == "form4"]
            if not form4_signals:
                return None
            # Take the highest-conviction signal.
            best = max(form4_signals, key=lambda s: s.conviction_score)
            score_bundle = score_signal(best, as_of)
            return emit_opinion(best, as_of, score_bundle)
        finally:
            thread_conn.close()

    return _fn


def _build_a1_congress_fn(
    db_path: str,
    pit: PITGateway,
    clock: Clock,
) -> Callable[[], Opinion | None]:
    """Return a zero-arg callable that produces Opinion | None for A1.congress.

    Each call opens a fresh SQLite connection so the advisor is safe to run
    in a background thread.
    """

    def _fn() -> Opinion | None:
        as_of: datetime = clock.now()
        thread_conn = get_connection(db_path)
        try:
            signals = detect_signals(thread_conn, as_of, cluster_min_people=2)
            # Filter to congress signals only.
            congress_signals = [s for s in signals if s.source == "congress"]
            if not congress_signals:
                return None
            best = max(congress_signals, key=lambda s: s.conviction_score)
            score_bundle = score_signal(best, as_of)
            return emit_opinion(best, as_of, score_bundle)
        finally:
            thread_conn.close()

    return _fn


def _build_a1_activist_fn(
    db_path: str,
    pit: PITGateway,
    clock: Clock,
) -> Callable[[], Opinion | None]:
    """Return a zero-arg callable that produces Opinion | None for A1.activist.

    Mirrors the insider/congress builders: open a fresh connection per call
    (thread-safe), detect signals, keep only ``form13d`` (Schedule 13D/G)
    signals, take the highest-conviction one, score it, and emit.
    """

    def _fn() -> Opinion | None:
        as_of: datetime = clock.now()
        thread_conn = get_connection(db_path)
        try:
            signals = detect_signals(thread_conn, as_of, cluster_min_people=2)
            activist = [s for s in signals if s.source == "form13d"]
            if not activist:
                return None
            best = max(activist, key=lambda s: s.conviction_score)
            score_bundle = score_signal(best, as_of)
            return emit_opinion(best, as_of, score_bundle)
        finally:
            thread_conn.close()

    return _fn


def _build_a1_fund_fn(
    db_path: str,
    pit: PITGateway,
    clock: Clock,
) -> Callable[[], Opinion | None]:
    """Return a zero-arg callable that produces Opinion | None for A1.fund.

    Mirrors the insider/congress/activist builders: open a fresh connection per
    call (thread-safe), detect signals, keep only ``form13f`` (13F-HR fund
    manager holdings delta) signals, take the highest-conviction one, score it,
    and emit.
    """

    def _fn() -> Opinion | None:
        as_of: datetime = clock.now()
        thread_conn = get_connection(db_path)
        try:
            signals = detect_signals(thread_conn, as_of, cluster_min_people=2)
            fund = [s for s in signals if s.source == "form13f"]
            if not fund:
                return None
            best = max(fund, key=lambda s: s.conviction_score)
            score_bundle = score_signal(best, as_of)
            return emit_opinion(best, as_of, score_bundle)
        finally:
            thread_conn.close()

    return _fn


def _build_a2_mirofish_fn(
    db_path: str,
    clock: Clock,
    breaker: Callable[[], None] | None,
) -> Callable[[Idea], list[Opinion]]:
    """Return a per-idea, list-valued callable for the A2.mirofish channel.

    Configured-or-noop at BUILD time: when ``MIROFISH_ENDPOINT`` is unset
    (``_get_endpoint()`` returns ``None``), this returns a fn that
    short-circuits to ``[]`` without ever touching the network.  Otherwise it
    returns a fn that opens a fresh connection per call (thread-safe, mirroring
    the A1 builders) and delegates to the frozen MiroFish adapter, which
    already fails closed.  ``is_backtest`` is derived from the clock type so
    backtests never replay live-looking data (mirofish spec §5.6).
    """
    from arbiter.adapters.mirofish import adapter as _mf  # noqa: PLC0415
    from arbiter.adapters.mirofish.http_client import _get_endpoint  # noqa: PLC0415

    if _get_endpoint() is None:
        log.info("mirofish.disabled", reason="MIROFISH_ENDPOINT unset; A2 inert")

        def _noop(idea: Idea) -> list[Opinion]:
            return []

        return _noop

    _is_backtest = isinstance(clock, BacktestClock)

    def _fn(idea: Idea) -> list[Opinion]:
        as_of = clock.now()
        thread_conn = get_connection(db_path)
        try:
            return _mf.run(
                idea,
                as_of,
                conn=thread_conn,
                breaker=breaker,
                is_backtest=_is_backtest,
            )
        except Exception as exc:  # noqa: BLE001  defense-in-depth; run() already fails closed
            log.warning("mirofish.fn_failed", ticker=idea.ticker, error=str(exc))
            return []
        finally:
            thread_conn.close()

    return _fn
