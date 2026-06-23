"""Isotonic regression calibration — Lane 9.

Used when the number of resolved outcomes is **≥ 200**.  Isotonic regression
is a non-parametric monotone fit that makes no shape assumption about the
stance→probability curve.  It outperforms Platt scaling when enough data
exists to support a flexible fit.

Background
----------
- Zadrozny & Elkan (2002) "Transforming Classifier Scores into Accurate
  Multiclass Probability Estimates."
- Niculescu-Mizil & Caruana (2005) — isotonic recommended for well-calibrated
  models with sufficient data.

INTERFACES.md §11.9: calibration owns raw→prob.
"""
from __future__ import annotations

import numpy as np
from sklearn.isotonic import IsotonicRegression


class IsotonicScaler:
    """Isotonic regression calibration for the high-data regime (≥ 200 outcomes).

    Fit a monotone (non-decreasing) mapping from raw stance to
    P(positive-alpha).

    The monotonicity constraint (increasing=True) ensures higher raw stance
    always maps to higher (or equal) probability, which is the physically
    required property for a directional signal.

    Attributes
    ----------
    _model : IsotonicRegression | None
        Fitted model.  None until :meth:`fit` is called.
    _x_min, _x_max : float
        Training domain boundaries for clamp-then-predict extrapolation.
    _fitted : bool
        True once :meth:`fit` has succeeded.
    """

    def __init__(self) -> None:
        self._model: IsotonicRegression | None = None
        self._x_min: float = -1.0
        self._x_max: float = 1.0
        self._fitted: bool = False

    @property
    def is_fitted(self) -> bool:
        """True if the model has been fitted."""
        return self._fitted

    def fit(self, stances: list[float], outcomes: list[int]) -> None:
        """Fit the isotonic scaler on raw stances and binary outcomes.

        Parameters
        ----------
        stances:
            List of raw stance scores ∈ [-1.0, 1.0].
        outcomes:
            Parallel list of binary outcome labels ∈ {+1, -1, 0}.
            Zero-labels (no-call) are excluded — same as PlattScaler.
        """
        if len(stances) != len(outcomes):
            raise ValueError(
                f"stances and outcomes must be the same length; "
                f"got {len(stances)} and {len(outcomes)}"
            )

        # Filter out no-call (0) labels.
        X, y = [], []
        for s, o in zip(stances, outcomes):
            if o != 0:
                X.append(s)
                y.append(1.0 if o == 1 else 0.0)

        if len(X) < 2:
            raise ValueError(
                f"Need at least 2 non-zero outcomes to fit IsotonicScaler, got {len(X)}"
            )
        if len(set(y)) < 2:
            raise ValueError(
                "Need both positive and negative outcomes to fit IsotonicScaler; "
                "all outcomes are the same class."
            )

        X_arr = np.array(X, dtype=float)
        y_arr = np.array(y, dtype=float)

        self._x_min = float(X_arr.min())
        self._x_max = float(X_arr.max())

        # increasing=True enforces monotone non-decreasing mapping.
        # out_of_bounds="clip" clamps extrapolation to training range endpoints.
        self._model = IsotonicRegression(increasing=True, out_of_bounds="clip")
        self._model.fit(X_arr, y_arr)
        self._fitted = True

    def predict_proba(self, raw_stance: float) -> float:
        """Return calibrated probability P(positive-alpha) for a raw stance.

        Parameters
        ----------
        raw_stance:
            Raw stance score ∈ [-1.0, 1.0].

        Returns
        -------
        float
            Calibrated probability ∈ [0.0, 1.0].

        Raises
        ------
        RuntimeError
            If called before :meth:`fit`.
        """
        if not self._fitted or self._model is None:
            raise RuntimeError(
                "IsotonicScaler has not been fitted yet; call fit() first."
            )
        # Clamp to training domain before predicting (out_of_bounds="clip" also
        # handles this but explicit clamp is clearer).
        clamped = max(self._x_min, min(self._x_max, raw_stance))
        prob = self._model.predict([clamped])[0]
        # Hard-clamp output to [0, 1] for floating-point safety.
        return float(np.clip(prob, 0.0, 1.0))
