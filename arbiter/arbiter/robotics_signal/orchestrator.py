"""Orchestrate one robotics-signal cycle: scan → phone digest (fail-closed)."""
from __future__ import annotations

from typing import Any, Callable, TypeVar

import structlog

from arbiter.robotics_signal.digest import push_digest
from arbiter.robotics_signal.scan import scan_robotics
from arbiter.robotics_signal.types import RoboticsReport, RoboticsScanResult

log = structlog.get_logger(__name__)
T = TypeVar("T")


def _safe(label: str, fn: Callable[[], T], default: T) -> T:
    """Run ``fn`` fail-closed — a bad step can never crash the cycle."""
    try:
        return fn()
    except Exception as exc:
        log.warning("robotics_signal." + label + ".failed", error=str(exc))
        return default


def run_robotics_scan(engine: Any, *, llm: Any = None, alerting: Any = None) -> RoboticsReport:
    """Run one robotics-signal cycle. Every step is ``_safe``-wrapped so a
    failed scan or push is logged, never raised.

    Part 1 (scanner core): scan → phone digest. Persistence (#3c) and the
    probationary advisor (#3d) hang off this same seam later.
    """
    as_of = engine.clock.now()
    scan = _safe(
        "scan",
        lambda: scan_robotics(as_of, engine.config, llm=llm),
        RoboticsScanResult(available=False, note="unavailable"),
    )
    report = RoboticsReport(as_of=as_of, scan=scan)
    alert = alerting if alerting is not None else getattr(engine, "alerting", None)
    if alert is not None:
        _safe("push", lambda: push_digest(report, alerting=alert), None)
    return report
