"""Signal and person scoring for A1 (Lane 6).

Scores BOTH signal-types AND people (per-insider / per-Congress-member)
side-by-side.  Cold-start uses a hard-coded prior until real outcomes exist;
real accuracy comes from Lane 14 later.

Design rules (INTERFACES.md §11)
---------------------------------
- No ``datetime.now()``.  Callers pass ``as_of``.
- ``score_provider`` parameter defaults to :class:`ColdStartProvider` which
  returns the prior.  Lane 14 will replace this with an empirical provider.
- Never imports the ingest lane or another in-progress lane.

Wave-C wiring note
-------------------
Lane 14 (outcome labeler) feeds real accuracy scores.  When it is wired up,
pass an :class:`EmpiricalScoreProvider` (or a subclass) that reads from the
``signal_type_scores`` / ``person_scores`` DB tables.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from arbiter.signals.detection import Signal, SignalType


# ---------------------------------------------------------------------------
# Minimum gate thresholds (must meet BOTH to be considered "gate-passing").
# ---------------------------------------------------------------------------

_GATE_MIN_SAMPLES: int = 10       # minimum number of outcome samples
_GATE_MIN_ACCURACY: float = 0.55  # minimum directional accuracy (55%)


# ---------------------------------------------------------------------------
# Cold-start priors (spec §2: prior until empirical outcomes exist)
# ---------------------------------------------------------------------------

# Directional accuracy priors (based on academic / practitioner literature).
_PRIOR_ACCURACY: dict[str, float] = {
    SignalType.CLUSTER_BUY.value: 0.62,          # cluster buys: strong prior
    SignalType.SINGLE_INSIDER_BUY.value: 0.58,   # single insider: moderate prior
    SignalType.CONGRESS_SECTOR.value: 0.55,       # congress: weak prior (MEDIUM horizon)
}
_PRIOR_ACCURACY_DEFAULT: float = 0.55

# Priors are "cold start" — gate is NOT considered passed until real samples arrive.
_PRIOR_PERSON_ACCURACY: float = 0.52  # new person: near-coin-flip prior


# ---------------------------------------------------------------------------
# ScoreBundle
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoreBundle:
    """Composite score for a signal.

    Attributes
    ----------
    signal_type:
        The :class:`~arbiter.signals.detection.SignalType` that was scored.
    signal_type_accuracy:
        Accuracy for this signal-type (cold-start prior or empirical).
    signal_type_samples:
        Number of resolved outcome samples for this signal-type.
    signal_type_gate_pass:
        True if signal-type meets the minimum sample/accuracy thresholds.
    person_ids:
        Person IDs from the underlying signal.
    person_accuracy_avg:
        Average accuracy across all persons in the signal.  If any person
        has 0 samples, their prior accuracy is used.
    person_min_samples:
        Minimum sample count across all persons (weakest link).
    person_gate_pass:
        True if ALL persons in the signal meet the gate thresholds.
    combined_score:
        Blended score in [0, 1] combining signal-type and person accuracy,
        weighted 60/40 (signal-type heavier because it generalises better
        in cold-start).
    is_cold_start:
        True when at least one component uses a prior (not empirical data).
    """

    signal_type: SignalType
    signal_type_accuracy: float
    signal_type_samples: int
    signal_type_gate_pass: bool
    person_ids: tuple[str, ...]
    person_accuracy_avg: float
    person_min_samples: int
    person_gate_pass: bool
    combined_score: float
    is_cold_start: bool


# ---------------------------------------------------------------------------
# Score provider protocol
# ---------------------------------------------------------------------------

class ScoreProvider(Protocol):
    """Protocol that score consumers depend on.

    Lane 14 will provide a concrete implementation backed by DB outcomes.
    The default is :class:`ColdStartProvider`.
    """

    def signal_type_score(
        self,
        signal_type: str,
        as_of: datetime,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[float, int]:
        """Return (accuracy, sample_count) for a signal-type.

        Return ``(prior, 0)`` when no empirical data exists.
        """
        ...

    def person_score(
        self,
        person_id: str,
        as_of: datetime,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[float, int]:
        """Return (accuracy, sample_count) for a person.

        Return ``(prior, 0)`` when no empirical data exists.
        """
        ...


class ColdStartProvider:
    """Default cold-start score provider.

    Always returns hard-coded priors with zero samples.  This is the
    correct behaviour before Lane 14 feeds real outcome data.
    """

    def signal_type_score(
        self,
        signal_type: str,
        as_of: datetime,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[float, int]:
        accuracy = _PRIOR_ACCURACY.get(signal_type, _PRIOR_ACCURACY_DEFAULT)
        return accuracy, 0

    def person_score(
        self,
        person_id: str,
        as_of: datetime,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[float, int]:
        return _PRIOR_PERSON_ACCURACY, 0


# Module-level default provider.
_DEFAULT_PROVIDER = ColdStartProvider()


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def score_signal_type(
    signal_type: str,
    as_of: datetime,
    conn: sqlite3.Connection | None = None,
    *,
    score_provider: ScoreProvider = _DEFAULT_PROVIDER,
) -> tuple[float, int, bool]:
    """Return ``(accuracy, samples, gate_pass)`` for a signal-type.

    Parameters
    ----------
    signal_type:
        String value of a :class:`SignalType`.
    as_of:
        Information timestamp (no future data).
    conn:
        Optional DB connection for empirical providers.
    score_provider:
        :class:`ScoreProvider` implementation.  Defaults to
        :class:`ColdStartProvider`.

    Returns
    -------
    Tuple of ``(accuracy: float, samples: int, gate_pass: bool)``.
    """
    accuracy, samples = score_provider.signal_type_score(signal_type, as_of, conn)
    gate_pass = (samples >= _GATE_MIN_SAMPLES) and (accuracy >= _GATE_MIN_ACCURACY)
    return accuracy, samples, gate_pass


def score_person(
    person_id: str,
    as_of: datetime,
    conn: sqlite3.Connection | None = None,
    *,
    score_provider: ScoreProvider = _DEFAULT_PROVIDER,
) -> tuple[float, int, bool]:
    """Return ``(accuracy, samples, gate_pass)`` for a person.

    Parameters
    ----------
    person_id:
        Person identifier (CIK for insiders, member ID for Congress).
    as_of:
        Information timestamp.
    conn:
        Optional DB connection for empirical providers.
    score_provider:
        :class:`ScoreProvider` implementation.

    Returns
    -------
    Tuple of ``(accuracy: float, samples: int, gate_pass: bool)``.
    """
    accuracy, samples = score_provider.person_score(person_id, as_of, conn)
    gate_pass = (samples >= _GATE_MIN_SAMPLES) and (accuracy >= _GATE_MIN_ACCURACY)
    return accuracy, samples, gate_pass


def score_signal(
    signal: Signal,
    as_of: datetime,
    conn: sqlite3.Connection | None = None,
    *,
    score_provider: ScoreProvider = _DEFAULT_PROVIDER,
) -> ScoreBundle:
    """Score a :class:`~arbiter.signals.detection.Signal` end-to-end.

    Scores signal-type and ALL persons in the signal, then combines them.

    Parameters
    ----------
    signal:
        Detected signal to score.
    as_of:
        Information timestamp.
    conn:
        Optional DB connection.
    score_provider:
        Score provider.  Defaults to cold-start prior.

    Returns
    -------
    :class:`ScoreBundle` with both axes scored.
    """
    # Score signal-type.
    st_accuracy, st_samples, st_gate = score_signal_type(
        signal.signal_type.value, as_of, conn, score_provider=score_provider
    )

    # Score each person.
    person_results: list[tuple[float, int, bool]] = []
    for pid in signal.person_ids:
        person_results.append(
            score_person(pid, as_of, conn, score_provider=score_provider)
        )

    if person_results:
        p_accuracy_avg = sum(r[0] for r in person_results) / len(person_results)
        p_min_samples = min(r[1] for r in person_results)
        p_gate = all(r[2] for r in person_results)
    else:
        p_accuracy_avg = _PRIOR_PERSON_ACCURACY
        p_min_samples = 0
        p_gate = False

    # Combined score: 60% signal-type, 40% person.
    combined = round(0.60 * st_accuracy + 0.40 * p_accuracy_avg, 4)

    # Cold-start if either side has no samples.
    is_cold = (st_samples < _GATE_MIN_SAMPLES) or (p_min_samples < _GATE_MIN_SAMPLES)

    return ScoreBundle(
        signal_type=signal.signal_type,
        signal_type_accuracy=st_accuracy,
        signal_type_samples=st_samples,
        signal_type_gate_pass=st_gate,
        person_ids=signal.person_ids,
        person_accuracy_avg=p_accuracy_avg,
        person_min_samples=p_min_samples,
        person_gate_pass=p_gate,
        combined_score=combined,
        is_cold_start=is_cold,
    )
