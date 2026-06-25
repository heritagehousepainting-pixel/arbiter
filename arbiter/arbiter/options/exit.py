"""Premium-stop exit decision logic for open option positions — P2.

Exit policy (locked by plan)
-----------------------------
Option exits are managed on the OPTIONS track exclusively — they must never
entangle ``exit_monitor.py`` or the equity exit logic.

This module provides ONE function: ``premium_stop_exit()``, which evaluates
all exit triggers and returns a close-reason string when an exit should fire,
or ``None`` when the position should stay open.

INTEGRATION CONTRACT (for the caller)
--------------------------------------
``premium_stop_exit()`` returns a DECISION only — it does NOT:
  - place a closing order (call ``client.place(close_order)`` yourself)
  - record an outcome (call ``outcomes.record_option_outcome(...)`` yourself)
  - write to any DB table

The integrator pattern:

    reason = premium_stop_exit(conn, client, ...)
    if reason is not None:
        # 1. Build and place the closing order via client.
        client.place(close_order)
        # 2. Record the outcome with the actual exit mid.
        outcomes.record_option_outcome(conn, ..., close_reason=reason, ...)

Return values
-------------
``"premium_stop"``     — current mid ≤ entry × (1 - config.option_premium_stop_pct)
``"horizon"``          — as_of date ≥ thesis_horizon_date
``"reversal"``         — sign(current_conviction) opposes sign(original_conviction)
``None``               — no trigger; position stays open

Trigger priority: premium_stop > horizon > reversal
(stop is the most urgent, reversal the least; horizon closes cleanly).

Never raises — all exceptions are caught and logged; returns None on error.
"""
from __future__ import annotations

import datetime
import logging
import math
import sqlite3
from typing import Optional

from arbiter.config import Config
from arbiter.options.alpaca_options_client import AlpacaOptionsClient

log = logging.getLogger(__name__)


def premium_stop_exit(
    conn: sqlite3.Connection,
    client: AlpacaOptionsClient,
    *,
    occ_symbol: str,
    entry_premium: float,
    contracts_qty: int,
    idea_id: str,
    open_ts: str,
    underlying: str,
    thesis_horizon_date: datetime.date,
    original_conviction: float,
    current_conviction: float,
    as_of: str,
    config: Config,
) -> Optional[str]:
    """Evaluate exit triggers for an open option position.

    Checks all exit triggers in priority order and returns the close-reason
    string when one fires, or ``None`` when no trigger is met.

    This function returns a DECISION only — it does not place orders or record
    outcomes.  See the module docstring for the required integrator pattern.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open arbiter DB connection.  Accepted for signature compatibility and
        future extension (e.g. reading DTE from stored data); not written to.
    client : AlpacaOptionsClient
        Initialised options client used to fetch the current option mid-price
        via ``client.snapshot([occ_symbol])``.
    occ_symbol : str
        OCC symbol of the open position.
    entry_premium : float
        Total premium paid to open the position (USD, positive value).
        Per-unit = ``entry_premium / (contracts_qty * 100)``.
    contracts_qty : int
        Number of contracts held.
    idea_id : str
        FK → ``ideas.idea_id``.  Included in log messages for traceability.
    open_ts : str
        Tz-aware UTC ISO timestamp when the position was opened (for logging).
    underlying : str
        Underlying equity ticker (for logging).
    thesis_horizon_date : datetime.date
        Calendar date on or after which the horizon trigger fires.
    original_conviction : float
        Signed conviction score at position open.  Positive = bullish (CALL),
        negative = bearish (PUT).
    current_conviction : float
        Current cycle's fused conviction.  Reversal fires when the sign flips
        relative to ``original_conviction``.
    as_of : str
        Tz-aware UTC ISO timestamp from the engine clock (used to extract
        today's date for the horizon check).
    config : Config
        Frozen arbiter config.  Reads ``config.option_premium_stop_pct``
        (default 0.50 → stop at −50% of premium) and ``config.options_mode``.

    Returns
    -------
    str | None
        ``"premium_stop"`` | ``"horizon"`` | ``"reversal"`` when a trigger
        fires; ``None`` when the position should stay open.

    Notes on each trigger
    ---------------------
    **premium_stop** (priority 1):
        Total current premium = ``mid × contracts_qty × 100``.
        Fires when ``current_total_premium ≤ entry_premium × (1 - stop_pct)``.
        Fetched from ``client.snapshot([occ_symbol])``; if the snapshot fails
        or returns no mid, the stop cannot be evaluated this cycle (fail-closed
        against spurious exits: returns None for that trigger only and
        continues to check the others).

    **horizon** (priority 2):
        Fires when the date extracted from ``as_of`` is on or after
        ``thesis_horizon_date``.  Purely deterministic — no network call.

    **reversal** (priority 3):
        Fires when ``sign(current_conviction) != sign(original_conviction)``
        AND both are non-zero (a zero conviction is ambiguous; do not exit).
    """
    # Guard: only active in paper mode.
    if config.options_mode != "paper":
        return None

    # ------------------------------------------------------------------
    # Priority 1: premium stop
    # ------------------------------------------------------------------
    stop_threshold = entry_premium * (1.0 - config.option_premium_stop_pct)

    try:
        snap_map = client.snapshot([occ_symbol])
        snap = snap_map.get(occ_symbol, {})

        bid = snap.get("bid")
        ask = snap.get("ask")

        # Derive mid from bid/ask when available; fall back gracefully.
        current_mid: float | None = None
        if (
            bid is not None
            and ask is not None
            and math.isfinite(float(bid))
            and math.isfinite(float(ask))
        ):
            current_mid = (float(bid) + float(ask)) / 2.0
        elif bid is not None and math.isfinite(float(bid)):
            current_mid = float(bid)
        elif ask is not None and math.isfinite(float(ask)):
            current_mid = float(ask)

        if current_mid is not None:
            current_total_premium = current_mid * contracts_qty * 100.0
            if current_total_premium <= stop_threshold:
                log.info(
                    "options_exit.premium_stop idea_id=%s underlying=%s occ=%s "
                    "entry=%.2f current=%.2f threshold=%.2f",
                    idea_id, underlying, occ_symbol,
                    entry_premium, current_total_premium, stop_threshold,
                )
                return "premium_stop"
        else:
            log.warning(
                "options_exit.no_mid_skip_stop idea_id=%s occ=%s",
                idea_id, occ_symbol,
            )
    except Exception:  # noqa: BLE001
        log.warning(
            "options_exit.snapshot_failed_skip_stop idea_id=%s occ=%s",
            idea_id, occ_symbol,
        )

    # ------------------------------------------------------------------
    # Priority 2: horizon expiry
    # ------------------------------------------------------------------
    try:
        # Extract the date portion from the tz-aware ISO string.
        as_of_date = datetime.date.fromisoformat(as_of[:10])
    except (ValueError, TypeError):
        log.warning(
            "options_exit.bad_as_of_date idea_id=%s as_of=%s", idea_id, as_of,
        )
        as_of_date = None

    if as_of_date is not None and as_of_date >= thesis_horizon_date:
        log.info(
            "options_exit.horizon idea_id=%s underlying=%s occ=%s as_of=%s horizon=%s",
            idea_id, underlying, occ_symbol,
            as_of_date.isoformat(), thesis_horizon_date.isoformat(),
        )
        return "horizon"

    # ------------------------------------------------------------------
    # Priority 3: conviction reversal
    # ------------------------------------------------------------------
    # Both values must be non-zero for a meaningful sign comparison.
    orig_sign = _sign(original_conviction)
    curr_sign = _sign(current_conviction)
    if orig_sign != 0 and curr_sign != 0 and orig_sign != curr_sign:
        log.info(
            "options_exit.reversal idea_id=%s underlying=%s occ=%s orig=%.3f curr=%.3f",
            idea_id, underlying, occ_symbol,
            original_conviction, current_conviction,
        )
        return "reversal"

    # No trigger fired.
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sign(value: float) -> int:
    """Return +1, -1, or 0 for the sign of *value*."""
    if value > 0.0:
        return 1
    if value < 0.0:
        return -1
    return 0
