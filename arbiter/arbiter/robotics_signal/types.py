"""Value types for the robotics early-insight signal (pure data)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

#: The stack layers a development can be tagged with (mirrors robotics_universe).
CATEGORIES: frozenset[str] = frozenset(
    {"compute", "brain", "components", "integrator", "deployment", "other"}
)


@dataclass(frozen=True)
class RoboticsDevelopment:
    """One notable robotics-sector development surfaced by the scan."""
    headline: str
    summary: str
    category: str                                   # one of CATEGORIES
    symbols: list[str] = field(default_factory=list)  # universe symbols involved
    trigger_hit: bool = False                       # looks like a watch-trigger fired
    trigger_name: str | None = None                 # universe symbol whose trigger hit
    sources: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RoboticsScanResult:
    """Result of one scan cycle. Fail-closed: ``available=False`` on any error."""
    developments: list[RoboticsDevelopment] = field(default_factory=list)
    available: bool = False
    note: str = ""

    @property
    def trigger_hits(self) -> list[RoboticsDevelopment]:
        return [d for d in self.developments if d.trigger_hit]


@dataclass(frozen=True)
class RoboticsReport:
    """The full output of one robotics-signal run."""
    as_of: datetime
    scan: RoboticsScanResult
