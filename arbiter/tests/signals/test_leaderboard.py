"""Tests for arbiter.signals.leaderboard — Lane 6.

Covers:
- Leaderboard renders both signal-type axis and person axis.
- Gate-failing rows are grayed / marked [GATE FAIL] (has samples, underperforms).
- Rows with 0 samples are marked [NO DATA] (distinct from [GATE FAIL]).
- All three SignalType values appear in signal-type section.
- Cold-start accuracy shown as '--' (zero samples).
- Empirical provider shows numeric accuracy.
- Person section shows placeholder when no person_ids provided.
- ANSI codes suppressed in plain mode.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from datetime import timedelta

from arbiter.signals.detection import SignalType
from arbiter.signals.leaderboard import (
    render_leaderboard,
    AdvisorPowerStat,
    net_dollar_expectancy,
    realized_lag_days,
)
from arbiter.signals.scoring import ColdStartProvider, _GATE_MIN_SAMPLES


_UTC = timezone.utc
_AS_OF = datetime(2026, 6, 1, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Stub empirical provider
# ---------------------------------------------------------------------------

class PartialEmpiricalProvider:
    """Provider where cluster_buy has data, others are cold."""

    def signal_type_score(self, signal_type, as_of, conn=None):
        if signal_type == SignalType.CLUSTER_BUY.value:
            return 0.65, 20  # gate passes
        return 0.55, 0  # cold

    def person_score(self, person_id, as_of, conn=None):
        if person_id == "P_KNOWN":
            return 0.70, 15  # gate passes
        return 0.52, 0  # cold


class UnderperformingProvider:
    """Provider where rows have samples but accuracy below gate threshold."""

    def signal_type_score(self, signal_type, as_of, conn=None):
        # Has 15 samples but 40% accuracy — below the 55% gate threshold.
        return 0.40, 15

    def person_score(self, person_id, as_of, conn=None):
        return 0.40, 15


# ---------------------------------------------------------------------------
# Signal-type axis
# ---------------------------------------------------------------------------

class TestSignalTypeAxis:
    def test_all_signal_types_appear(self):
        out = render_leaderboard(_AS_OF, plain=True)
        for st in SignalType:
            assert st.value in out, f"{st.value} not found in leaderboard"

    def test_cold_start_accuracy_shown_as_placeholder(self):
        out = render_leaderboard(_AS_OF, plain=True)
        # With cold-start (0 samples), accuracy should be shown as '--'.
        assert "--" in out

    def test_all_cold_start_rows_no_data(self):
        out = render_leaderboard(_AS_OF, plain=True)
        lines = out.splitlines()
        # Data rows are those containing a known SignalType value.
        signal_type_values = {st.value for st in SignalType}
        data_rows = [l for l in lines if any(v in l for v in signal_type_values)]
        assert data_rows, "no signal-type data rows found"
        # Every data row should show [NO DATA] (0 samples), never [GATE FAIL].
        for row in data_rows:
            assert "[NO DATA]" in row, f"expected [NO DATA] in: {row!r}"
            assert "[GATE FAIL]" not in row, f"unexpected [GATE FAIL] in: {row!r}"

    def test_gate_passing_row_not_marked_gate_fail(self):
        provider = PartialEmpiricalProvider()
        out = render_leaderboard(_AS_OF, plain=True, score_provider=provider)
        lines = out.splitlines()
        # Find the cluster_buy line.
        cluster_lines = [l for l in lines if SignalType.CLUSTER_BUY.value in l]
        assert cluster_lines, "cluster_buy line not found"
        # The cluster_buy line should NOT say [GATE FAIL].
        assert "[GATE FAIL]" not in cluster_lines[0]

    def test_other_rows_no_data_in_partial_provider(self):
        """congress_sector has 0 samples in the partial provider → [NO DATA]."""
        provider = PartialEmpiricalProvider()
        out = render_leaderboard(_AS_OF, plain=True, score_provider=provider)
        lines = out.splitlines()
        congress_lines = [l for l in lines if SignalType.CONGRESS_SECTOR.value in l]
        assert congress_lines
        assert "[NO DATA]" in congress_lines[0]
        assert "[GATE FAIL]" not in congress_lines[0]


# ---------------------------------------------------------------------------
# [NO DATA] vs [GATE FAIL] distinction
# ---------------------------------------------------------------------------

class TestNoDataVsGateFail:
    """Verify the semantic distinction between never-traded and underperformed."""

    def test_no_samples_shows_no_data_not_gate_fail(self):
        """0 samples → [NO DATA] in data rows, never [GATE FAIL] in data rows."""
        out = render_leaderboard(_AS_OF, plain=True)
        lines = out.splitlines()
        signal_type_values = {st.value for st in SignalType}
        data_rows = [l for l in lines if any(v in l for v in signal_type_values)]
        assert data_rows
        for row in data_rows:
            assert "[NO DATA]" in row, f"expected [NO DATA] in: {row!r}"
            assert "[GATE FAIL]" not in row, f"unexpected [GATE FAIL] in: {row!r}"

    def test_samples_but_low_accuracy_shows_gate_fail(self):
        """Has samples but accuracy < threshold → [GATE FAIL] in data rows, not [NO DATA]."""
        provider = UnderperformingProvider()
        out = render_leaderboard(_AS_OF, plain=True, score_provider=provider)
        lines = out.splitlines()
        signal_type_values = {st.value for st in SignalType}
        data_rows = [l for l in lines if any(v in l for v in signal_type_values)]
        assert data_rows
        for row in data_rows:
            assert "[GATE FAIL]" in row, f"expected [GATE FAIL] in: {row!r}"
            assert "[NO DATA]" not in row, f"unexpected [NO DATA] in: {row!r}"

    def test_gate_fail_for_underperforming_person(self):
        """Person with samples but low accuracy → [GATE FAIL]."""
        provider = UnderperformingProvider()
        out = render_leaderboard(
            _AS_OF, person_ids=["P_WEAK"], plain=True, score_provider=provider
        )
        lines = out.splitlines()
        person_lines = [l for l in lines if "P_WEAK" in l]
        assert person_lines
        assert "[GATE FAIL]" in person_lines[0]
        assert "[NO DATA]" not in person_lines[0]

    def test_no_data_for_person_with_zero_samples(self):
        """Person with 0 samples → [NO DATA]."""
        out = render_leaderboard(_AS_OF, person_ids=["P_NEW"], plain=True)
        lines = out.splitlines()
        person_lines = [l for l in lines if "P_NEW" in l]
        assert person_lines
        assert "[NO DATA]" in person_lines[0]
        assert "[GATE FAIL]" not in person_lines[0]


# ---------------------------------------------------------------------------
# Person axis
# ---------------------------------------------------------------------------

class TestPersonAxis:
    def test_no_person_ids_shows_placeholder(self):
        out = render_leaderboard(_AS_OF, person_ids=None, plain=True)
        assert "no persons tracked" in out.lower() or "lane 14" in out.lower()

    def test_empty_person_ids_shows_placeholder(self):
        out = render_leaderboard(_AS_OF, person_ids=[], plain=True)
        assert "no persons tracked" in out.lower() or "lane 14" in out.lower()

    def test_known_person_appears_in_output(self):
        provider = PartialEmpiricalProvider()
        out = render_leaderboard(
            _AS_OF, person_ids=["P_KNOWN"], plain=True, score_provider=provider
        )
        assert "P_KNOWN" in out

    def test_gate_passing_person_not_marked_fail(self):
        provider = PartialEmpiricalProvider()
        out = render_leaderboard(
            _AS_OF, person_ids=["P_KNOWN"], plain=True, score_provider=provider
        )
        lines = out.splitlines()
        person_lines = [l for l in lines if "P_KNOWN" in l]
        assert person_lines
        assert "[GATE FAIL]" not in person_lines[0]

    def test_cold_person_marked_no_data(self):
        """A person with 0 samples should show [NO DATA], not [GATE FAIL]."""
        out = render_leaderboard(
            _AS_OF, person_ids=["P_COLD"], plain=True
        )
        lines = out.splitlines()
        person_lines = [l for l in lines if "P_COLD" in l]
        assert person_lines
        assert "[NO DATA]" in person_lines[0]
        assert "[GATE FAIL]" not in person_lines[0]

    def test_multiple_persons_all_rendered(self):
        out = render_leaderboard(
            _AS_OF,
            person_ids=["P001", "P002", "P003"],
            plain=True,
        )
        for pid in ["P001", "P002", "P003"]:
            assert pid in out


# ---------------------------------------------------------------------------
# ANSI / formatting
# ---------------------------------------------------------------------------

class TestFormatting:
    def test_plain_mode_no_ansi_codes(self):
        out = render_leaderboard(_AS_OF, plain=True)
        assert "\033[" not in out

    def test_output_contains_both_section_headers(self):
        out = render_leaderboard(_AS_OF, plain=True)
        assert "Signal-Type" in out or "signal" in out.lower()
        assert "Person" in out or "person" in out.lower()

    def test_output_contains_gate_threshold_note(self):
        out = render_leaderboard(_AS_OF, plain=True)
        assert "Gate" in out or "gate" in out.lower()

    def test_output_is_string(self):
        out = render_leaderboard(_AS_OF, plain=True)
        assert isinstance(out, str)
        assert len(out) > 0

    def test_as_of_appears_in_header(self):
        out = render_leaderboard(_AS_OF, plain=True)
        assert "2026-06-01" in out

    def test_sample_column_present(self):
        out = render_leaderboard(_AS_OF, plain=True)
        assert "Samples" in out or "samples" in out.lower()

    def test_accuracy_column_present(self):
        out = render_leaderboard(_AS_OF, plain=True)
        assert "Accuracy" in out or "accuracy" in out.lower()


# ---------------------------------------------------------------------------
# B-STATS: advisor power / economic reporting (I1 + I2)
# ---------------------------------------------------------------------------

def _stat(advisor_id="A1.good", *, eff_n=55.0, lo=0.20, hi=0.60,
          graduated=True, net=0.0, lag=2.0):
    return AdvisorPowerStat(
        advisor_id=advisor_id, effective_n=eff_n, skill_ci_low=lo,
        skill_ci_high=hi, graduated=graduated, net_dollars=net,
        realized_lag_days=lag,
    )


class TestCompanionMetrics:
    def test_net_dollar_expectancy_subtracts_costs(self):
        assert net_dollar_expectancy([100.0, 50.0], costs=[30.0]) == 120.0

    def test_net_dollar_positive_alpha_zero_dollars(self):
        """Positive gross alpha eaten by costs lands near $0 — the I1 point."""
        assert abs(net_dollar_expectancy([100.0], costs=[100.0])) < 1e-9

    def test_realized_lag_mean_days(self):
        sigs = [_AS_OF, _AS_OF]
        fills = [_AS_OF + timedelta(days=2), _AS_OF + timedelta(days=4)]
        assert realized_lag_days(sigs, fills) == 3.0

    def test_realized_lag_empty_is_zero(self):
        assert realized_lag_days([], []) == 0.0


class TestMDEProperty:
    def test_mde_shrinks_with_effective_n(self):
        assert _stat(eff_n=100.0).mde < _stat(eff_n=10.0).mde


class TestAdvisorPowerSection:
    def test_no_section_without_stats(self):
        out = render_leaderboard(_AS_OF, plain=True)
        assert "Advisor Power" not in out

    def test_section_renders_with_stats(self):
        out = render_leaderboard(
            _AS_OF, plain=True, advisor_stats=[_stat()],
        )
        assert "Advisor Power" in out
        assert "A1.good" in out

    def test_power_columns_present(self):
        out = render_leaderboard(_AS_OF, plain=True, advisor_stats=[_stat()])
        # MDE, skill CI, net-dollar, lag headers all surface.
        assert "MDE" in out
        assert "CI" in out
        assert "Net $" in out or "Net" in out
        assert "Lag" in out

    def test_graduated_advisor_marked_graduated(self):
        out = render_leaderboard(_AS_OF, plain=True,
                                 advisor_stats=[_stat(graduated=True)])
        line = [l for l in out.splitlines() if "A1.good" in l][0]
        assert "[GRADUATED]" in line

    def test_null_advisor_marked_shadow(self):
        out = render_leaderboard(
            _AS_OF, plain=True,
            advisor_stats=[_stat("A1.null", lo=-0.20, hi=0.10, graduated=False)],
        )
        line = [l for l in out.splitlines() if "A1.null" in l][0]
        assert "[SHADOW]" in line

    def test_positive_alpha_zero_dollar_visible(self):
        """A positive-skill / ~$0 advisor shows the $0 in the report."""
        out = render_leaderboard(
            _AS_OF, plain=True,
            advisor_stats=[_stat("A1.good", lo=0.2, hi=0.6, net=0.0)],
        )
        line = [l for l in out.splitlines() if "A1.good" in l][0]
        assert "[GRADUATED]" in line
        # the net-dollar column shows 0 even though skill CI is positive
        assert " 0 " in line or line.rstrip().endswith("0  [GRADUATED]") or "  0  " in line

    def test_plain_mode_no_ansi_in_power_section(self):
        out = render_leaderboard(_AS_OF, plain=True, advisor_stats=[_stat()])
        assert "\033[" not in out
