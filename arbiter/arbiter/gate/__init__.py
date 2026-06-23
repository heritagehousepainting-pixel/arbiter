"""Paper→live gate — Lane 12c.

Stays CLOSED in the MVP; criteria machinery is built and tested now so that
opening the gate requires only a manual approval call, not a code change.

Public surface::

    from arbiter.gate.criteria import evaluate, GateResult, CRITERIA_HASH
    from arbiter.gate.approval import record_approval, current_approval, is_approved
    from arbiter.gate.ramp import advance_stage, current_stage, STAGE_ORDER

Design constraints (INTERFACES.md §11):
    - No ``datetime.now()`` — all ``as_of`` / clock values passed in by caller.
    - Fail-closed: no approval / expired approval → live disabled.
    - Criteria hash is immutable mid-run: changing it is detected and rejected.
    - ``from __future__ import annotations`` everywhere (py3.11+).
"""
from __future__ import annotations

from arbiter.gate.criteria import evaluate, GateResult, CRITERIA_HASH
from arbiter.gate.approval import record_approval, current_approval, is_approved
from arbiter.gate.ramp import advance_stage, current_stage, STAGE_ORDER

__all__ = [
    "evaluate",
    "GateResult",
    "CRITERIA_HASH",
    "record_approval",
    "current_approval",
    "is_approved",
    "advance_stage",
    "current_stage",
    "STAGE_ORDER",
]
