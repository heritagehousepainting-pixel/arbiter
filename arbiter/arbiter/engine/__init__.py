"""Composition root — Wave C (now a package after the H1 refactor).

The ``Engine`` god-object was split into cohesive helper modules with ZERO
behaviour change:

  - ``arbiter.engine.advisors``     — A1/A2 advisor builders + market-hours heuristic
  - ``arbiter.engine.reconcile``    — pending-order reconciliation cluster
  - ``arbiter.engine.safety_ops``   — safety gate / breakers / risk-seeding / exits
  - ``arbiter.engine.learning``     — learning-input assembly + opinion persistence
  - ``arbiter.engine._engine``      — the ``Engine`` dataclass + ``build_engine``

The public surface is preserved byte-for-byte: ``from arbiter.engine import
Engine, build_engine`` and ``arbiter.engine.CycleResult`` keep working, and the
test-monkeypatched ``arbiter.engine.build_executor`` is re-exported here too.
"""
from __future__ import annotations

from arbiter.engine._engine import (
    AlpacaAdapter,
    CycleResult,
    Engine,
    SimExecutor,
    _build_a1_activist_fn,
    _build_a1_congress_fn,
    _build_a1_fund_fn,
    _build_a1_insider_fn,
    _build_a2_mirofish_fn,
    _us_market_open,
    build_engine,
    build_executor,
)

__all__ = [
    "Engine",
    "build_engine",
    "CycleResult",
    "build_executor",
    "AlpacaAdapter",
    "SimExecutor",
    "_us_market_open",
    "_build_a1_insider_fn",
    "_build_a1_congress_fn",
    "_build_a1_activist_fn",
    "_build_a1_fund_fn",
    "_build_a2_mirofish_fn",
]
