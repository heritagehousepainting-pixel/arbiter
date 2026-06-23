"""Outcome labeler — Lane 14a keystone.

Labels a closed idea with a SPY-beta-adjusted alpha (continuous, in basis points)
and a ±25bps binary signal for display/calibration.

Formula (INTERFACES.md §6 / design spec §4.1):
    alpha_i = r_i(t0, t1) − beta_i × r_SPY(t0, t1)

Where (E5 — beta convention is FROZEN to LOG space, end-to-end):
    - r_i(t0, t1)   = log(exit_price / entry_price) = log(1 + R_i)  (LOG return)
    - r_SPY(t0, t1) = same LOG formula for SPY
    - beta_i        = 252-day rolling OLS beta as of t0−1, fit on daily LOG
                      returns (see data/beta.py), impute 1.0 + flag

beta is FIT on log returns (data/beta.py) and is now APPLIED to log returns here.
Previously the labeler applied a log-space beta to SIMPLE returns, which mixed
conventions and LEAKED market direction into alpha (E5).  ``alpha_bps`` is still
the SAME scalar ⇒ small numeric churn vs the old simple-return formula; the sign
and the calibration/Brier consumers are unaffected in convention.
    - entry_price   = filing-date+1 OPEN, net modeled slippage
    - alpha_bps     = alpha_i × 10_000 (continuous; drives trust)
    - binary        = +1 if alpha_bps > +25, −1 if < −25, else 0 ("no-call")

label_kind variants:
    "normal"           — full horizon reached, clean entry/exit
    "early_exit"       — position closed before horizon expiry
    "reversal"         — conviction flip triggered exit
    "corporate_event"  — halted by M&A, delisting, trading halt, etc.
    "partial"          — partial fill / data gap

All prices come via PIT (no look-ahead). No datetime.now() calls.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import numpy as np

from arbiter.contract.seams import Idea, ResolvedOutcome
from arbiter.data.beta import beta_252d
from arbiter.data.pit import PITGateway
from arbiter.data.replay_clock import iter_trading_days
from arbiter.data.slippage import model_slippage

_logger = logging.getLogger(__name__)

# Binary threshold in basis points (INTERFACES.md §6)
_BINARY_THRESHOLD_BPS: float = 25.0

# Valid label kinds
LABEL_KINDS = frozenset({
    "normal",
    "early_exit",
    "reversal",
    "corporate_event",
    "partial",
})

_SPY = "SPY"


def label(
    idea: Idea,
    *,
    pit: PITGateway,
    cutoff_as_of: datetime,
    advisor_id: str,
    advisor_confidence: float,
    stance_score: float = 0.0,
    exit_price: float | None = None,
    exit_as_of: datetime | None = None,
    label_kind: str = "normal",
    abstained: bool = False,
    spread: float = 0.0,
) -> ResolvedOutcome:
    """Compute and return the SPY-beta-adjusted outcome label for *idea*.

    Parameters
    ----------
    idea:
        The closed ``Idea`` object.  ``idea.as_of`` is t0 (the filing date /
        original information timestamp).
    pit:
        Point-in-time gateway — the ONLY source of prices.  No look-ahead.
    cutoff_as_of:
        Information timestamp representing "now" in the backtest or live run.
        Used as the upper bound for PIT reads (never as wall-clock).
        Previously named ``clock`` — renamed to clarify it is a datetime, not
        a Clock object.
    advisor_id:
        The advisor being evaluated.
    advisor_confidence:
        The confidence value from the original opinion.
    stance_score:
        The advisor's ACTUAL directional forecast in [-1, 1] from the original
        opinion (#5a).  Pure metadata carried onto the outcome so the Brier
        scores against the real stance; touches no price read.  Defaults to 0.0
        (neutral) for the legacy / no-opinion fallback path.
    exit_price:
        Explicit exit price override (e.g. for early_exit / corporate_event).
        If ``None``, the labeler reads ``price_close`` at ``exit_as_of`` from PIT.
    exit_as_of:
        Timestamp of the exit price observation.  Defaults to the next trading
        day on or after ``idea.as_of + horizon_days`` (standard horizon expiry).
        If this falls on a weekend/holiday, it is advanced to the next trading
        day to avoid silent use of a prior day's close as a stand-in.
    label_kind:
        One of LABEL_KINDS.  Defaults to ``"normal"``.
    abstained:
        True when the advisor abstained on this idea.  Produces alpha_bps=0.0,
        binary=0.
    spread:
        Bid-ask spread in price units for slippage calculation.  Defaults to 0.0
        when unavailable (conservative under-estimate of slippage cost).

    Returns
    -------
    ResolvedOutcome
        Frozen outcome dataclass ready for storage and downstream use.

    Raises
    ------
    ValueError
        If label_kind is not one of LABEL_KINDS.
    LookupError
        If a required price cannot be found via PIT as of the given timestamp.
    """
    if label_kind not in LABEL_KINDS:
        raise ValueError(
            f"label_kind must be one of {sorted(LABEL_KINDS)!r}, got {label_kind!r}"
        )

    # Abstained opinions: return a zero-alpha, no-call outcome immediately.
    if abstained:
        return ResolvedOutcome(
            idea_id=idea.idea_id,
            advisor_id=advisor_id,
            ticker=idea.ticker,
            alpha_bps=0.0,
            binary=0,
            advisor_confidence=advisor_confidence,
            stance_score=stance_score,
            abstained=True,
            horizon_days=idea.horizon_days,
            label_kind=label_kind,
        )

    # --- t0: the information date (filing date / idea.as_of) ---
    t0: datetime = idea.as_of

    # --- entry: next TRADING day after t0, OPEN, net modeled slippage ---
    # Advance past t0 to the next trading day.  If t0+1 calendar day falls
    # on a weekend or holiday, the NYSE would have been closed; using the
    # prior bar's open as the entry price would under-state the lag and
    # inflate returns.  We advance to the next trading day instead.
    t1_entry = _next_trading_day(t0)
    # Bound the entry read by cutoff_as_of too (no-look-ahead): the exit read was
    # already clamped, but entry + beta were only half-bounded.  An entry/beta
    # read past the information cutoff would leak future data.  Clamp the as_of
    # passed to PIT — the PIT gateway still raises LookupError if the (clamped)
    # bar is unavailable, preserving the leave-MONITORED-and-retry behavior.
    effective_entry_as_of = min(t1_entry, cutoff_as_of)
    entry_open = _get_price_open(idea.ticker, effective_entry_as_of, pit)
    slippage_adjusted_entry = model_slippage(entry_open, spread)

    # --- exit: t1 CLOSE (default) or override ---
    if exit_as_of is None:
        # Default horizon end: advance to next trading day if the calendar
        # date t0+horizon_days is a non-trading day.
        raw_exit = t0 + timedelta(days=idea.horizon_days)
        exit_as_of = _on_or_next_trading_day(raw_exit)

    # Guard: never read beyond cutoff_as_of (PIT look-ahead guard)
    effective_exit_as_of = min(exit_as_of, cutoff_as_of)

    if exit_price is not None:
        ticker_exit = exit_price
    else:
        ticker_exit = _get_price_close(idea.ticker, effective_exit_as_of, pit)

    spy_entry_open = _get_price_open(_SPY, effective_entry_as_of, pit)
    spy_exit = _get_price_close(_SPY, effective_exit_as_of, pit)

    # --- beta: 252-day rolling as of t0−1 ---
    # Bound the beta as_of by cutoff_as_of too (no-look-ahead): beta_252d reads
    # a price window ending at its as_of, so a beta as_of past the cutoff would
    # leak future bars into the regression.  t0−1 is normally well before the
    # cutoff, but clamp defensively to keep every PIT read uniformly bounded.
    beta_as_of = min(t0 - timedelta(days=1), cutoff_as_of)
    beta_i, beta_imputed = _get_beta_safe(idea.ticker, beta_as_of, pit)

    if beta_imputed:
        _logger.warning(
            "outcome_labeler: beta imputed to 1.0 for %s as of %s (idea_id=%s)",
            idea.ticker,
            beta_as_of.isoformat(),
            idea.idea_id,
        )

    # --- returns (LOG space, entry net slippage) — E5 FROZEN convention ---
    # beta_i is fit on daily LOG returns (data/beta.py), so we MUST apply it to
    # log returns here.  Applying a log-space beta to simple returns leaked
    # market direction into alpha.
    r_i = _log_return(slippage_adjusted_entry, ticker_exit)
    r_spy = _log_return(spy_entry_open, spy_exit)

    # --- alpha ---
    alpha_raw = float(r_i) - beta_i * float(r_spy)
    alpha_bps = alpha_raw * 10_000.0

    # --- binary (±25bps band → 0) ---
    binary = _to_binary(alpha_bps)

    return ResolvedOutcome(
        idea_id=idea.idea_id,
        advisor_id=advisor_id,
        ticker=idea.ticker,
        alpha_bps=alpha_bps,
        binary=binary,
        advisor_confidence=advisor_confidence,
        stance_score=stance_score,
        abstained=False,
        horizon_days=idea.horizon_days,
        label_kind=label_kind,
    )


# ---------------------------------------------------------------------------
# Trading-day helpers
# ---------------------------------------------------------------------------

def _next_trading_day(after: datetime) -> datetime:
    """Return the first trading day strictly after *after* (i.e. t0+1 or later).

    Advances day-by-day until a trading day is found.  This prevents a
    weekend/holiday at t0+1 from silently using the prior bar's open as
    the entry price (which would understate the actual entry lag).
    """
    from arbiter.data.replay_clock import _is_trading_day  # same module, internal

    candidate = after + timedelta(days=1)
    # Safety cap: should never loop more than ~10 days (longest holiday run).
    for _ in range(20):
        if _is_trading_day(candidate.date()):
            return candidate
        candidate = candidate + timedelta(days=1)
    # Should never reach here for valid date ranges; return best guess.
    return candidate  # pragma: no cover


def _on_or_next_trading_day(dt: datetime) -> datetime:
    """Return *dt* if it is a trading day, else advance to the next trading day.

    Used for the default exit timestamp so that a horizon-end date that falls
    on a weekend/holiday does not silently pull the prior bar's close.
    """
    from arbiter.data.replay_clock import _is_trading_day  # same module, internal

    candidate = dt
    for _ in range(20):
        if _is_trading_day(candidate.date()):
            return candidate
        candidate = candidate + timedelta(days=1)
    return candidate  # pragma: no cover


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_price_open(ticker: str, as_of: datetime, pit: PITGateway) -> float:
    """Read price_open from PIT; raise LookupError if missing."""
    value = pit.get("price_open", ticker, as_of)
    if value is None:
        raise LookupError(
            f"price_open for {ticker!r} not available as of {as_of.isoformat()!r}"
        )
    return float(value)  # type: ignore[arg-type]


def _get_price_close(ticker: str, as_of: datetime, pit: PITGateway) -> float:
    """Read price_close from PIT; raise LookupError if missing."""
    value = pit.get("price_close", ticker, as_of)
    if value is None:
        raise LookupError(
            f"price_close for {ticker!r} not available as of {as_of.isoformat()!r}"
        )
    return float(value)  # type: ignore[arg-type]


def _get_beta_safe(
    ticker: str,
    as_of: datetime,
    pit: PITGateway,
) -> tuple[float, bool]:
    """Return (beta, was_imputed) — imputes 1.0 + flags when data is thin."""
    # beta_252d() itself imputes 1.0 and logs when data is insufficient.
    # We detect imputation by comparing to a sentinel call with no data.
    # Since beta_252d returns 1.0 on imputation, we have to track it via a
    # lightweight wrapper that intercepts the warning.
    import logging as _logging

    class _FlagHandler(_logging.Handler):
        flagged = False

        def emit(self, record: _logging.LogRecord) -> None:  # noqa: D102
            if "imputing 1.0" in record.getMessage():
                self.flagged = True

    handler = _FlagHandler()
    _beta_logger = _logging.getLogger("arbiter.data.beta")
    _beta_logger.addHandler(handler)
    try:
        beta = beta_252d(ticker, as_of, pit)
    finally:
        _beta_logger.removeHandler(handler)

    return beta, handler.flagged


def _simple_return(entry: float, exit_: float) -> np.floating:  # type: ignore[type-arg]
    """Compute (exit − entry) / entry as a numpy float for consistency."""
    return np.float64(exit_ - entry) / np.float64(entry)


def _log_return(entry: float, exit_: float) -> np.floating:  # type: ignore[type-arg]
    """Compute log(exit / entry) = log(1 + simple_return) as a numpy float.

    LOG return so the alpha formula matches the LOG-space beta (E5 FROZEN).
    Guards against non-positive prices (would make log undefined): falls back to
    the simple return in that degenerate case so the labeler never raises here.
    """
    if entry <= 0.0 or exit_ <= 0.0:
        return _simple_return(entry, exit_)
    return np.log(np.float64(exit_) / np.float64(entry))


def _to_binary(alpha_bps: float) -> int:
    """Map alpha_bps to +1 / 0 / -1.

    Band: |alpha_bps| <= 25 -> 0 ("no-call").
    """
    if alpha_bps > _BINARY_THRESHOLD_BPS:
        return 1
    if alpha_bps < -_BINARY_THRESHOLD_BPS:
        return -1
    return 0
