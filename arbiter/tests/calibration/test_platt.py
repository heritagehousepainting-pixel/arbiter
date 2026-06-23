"""Tests for PlattScaler — logistic calibration (< 200 outcomes)."""
from __future__ import annotations

import pytest

from arbiter.calibration.platt import PlattScaler


def _make_separable_dataset(n: int = 100) -> tuple[list[float], list[int]]:
    """Return a perfectly-separable dataset: positive stances -> +1, negative -> -1."""
    stances = []
    labels = []
    for i in range(n):
        # Positive half
        s = (i + 1) / n          # 0.01 .. 1.0
        stances.append(s)
        labels.append(1)
        # Negative half
        stances.append(-s)
        labels.append(-1)
    return stances, labels


class TestPlattScaler:

    def test_fit_and_predict_on_separable_set(self) -> None:
        """PlattScaler must fit on a clearly separable dataset without error."""
        stances, labels = _make_separable_dataset(50)
        scaler = PlattScaler()
        scaler.fit(stances, labels)
        assert scaler.is_fitted

    def test_predict_proba_range(self) -> None:
        """Output must be in [0, 1] for all inputs."""
        stances, labels = _make_separable_dataset(50)
        scaler = PlattScaler()
        scaler.fit(stances, labels)
        for raw in (-1.0, -0.5, 0.0, 0.5, 1.0):
            prob = scaler.predict_proba(raw)
            assert 0.0 <= prob <= 1.0, f"prob={prob} out of [0,1] for stance={raw}"

    def test_monotone_mapping_on_separable_set(self) -> None:
        """A logistic fit on separable data must produce monotone probabilities."""
        stances, labels = _make_separable_dataset(50)
        scaler = PlattScaler()
        scaler.fit(stances, labels)
        probe = [-1.0, -0.5, -0.1, 0.0, 0.1, 0.5, 1.0]
        probs = [scaler.predict_proba(s) for s in probe]
        for i in range(len(probs) - 1):
            assert probs[i] <= probs[i + 1] + 1e-9, (
                f"Non-monotone at index {i}: prob[{probe[i]}]={probs[i]:.4f} "
                f"> prob[{probe[i+1]}]={probs[i+1]:.4f}"
            )

    def test_positive_stance_gives_higher_prob_than_negative(self) -> None:
        """After fitting separable data, P(stance=+1) > P(stance=-1)."""
        stances, labels = _make_separable_dataset(50)
        scaler = PlattScaler()
        scaler.fit(stances, labels)
        assert scaler.predict_proba(0.9) > scaler.predict_proba(-0.9)

    def test_not_fitted_raises_on_predict(self) -> None:
        """Calling predict_proba before fit must raise RuntimeError."""
        scaler = PlattScaler()
        with pytest.raises(RuntimeError, match="not been fitted"):
            scaler.predict_proba(0.5)

    def test_mismatched_lengths_raise(self) -> None:
        scaler = PlattScaler()
        with pytest.raises(ValueError, match="same length"):
            scaler.fit([0.1, 0.2], [1])

    def test_too_few_samples_raise(self) -> None:
        """Fewer than 2 non-zero outcomes must raise ValueError."""
        scaler = PlattScaler()
        with pytest.raises(ValueError):
            scaler.fit([0.5], [1])

    def test_single_class_raises(self) -> None:
        """All-same-class labels must raise ValueError (can't fit logistic)."""
        scaler = PlattScaler()
        with pytest.raises(ValueError):
            scaler.fit([0.1, 0.5, 0.9], [1, 1, 1])

    def test_zero_labels_are_excluded(self) -> None:
        """No-call labels (0) are excluded; the model fits on non-zero labels only."""
        # Mix of 0-labels and real labels — must still fit if enough non-zero.
        stances = [0.8, -0.8, 0.0, 0.9, -0.9]
        labels  = [  1,   -1,   0,   1,   -1]
        scaler = PlattScaler()
        scaler.fit(stances, labels)
        assert scaler.is_fitted
