"""A5.robotics advisor — turns robotics trigger-hits into probationary opinions.

Mirrors A4.macro exactly: registered probationary at import, SHORT horizon (7d),
fail-closed, look-ahead-gated under ``BacktestClock``.  Additional safety on top
of A4:

- **Kill-switch:** returns ``[]`` unless ``config.robotics_advisor_enabled`` — the
  advisor is DORMANT by default until the creator explicitly flips it.
- **Tradeable-only:** emits an Opinion for a trigger's ``trigger_name`` symbol ONLY
  when that symbol is a US-listed / priceable name in the canonical robotics
  universe.  A trigger firing on a private/foreign chokepoint (priceable=False) or
  on a symbol absent from the universe never becomes a trade.
- **Weight-capped:** the emitted confidence is bounded by ``a5_weight_cap`` (small)
  so an unproven robotics nudge can never speak as loudly as a graduated advisor.
- **Recency-windowed:** only trigger-hits inside a 7-day window are read, so a
  stale signal stops nudging.

A fired trigger is read as BULLISH on the robotics thesis (positive stance).
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime

import structlog

from arbiter.contract.opinion import Opinion, default_registry, validate_opinion
from arbiter.data.clock import BacktestClock, Clock
from arbiter.data.robotics_universe import robotics_universe
from arbiter.db.helpers import generate_ulid
from arbiter.robotics_signal.store import read_active_trigger_hits
from arbiter.types import ConfidenceSource

log = structlog.get_logger(__name__)

ADVISOR_ID = "A5.robotics"
_HORIZON_DAYS = 7  # SHORT bucket, mirroring A4.macro
_WINDOW_DAYS = 7  # findings expire after a week
_STANCE = 0.5  # a fired trigger reads bullish on the robotics thesis
_CONF = 0.35  # base confidence, then bounded by a5_weight_cap

default_registry.register(ADVISOR_ID)


def _priceable_symbols() -> set[str]:
    """US-listed / priceable universe symbols — the only tradeable ones."""
    return {r["symbol"] for r in robotics_universe() if r.get("priceable")}


def gather_a5_opinions(conn: sqlite3.Connection, clock: Clock,
                       config: object) -> list[Opinion]:
    try:
        if not getattr(config, "robotics_advisor_enabled", False):
            return []  # kill-switch: dormant by default
        if isinstance(clock, BacktestClock):
            return []  # look-ahead-safe: live-only
        as_of: datetime = clock.now()
        advisor_id = getattr(config, "a5_advisor_id", ADVISOR_ID)
        min_conf = float(getattr(config, "a5_min_confidence", 0.0))
        min_stance = float(getattr(config, "a5_min_stance", 0.25))
        weight_cap = float(getattr(config, "a5_weight_cap", 0.25))
        conf = min(_CONF, weight_cap)
        stance = _STANCE
        priceable = _priceable_symbols()
        run_group = generate_ulid()
        out: list[Opinion] = []
        seen: set[str] = set()
        if conf < min_conf or abs(stance) < min_stance:
            return []  # significance gate — same threshold for every trigger-hit
        for hit in read_active_trigger_hits(conn, as_of, window_days=_WINDOW_DAYS):
            symbol = hit.get("trigger_name")
            if not symbol or symbol not in priceable:
                continue  # tradeable-only: never opine on a non-priceable name
            headline = hit.get("headline", "")
            key = f"{symbol}:{headline}"
            if key in seen:
                continue
            seen.add(key)
            fp = hashlib.sha256(key.encode()).hexdigest()
            op = Opinion(
                advisor_id=advisor_id, ticker=symbol, stance_score=stance,
                confidence=conf, confidence_source=ConfidenceSource.MODELED,
                horizon_days=_HORIZON_DAYS, as_of=as_of,
                rationale=f"A5.robotics {symbol}: {headline}"[:500],
                source_fingerprint=fp, run_group_id=run_group)
            validate_opinion(op)
            out.append(op)
        log.info("a5.robotics.complete", opinion_count=len(out))
        return out
    except Exception as exc:  # fail-closed
        log.warning("a5.robotics.unexpected", error=str(exc))
        return []
