"""Opinion dataclass and advisor registry — Lane 9 core.

Implements INTERFACES.md §2 exactly.

Key conventions (INTERFACES.md §11):
- Abstain is represented by NOT emitting an Opinion (None), never a zero-stance Opinion.
- ``as_of`` is always a tz-aware UTC information timestamp, never wall-clock.
- ``stance_score`` ∈ [-1.0, 1.0] (directional, not a calibrated probability).
- ``confidence`` ∈ [0.0, 1.0].
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from arbiter.types import ConfidenceSource, HorizonBucket, bucket_for_days


@dataclass(frozen=True)
class Opinion:
    """Single raw directional opinion from one advisor.

    Every advisor emits RAW stance only — never calibrated probabilities.
    Calibration is handled downstream (Lane calibration step, upstream of fusion).

    Fields
    ------
    advisor_id:
        Dotted ID identifying the advisor and sub-source, e.g. "A1.insider",
        "A1.congress", "A2.mirofish".
    ticker:
        Exchange ticker symbol (e.g. "AAPL").
    stance_score:
        Directional signal in [-1.0, 1.0].  Positive = long, negative = short.
        **Abstain is represented by None, not 0.0.**
    confidence:
        Advisor's self-assessed confidence in [0.0, 1.0].
    confidence_source:
        How the confidence figure was derived (INTERFACES.md §1 ConfidenceSource).
    horizon_days:
        Advisor's stated horizon in calendar days.  Must be > 0 and ≤ 365.
    as_of:
        Information timestamp (tz-aware UTC).  This is when the underlying
        information became available, NOT wall-clock time.
    rationale:
        Human-readable explanation of the opinion.
    source_fingerprint:
        Opaque string used for correlation detection (e.g. SHA-256 of the
        underlying filing or event).  Two opinions with the same fingerprint
        derive from the same underlying event.
    run_group_id:
        Shared across all opinions from a single multi-opinion run (e.g. a
        MiroFish swarm session).  Single-opinion runs use a fresh ULID here.
    """

    advisor_id: str
    ticker: str
    stance_score: float
    confidence: float
    confidence_source: ConfidenceSource
    horizon_days: int
    as_of: datetime
    rationale: str
    source_fingerprint: str
    run_group_id: str

    @property
    def horizon_bucket(self) -> HorizonBucket:
        """Map horizon_days to the appropriate HorizonBucket.

        Delegates to ``arbiter.types.bucket_for_days`` so the mapping is
        always consistent with the canonical definition.

        Raises ValueError if horizon_days > 365 (INTERFACES.md §10b.1).
        """
        return bucket_for_days(self.horizon_days)


def validate_opinion(op: Opinion) -> None:
    """Raise ValueError on any contract violation.

    Checks (INTERFACES.md §2):
    - stance_score ∈ [-1.0, 1.0]
    - confidence ∈ [0.0, 1.0]
    - horizon_days > 0
    - horizon_days ≤ 365 (bucket_for_days raises if violated — we surface it)
    - as_of is tz-aware (has tzinfo set)
    - advisor_id is non-empty
    - ticker is non-empty
    - source_fingerprint is non-empty
    - run_group_id is non-empty
    """
    errors: list[str] = []

    if op.stance_score < -1.0 or op.stance_score > 1.0:
        errors.append(
            f"stance_score must be in [-1.0, 1.0], got {op.stance_score!r}"
        )

    if op.confidence < 0.0 or op.confidence > 1.0:
        errors.append(
            f"confidence must be in [0.0, 1.0], got {op.confidence!r}"
        )

    if op.horizon_days <= 0:
        errors.append(
            f"horizon_days must be > 0, got {op.horizon_days!r}"
        )

    if op.as_of.tzinfo is None:
        errors.append(
            "as_of must be tz-aware (tzinfo must be set); received a naive datetime"
        )

    if not op.advisor_id:
        errors.append("advisor_id must be non-empty")

    if not op.ticker:
        errors.append("ticker must be non-empty")

    if not op.source_fingerprint:
        errors.append("source_fingerprint must be non-empty")

    if not op.run_group_id:
        errors.append("run_group_id must be non-empty")

    # Validate horizon bucket mapping (raises ValueError for days > 365).
    # We do this last so the other errors are collected first.
    if op.horizon_days > 0:
        try:
            bucket_for_days(op.horizon_days)
        except ValueError as exc:
            errors.append(str(exc))

    if errors:
        raise ValueError(
            "Opinion contract violations:\n" + "\n".join(f"  - {e}" for e in errors)
        )


class AdvisorRegistry:
    """Thread-safe registry of advisor IDs and their metadata.

    Advisors self-register at startup via ``register()``.  The engine
    reads ``all_ids()`` to build the advisor pool.

    This is a module-level singleton (``default_registry``); advisors
    can also instantiate their own registry in tests.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._advisors: dict[str, dict] = {}

    def register(
        self,
        advisor_id: str,
        *,
        hard_weight_cap: float | None = None,
    ) -> None:
        """Register an advisor.

        Parameters
        ----------
        advisor_id:
            Unique dotted identifier (e.g. "A1.insider").
        hard_weight_cap:
            Optional hard cap on this advisor's fusion weight.
            ``None`` means the system default caps apply.
            MiroFish (A2.*) should always pass 0.35 per INTERFACES §5.
        """
        if not advisor_id:
            raise ValueError("advisor_id must be non-empty")
        with self._lock:
            self._advisors[advisor_id] = {
                "advisor_id": advisor_id,
                "hard_weight_cap": hard_weight_cap,
            }

    def all_ids(self) -> list[str]:
        """Return sorted list of all registered advisor IDs."""
        with self._lock:
            return sorted(self._advisors.keys())

    def get_metadata(self, advisor_id: str) -> dict:
        """Return metadata dict for an advisor, or raise KeyError if not registered."""
        with self._lock:
            return dict(self._advisors[advisor_id])


# Module-level default registry — advisors import and call .register() here.
default_registry = AdvisorRegistry()
