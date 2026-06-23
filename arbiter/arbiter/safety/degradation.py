"""Degradation ladder for the safety layer (Lane L4).

Maps DegradationLevel values (0–4) to their trading implications and
provides the ``evaluate_level`` helper the gate uses after combining all
signals.

Levels (from DegradationLevel in arbiter.types):
    0  NORMAL     — full trading, multiplier=1.0
    1  CAUTION    — full trading, multiplier=1.0 (log warning only)
    2  DEGRADED   — trading at reduced size (multiplier=0.25 from quorum)
    3  RESTRICTED — trading blocked (size=0), allowed=False
    4  HALTED     — trading blocked (size=0), allowed=False

INTERFACES.md §8 / spec §3.9:
    "Levels 3–4 supersede and force size 0 / halt."

Public API
----------
LevelPolicy  — frozen dataclass describing what a level implies.
LEVEL_POLICIES — mapping DegradationLevel → LevelPolicy
level_supersedes_trading(level) → bool
"""
from __future__ import annotations

from dataclasses import dataclass

from arbiter.types import DegradationLevel


@dataclass(frozen=True)
class LevelPolicy:
    """Policy implied by a single DegradationLevel.

    Attributes
    ----------
    blocks_trading:
        If True, ``allowed`` must be False regardless of other signals.
    forced_size_multiplier:
        When not None, this value overrides the quorum multiplier.
        Levels 3 and 4 force 0.0; others leave it to quorum.
    description:
        Short human-readable summary.
    """

    blocks_trading: bool
    forced_size_multiplier: float | None
    description: str


# INTERFACES.md §8 table: levels 3–4 supersede and force size 0.
LEVEL_POLICIES: dict[DegradationLevel, LevelPolicy] = {
    DegradationLevel.NORMAL: LevelPolicy(
        blocks_trading=False,
        forced_size_multiplier=None,
        description="normal operations",
    ),
    DegradationLevel.CAUTION: LevelPolicy(
        blocks_trading=False,
        forced_size_multiplier=None,
        description="caution — elevated monitoring, no size reduction",
    ),
    DegradationLevel.DEGRADED: LevelPolicy(
        blocks_trading=False,
        forced_size_multiplier=None,
        description="degraded — reduced quorum, size reduced to 25%",
    ),
    DegradationLevel.RESTRICTED: LevelPolicy(
        blocks_trading=True,
        forced_size_multiplier=0.0,
        description="restricted — trading blocked",
    ),
    DegradationLevel.HALTED: LevelPolicy(
        blocks_trading=True,
        forced_size_multiplier=0.0,
        description="halted — all trading suspended",
    ),
}


def level_supersedes_trading(level: DegradationLevel) -> bool:
    """Return True if this level forces trading to stop regardless of quorum.

    Levels RESTRICTED (3) and HALTED (4) supersede quorum and breaker
    decisions by locking size to 0 and setting allowed=False.

    Parameters
    ----------
    level:
        The combined degradation level to evaluate.
    """
    return LEVEL_POLICIES[level].blocks_trading


def effective_multiplier(
    level: DegradationLevel,
    quorum_multiplier: float,
) -> float:
    """Return the final size multiplier given degradation level + quorum result.

    If the level has a ``forced_size_multiplier``, that wins unconditionally.
    Otherwise the quorum multiplier is used.

    Parameters
    ----------
    level:
        The combined degradation level.
    quorum_multiplier:
        The multiplier proposed by the quorum assessment (0.0, 0.25, or 1.0).
    """
    policy = LEVEL_POLICIES[level]
    if policy.forced_size_multiplier is not None:
        return policy.forced_size_multiplier
    return quorum_multiplier


def highest_level(*levels: DegradationLevel) -> DegradationLevel:
    """Return the most-severe DegradationLevel from the given set.

    Severity order: NORMAL < CAUTION < DEGRADED < RESTRICTED < HALTED.
    Since DegradationLevel is an int-enum (0–4), max() works directly.

    Parameters
    ----------
    *levels:
        One or more DegradationLevel values to compare.

    Raises
    ------
    ValueError
        If no levels are provided.
    """
    if not levels:
        raise ValueError("highest_level requires at least one level")
    return max(levels, key=lambda lv: lv.value)
