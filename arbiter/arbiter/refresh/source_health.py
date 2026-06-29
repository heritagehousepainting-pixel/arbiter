"""Deterministic data-source staleness checks (fail-closed)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Callable

import structlog

from arbiter.refresh.types import HealthResult, StaleFlag, StaleSource

log = structlog.get_logger(__name__)

# Per-source max ingest age before we call it stale (calendar days).
_MAX_AGE_DAYS: dict[str, int] = {
    "form4": 14, "form13d": 30, "form13f": 100, "congress": 14,
}


def default_ingest_age_fn(conn: sqlite3.Connection, source: str) -> int | None:
    """Days since the newest row for `source` in the filings table, or None."""
    try:
        row = conn.execute(
            "SELECT MAX(ingested_at) FROM filings WHERE source = ?", (source,)
        ).fetchone()
        if not row or not row[0]:
            return None
        newest = datetime.fromisoformat(row[0])
        return (datetime.now(tz=newest.tzinfo) - newest).days
    except Exception:  # table/column shape differences -> unknown, never crash
        return None


def scan_source_health(conn: Any, as_of: datetime, *,
                       ingest_age_fn: Callable[[Any, str], int | None] | None = None
                       ) -> HealthResult:
    age_fn = ingest_age_fn or default_ingest_age_fn
    sources: list[StaleSource] = []
    for src, max_age in _MAX_AGE_DAYS.items():
        try:
            age = age_fn(conn, src)
        except Exception as exc:
            log.warning("refresh.health.age_failed", source=src, error=str(exc))
            age = None
        if age is None:
            sources.append(StaleSource(source=src, reason="ingest age unknown",
                                       confirmed=False))
        elif age > max_age:
            sources.append(StaleSource(source=src,
                                       reason=f"last ingest {age}d ago (>{max_age}d)",
                                       confirmed=True))
    return HealthResult(sources=sources)


def merge_flags(health: HealthResult, flags: list[StaleFlag]) -> HealthResult:
    seen = {s.source for s in health.sources}
    extra = [StaleSource(source=f.source, reason=f"news: {f.reason}", confirmed=True)
             for f in flags if f.source not in seen]
    return HealthResult(sources=[*health.sources, *extra])
