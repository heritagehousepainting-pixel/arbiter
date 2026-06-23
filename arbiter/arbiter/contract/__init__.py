"""Advisor contract — Lane 9 core.

Exports the Opinion dataclass, validate_opinion, AdvisorRegistry,
and all seam dataclasses from a single place so downstream lanes
have one import root.

    from arbiter.contract import Opinion, validate_opinion, AdvisorRegistry
    from arbiter.contract import FusionOutput, WeightBundle, EqualWeightBundle
    from arbiter.contract import ResolvedOutcome, Idea, TradingDecision, PaperOrder
"""
from __future__ import annotations

from arbiter.contract.opinion import AdvisorRegistry, Opinion, validate_opinion
from arbiter.contract.seams import (
    AdvisorWeight,
    EqualWeightBundle,
    FusionOutput,
    Idea,
    PaperOrder,
    ResolvedOutcome,
    TradingDecision,
    WeightBundle,
)

__all__ = [
    "Opinion",
    "validate_opinion",
    "AdvisorRegistry",
    "FusionOutput",
    "AdvisorWeight",
    "WeightBundle",
    "EqualWeightBundle",
    "ResolvedOutcome",
    "Idea",
    "TradingDecision",
    "PaperOrder",
]
