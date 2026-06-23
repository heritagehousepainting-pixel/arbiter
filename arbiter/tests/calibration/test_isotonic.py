"""Tests for IsotonicScaler — isotonic regression calibration (≥ 200 outcomes)."""
from __future__ import annotations

import pytest

from arbiter.calibration.isotonic import IsotonicScaler


def _make_large_dataset(n: int = 200) -> tuple[list[float], list[int]]:
    """Return n pairs where positive stance → +1, negative → -1."""
    stances = []
    labels = []
    for i in range(n):
        s = (i + 1) / n
        stances.append(s)
        labels.append(1)
        stances.append(-s)
        labels.append(-1)
    return stances, labels


class TestIsotonicScaler:

    def test_fit_succeeds_on_large_set(self) -> None:
        stances, labels = _make_large_dataset(200)
        scaler = IsotonicScaler()
        scaler.fit(stances, labels)
        assert scaler.is_fitted

    def test_predict_proba_in_unit_interval(self) -> None:
        stances, labels = _make_large_dataset(200)
        scaler = IsotonicScaler()
        scaler.fit(stances, labels)
        for raw in (-1.0, -0.5, 0.0, 0.5, 1.0):
            prob = scaler.predict_proba(raw)
            assert 0.0 <= prob <= 1.0, f"prob={prob} out of [0,1] for stance={raw}"

    def test_monotone_output(self) -> None:
        """Isotonic regression is non-decreasing by construction."""
        stances, labels = _make_large_dataset(200)
        scaler = IsotonicScaler()
        scaler.fit(stances, labels)
        probe = [-0.9, -0.6, -0.3, 0.0, 0.3, 0.6, 0.9]
        probs = [scaler.predict_proba(s) for s in probe]
        for i in range(len(probs) - 1):
            assert probs[i] <= probs[i + 1] + 1e-9, (
                f"Non-monotone at index {i}: {probe[i]}->{probs[i]:.4f}, "
                f"{probe[i+1]}->{probs[i+1]:.4f}"
            )

    def test_positive_greater_than_negative(self) -> None:
        stances, labels = _make_large_dataset(200)
        scaler = IsotonicScaler()
        scaler.fit(stances, labels)
        assert scaler.predict_proba(0.8) >= scaler.predict_proba(-0.8)

    def test_not_fitted_raises(self) -> None:
        scaler = IsotonicScaler()
        with pytest.raises(RuntimeError, match="not been fitted"):
            scaler.predict_proba(0.3)

    def test_mismatched_lengths_raise(self) -> None:
        scaler = IsotonicScaler()
        with pytest.raises(ValueError, match="same length"):
            scaler.fit([0.1, 0.2], [1])

    def test_too_few_samples_raise(self) -> None:
        scaler = IsotonicScaler()
        with pytest.raises(ValueError):
            scaler.fit([0.5], [1])

    def test_single_class_raises(self) -> None:
        scaler = IsotonicScaler()
        with pytest.raises(ValueError):
            scaler.fit([0.1, 0.5, 0.9], [1, 1, 1])

    def test_extrapolation_clamped(self) -> None:
        """Predictions for stances outside training range should not raise."""
        stances = [0.1, 0.3, 0.5, -0.1, -0.3, -0.5]
        labels  = [  1,   1,   1,   -1,   -1,   -1]
        scaler = IsotonicScaler()
        scaler.fit(stances, labels)
        # 0.9 is outside training domain [−0.5, 0.5]
        prob = scaler.predict_proba(0.9)
        assert 0.0 <= prob <= 1.0

    def test_zero_labels_excluded(self) -> None:
        """No-call (0) labels are filtered before fitting."""
        stances = [0.8, -0.8, 0.0, 0.9, -0.9, 0.0]
        labels  = [  1,   -1,   0,   1,   -1,   0]
        scaler = IsotonicScaler()
        scaler.fit(stances, labels)
        assert scaler.is_fitted
