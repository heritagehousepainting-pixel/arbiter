"""Deduplication of same-run_group_id opinions within a bucket.

Rule (INTERFACES.md §2, design spec §3.3):
- Opinions sharing the same ``run_group_id`` IN THE SAME HorizonBucket are
  merged into one logical opinion (the stance is averaged, confidence
  is taken as the mean, horizon_days and other metadata from the first).
- Opinions from the same ``run_group_id`` in DIFFERENT buckets remain
  independent (MiroFish SHORT + MEDIUM case).

After dedup the bucket pool has at most one logical opinion per
(advisor_id, run_group_id) pair within that bucket.  If multiple
opinions from the same run_group arrive from DIFFERENT advisors they
are treated independently (each keeps its weight).
"""
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from arbiter.contract.opinion import Opinion

if TYPE_CHECKING:
    from arbiter.types import HorizonBucket


def dedup_bucket(opinions: list[Opinion]) -> list[Opinion]:
    """Merge same-run_group_id opinions within a single bucket.

    All opinions passed in MUST be from the same HorizonBucket.
    The function groups by (advisor_id, run_group_id) and collapses
    each group to a single representative opinion.

    Merge strategy:
    - ``stance_score``: arithmetic mean of the group.
    - ``confidence``: arithmetic mean of the group.
    - ``horizon_days``: mean (rounded to nearest int).
    - All other fields: taken from the *first* opinion in the group
      (advisor_id, ticker, rationale, source_fingerprint, run_group_id,
      confidence_source, as_of).

    Returns
    -------
    list[Opinion]
        Deduplicated opinions (order: first-seen advisor_id, then
        first-seen run_group_id within each advisor).
    """
    if not opinions:
        return []

    # Group by (advisor_id, run_group_id) preserving insertion order.
    groups: dict[tuple[str, str], list[Opinion]] = {}
    for op in opinions:
        key = (op.advisor_id, op.run_group_id)
        if key not in groups:
            groups[key] = []
        groups[key].append(op)

    merged: list[Opinion] = []
    for group_ops in groups.values():
        if len(group_ops) == 1:
            merged.append(group_ops[0])
        else:
            # Merge: average stance, confidence, horizon_days.
            n = len(group_ops)
            avg_stance = sum(op.stance_score for op in group_ops) / n
            avg_confidence = sum(op.confidence for op in group_ops) / n
            avg_horizon = round(sum(op.horizon_days for op in group_ops) / n)

            # Clamp to valid ranges.
            avg_stance = max(-1.0, min(1.0, avg_stance))
            avg_confidence = max(0.0, min(1.0, avg_confidence))

            representative = replace(
                group_ops[0],
                stance_score=avg_stance,
                confidence=avg_confidence,
                horizon_days=avg_horizon,
            )
            merged.append(representative)

    return merged
