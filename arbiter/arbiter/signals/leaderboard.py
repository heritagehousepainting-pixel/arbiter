"""CLI leaderboard for A1 signal-types and people — Lane 6.

Renders a two-section table:
1. Signal-type accuracy (rows: cluster_buy, single_insider_buy, congress_sector).
2. Person accuracy (per-insider / per-Congress-member).

Gate-failing rows are grayed in the output using ANSI escape codes (or marked
with ``[GATE FAIL]`` in plain-text mode for non-TTY / test output).

Accuracy is placeholder (``--`` / cold-start prior) until Lane 14 feeds real
outcome numbers.

Design rules (INTERFACES.md §11)
---------------------------------
- No ``datetime.now()``.  Callers pass ``as_of``.
- No imports from ingest lane or other in-progress lanes.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from arbiter.signals.detection import SignalType
from arbiter.trust.ledger import minimum_detectable_effect
from arbiter.signals.scoring import (
    ScoreBundle,
    ScoreProvider,
    ColdStartProvider,
    _GATE_MIN_SAMPLES,
    _GATE_MIN_ACCURACY,
    score_signal_type,
    score_person,
)

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

_ANSI_GRAY = "\033[90m"
_ANSI_RESET = "\033[0m"
_ANSI_BOLD = "\033[1m"


def _use_color() -> bool:
    """Return True if we should emit ANSI codes (TTY only)."""
    return sys.stdout.isatty()


def _gray(text: str, *, plain: bool = False) -> str:
    if plain or not _use_color():
        return text
    return f"{_ANSI_GRAY}{text}{_ANSI_RESET}"


def _bold(text: str, *, plain: bool = False) -> str:
    if plain or not _use_color():
        return text
    return f"{_ANSI_BOLD}{text}{_ANSI_RESET}"


# ---------------------------------------------------------------------------
# Row dataclasses
# ---------------------------------------------------------------------------

@dataclass
class LeaderboardRow:
    """One row in the leaderboard."""

    label: str          # signal-type name or person_id
    samples: int
    accuracy: float | None  # None = cold-start
    gate_pass: bool
    extra: str = ""     # optional detail (e.g. person name, source)


# ---------------------------------------------------------------------------
# Advisor power / economic reporting (I1 + I2)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AdvisorPowerStat:
    """Per-advisor statistical-power + economic companion metrics.

    Surfaces what a bare accuracy/count hides (I2): how much skill could even be
    DETECTED at the current effective-n, the real bootstrap skill CI that gates
    graduation, plus the economic companions (I1) — net dollars earned and the
    realized lag — so a "positive alpha / ~$0 dollar" advisor is visible at a
    glance instead of looking healthy on accuracy alone.

    All inputs are passed in (computed in the trust lane); this module only
    renders.  No datetime.now() — any timing is already baked into the stats.
    """

    advisor_id: str
    effective_n: float          # decay-weighted Kish n (power input)
    skill_ci_low: float         # bootstrap CI lower bound on BSS (can be <0)
    skill_ci_high: float        # bootstrap CI upper bound on BSS
    graduated: bool             # passed the significance/effective-n gate
    net_dollars: float          # net $ expectancy realized by this advisor [I1]
    realized_lag_days: float    # mean entry lag (signal → fill) in days [I1]

    @property
    def mde(self) -> float:
        """Minimum detectable effect (BSS scale) at this advisor's effective-n."""
        return minimum_detectable_effect(self.effective_n)


def net_dollar_expectancy(
    realized_pnl: Sequence[float],
    costs: Sequence[float] | None = None,
) -> float:
    """Net-dollar expectancy companion metric [I1].

    Σ realized P&L − Σ costs (slippage/fees).  A genuinely-positive-alpha advisor
    whose edge is eaten by costs lands near $0 here — the number the leaderboard
    surfaces so "positive alpha / ~$0 dollar" is not invisible.
    """
    gross = float(sum(realized_pnl))
    fees = float(sum(costs)) if costs is not None else 0.0
    return gross - fees


def realized_lag_days(
    signal_dates: Sequence[datetime],
    fill_dates: Sequence[datetime],
) -> float:
    """Mean realized lag (signal → fill) in days [I1].

    Companion metric: alpha measured at the signal date is unrealizable if fills
    lag by days.  Returns 0.0 when there are no paired dates.  No datetime.now()
    — both date sequences are supplied by the caller.
    """
    pairs = [
        (f - s).total_seconds() / 86400.0
        for s, f in zip(signal_dates, fill_dates)
    ]
    if not pairs:
        return 0.0
    return sum(pairs) / len(pairs)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_leaderboard(
    as_of: datetime,
    *,
    person_ids: Sequence[str] | None = None,
    score_provider: ScoreProvider = ColdStartProvider(),
    advisor_stats: Sequence[AdvisorPowerStat] | None = None,
    plain: bool = False,
    width: int = 72,
) -> str:
    """Render the leaderboard as a formatted string.

    Parameters
    ----------
    as_of:
        Information timestamp.
    person_ids:
        Optional list of person IDs to include in the people section.
        If ``None``, an empty placeholder row is shown.
    score_provider:
        Score provider (defaults to cold-start).
    plain:
        If True, emit plain ASCII without ANSI codes (useful for tests /
        log files / non-TTY CI output).
    width:
        Target line width for the table header separator.

    Returns
    -------
    Formatted string ready to print to stdout.
    """
    lines: list[str] = []
    sep = "-" * width

    header = _bold(f"  A1 Signal Leaderboard  —  as_of={as_of.isoformat()}", plain=plain)
    lines.append(sep)
    lines.append(header)
    lines.append(sep)

    # -----------------------------------------------------------------------
    # Section 1: Signal-type accuracy
    # -----------------------------------------------------------------------
    lines.append(_bold("Signal-Type Axis", plain=plain))
    col_header = f"  {'Signal Type':<28}  {'Samples':>7}  {'Accuracy':>9}  Status"
    lines.append(col_header)
    lines.append("  " + "-" * (width - 2))

    for st in SignalType:
        accuracy, samples, gate_pass = score_signal_type(
            st.value, as_of, score_provider=score_provider
        )
        row = _format_signal_type_row(st.value, accuracy, samples, gate_pass, plain=plain)
        lines.append(row)

    lines.append("")

    # -----------------------------------------------------------------------
    # Section 2: Person accuracy
    # -----------------------------------------------------------------------
    lines.append(_bold("Person Axis", plain=plain))
    col_header = f"  {'Person ID':<28}  {'Samples':>7}  {'Accuracy':>9}  Status"
    lines.append(col_header)
    lines.append("  " + "-" * (width - 2))

    if not person_ids:
        placeholder = _gray(
            "  (no persons tracked yet — Lane 14 will populate this)",
            plain=plain,
        )
        lines.append(placeholder)
    else:
        for pid in person_ids:
            accuracy, samples, gate_pass = score_person(
                pid, as_of, score_provider=score_provider
            )
            row = _format_person_row(pid, accuracy, samples, gate_pass, plain=plain)
            lines.append(row)

    # -----------------------------------------------------------------------
    # Section 3: Advisor power / economic axis (I1 + I2)
    # -----------------------------------------------------------------------
    if advisor_stats:
        lines.append("")
        lines.append(_bold("Advisor Power & Economics", plain=plain))
        col_header = (
            f"  {'Advisor':<16}  {'EffN':>5}  {'MDE':>6}  "
            f"{'Skill CI':>15}  {'Net $':>9}  {'Lag(d)':>6}  Status"
        )
        lines.append(col_header)
        lines.append("  " + "-" * (width - 2))
        for st in advisor_stats:
            lines.append(_format_advisor_power_row(st, plain=plain))

    lines.append(sep)
    lines.append(
        _gray(
            f"  Gate thresholds: samples>={_GATE_MIN_SAMPLES}, "
            f"accuracy>={_GATE_MIN_ACCURACY:.0%}  |  [NO DATA] = never traded  |  [GATE FAIL] = underperformed",
            plain=plain,
        )
    )
    if advisor_stats:
        lines.append(
            _gray(
                "  EffN = decay-weighted effective n  |  MDE = min detectable skill "
                "at this n  |  Skill CI = bootstrap 90% CI on BSS  |  "
                "[GRADUATED] = CI>0 & EffN ok  |  [SHADOW] = not yet significant",
                plain=plain,
            )
        )
    lines.append("")

    return "\n".join(lines)


def _format_advisor_power_row(st: AdvisorPowerStat, *, plain: bool = False) -> str:
    mde = st.mde
    mde_str = "inf" if mde == float("inf") else f"{mde:.3f}"
    ci_str = f"[{st.skill_ci_low:+.2f},{st.skill_ci_high:+.2f}]"
    status = "[GRADUATED]" if st.graduated else "[SHADOW]"
    row = (
        f"  {st.advisor_id[:16]:<16}  {st.effective_n:>5.1f}  {mde_str:>6}  "
        f"{ci_str:>15}  {st.net_dollars:>9.0f}  {st.realized_lag_days:>6.1f}  {status}"
    )
    if not st.graduated:
        row = _gray(row, plain=plain)
    return row


def _format_accuracy(accuracy: float | None, samples: int) -> str:
    if accuracy is None or samples == 0:
        return "--"
    return f"{accuracy:.1%}"


def _format_signal_type_row(
    signal_type: str,
    accuracy: float,
    samples: int,
    gate_pass: bool,
    *,
    plain: bool = False,
) -> str:
    acc_str = _format_accuracy(accuracy if samples > 0 else None, samples)
    if gate_pass:
        status = "OK"
    elif samples == 0:
        status = "[NO DATA]"
    else:
        status = "[GATE FAIL]"
    row = f"  {signal_type:<28}  {samples:>7}  {acc_str:>9}  {status}"
    if not gate_pass:
        row = _gray(row, plain=plain)
    return row


def _format_person_row(
    person_id: str,
    accuracy: float,
    samples: int,
    gate_pass: bool,
    *,
    plain: bool = False,
) -> str:
    acc_str = _format_accuracy(accuracy if samples > 0 else None, samples)
    if gate_pass:
        status = "OK"
    elif samples == 0:
        status = "[NO DATA]"
    else:
        status = "[GATE FAIL]"
    label = person_id[:28]
    row = f"  {label:<28}  {samples:>7}  {acc_str:>9}  {status}"
    if not gate_pass:
        row = _gray(row, plain=plain)
    return row
