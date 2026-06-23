"""Market-hours intraday runtime daemon — sub-project #3 (Decisions 3/4/5).

A long-running loop launched once by launchd (``KeepAlive=true``) that, while the
US market is open, runs a CHEAP fast iteration (reconcile + exit-monitor stop /
horizon checks against the LIVE current price) every ``fast_interval_s`` and a
slow FULL cycle (ingest + entries + reversal) only at configured ET times.  While
closed it long-sleeps (capped) until the next open.  On the open→closed transition
it runs ONE post-close full reconcile + outcome sweep (amendment C6).

Resilience (Decision 5):
  * one bad iteration is caught/logged with exponential backoff — never kills the loop;
  * the engine safety gate (paused / kill-switch / breaker) is consulted EVERY
    iteration; a halted engine early-returns but the LOOP KEEPS POLLING and resumes
    when cleared;
  * SIGTERM/SIGINT set the injected ``stop_event`` for a graceful exit;
  * a heartbeat file is rewritten atomically each iteration;
  * a single-instance ``flock`` guards against a daemon + the 18:30 one-shot both
    mutating the same SQLite DB.

OFFLINE testability (Decision 6): ``sleep_fn``, ``stop_event``, ``clock``, and
``calendar`` are all injected.  In tests ``sleep_fn`` advances the BacktestClock and
sets ``stop_event`` after N iterations, so the loop runs with ZERO real time and NO
network.  ``time.sleep`` is imported lazily inside the default factory only.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

import structlog

log = structlog.get_logger(__name__)

# Cap any single sleep so a clock skew / missed wake is recoverable and a
# stop_event interrupts promptly (Decision 5).
_MAX_SLEEP_S: float = 15 * 60.0
_BACKOFF_CAP_S: float = 600.0
# Autonomy: a durable pause from a broker-fatal/breaker trip is treated as
# transient and auto-recovered, up to this many CONSECUTIVE times (the counter
# resets after a clean iteration). The kill switch is exempt — a human halt is
# always respected. Past the cap the engine stays paused and pages the operator.
_MAX_CONSECUTIVE_RECOVERIES: int = 5


def _default_sleep(seconds: float) -> None:
    import time  # lazy — the ONLY place real wall-clock time is consumed

    time.sleep(seconds)


def _parse_full_times(spec: str) -> list[tuple[int, int]]:
    """Parse "09:45,15:30" → [(9, 45), (15, 30)] (ET hour/minute pairs)."""
    out: list[tuple[int, int]] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        hh, _, mm = part.partition(":")
        out.append((int(hh), int(mm)))
    return out


@dataclass
class DaemonState:
    """Mutable scheduling state tracked across iterations."""

    last_ingest_date: Any = None        # date of the last ingest
    fired_full_slots: set = field(default_factory=set)  # (date, (h, m)) already run
    last_session_open: bool | None = None  # for open→closed transition detection
    backoff_s: float = 0.0
    consecutive_recoveries: int = 0     # auto-recoveries since the last clean iteration
    recovery_capped_alerted: bool = False  # one-shot alert when the cap is hit


def _heartbeat(path: str | None, payload: dict) -> None:
    """Atomically (re)write the heartbeat file.  Fail-safe."""
    if not path:
        return
    import json  # lazy

    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as fh:
            fh.write(json.dumps(payload))
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        log.warning("daemon.heartbeat_write_failed", error=str(exc))


def _acquire_single_instance_lock(pidfile: str):
    """Acquire an exclusive flock on *pidfile*.  Returns the open fd or None.

    A second daemon (or the 18:30 one-shot) that cannot grab the lock exits
    cleanly so two processes never mutate the same SQLite DB concurrently.
    """
    import fcntl  # lazy (POSIX)

    try:
        os.makedirs(os.path.dirname(pidfile) or ".", exist_ok=True)
        fd = open(pidfile, "w")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        return fd
    except Exception as exc:  # noqa: BLE001
        log.error("daemon.single_instance_lock_held", pidfile=pidfile, error=str(exc))
        return None


def _et_now(now: datetime):
    from zoneinfo import ZoneInfo  # lazy

    return now.astimezone(ZoneInfo("America/New_York"))


def _due_full_slots(now: datetime, full_times: list[tuple[int, int]], state: DaemonState):
    """Return the list of configured full-cycle slots due at *now* (ET), unfired.

    C7a missed-slot rule: on startup, if a slot earlier today has already passed
    and no full cycle ran for it, it is returned ONCE here (not every missed
    slot fired repeatedly — each (date, slot) is recorded in fired_full_slots).
    """
    et = _et_now(now)
    today = et.date()
    due: list[tuple[int, int]] = []
    for (h, m) in full_times:
        key = (today, (h, m))
        if key in state.fired_full_slots:
            continue
        slot_minutes = h * 60 + m
        now_minutes = et.hour * 60 + et.minute
        if now_minutes >= slot_minutes:
            due.append((h, m))
    return due


# Only these TRANSIENT/operational breakers self-heal. RISK & defense breakers
# (daily_loss, per_position_intraday, confidence_distribution_shift,
# a3_volume_anomaly) are deliberate stops — they latch and REQUIRE human review;
# the daemon never auto-resets them.
_OPERATIONAL_BREAKERS: frozenset[str] = frozenset(
    {"broker_non_200", "mirofish_3x_consecutive_fail"}
)


def _auto_recover_if_paused(engine: Any, now: datetime, state: DaemonState) -> None:
    """Self-heal a *transient operational* pause so the daemon runs human-free.

    Strict guard rails so this does NOT defeat the safety framework:
      * The kill switch is authoritative — if halted, never auto-resume.
      * If ANY risk/defense breaker is latched (daily_loss, per_position, …),
        DO NOT recover — page once and stay paused for human review.
      * Only operational breakers (broker_non_200 / mirofish failures) are
        reset, and only up to ``_MAX_CONSECUTIVE_RECOVERIES`` consecutive times
        (counter resets after a clean iteration). Past the cap: page + stay paused.
    """
    if not getattr(engine, "paused", False):
        return
    # Respect the kill switch — never auto-resume past a human halt.
    from arbiter.safety.kill_switch import KillSwitch  # noqa: PLC0415
    try:
        if KillSwitch(engine.config).is_halted(as_of=now):
            return
    except Exception:  # noqa: BLE001 — kill-switch unknown → fail closed, stay paused
        return

    try:
        latched = [
            n for (n,) in engine.conn.execute(
                "SELECT breaker_name FROM breaker_state WHERE latched=1"
            ).fetchall()
        ]
    except Exception:  # noqa: BLE001 — cannot inspect breakers → fail closed
        return

    # NEVER auto-recover past a risk/defense breaker — that is a deliberate stop.
    risk_latched = [n for n in latched if n not in _OPERATIONAL_BREAKERS]
    if risk_latched:
        if not state.recovery_capped_alerted:
            state.recovery_capped_alerted = True
            log.critical(
                "daemon.risk_breaker_latched_no_autorecover",
                breakers=risk_latched, as_of=now.isoformat(),
            )
            try:
                engine._fire_critical_alert(
                    message=(
                        f"RISK breaker(s) latched {risk_latched} — engine stays paused "
                        "for manual review (NOT auto-recovered)."
                    ),
                    ctx={"as_of": now.isoformat(), "breakers": risk_latched},
                    as_of=now,
                )
            except Exception:  # noqa: BLE001
                pass
        return

    if state.consecutive_recoveries >= _MAX_CONSECUTIVE_RECOVERIES:
        if not state.recovery_capped_alerted:
            state.recovery_capped_alerted = True
            log.critical("daemon.auto_recovery_capped", as_of=now.isoformat())
            try:
                engine._fire_critical_alert(
                    message=(
                        f"Daemon auto-recovery cap ({_MAX_CONSECUTIVE_RECOVERIES}) reached — "
                        "engine stays paused, manual review needed."
                    ),
                    ctx={"as_of": now.isoformat()},
                    as_of=now,
                )
            except Exception:  # noqa: BLE001
                pass
        return

    # Only operational breakers are latched here — reset them and resume.
    from arbiter.safety.breakers import CircuitBreaker  # noqa: PLC0415
    try:
        cb = CircuitBreaker()
        for name in latched:
            cb.reset(name, engine.conn)
        engine.conn.commit()
        engine.resume()
        state.consecutive_recoveries += 1
        log.warning(
            "daemon.auto_recovered",
            attempt=state.consecutive_recoveries,
            breakers_reset=latched,
            as_of=now.isoformat(),
        )
        try:
            # Notify the operator WITHOUT re-pausing.  ``_fire_critical_alert``
            # acts on the ``AutoPauseSentinel`` a critical alert returns and would
            # IMMEDIATELY re-pause the engine we just resumed (a self-defeating
            # loop that burns the recovery budget and leaves the daemon stuck
            # paused).  Call alerting directly and DISCARD the sentinel: the
            # webhook page still fires (critical tier posts regardless), but the
            # resume stands.
            engine.alerting.alert(
                "critical",
                (
                    f"Daemon auto-recovered from a transient pause (attempt "
                    f"{state.consecutive_recoveries}/{_MAX_CONSECUTIVE_RECOVERIES}) — resuming."
                ),
                {"as_of": now.isoformat()},
                as_of=now,
            )
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001 — recovery best-effort; never kill the loop
        log.error("daemon.auto_recover_failed", error=str(exc), as_of=now.isoformat())


def run_daemon(
    engine: Any,
    *,
    ingest_fn: Callable[[], Any] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    stop_event: threading.Event | None = None,
    fast_interval_s: float | None = None,
    full_times: list[tuple[int, int]] | None = None,
    heartbeat_path: str | None = None,
    max_iterations: int | None = None,
    install_signals: bool = True,
) -> DaemonState:
    """Run the resilient market-hours daemon loop.

    Parameters are injected for OFFLINE testability.  Returns the final
    ``DaemonState`` (useful for test assertions).

    ``max_iterations`` bounds the loop as a belt-and-suspenders test guard.
    """
    config = engine.config
    clock = engine.clock
    calendar = engine.market_calendar

    if sleep_fn is None:
        sleep_fn = _default_sleep
    if stop_event is None:
        stop_event = threading.Event()
    if fast_interval_s is None:
        fast_interval_s = float(getattr(config, "fast_interval_s", 180))
    if full_times is None:
        full_times = _parse_full_times(getattr(config, "full_cycle_times_et", "09:45,15:30"))
    if heartbeat_path is None:
        heartbeat_path = getattr(config, "daemon_heartbeat_path", "data/arbiter-daemon.heartbeat")

    if install_signals:
        _install_signal_handlers(stop_event)

    state = DaemonState()
    iters = 0

    while not stop_event.is_set():
        if max_iterations is not None and iters >= max_iterations:
            break
        iters += 1
        now: datetime = clock.now()
        try:
            # Autonomy: self-heal a transient pause before doing any work, so a
            # single bad order / breaker trip cannot halt the daemon forever.
            # (The kill switch is exempt — a human halt is always respected.)
            _auto_recover_if_paused(engine, now, state)

            session = calendar.session(now)

            if session.is_open:
                # FULL cycle at configured ET slots (ingest + entries + reversal).
                for slot in _due_full_slots(now, full_times, state):
                    _run_full_cycle(engine, ingest_fn, now, state)
                    state.fired_full_slots.add((_et_now(now).date(), slot))

                # FAST iteration every interval (reconcile + stop/horizon checks).
                engine.run_fast_iteration(now)

                # A clean (non-paused) iteration resets the auto-recovery budget.
                if not getattr(engine, "paused", False):
                    state.consecutive_recoveries = 0
                    state.recovery_capped_alerted = False

                _heartbeat(heartbeat_path, _hb(now, session, engine, state, "fast"))
                state.last_session_open = True
                state.backoff_s = 0.0
                sleep_fn(fast_interval_s)
            else:
                # Open → closed transition (C6): final reconcile + outcome sweep,
                # ONCE, owned by the daemon (the 18:30 one-shot is a down-fallback).
                if state.last_session_open:
                    _run_post_close_sweep(engine, now)
                state.last_session_open = False
                _heartbeat(heartbeat_path, _hb(now, session, engine, state, "closed"))
                state.backoff_s = 0.0
                sleep_fn(_until_next_open_capped(now, session))
        except Exception as exc:  # noqa: BLE001  (broad: one bad iteration must not kill the loop)
            log.error("daemon.iteration_failed", error=str(exc), as_of=now.isoformat())
            state.backoff_s = min(
                _BACKOFF_CAP_S,
                (state.backoff_s * 2.0) if state.backoff_s else 30.0,
            )
            sleep_fn(state.backoff_s)

    # Graceful shutdown: a final reconcile to catch any late fills.
    try:
        if isinstance_adapter(engine):
            engine._reconcile_pending_orders(clock.now())
    except Exception as exc:  # noqa: BLE001
        log.warning("daemon.shutdown_reconcile_failed", error=str(exc))
    log.info("daemon.stopped", iterations=iters)
    return state


def isinstance_adapter(engine: Any) -> bool:
    from arbiter.execution.alpaca_adapter import AlpacaAdapter  # noqa: PLC0415

    return isinstance(engine.executor, AlpacaAdapter)


def _run_full_cycle(engine: Any, ingest_fn: Callable[[], Any] | None, now: datetime, state: DaemonState) -> None:
    """Run ingest (~daily, idempotent) then a full decision cycle."""
    et_date = _et_now(now).date()
    if ingest_fn is not None and state.last_ingest_date != et_date:
        try:
            ingest_fn()
            state.last_ingest_date = et_date
        except Exception as exc:  # noqa: BLE001
            log.warning("daemon.ingest_failed", error=str(exc))
    engine.run_cycle(as_of=now)


def _run_post_close_sweep(engine: Any, now: datetime) -> None:
    """C6: post-close full reconcile + outcome sweep at the open→closed edge."""
    log.info("daemon.session_closed", as_of=now.isoformat())
    try:
        if isinstance_adapter(engine):
            engine._reconcile_pending_orders(now)
    except Exception as exc:  # noqa: BLE001
        log.warning("daemon.post_close_reconcile_failed", error=str(exc))
    # A full run_cycle runs the outcome sweep; but to avoid placing entries
    # post-close we run just the exit-monitor closeout retries + sweep.
    try:
        from arbiter.orchestrator import outcome_runner  # noqa: PLC0415
        from arbiter.contract.seams import Idea  # noqa: PLC0415

        def _advisor_id_for(idea: Idea) -> str:
            return "A1.insider" if idea.horizon_days >= 180 else "A1.congress"

        outcome_runner.run_outcome_sweep(
            engine.conn, pit=engine.pit, clock=engine.clock,
            advisor_id_for=_advisor_id_for, advisor_confidence_for=None,
            audit_path=engine.config.audit_path,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("daemon.post_close_sweep_failed", error=str(exc))


def _until_next_open_capped(now: datetime, session: Any) -> float:
    """Seconds to sleep while closed — capped at _MAX_SLEEP_S (Decision 5)."""
    next_open = getattr(session, "next_open", None)
    if next_open is None:
        return _MAX_SLEEP_S
    delta = (next_open - now).total_seconds()
    if delta <= 0:
        return 1.0
    return min(_MAX_SLEEP_S, delta)


def _hb(now: datetime, session: Any, engine: Any, state: DaemonState, kind: str) -> dict:
    try:
        open_positions = len(engine.executor.get_positions())
    except Exception:  # noqa: BLE001
        open_positions = None
    return {
        "now": now.isoformat(),
        "is_open": getattr(session, "is_open", None),
        "next_open": getattr(session, "next_open", None).isoformat() if getattr(session, "next_open", None) else None,
        "next_close": getattr(session, "next_close", None).isoformat() if getattr(session, "next_close", None) else None,
        "iteration_kind": kind,
        "open_positions": open_positions,
        "paused": getattr(engine, "paused", None),
        "backoff_s": state.backoff_s,
    }


def _install_signal_handlers(stop_event: threading.Event) -> None:
    import signal  # lazy

    def _handler(signum, frame):  # noqa: ANN001
        log.info("daemon.signal_received", signum=signum)
        stop_event.set()

    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
    except (ValueError, OSError) as exc:
        # Not on the main thread (e.g. under pytest) — skip; tests inject stop_event.
        log.info("daemon.signal_install_skipped", error=str(exc))


def main(config: Any = None) -> DaemonState:
    """Real entrypoint for ``arbiter daemon`` (launchd)."""
    from arbiter.engine import build_engine  # noqa: PLC0415
    from arbiter.ingest.runner import run_ingest  # noqa: PLC0415

    engine = build_engine(config)
    cfg = engine.config

    pidfile = os.path.join(os.path.dirname(cfg.db_path) or ".", "arbiter-daemon.pid")
    lock_fd = _acquire_single_instance_lock(pidfile)
    if lock_fd is None:
        log.error("daemon.exiting_lock_held")
        return DaemonState()

    def _ingest_fn() -> None:
        run_ingest(cfg, conn=engine.conn, clock=lambda: engine.clock.now().isoformat())

    try:
        return run_daemon(engine, ingest_fn=_ingest_fn)
    finally:
        try:
            lock_fd.close()
        except Exception:  # noqa: BLE001
            pass
