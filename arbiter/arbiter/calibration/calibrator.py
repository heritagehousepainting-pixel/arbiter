"""Calibrator — the calibration seam consumed by fusion (Lane 10).

This is the public interface that fusion imports.  It wraps either a
:class:`~arbiter.calibration.platt.PlattScaler` or an
:class:`~arbiter.calibration.isotonic.IsotonicScaler` (chosen by sample
count) and falls back to the STANCE_BASE prior when no fitted model exists.

Model selection rule (INTERFACES.md §11.9):
  - < 200 non-zero outcomes  → Platt scaling (logistic)
  - ≥ 200 non-zero outcomes  → Isotonic regression
  - no outcomes at all       → STANCE_BASE cold-start prior

Per-advisor, per-horizon-bucket stratification:
  Each (advisor_id, HorizonBucket) cell has its own independent model so
  that insider short-term patterns don't bleed into MiroFish long-term fits.

DB persistence:
  Fitted parameters are stored in ``calibration_params`` (migration 012).
  ``fit()`` is called with a list of :class:`~arbiter.contract.seams.ResolvedOutcome`
  objects; ``transform()`` is what fusion calls at every cycle.

Usage (fusion side)::

    calibrator = Calibrator(advisor_id="A1.insider")
    calibrator.fit(outcomes)          # outcomes = list[ResolvedOutcome]
    prob = calibrator.transform(raw_stance=0.8, horizon_days=15)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Sequence

from arbiter.calibration.isotonic import IsotonicScaler
from arbiter.db.helpers import generate_ulid
from arbiter.calibration.platt import PlattScaler
from arbiter.calibration.stance_base import lookup_prior
from arbiter.contract.seams import ResolvedOutcome
from arbiter.types import HorizonBucket, bucket_for_days

# Threshold for switching from Platt to isotonic regression.
_ISOTONIC_THRESHOLD = 200

# Minimum non-zero outcomes required before fitting any model.
# Below this we remain in cold-start (STANCE_BASE) mode.
_MIN_FIT_SAMPLES = 2


class Calibrator:
    """Per-advisor calibration engine.

    Maintains one model per :class:`~arbiter.types.HorizonBucket`,
    selected by sample count from the resolved outcomes for this advisor.

    Parameters
    ----------
    advisor_id:
        The full dotted advisor ID (e.g. "A1.insider").  Used to filter
        outcomes and to key the cold-start prior lookup.
    conn:
        Optional SQLite connection.  If provided, :meth:`fit` persists
        fitted parameters to ``calibration_params`` and :meth:`load`
        can restore them.  Tests may pass ``None`` to skip persistence.

    Attributes
    ----------
    advisor_id : str
    _models : dict[HorizonBucket, PlattScaler | IsotonicScaler]
        Fitted model per bucket (populated after :meth:`fit`).
    _n_outcomes : dict[HorizonBucket, int]
        Count of non-zero outcomes used per bucket.
    """

    # FROZEN CONTRACT (E2/E4): every branch of transform() — identity, prior,
    # Platt, isotonic — returns P(positive-alpha) ∈ [0, 1].  pool.py maps to the
    # signed signal via 2*p - 1 before weighting.  This flag signals that space.
    outputs_probability: bool = True

    def __init__(
        self,
        advisor_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        if not advisor_id:
            raise ValueError("advisor_id must be non-empty")
        self.advisor_id = advisor_id
        self._conn = conn
        self._models: dict[HorizonBucket, PlattScaler | IsotonicScaler] = {}
        self._n_outcomes: dict[HorizonBucket, int] = {}

    # ------------------------------------------------------------------
    # Cold-start detection
    # ------------------------------------------------------------------

    @property
    def is_cold_start(self) -> bool:
        """True if NO bucket has a fitted model (overall cold-start flag).

        This is the property that fusion reads via ``calibrator.is_cold_start``
        (no-arg, bool result).  Returns True when ``_models`` is empty, i.e.
        before any call to :meth:`fit` has produced a fitted model.

        For per-bucket cold-start detection use :meth:`is_cold_start_for`.
        """
        return len(self._models) == 0

    def is_cold_start_for(self, horizon_bucket: HorizonBucket) -> bool:
        """Return True if no fitted model exists for the specified bucket.

        Parameters
        ----------
        horizon_bucket:
            The specific :class:`~arbiter.types.HorizonBucket` to check.
        """
        return horizon_bucket not in self._models

    # ------------------------------------------------------------------
    # Fitting
    # ------------------------------------------------------------------

    def fit(self, outcomes: Sequence[ResolvedOutcome]) -> None:
        """Fit calibration models from resolved outcomes.

        Each :class:`~arbiter.contract.seams.ResolvedOutcome` is bucketed
        by its ``horizon_days`` into a :class:`~arbiter.types.HorizonBucket`.
        A separate model is fit per bucket.

        Parameters
        ----------
        outcomes:
            Sequence of :class:`~arbiter.contract.seams.ResolvedOutcome`.
            Only outcomes matching ``self.advisor_id`` are used.
            Abstained outcomes are excluded.
            No-call (``binary == 0``) outcomes are filtered inside
            :class:`PlattScaler` / :class:`IsotonicScaler`.
        """
        # Stratify by bucket.
        bucket_stances: dict[HorizonBucket, list[float]] = {b: [] for b in HorizonBucket}
        bucket_labels: dict[HorizonBucket, list[int]] = {b: [] for b in HorizonBucket}

        for outcome in outcomes:
            if outcome.advisor_id != self.advisor_id:
                continue
            if outcome.abstained:
                continue
            try:
                bucket = bucket_for_days(outcome.horizon_days)
            except ValueError:
                # horizon_days > 365 or <= 0 — skip invalid rows.
                continue
            # Fit on the advisor's REAL persisted directional stance (#5a / E2).
            # The previous formula X = sign(binary) * confidence was a LEAK: it
            # is a function of the realized label, so the "calibration" trivially
            # recovered the label rather than learning the stance→outcome map.
            # We now fit X = ResolvedOutcome.stance_score (the actual forecast).
            #
            # Legacy / proxy rows carry stance_score == 0.0 (e.g. the neutral
            # attribution fallback, or pre-#5a rows with no recovered opinion).
            # A degenerate constant-0 feature would collapse the logistic fit, so
            # we EXCLUDE those rows from the fit entirely.  They are still counted
            # nowhere (not added to stances/labels), so they never reach the
            # scaler and never inflate n_nonzero.
            if outcome.stance_score == 0.0:
                continue

            raw_stance = max(-1.0, min(1.0, outcome.stance_score))
            bucket_stances[bucket].append(raw_stance)
            bucket_labels[bucket].append(outcome.binary)

        # Fit one model per bucket that has enough data.
        for bucket in HorizonBucket:
            stances = bucket_stances[bucket]
            labels = bucket_labels[bucket]
            # Count non-zero labels (no-call excluded from fitting).
            n_nonzero = sum(1 for lbl in labels if lbl != 0)
            self._n_outcomes[bucket] = n_nonzero

            if n_nonzero < _MIN_FIT_SAMPLES:
                # Not enough data — skip, will use cold-start prior.
                self._models.pop(bucket, None)
                continue

            try:
                if n_nonzero >= _ISOTONIC_THRESHOLD:
                    model: PlattScaler | IsotonicScaler = IsotonicScaler()
                else:
                    model = PlattScaler()
                model.fit(stances, labels)
                self._models[bucket] = model
            except ValueError:
                # e.g. only one class present — fall back to cold-start.
                self._models.pop(bucket, None)

    # ------------------------------------------------------------------
    # Transform (the seam fusion calls)
    # ------------------------------------------------------------------

    def transform(self, raw_stance: float, horizon_days: int) -> float:
        """Map a raw stance to a calibrated probability.

        This is the primary seam consumed by fusion.

        Parameters
        ----------
        raw_stance:
            Raw stance score ∈ [-1.0, 1.0] as emitted by the advisor.
        horizon_days:
            Advisor's stated horizon in calendar days (used to determine
            the HorizonBucket and select the appropriate model).

        Returns
        -------
        float
            Calibrated probability P(positive-alpha) ∈ [0.0, 1.0].
            Falls back to the STANCE_BASE prior if no model is fitted for
            this bucket.
        """
        if raw_stance < -1.0 or raw_stance > 1.0:
            raise ValueError(
                f"raw_stance must be in [-1.0, 1.0], got {raw_stance!r}"
            )

        try:
            bucket = bucket_for_days(horizon_days)
        except ValueError:
            # Invalid horizon — use cold-start prior with SHORT as fallback.
            return lookup_prior(self.advisor_id, raw_stance, HorizonBucket.SHORT)

        model = self._models.get(bucket)
        if model is None:
            # Cold-start: return prior from STANCE_BASE table.
            return lookup_prior(self.advisor_id, raw_stance, bucket)

        prob = model.predict_proba(raw_stance)
        # Hard-clamp to [0, 1] for floating-point safety.
        return max(0.0, min(1.0, prob))

    def transform_for(
        self, advisor_id: str, raw_stance: float, horizon_days: int
    ) -> float:
        """Per-advisor seam (D5).  Defaults to ``self.transform`` — a single
        ``Calibrator`` is already advisor-specific, so ``advisor_id`` is accepted
        for signature parity with ``MultiAdvisorCalibrator`` and ignored here.
        """
        return self.transform(raw_stance, horizon_days)

    # ------------------------------------------------------------------
    # Model-type inspection (for logging / tests)
    # ------------------------------------------------------------------

    def model_type(self, bucket: HorizonBucket) -> str:
        """Return the model type string for a given bucket.

        Returns
        -------
        "isotonic" | "platt" | "cold_start"
        """
        model = self._models.get(bucket)
        if model is None:
            return "cold_start"
        if isinstance(model, IsotonicScaler):
            return "isotonic"
        return "platt"

    def n_outcomes(self, bucket: HorizonBucket) -> int:
        """Return the number of non-zero outcomes used to fit the model for bucket."""
        return self._n_outcomes.get(bucket, 0)

    def total_nonzero_outcomes(self) -> int:
        """Total non-zero outcomes seen across ALL buckets (for the wiring-level
        thin-sample gate, D5).  Populated by :meth:`fit`."""
        return sum(self._n_outcomes.values())

    def max_bucket_nonzero_outcomes(self) -> int:
        """The largest per-bucket non-zero outcome count across buckets (the
        per-(advisor,bucket) sample the wiring-level gate can key on, D5)."""
        return max(self._n_outcomes.values(), default=0)

    # ------------------------------------------------------------------
    # DB persistence (optional — requires conn)
    # ------------------------------------------------------------------

    def persist(self, as_of: datetime) -> None:
        """Persist fitted model metadata to the DB (calibration_params table).

        Parameters
        ----------
        as_of:
            Information timestamp (tz-aware UTC) for this calibration run.
            Never uses datetime.now() — caller must supply the clock.

        Notes
        -----
        Stores: advisor_id, bucket, model_type, n_outcomes, as_of.
        The actual sklearn model objects are NOT serialised here (they are
        re-fit from outcomes on startup).  This table provides an audit trail
        and the metadata needed by Wave-C fusion to detect staleness.
        """
        if self._conn is None:
            raise RuntimeError("Calibrator has no DB connection; pass conn= to persist.")
        if as_of.tzinfo is None:
            raise ValueError("as_of must be tz-aware UTC; got a naive datetime.")

        cursor = self._conn.cursor()
        created_at = as_of.isoformat()
        for bucket in HorizonBucket:
            mtype = self.model_type(bucket)
            n = self.n_outcomes(bucket)
            cursor.execute(
                """
                INSERT INTO calibration_params
                    (id, advisor_id, horizon_bucket, model_type, n_outcomes, as_of, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    # ULID PK + clock-based created_at: 012_calibration.sql has no
                    # SQL-level defaults (convention §10/§11), so the Python layer
                    # must supply both — otherwise the PK is silently NULL.
                    generate_ulid(),
                    self.advisor_id,
                    bucket.value,
                    mtype,
                    n,
                    created_at,
                    created_at,
                ),
            )
        self._conn.commit()
