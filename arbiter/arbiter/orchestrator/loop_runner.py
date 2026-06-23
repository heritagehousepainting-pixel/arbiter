"""Scheduled-loop runner — Lane 13 entrypoint for cron/launchd.

One invocation = one full pass: ingest → one decision cycle.
This is NOT a daemon.  It is designed to be triggered externally (cron,
launchd) and exits cleanly when done.

Fault isolation
---------------
If ingest raises, the error is logged and recorded in the RunReport, but
the cycle STILL runs — it acts on already-stored filings so stale ingest
data is better than a completely skipped cycle.

Wiring
------
``main(config)`` is the real entrypoint that cron/launchd calls via::

    arbiter run

The CLI integrator wires the ``run`` subcommand to ``main()``.
"""
from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


@dataclass
class RunReport:
    """Summary of a single scheduled run.

    Attributes
    ----------
    as_of:
        The information timestamp used for this run.
    ingest_ok:
        True if ingest completed without raising.  False if ingest raised;
        the cycle was still attempted.
    ingest_error:
        The exception caught from ingest, or None if ingest succeeded.
    cycle_result:
        Whatever the cycle callable returned.  None if the cycle itself
        raised (which is propagated, not swallowed).
    """

    as_of: datetime
    ingest_ok: bool
    ingest_error: BaseException | None = field(default=None, repr=False)
    cycle_result: Any = field(default=None)


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------


def run_once(
    *,
    ingest_fn: Callable[[], Any],
    cycle_fn: Callable[[datetime], Any],
    clock: Any,
    run_ingest_first: bool = True,
) -> RunReport:
    """Run ingest then one decision cycle; return a RunReport.

    Parameters
    ----------
    ingest_fn:
        Zero-argument callable that performs data ingestion.  May return
        anything (result is not used).  If it raises, the exception is
        logged and recorded in the report but the cycle still runs.
    cycle_fn:
        Callable that accepts a single tz-aware ``datetime`` (as_of) and
        runs one decision cycle.  Its return value is stored in
        ``RunReport.cycle_result``.
    clock:
        Clock instance (``arbiter.data.clock.Clock`` or compatible).
        ``clock.now()`` is the ONLY source of the current time.
    run_ingest_first:
        If False, skip ingest entirely (useful for re-runs acting on
        already-stored data).  Defaults to True.

    Returns
    -------
    RunReport
        Always returned, even when ingest failed.  Cycle exceptions are
        NOT swallowed — they propagate to the caller.
    """
    as_of: datetime = clock.now()

    ingest_ok: bool = True
    ingest_error: BaseException | None = None

    if run_ingest_first:
        try:
            ingest_fn()
            log.info("loop_runner.ingest.ok")
        except Exception as exc:  # noqa: BLE001
            ingest_ok = False
            ingest_error = exc
            log.warning(
                "loop_runner.ingest.failed — proceeding to cycle on stored data",
                exc_info=exc,
            )

    log.info("loop_runner.cycle.start", as_of=as_of.isoformat())
    cycle_result = cycle_fn(as_of)
    log.info("loop_runner.cycle.done")

    return RunReport(
        as_of=as_of,
        ingest_ok=ingest_ok,
        ingest_error=ingest_error,
        cycle_result=cycle_result,
    )


# ---------------------------------------------------------------------------
# Main entrypoint (real wiring for cron/launchd)
# ---------------------------------------------------------------------------


def main(config: Any = None) -> RunReport:
    """Build real wiring and call ``run_once``.

    This is the function that ``arbiter run`` (via the CLI integrator)
    calls.  It lazily imports the engine and ingest runner so the module
    stays import-safe even when optional dependencies are absent.

    Parameters
    ----------
    config:
        Optional frozen ``Config``.  If None, the engine and ingest runner
        each load their own config from ``config/arbiter.toml`` + env.

    Returns
    -------
    RunReport
    """
    # Lazy imports — keeps module light; avoids circular imports at load time.
    import os  # noqa: PLC0415

    from arbiter.engine import build_engine  # noqa: PLC0415
    from arbiter.ingest.runner import run_ingest  # noqa: PLC0415
    from arbiter.runtime.daemon import _acquire_single_instance_lock  # noqa: PLC0415

    engine = build_engine(config)
    clock = engine.clock
    conn = engine.conn

    # C6: the 18:30 one-shot is a flock-guarded "daemon was down" fallback.  If
    # the daemon already holds the single-instance lock, this run no-ops cleanly
    # so two processes never mutate the same SQLite DB concurrently.
    _pidfile = os.path.join(os.path.dirname(engine.config.db_path) or ".", "arbiter-daemon.pid")
    _lock_fd = _acquire_single_instance_lock(_pidfile)
    if _lock_fd is None:
        log.info("loop_runner.daemon_holds_lock — skipping one-shot (daemon owns the session)")
        return RunReport(as_of=clock.now(), ingest_ok=True, ingest_error=None, cycle_result=None)

    def _ingest_fn() -> None:
        # run_ingest wants clock as a Callable[[], str] (ISO timestamp), not a Clock.
        run_ingest(engine.config, conn=conn, clock=lambda: clock.now().isoformat())

    def _cycle_fn(as_of: datetime) -> Any:
        return engine.run_cycle(as_of=as_of)

    try:
        return run_once(
            ingest_fn=_ingest_fn,
            cycle_fn=_cycle_fn,
            clock=clock,
            run_ingest_first=True,
        )
    finally:
        try:
            _lock_fd.close()  # release the single-instance flock
        except Exception:  # noqa: BLE001
            pass
