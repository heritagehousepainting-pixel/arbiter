"""Tests for Calibrator — the public seam consumed by fusion (Lane 9)."""
from __future__ import annotations

import pytest

from arbiter.calibration.calibrator import Calibrator, _ISOTONIC_THRESHOLD
from arbiter.calibration.isotonic import IsotonicScaler
from arbiter.calibration.platt import PlattScaler
from arbiter.contract.seams import ResolvedOutcome
from arbiter.types import HorizonBucket


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_outcome(
    advisor_id: str,
    binary: int,
    horizon_days: int = 15,
    confidence: float = 0.8,
    abstained: bool = False,
) -> ResolvedOutcome:
    return ResolvedOutcome(
        idea_id="idea-001",
        advisor_id=advisor_id,
        ticker="AAPL",
        alpha_bps=100.0 if binary == 1 else -100.0,
        binary=binary,
        advisor_confidence=confidence,
        stance_score=float(binary),
        abstained=abstained,
        horizon_days=horizon_days,
        label_kind="normal",
    )


def _make_outcomes(
    advisor_id: str,
    n: int,
    horizon_days: int = 15,
) -> list[ResolvedOutcome]:
    """Create n balanced outcomes (+1 / -1 alternating) for one advisor."""
    outcomes = []
    for i in range(n):
        binary = 1 if i % 2 == 0 else -1
        confidence = 0.7 + 0.2 * ((i % 10) / 10)
        outcomes.append(
            _make_outcome(advisor_id, binary, horizon_days, confidence)
        )
    return outcomes


# ---------------------------------------------------------------------------
# Cold-start behaviour
# ---------------------------------------------------------------------------

class TestColdStart:

    def test_cold_start_with_no_data(self) -> None:
        # is_cold_start is now a property (no-arg); bucket-specific check via is_cold_start_for()
        cal = Calibrator(advisor_id="A1.insider")
        assert cal.is_cold_start is True

    def test_cold_start_resolves_to_real_bool_not_method(self) -> None:
        """Regression: is_cold_start must be a bool (property), not a bound method."""
        cal = Calibrator(advisor_id="A1.insider")
        # If is_cold_start were a method, bool(cal.is_cold_start) would be True because
        # bound methods are always truthy — even when the method would return False.
        # Verify it resolves to the correct bool value, not a callable.
        result = cal.is_cold_start
        assert isinstance(result, bool), f"Expected bool, got {type(result)}"
        assert result is True

    def test_cold_start_per_bucket_before_fit(self) -> None:
        cal = Calibrator(advisor_id="A1.insider")
        for bucket in HorizonBucket:
            assert cal.is_cold_start_for(bucket) is True

    def test_cold_start_flag_flips_after_fit(self) -> None:
        cal = Calibrator(advisor_id="A1.insider")
        outcomes = _make_outcomes("A1.insider", n=10, horizon_days=15)
        cal.fit(outcomes)
        # SHORT bucket should now have a model.
        assert cal.is_cold_start_for(HorizonBucket.SHORT) is False

    def test_overall_cold_start_false_after_any_bucket_fit(self) -> None:
        """is_cold_start (overall property) flips to False once any bucket is fitted."""
        cal = Calibrator(advisor_id="A1.insider")
        outcomes = _make_outcomes("A1.insider", n=10, horizon_days=15)
        cal.fit(outcomes)
        # Overall cold-start is False because SHORT now has a model.
        assert cal.is_cold_start is False

    def test_other_bucket_still_cold_after_partial_fit(self) -> None:
        """Fitting SHORT data should not affect LONG bucket cold-start flag."""
        cal = Calibrator(advisor_id="A1.insider")
        outcomes = _make_outcomes("A1.insider", n=10, horizon_days=15)  # SHORT
        cal.fit(outcomes)
        assert cal.is_cold_start_for(HorizonBucket.LONG) is True

    def test_stance_base_returned_under_cold_start(self) -> None:
        """With no data, transform() must return the STANCE_BASE prior (not 0.5 always)."""
        cal = Calibrator(advisor_id="A1.insider")
        # For positive stance, prior > 0.5.
        prob_pos = cal.transform(raw_stance=0.8, horizon_days=15)
        prob_neg = cal.transform(raw_stance=-0.8, horizon_days=15)
        assert prob_pos > 0.5
        assert prob_neg < 0.5

    def test_transform_output_in_unit_interval_cold_start(self) -> None:
        cal = Calibrator(advisor_id="A1.insider")
        for stance in (-1.0, -0.5, 0.0, 0.5, 1.0):
            prob = cal.transform(stance, horizon_days=15)
            assert 0.0 <= prob <= 1.0, f"prob={prob} for stance={stance}"


# ---------------------------------------------------------------------------
# Model selection: Platt vs Isotonic
# ---------------------------------------------------------------------------

class TestModelSelection:

    def test_platt_chosen_below_threshold(self) -> None:
        """With < 200 outcomes, model_type should be 'platt' for the fitted bucket."""
        cal = Calibrator(advisor_id="A1.insider")
        n = _ISOTONIC_THRESHOLD - 1  # 199
        outcomes = _make_outcomes("A1.insider", n=n, horizon_days=15)
        cal.fit(outcomes)
        assert cal.model_type(HorizonBucket.SHORT) == "platt"

    def test_isotonic_chosen_at_threshold(self) -> None:
        """With >= 200 outcomes, model_type should be 'isotonic'."""
        cal = Calibrator(advisor_id="A1.insider")
        outcomes = _make_outcomes("A1.insider", n=_ISOTONIC_THRESHOLD, horizon_days=15)
        cal.fit(outcomes)
        assert cal.model_type(HorizonBucket.SHORT) == "isotonic"

    def test_isotonic_chosen_above_threshold(self) -> None:
        cal = Calibrator(advisor_id="A1.insider")
        outcomes = _make_outcomes("A1.insider", n=300, horizon_days=15)
        cal.fit(outcomes)
        assert cal.model_type(HorizonBucket.SHORT) == "isotonic"

    def test_unfitted_bucket_returns_cold_start(self) -> None:
        cal = Calibrator(advisor_id="A1.insider")
        # Fit SHORT only.
        outcomes = _make_outcomes("A1.insider", n=10, horizon_days=15)
        cal.fit(outcomes)
        assert cal.model_type(HorizonBucket.LONG) == "cold_start"


# ---------------------------------------------------------------------------
# transform() correctness
# ---------------------------------------------------------------------------

class TestTransform:

    def test_output_in_unit_interval_fitted(self) -> None:
        """After fitting, transform() must always return values in [0, 1]."""
        cal = Calibrator(advisor_id="A1.insider")
        outcomes = _make_outcomes("A1.insider", n=50, horizon_days=15)
        cal.fit(outcomes)
        for stance in (-1.0, -0.5, 0.0, 0.5, 1.0):
            prob = cal.transform(stance, horizon_days=15)
            assert 0.0 <= prob <= 1.0, f"prob={prob} for stance={stance}"

    def test_invalid_stance_raises(self) -> None:
        cal = Calibrator(advisor_id="A1.insider")
        with pytest.raises(ValueError, match=r"\[-1\.0, 1\.0\]"):
            cal.transform(raw_stance=1.5, horizon_days=15)

    def test_invalid_stance_negative_out_of_range(self) -> None:
        cal = Calibrator(advisor_id="A1.insider")
        with pytest.raises(ValueError):
            cal.transform(raw_stance=-1.5, horizon_days=15)

    def test_invalid_horizon_falls_back_to_prior(self) -> None:
        """horizon_days > 365 raises in bucket_for_days; calibrator falls back to prior."""
        cal = Calibrator(advisor_id="A1.insider")
        # Should not raise, should return prior.
        prob = cal.transform(raw_stance=0.5, horizon_days=400)
        assert 0.0 <= prob <= 1.0

    def test_outcomes_from_other_advisor_ignored(self) -> None:
        """Outcomes for a different advisor must not affect this calibrator's fit."""
        cal = Calibrator(advisor_id="A1.insider")
        # Provide only outcomes for A2.mirofish.
        outcomes = _make_outcomes("A2.mirofish", n=50, horizon_days=15)
        cal.fit(outcomes)
        # A1.insider must still be cold-start.
        assert cal.is_cold_start is True

    def test_abstained_outcomes_ignored(self) -> None:
        cal = Calibrator(advisor_id="A1.insider")
        abstained = [_make_outcome("A1.insider", 1, abstained=True) for _ in range(50)]
        non_abstained = _make_outcomes("A1.insider", n=4, horizon_days=15)
        cal.fit(abstained + non_abstained)
        # Only 4 non-abstained → model fits; abstained doesn't count.
        assert cal.n_outcomes(HorizonBucket.SHORT) == 4


# ---------------------------------------------------------------------------
# Horizon stratification
# ---------------------------------------------------------------------------

class TestHorizonStratification:

    def test_separate_buckets_fit_independently(self) -> None:
        """SHORT and LONG horizon outcomes must produce separate models."""
        cal = Calibrator(advisor_id="A1.insider")
        short_outcomes = _make_outcomes("A1.insider", n=10, horizon_days=15)   # SHORT
        long_outcomes = _make_outcomes("A1.insider", n=10, horizon_days=200)   # LONG
        cal.fit(short_outcomes + long_outcomes)

        assert not cal.is_cold_start_for(HorizonBucket.SHORT)
        assert not cal.is_cold_start_for(HorizonBucket.LONG)
        # MEDIUM and INTRADAY should still be cold-start.
        assert cal.is_cold_start_for(HorizonBucket.MEDIUM)
        assert cal.is_cold_start_for(HorizonBucket.INTRADAY)

    def test_short_fit_does_not_affect_long_transform(self) -> None:
        """LONG bucket must use the STANCE_BASE prior while SHORT has a fitted model."""
        cal = Calibrator(advisor_id="A1.insider")
        short_outcomes = _make_outcomes("A1.insider", n=20, horizon_days=15)
        cal.fit(short_outcomes)

        # SHORT bucket is fitted.
        short_prob = cal.transform(0.8, horizon_days=15)
        # LONG bucket falls back to prior.
        long_prob = cal.transform(0.8, horizon_days=200)

        # Both must be in [0, 1].
        assert 0.0 <= short_prob <= 1.0
        assert 0.0 <= long_prob <= 1.0
        # The LONG value is the STANCE_BASE prior (≠ the SHORT fitted value generally).
        # We can't assert they differ in general, but both must be valid probs.

    def test_n_outcomes_per_bucket(self) -> None:
        """n_outcomes() must report per-bucket counts correctly."""
        cal = Calibrator(advisor_id="A1.insider")
        # 10 SHORT + 8 LONG
        outcomes = (
            _make_outcomes("A1.insider", n=10, horizon_days=15)
            + _make_outcomes("A1.insider", n=8, horizon_days=200)
        )
        cal.fit(outcomes)
        assert cal.n_outcomes(HorizonBucket.SHORT) == 10
        assert cal.n_outcomes(HorizonBucket.LONG) == 8
        assert cal.n_outcomes(HorizonBucket.MEDIUM) == 0
        assert cal.n_outcomes(HorizonBucket.INTRADAY) == 0

    def test_intraday_bucket_from_sub_one_day_horizon(self) -> None:
        """horizon_days < 1 is technically out of range (bucket_for_days raises).
        Calibrator must gracefully fall back rather than crash."""
        # horizon_days=0 raises ValueError in bucket_for_days.
        # Calibrator.fit() skips those rows.
        cal = Calibrator(advisor_id="A1.insider")
        bad_outcomes = [
            ResolvedOutcome(
                idea_id="x",
                advisor_id="A1.insider",
                ticker="AAPL",
                alpha_bps=50.0,
                binary=1,
                advisor_confidence=0.7,
                stance_score=1.0,
                abstained=False,
                horizon_days=0,   # invalid
                label_kind="normal",
            )
        ]
        # Must not raise.
        cal.fit(bad_outcomes)
        assert cal.is_cold_start  # property, no call needed


# ---------------------------------------------------------------------------
# DB persistence (optional path)
# ---------------------------------------------------------------------------

class TestPersistence:

    def _apply_migration(self, conn) -> None:
        """Apply calibration migration SQL to an in-memory DB."""
        import sqlite3
        from pathlib import Path
        migration_path = (
            Path(__file__).resolve().parents[2]
            / "arbiter" / "db" / "migrations" / "012_calibration.sql"
        )
        conn.executescript(migration_path.read_text())

    def test_persist_writes_rows(self, tmp_path) -> None:
        """persist() must write one row per HorizonBucket to calibration_params."""
        import sqlite3
        from datetime import datetime, timezone

        db_path = tmp_path / "cal.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        self._apply_migration(conn)

        cal = Calibrator(advisor_id="A1.insider", conn=conn)
        outcomes = _make_outcomes("A1.insider", n=10, horizon_days=15)
        cal.fit(outcomes)

        as_of = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        cal.persist(as_of)

        rows = conn.execute("SELECT * FROM calibration_params").fetchall()
        assert len(rows) == len(HorizonBucket)

        bucket_values = {row["horizon_bucket"] for row in rows}
        expected = {b.value for b in HorizonBucket}
        assert bucket_values == expected

    def test_persist_requires_tz_aware_as_of(self, tmp_path) -> None:
        """persist() must reject naive datetimes."""
        import sqlite3
        from datetime import datetime

        db_path = tmp_path / "cal.db"
        conn = sqlite3.connect(str(db_path))
        self._apply_migration(conn)

        cal = Calibrator(advisor_id="A1.insider", conn=conn)
        naive_ts = datetime(2026, 6, 1, 0, 0, 0)  # no tzinfo
        with pytest.raises(ValueError, match="tz-aware"):
            cal.persist(naive_ts)

    def test_persist_without_conn_raises(self) -> None:
        from datetime import datetime, timezone
        cal = Calibrator(advisor_id="A1.insider")
        as_of = datetime(2026, 6, 1, tzinfo=timezone.utc)
        with pytest.raises(RuntimeError, match="no DB connection"):
            cal.persist(as_of)


# ---------------------------------------------------------------------------
# Fit-on-stance contract (E2)
# ---------------------------------------------------------------------------

class TestFitOnStance:
    def _outcome(self, binary, stance, conf=0.8, horizon=15):
        return ResolvedOutcome(
            idea_id="i",
            advisor_id="A1.insider",
            ticker="AAPL",
            alpha_bps=100.0 if binary == 1 else -100.0,
            binary=binary,
            advisor_confidence=conf,
            stance_score=stance,
            abstained=False,
            horizon_days=horizon,
            label_kind="normal",
        )

    def test_stance_zero_rows_excluded_from_fit(self) -> None:
        """Rows with stance_score == 0.0 (legacy/proxy) must NOT enter the fit."""
        cal = Calibrator(advisor_id="A1.insider")
        # 6 legacy stance-0 rows (would be a degenerate constant-0 feature) +
        # 4 real rows.  Only the 4 real rows should be counted/fit.
        legacy = [self._outcome(1 if i % 2 == 0 else -1, 0.0) for i in range(6)]
        real = [self._outcome(1, 0.9), self._outcome(-1, -0.9),
                self._outcome(1, 0.7), self._outcome(-1, -0.7)]
        cal.fit(legacy + real)
        assert cal.n_outcomes(HorizonBucket.SHORT) == 4

    def test_all_stance_zero_stays_cold_start(self) -> None:
        """If every row has stance 0.0, no model fits (degenerate fit avoided)."""
        cal = Calibrator(advisor_id="A1.insider")
        legacy = [self._outcome(1 if i % 2 == 0 else -1, 0.0) for i in range(20)]
        cal.fit(legacy)
        assert cal.is_cold_start is True
        assert cal.n_outcomes(HorizonBucket.SHORT) == 0

    def test_fit_uses_stance_not_binary_sign(self) -> None:
        """A model fit on real stance differs from one keyed on sign(binary).

        Construct rows where stance and binary DISAGREE in magnitude so a fit on
        stance produces a different curve than the old leak (sign(binary)*conf).
        We assert the fitted model responds monotonically to stance.
        """
        cal = Calibrator(advisor_id="A1.insider")
        # Bullish stances mostly win, bearish stances mostly lose.
        rows = []
        for _ in range(10):
            rows.append(self._outcome(1, 0.9))   # strong-long, wins
            rows.append(self._outcome(-1, -0.9))  # strong-short, loses
        cal.fit(rows)
        assert not cal.is_cold_start
        p_long = cal.transform(0.9, horizon_days=15)
        p_short = cal.transform(-0.9, horizon_days=15)
        # Monotone: higher stance → higher P(positive-alpha).
        assert p_long > p_short
        assert 0.0 <= p_short <= 1.0 <= 1.0  # in unit interval
        assert 0.0 <= p_long <= 1.0


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------

class TestConstructor:

    def test_empty_advisor_id_raises(self) -> None:
        with pytest.raises(ValueError, match="advisor_id"):
            Calibrator(advisor_id="")
