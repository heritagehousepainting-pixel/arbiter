"""Frozen seam dataclasses — Lane 9 core.

ALL cross-lane seam types live here so every downstream lane imports
from ONE canonical location.  Implements INTERFACES.md §4–§9.

Import pattern::

    from arbiter.contract.seams import (
        FusionOutput, AdvisorWeight, WeightBundle, EqualWeightBundle,
        ResolvedOutcome, Idea, TradingDecision, PaperOrder,
    )
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from arbiter.types import (
    DegradationLevel,
    HorizonBucket,
    IdeaState,
    OrderSide,
)


# ---------------------------------------------------------------------------
# §4  Fusion output — produced by arbiter/fusion/output.py (Lane L10)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FusionOutput:
    """Engine output for one horizon-bucket per cycle.

    Phase-1 uses equal weights (``cold_start=True`` until calibration exists).
    Trust-weighted fusion (Phase 3) consumes a ``WeightBundle``.

    Fields
    ------
    bucket:
        The HorizonBucket this fusion result covers.
    conviction:
        Signed aggregate signal: signal_strength × diversity_factor − lone_bull_tax.
    dispersion:
        Spread of advisor stances (standard deviation of weighted stance_scores).
    effective_n:
        Diversity-adjusted advisor count: 1 / (Σᵢ Σⱼ wᵢ wⱼ ρᵢⱼ).
    n_opinions:
        Raw count of opinions pooled.
    advisor_contributions:
        Per-advisor contribution to the conviction signal (advisor_id → float).
    vetoes:
        advisor_ids that triggered a hard-veto.
    cold_start:
        True while the calibration prior dominates (< ~30 resolved outcomes).
    """

    bucket: HorizonBucket
    conviction: float
    dispersion: float
    effective_n: float
    n_opinions: int
    advisor_contributions: dict[str, float]
    vetoes: list[str]
    cold_start: bool


# ---------------------------------------------------------------------------
# §5  Trust ledger output — produced by arbiter/trust/ledger.py (Lane L11)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AdvisorWeight:
    """Per-advisor LOG-POOL weight as emitted by the trust ledger.

    Note: ``weight`` is a LOG-POOL weight, NOT a simplex weight (does not
    necessarily sum to 1 across advisors).  weight=0.0 disables the advisor.

    Caps (INTERFACES.md §5):
    - Ceiling: 0.50 (sample-gated)
    - MiroFish (A2.*) hard cap: 0.35 forever
    - Negative-skill advisors: 0.0 + diagnostic hold
    - Thin-sample positive floor: 0.02
    """

    advisor_id: str
    weight: float
    ci_low: float
    ci_high: float
    shadow: bool  # True = recorded but zero live weight (onboarding mode)


@dataclass(frozen=True)
class WeightBundle:
    """Collection of advisor weights + pairwise correlation matrix.

    ``correlation_matrix`` keys are (advisor_id_i, advisor_id_j) pairs.

    Correlation-matrix defaults (two distinct concepts — do not conflate):
    - **Trust ledger sparse prior (Lane 11)**: ρ = 0.5 per INTERFACES.md §5.
      This is the prior Lane 11 uses when estimating cross-advisor correlations
      before Phase-5 data is available.  The ledger MAY emit a WeightBundle with
      some pairs pre-filled at 0.5 as its sparse estimate.
    - **Fusion empty-matrix default (Lane 10)**: ρ = 0.0 for any pair absent
      from ``correlation_matrix`` when fusion looks it up.  Phase-1 ships an
      EqualWeightBundle with an empty matrix, so fusion treats all advisors as
      independent (no deflation) until a real matrix arrives in Phase 5.
    These are two different defaults at two different layers.  See also
    ``fusion/correlation.py`` for the authoritative comment on this distinction.
    """

    weights: dict[str, AdvisorWeight]
    correlation_matrix: dict[tuple[str, str], float]


def EqualWeightBundle(advisor_ids: list[str]) -> WeightBundle:
    """Factory: Phase-1 equal-weight bundle with empty correlation matrix.

    All advisors receive a raw LOG-POOL weight of 1.0 each.

    Weight convention (INTERFACES.md §5):
        ``AdvisorWeight.weight`` is a LOG-POOL weight — NOT a simplex weight.
        It does NOT need to sum to 1 across advisors.  ``pool.py`` performs
        the single authoritative normalisation step (divides each raw weight
        by the sum of all active weights to produce simplex weights for the
        pool computation).  Phase-3 implementers: store real trust scores as
        raw log-pool weights here; do NOT pre-normalise.

    With equal raw weights of 1.0 each, normalisation in ``pool.py`` produces
    equal simplex weights (1/N each) — identical behaviour to the previous
    1/N encoding, but semantically correct for Phase 3.

    Parameters
    ----------
    advisor_ids:
        List of advisor ID strings to include.  Order does not matter.

    Returns
    -------
    WeightBundle
        All weights = 1.0 (raw log-pool), shadow=False, ci_low=ci_high=1.0.
        Empty correlation matrix (fusion defaults to ρ=0.0 for missing pairs;
        the 0.5 sparse prior mentioned in §5 is for the trust ledger's output,
        not fusion's empty-matrix default — see correlation.py for details).
    """
    if len(advisor_ids) == 0:
        return WeightBundle(weights={}, correlation_matrix={})

    weights = {
        aid: AdvisorWeight(
            advisor_id=aid,
            weight=1.0,
            ci_low=1.0,
            ci_high=1.0,
            shadow=False,
        )
        for aid in advisor_ids
    }
    return WeightBundle(weights=weights, correlation_matrix={})


# ---------------------------------------------------------------------------
# §6  Resolved outcome — produced by arbiter/evaluation/outcome_labeler.py (L14)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedOutcome:
    """Ground-truth outcome label for one advisor opinion on one idea.

    ``alpha_bps`` drives trust updates (continuous).
    ``binary`` is for display and calibration only (not trust).

    Outcome formula (INTERFACES.md §6):
        alpha_i = R_i(t0, t1) − beta_i × R_SPY(t0, t1)
        Entry = filing-date+1 OPEN, net modeled slippage.
        beta_i = 252-day rolling beta as of t0−1 (impute 1.0 + log flag).
    """

    idea_id: str
    advisor_id: str
    ticker: str
    alpha_bps: float           # SPY-beta-adjusted alpha, net slippage; drives trust
    binary: int                # +1 / 0 / -1 (±25bps band → 0 "no-call"); display only
    advisor_confidence: float
    stance_score: float        # advisor's ACTUAL directional forecast in [-1,1]; Brier forecast (#5a)
    abstained: bool
    horizon_days: int
    label_kind: str            # "normal"|"early_exit"|"reversal"|"corporate_event"|"partial"|"counterfactual"

    def __post_init__(self) -> None:
        # Clamp the two bounded fields to their contract ranges (E1).  An
        # out-of-range stance_score (∉ [-1, 1]) or advisor_confidence (∉ [0, 1])
        # — a data/ingest bug — would push the Brier forecast outside [0, 1],
        # blow BS past 1.0, and drive BSS to large negatives (e.g. −8), which can
        # PERMANENTLY mute an advisor.  Clamp at construction so every consumer
        # (Brier, calibration) sees in-range values.  Frozen dataclass → use
        # object.__setattr__.
        object.__setattr__(
            self, "stance_score", max(-1.0, min(1.0, self.stance_score))
        )
        object.__setattr__(
            self, "advisor_confidence", max(0.0, min(1.0, self.advisor_confidence))
        )


# ---------------------------------------------------------------------------
# §7  Idea object — owned by arbiter/orchestrator/idea.py (Lane L13)
# ---------------------------------------------------------------------------

@dataclass
class Idea:
    """Mutable idea object tracking the lifecycle of a trade thesis.

    ``state`` is intentionally mutable (the only mutable field) — the FSM
    in lifecycle.py transitions it in-place.  All other fields are set at
    construction and should not change.

    Dedupe key = (ticker, horizon_bucket.value).  Concurrent ideas on the
    same ticker in DIFFERENT buckets are allowed (capped at MAX_TICKER_EXPOSURE).

    FSM states (INTERFACES.md §7 / IdeaState enum):
        NASCENT → GATHERING → PROVISIONAL_DECIDED → FINAL_DECIDED
        → EXECUTED → MONITORED → OUTCOME_READY → CLOSED
        (or → ABANDONED from any pre-EXECUTED state)
    """

    idea_id: str                  # ULID
    ticker: str
    thesis: str
    horizon_days: int
    state: IdeaState
    as_of: datetime               # original information timestamp (passed to L14 on OUTCOME_READY)
    dedupe_key: tuple[str, str]   # (ticker, horizon_bucket.value)


# ---------------------------------------------------------------------------
# §8  Safety gate output — owned by arbiter/safety/ (Lane L4)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TradingDecision:
    """Result of the safety gate check before every order.

    Quorum rules (INTERFACES.md §8):
    - 2+ live advisors → size_multiplier=1.0, level=NORMAL
    - 1 live advisor   → size_multiplier=0.25, level=DEGRADED
    - 0 live advisors  → size_multiplier=0.0, level=HALTED, allowed=False
    """

    allowed: bool
    size_multiplier: float   # 1.0 normal, 0.25 DEGRADED (1 advisor), 0.0 HALTED
    level: DegradationLevel
    reasons: list[str]


# ---------------------------------------------------------------------------
# §9  Paper order — owned by arbiter/policy/ + arbiter/execution/ (Lane L12)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PaperOrder:
    """Idempotent paper (sim) order record.

    ``dedup_hash`` = sha256(ticker + side + horizon + entry_date + advisor_signature).
    ``exits`` stores all three exit triggers set at entry time (never revised up).

    ``exits`` expected keys:
        "stop_loss"            : float  (price level)
        "horizon_expiry"       : date   (calendar date)
        "conviction_reversal"  : float  (conviction threshold that triggers exit)
    """

    order_id: str            # ULID
    dedup_hash: str
    ticker: str
    side: OrderSide
    qty: float
    horizon_bucket: HorizonBucket
    entry_date: date
    advisor_signature: str
    exits: dict              # {"stop_loss": float, "horizon_expiry": date, "conviction_reversal": float}
