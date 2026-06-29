"""Frozen result types for the Monday Refresh scans."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class PositionFinding:
    ticker: str
    headlines: list[str]
    sentiment: float          # [-1, 1]; 0.0 when unavailable
    severity: Severity
    available: bool           # False => Finnhub unavailable for this ticker


@dataclass(frozen=True)
class MacroFinding:
    summary: str
    severity: Severity
    affected_tickers: list[str]
    sources: list[str]


@dataclass(frozen=True)
class StaleFlag:
    source: str               # e.g. "activist_filers"
    reason: str
    sources: list[str]


@dataclass(frozen=True)
class MacroResult:
    findings: list[MacroFinding]
    stale_flags: list[StaleFlag]
    available: bool           # False => Claude skipped/unavailable
    note: str                 # human-readable status when not available


@dataclass(frozen=True)
class StaleSource:
    source: str
    reason: str
    confirmed: bool           # deterministic confirmation (or matched news flag)


@dataclass(frozen=True)
class HealthResult:
    sources: list[StaleSource]

    def confirmed_stale(self) -> list[StaleSource]:
        return [s for s in self.sources if s.confirmed]


@dataclass(frozen=True)
class RefreshReport:
    as_of: datetime
    positions: list[PositionFinding]
    macro: MacroResult
    health: HealthResult
    fed_tickers: list[str] = field(default_factory=list)
    reingested: list[str] = field(default_factory=list)
