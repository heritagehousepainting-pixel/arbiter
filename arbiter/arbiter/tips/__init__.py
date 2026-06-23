"""Tips layer — Lane 8 (A3 anti-manipulation defenses).

SHADOW / DORMANT in MVP
-----------------------
Tips never produce a live Opinion in Phase-6 MVP.  They are wired in
forward-test-only mode: every tip is recorded and scored, but the
``diversity.corroborate()`` gate enforces abstain (None) unless ≥ 2
independent sources agree.  Even when corroborated, the tip layer emits
nothing into the live fusion pool until the A3 advisor is promoted out of
shadow in a later phase.

This module re-exports the public surface so callers can do::

    from arbiter.tips import UnverifiedTip, TipSource

Lane wiring (Wave-C)
--------------------
``VolumeAnomalyGate`` lives in ``arbiter.defenses.volume_anomaly`` and is
consumed by:
  - Lane 4 (``arbiter.safety.breakers.check_a3_volume_anomaly``) — trips the
    ``a3_volume_anomaly`` latching circuit-breaker.
  - Lane 13 (orchestrator sweep) — gates idea creation on held names.

See ``arbiter/defenses/volume_anomaly.py`` for the wiring interface.
"""
from __future__ import annotations

from arbiter.tips.source import TipSource, UnverifiedTip

__all__ = ["TipSource", "UnverifiedTip"]
