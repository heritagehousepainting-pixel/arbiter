"""Platt scaling calibration — Lane 9.

Used when the number of resolved outcomes is **< 200**.  Platt scaling fits
a logistic regression (sigmoid) on the raw stance scores to produce calibrated
probabilities.  It handles small-sample regimes better than isotonic regression
because the logistic curve is a single-degree-of-freedom fit that resists
overfitting on thin data.

References
----------
- Platt (1999) "Probabilistic Outputs for Support Vector Machines..."
- Niculescu-Mizil & Caruana (2005) — Platt scaling is well-suited for methods
  that produce a monotone score but with poor calibration.

INTERFACES.md §11.9: calibration owns raw→prob.
"""
from __future__ import annotations

import numpy as np
from sklearn.linear_model import LogisticRegression


class PlattScaler:
    """Logistic calibration model for the low-data regime (< 200 outcomes).

    Fit a logistic regression on (raw_stance, ) → binary_outcome
    where binary_outcome ∈ {0, 1} (we convert +1/-1 labels upstream).

    The model forces a single feature (raw stance) and ``max_iter=1000``
    to ensure convergence on small datasets.

    Attributes
    ----------
    _model : LogisticRegression | None
        Fitted model.  None until :meth:`fit` is called.
    _fitted : bool
        True once :meth:`fit` has succeeded.
    """

    def __init__(self) -> None:
        self._model: LogisticRegression | None = None
        self._fitted: bool = False

    @property
    def is_fitted(self) -> bool:
        """True if the model has been fitted."""
        return self._fitted

    def fit(self, stances: list[float], outcomes: list[int]) -> None:
        """Fit the Platt scaler on raw stances and binary outcomes.

        Parameters
        ----------
        stances:
            List of raw stance scores ∈ [-1.0, 1.0].
        outcomes:
            Parallel list of binary outcome labels.  Accepts {+1, -1, 0}.
            Zero-labels (no-call) are **excluded** from fitting because they
            are ambiguous — the ±25bps band makes them uninformative.
        """
        if len(stances) != len(outcomes):
            raise ValueError(
                f"stances and outcomes must be the same length; "
                f"got {len(stances)} and {len(outcomes)}"
            )

        # Filter out no-call (0) labels — ambiguous, exclude from fit.
        X, y = [], []
        for s, o in zip(stances, outcomes):
            if o != 0:
                X.append([s])
                y.append(1 if o == 1 else 0)

        if len(X) < 2:
            raise ValueError(
                f"Need at least 2 non-zero outcomes to fit PlattScaler, got {len(X)}"
            )
        if len(set(y)) < 2:
            raise ValueError(
                "Need both positive and negative outcomes to fit PlattScaler; "
                "all outcomes are the same class."
            )

        self._model = LogisticRegression(max_iter=1000, solver="lbfgs")
        self._model.fit(np.array(X), np.array(y))
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
            raise RuntimeError("PlattScaler has not been fitted yet; call fit() first.")
        prob = self._model.predict_proba([[raw_stance]])[0]
        # predict_proba returns [P(class=0), P(class=1)]; we return P(class=1).
        return float(prob[1])
