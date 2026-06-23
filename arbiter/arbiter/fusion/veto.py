"""Hard-veto layer for the fusion engine.

An advisor can "hard-veto" a bucket by emitting an opinion with
``stance_score == -1.0`` AND ``confidence == 1.0`` (full-conviction short),
or more generally by being registered in the veto registry.

Design decision (Phase 1):
- Hard veto is signalled by an opinion with the special sentinel value:
  ``stance_score`` exactly ±1.0 (full conviction) AND ``confidence`` == 1.0.
  This avoids adding a new field to the frozen Opinion dataclass.
- Alternatively, the engine may receive an explicit veto_ids list; this
  module supports both paths.

Veto effect:
- All opinions in the SAME bucket are zeroed out.
- The FusionOutput for that bucket has conviction=0.0, dispersion=0.0,
  effective_n=0.0, n_opinions=0, and vetoes=[<advisor_id>, ...].

Lone-veto rule: a single advisor vetoing produces a full zero; this is
intentional (hard-veto = hard stop, not soft downweighting).
"""
from __future__ import annotations

from arbiter.contract.opinion import Opinion


def detect_vetoes(opinions: list[Opinion]) -> list[str]:
    """Return advisor_ids that issued a hard veto in this bucket.

    A hard veto is defined as an opinion where:
        ``confidence == 1.0`` AND ``abs(stance_score) == 1.0``

    This is the Phase-1 sentinel convention (no new fields needed on Opinion).

    Parameters
    ----------
    opinions:
        All opinions for a bucket (after None-filtering, before dedup).

    Returns
    -------
    list[str]
        Sorted list of advisor_ids that hard-vetoed.  Empty if none.
    """
    veto_ids: list[str] = []
    for op in opinions:
        if op.confidence == 1.0 and abs(op.stance_score) == 1.0:
            veto_ids.append(op.advisor_id)
    return sorted(set(veto_ids))
