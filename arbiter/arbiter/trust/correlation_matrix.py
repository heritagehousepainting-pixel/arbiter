"""Rolling pairwise correlation matrix — Lane 11 / trust sub-module.

Builds a correlation matrix of advisor outcome co-movement for use by the
fusion layer (Lane 10).  Phase-5 active; built now but dormant until
fusion consumes it.

Two sources of correlation signal:
1. Outcome co-movement: sign agreement / disagreement on resolved outcomes
   where both advisors were non-abstain.
2. Event-fingerprint collisions: two advisors sharing the same
   source_fingerprint → their opinions are NOT independent (ρ boosted toward 1.0).

Default prior: ρ = 0.5 when the sample is sparse (< MIN_PAIRS observations).
This implements INTERFACES.md §5: "ρij; default 0.5 prior when sparse".

No datetime.now(); callers supply as_of.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

import numpy as np
from scipy import stats as sp_stats

from arbiter.contract.seams import ResolvedOutcome

DEFAULT_PRIOR: float = 0.5      # Prior ρ when sample is sparse
MIN_PAIRS: int = 10             # Minimum co-observations to trust sample ρ
FINGERPRINT_BOOST: float = 0.9  # ρ assigned for fingerprint collision pairs


@dataclass
class CorrelationMatrix:
    """Rolling pairwise correlation matrix built from outcome co-movement.

    Parameters
    ----------
    outcomes_by_advisor:
        Mapping of advisor_id → list of (ResolvedOutcome, resolved_at datetime).
        Only non-abstain outcomes are used for correlation; abstains are ignored.

    Usage
    -----
    Build:
        cm = CorrelationMatrix.build(outcomes_by_advisor, fingerprints_by_advisor)
    Query:
        rho = cm.get(adv_a, adv_b)  # returns prior if sparse

    Wave-C wiring: ``fingerprints_by_advisor`` (event fingerprints per advisor)
    comes from Lane 13/14 or the opinion store — passed in as param.
    """

    _matrix: dict[tuple[str, str], float] = field(default_factory=dict)
    _advisor_ids: list[str] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        outcomes_by_advisor: dict[str, list[tuple[ResolvedOutcome, datetime]]],
        fingerprints_by_advisor: dict[str, set[str]] | None = None,
        as_of: datetime | None = None,
    ) -> "CorrelationMatrix":
        """Build the correlation matrix from outcome history.

        Parameters
        ----------
        outcomes_by_advisor:
            {advisor_id: [(ResolvedOutcome, resolved_at), ...]}
        fingerprints_by_advisor:
            {advisor_id: {source_fingerprint, ...}}
            When two advisors share ≥1 fingerprint their ρ is set to
            FINGERPRINT_BOOST (0.9) if the sample estimate is lower.
        as_of:
            Not used for computation but stored for audit (optional).

        Returns
        -------
        CorrelationMatrix
        """
        advisor_ids = sorted(outcomes_by_advisor.keys())
        matrix: dict[tuple[str, str], float] = {}

        # Self-correlation is always 1.0
        for aid in advisor_ids:
            matrix[(aid, aid)] = 1.0

        # Pairwise
        for i, adv_a in enumerate(advisor_ids):
            for adv_b in advisor_ids[i + 1:]:
                rho = cls._compute_pair_rho(
                    outcomes_by_advisor[adv_a],
                    outcomes_by_advisor[adv_b],
                )

                # Fingerprint collision override
                if fingerprints_by_advisor:
                    fps_a = fingerprints_by_advisor.get(adv_a, set())
                    fps_b = fingerprints_by_advisor.get(adv_b, set())
                    if fps_a & fps_b:  # any common fingerprint
                        rho = max(rho, FINGERPRINT_BOOST)

                matrix[(adv_a, adv_b)] = rho
                matrix[(adv_b, adv_a)] = rho

        cm = cls()
        cm._matrix = matrix
        cm._advisor_ids = advisor_ids
        return cm

    @staticmethod
    def _compute_pair_rho(
        outcomes_a: list[tuple[ResolvedOutcome, datetime]],
        outcomes_b: list[tuple[ResolvedOutcome, datetime]],
    ) -> float:
        """Compute pairwise Pearson ρ from co-movement on shared ideas.

        Only uses ideas where BOTH advisors submitted non-abstain outcomes.
        Falls back to DEFAULT_PRIOR (0.5) if fewer than MIN_PAIRS co-observations.

        Co-movement signal: signed alpha_bps values (continuous; more
        informative than binary labels alone).
        """
        # Index by idea_id for fast join
        idx_a: dict[str, float] = {
            o.idea_id: o.alpha_bps
            for o, _ in outcomes_a
            if not o.abstained
        }
        idx_b: dict[str, float] = {
            o.idea_id: o.alpha_bps
            for o, _ in outcomes_b
            if not o.abstained
        }

        shared_ideas = set(idx_a) & set(idx_b)
        if len(shared_ideas) < MIN_PAIRS:
            return DEFAULT_PRIOR

        vec_a = np.array([idx_a[iid] for iid in shared_ideas])
        vec_b = np.array([idx_b[iid] for iid in shared_ideas])

        # scipy pearsonr returns (r, p_value); we only need r
        # ConstantInputWarning is expected when all values are identical → NaN → fall back to prior
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", sp_stats.ConstantInputWarning)
            r, _ = sp_stats.pearsonr(vec_a, vec_b)

        # Guard NaN (happens when all values are identical — constant input)
        if np.isnan(r):
            return DEFAULT_PRIOR

        return float(np.clip(r, -1.0, 1.0))

    def get(self, advisor_a: str, advisor_b: str) -> float:
        """Return ρ for the advisor pair, using DEFAULT_PRIOR when sparse/unknown.

        Symmetric: get(a, b) == get(b, a).
        """
        if advisor_a == advisor_b:
            return 1.0
        # Try both orderings
        rho = self._matrix.get((advisor_a, advisor_b))
        if rho is None:
            rho = self._matrix.get((advisor_b, advisor_a))
        return DEFAULT_PRIOR if rho is None else rho

    def to_bundle_dict(self) -> dict[tuple[str, str], float]:
        """Return a copy of the matrix suitable for WeightBundle.correlation_matrix."""
        return dict(self._matrix)

    @property
    def advisor_ids(self) -> list[str]:
        """Sorted list of advisor IDs included in this matrix."""
        return list(self._advisor_ids)
