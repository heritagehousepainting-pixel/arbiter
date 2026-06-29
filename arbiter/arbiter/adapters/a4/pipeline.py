"""A4.macro advisor — turns persisted macro findings into probationary opinions.

Mirrors A3.news: registered probationary at import (EQUAL_FLOOR until graduated),
SHORT horizon (7d), fail-closed, network-/look-ahead-gated under BacktestClock.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime

import structlog

from arbiter.contract.opinion import Opinion, default_registry, validate_opinion
from arbiter.data.clock import BacktestClock, Clock
from arbiter.db.helpers import generate_ulid
from arbiter.refresh.findings_store import read_active_findings
from arbiter.refresh.types import MacroFinding, Severity
from arbiter.types import ConfidenceSource

log = structlog.get_logger(__name__)

ADVISOR_ID = "A4.macro"
_HORIZON_DAYS = 7  # SHORT bucket
_SEV_STANCE = {Severity.HIGH: 0.5, Severity.MEDIUM: 0.3, Severity.LOW: 0.15}
_SEV_CONF = {Severity.HIGH: 0.45, Severity.MEDIUM: 0.30, Severity.LOW: 0.15}

default_registry.register(ADVISOR_ID)


def _stance(f: MacroFinding) -> float:
    # Macro risk reads bearish on the broad market by default; magnitude by severity.
    return -_SEV_STANCE.get(f.severity, 0.15)


def gather_a4_opinions(conn: sqlite3.Connection, clock: Clock,
                       config: object) -> list[Opinion]:
    try:
        if isinstance(clock, BacktestClock):
            return []
        as_of: datetime = clock.now()
        advisor_id = getattr(config, "a4_advisor_id", ADVISOR_ID)
        min_conf = float(getattr(config, "a4_min_confidence", 0.0))
        min_stance = float(getattr(config, "a4_min_stance", 0.25))
        run_group = generate_ulid()
        out: list[Opinion] = []
        seen: set[str] = set()
        for f in read_active_findings(conn, as_of):
            conf = _SEV_CONF.get(f.severity, 0.15)
            if conf < min_conf:
                continue
            stance = _stance(f)
            if abs(stance) < min_stance:
                continue
            for ticker in f.affected_tickers:
                key = f"{ticker}:{f.summary}"
                if key in seen:
                    continue
                seen.add(key)
                fp = hashlib.sha256(key.encode()).hexdigest()
                op = Opinion(
                    advisor_id=advisor_id, ticker=ticker, stance_score=stance,
                    confidence=conf, confidence_source=ConfidenceSource.MODELED,
                    horizon_days=_HORIZON_DAYS, as_of=as_of,
                    rationale=f"A4.macro {ticker}: {f.summary}"[:500],
                    source_fingerprint=fp, run_group_id=run_group)
                validate_opinion(op)
                out.append(op)
        log.info("a4.macro.complete", opinion_count=len(out))
        return out
    except Exception as exc:  # fail-closed
        log.warning("a4.macro.unexpected", error=str(exc))
        return []
