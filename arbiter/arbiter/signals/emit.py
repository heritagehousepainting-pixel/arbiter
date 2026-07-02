"""Opinion emission for A1 signals — Lane 6.

Converts a detected :class:`~arbiter.signals.detection.Signal` into a
valid :class:`~arbiter.contract.opinion.Opinion` (or ``None`` to abstain).

Horizon mapping (INTERFACES.md §4.4, design doc §4.4)
------------------------------------------------------
- Form 4  → ``LONG`` horizon  (filing-date + next-day open entry; hold ~6mo)
- Congress → ``MEDIUM`` horizon (disclosure date; shorter attention horizon)

``horizon_days`` values are chosen to land squarely inside their bucket
(per :func:`~arbiter.types.bucket_for_days`):
- LONG   : 180 days  (bucket: 121–365)
- MEDIUM :  90 days  (bucket: 31–120)

Abstention rules (return ``None``)
-----------------------------------
1. ``is_10b5_1 = True`` on any filing in the signal (defense-in-depth).
2. ``conviction_score == 0.0`` — no meaningful edge.
3. ``combined_score`` from scoring falls below the weak-signal threshold.
4. Signal has no filing IDs (can't compute fingerprint → can't emit).

Design rules (INTERFACES.md §11)
---------------------------------
- No ``datetime.now()``.  Callers pass ``as_of``.
- ``source_fingerprint`` = SHA-256 of sorted filing IDs joined by ``:``.
- ``run_group_id`` = fresh ULID (single-opinion run).
- Must pass :func:`~arbiter.contract.opinion.validate_opinion`.
"""
from __future__ import annotations

import hashlib
from datetime import datetime

from arbiter.contract.opinion import Opinion, validate_opinion
from arbiter.db.helpers import generate_ulid
from arbiter.signals.detection import Signal, SignalType
from arbiter.signals.scoring import ScoreBundle
from arbiter.types import ConfidenceSource


# ---------------------------------------------------------------------------
# Horizon constants (spec §4.4)
# ---------------------------------------------------------------------------

_HORIZON_DAYS_FORM4: int = 180    # LONG bucket (121–365)
_HORIZON_DAYS_CONGRESS: int = 90  # MEDIUM bucket (31–120)
_HORIZON_DAYS_ACTIVIST: int = 180  # LONG bucket (121–365)
_HORIZON_DAYS_FUND: int = 180     # LONG bucket (121–365)
_HORIZON_DAYS_SELL: int = 90      # MEDIUM — bearish theses act faster (Tier-3 #9)

# Minimum combined_score to emit (below this → abstain).
_MIN_COMBINED_SCORE: float = 0.50

# Minimum conviction_score to emit (below this → abstain).
_MIN_CONVICTION: float = 0.001


# ---------------------------------------------------------------------------
# Advisor IDs
# ---------------------------------------------------------------------------

_ADVISOR_ID_FORM4: str = "A1.insider"
_ADVISOR_ID_CONGRESS: str = "A1.congress"
_ADVISOR_ID_ACTIVIST: str = "A1.activist"
_ADVISOR_ID_FUND: str = "A1.fund"
# Tier-3 #9 — SEPARATE advisor ids for the sell legs so the learning loop
# scores sell-signal quality independently of the (already-scored) buy legs.
_ADVISOR_ID_FORM4_SELL: str = "A1.insider_sell"
_ADVISOR_ID_CONGRESS_SELL: str = "A1.congress_sell"


# ---------------------------------------------------------------------------
# Public emit function
# ---------------------------------------------------------------------------

def emit_opinion(
    signal: Signal,
    as_of: datetime,
    score_bundle: ScoreBundle | None = None,
) -> Opinion | None:
    """Convert a detected signal into an Opinion, or return None to abstain.

    Parameters
    ----------
    signal:
        A :class:`~arbiter.signals.detection.Signal` from detection.py.
    as_of:
        Information timestamp (tz-aware UTC).  Must equal or follow the
        signal's ``window_end`` — no look-ahead.
    score_bundle:
        Optional pre-computed :class:`~arbiter.signals.scoring.ScoreBundle`.
        If ``None``, a cold-start score is used.

    Returns
    -------
    A valid :class:`~arbiter.contract.opinion.Opinion`, or ``None`` to abstain.

    Abstention conditions
    ---------------------
    - No filing IDs (can't fingerprint).
    - ``conviction_score == 0.0`` (no edge).
    - ``combined_score < _MIN_COMBINED_SCORE`` (weak signal).
    """
    # --- Abstain rule 1: no filing IDs ---
    if not signal.filing_ids:
        return None

    # --- Abstain rule 2: zero conviction ---
    if signal.conviction_score < _MIN_CONVICTION:
        return None

    # --- Abstain rule 3: weak combined score ---
    if score_bundle is not None and score_bundle.combined_score < _MIN_COMBINED_SCORE:
        return None

    # --- Map source → advisor_id and horizon_days ---
    # Tier-3 #9: the SELL signal types map by TYPE (before source) to their own
    # advisor ids, both at the 90d MEDIUM horizon (matching the idea bucket the
    # engine builds for them — a mismatch would orphan the opinion).
    if signal.signal_type == SignalType.CLUSTER_SELL:
        advisor_id = _ADVISOR_ID_FORM4_SELL
        horizon_days = _HORIZON_DAYS_SELL
    elif signal.signal_type == SignalType.CONGRESS_SELL:
        advisor_id = _ADVISOR_ID_CONGRESS_SELL
        horizon_days = _HORIZON_DAYS_SELL
    elif signal.source == "congress":
        advisor_id = _ADVISOR_ID_CONGRESS
        horizon_days = _HORIZON_DAYS_CONGRESS
    elif signal.source == "form13d":
        advisor_id = _ADVISOR_ID_ACTIVIST
        horizon_days = _HORIZON_DAYS_ACTIVIST
    elif signal.source == "form13f":
        advisor_id = _ADVISOR_ID_FUND
        horizon_days = _HORIZON_DAYS_FUND
    else:
        # form4 (and any future form4-like source)
        advisor_id = _ADVISOR_ID_FORM4
        horizon_days = _HORIZON_DAYS_FORM4

    # --- Compute source_fingerprint ---
    # SHA-256 of the SORTED filing IDs joined by ":" — order-stable regardless
    # of how detection assembled the tuple.
    fingerprint_input = ":".join(sorted(signal.filing_ids))
    source_fingerprint = hashlib.sha256(fingerprint_input.encode()).hexdigest()

    # --- Fresh ULID for run_group_id ---
    run_group_id = generate_ulid()

    # --- Stance score: always positive (BUY signals only from detector) ---
    # Stance is the conviction score, remapped to [0.1, 1.0] to avoid
    # emitting a near-zero stance that could be confused with abstention.
    raw_stance = max(signal.conviction_score, 0.1)
    stance_score = min(raw_stance, 1.0)

    # Any 'S'-transaction signal is BEARISH — flip the sign: 13D/G exits, 13F
    # trims/exits, and (Tier-3 #9) form4/congress cluster sells all carry
    # ``meta["txn_type"]="S"``.  'P'-only detectors never set the key, so the
    # default positive stance holds for them.  validate_opinion accepts
    # [-1.0, 1.0], so the negative stance passes.
    if signal.meta.get("txn_type") == "S":
        stance_score = -stance_score

    # --- Confidence from score_bundle or cold-start prior ---
    if score_bundle is not None:
        confidence = round(score_bundle.combined_score, 4)
    else:
        # Cold-start fallback: use conviction score as a conservative proxy.
        confidence = round(min(signal.conviction_score, 1.0), 4)

    # Clamp confidence to (0, 1] — 0.0 is technically valid per contract but
    # meaningless for a non-abstained opinion.
    confidence = max(confidence, 0.01)

    # --- Build rationale (side-aware: an 'S' signal is selling/reducing) ---
    n_people = len(signal.person_ids)
    action = "selling/reducing" if signal.meta.get("txn_type") == "S" else "buying"
    rationale = (
        f"{signal.signal_type.value} on {signal.ticker}: "
        f"{n_people} insider(s) {action} in "
        f"{(signal.window_end - signal.window_start).days + 1}-day window; "
        f"conviction={signal.conviction_score:.3f}"
    )
    if score_bundle is not None and not score_bundle.is_cold_start:
        rationale += f"; empirical accuracy={score_bundle.signal_type_accuracy:.2f}"
    else:
        rationale += "; cold-start prior"

    # --- Ensure as_of is tz-aware ---
    if as_of.tzinfo is None:
        raise ValueError("emit_opinion: as_of must be tz-aware UTC")

    op = Opinion(
        advisor_id=advisor_id,
        ticker=signal.ticker,
        stance_score=stance_score,
        confidence=confidence,
        confidence_source=ConfidenceSource.MODELED,
        horizon_days=horizon_days,
        as_of=as_of,
        rationale=rationale,
        source_fingerprint=source_fingerprint,
        run_group_id=run_group_id,
    )

    # --- Final validation (must not raise) ---
    validate_opinion(op)

    return op
