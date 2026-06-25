"""Option position lifecycle manager — P2 paper-trading exit loop.

``manage_option_positions`` is called once per engine cycle.  It iterates all
open positions, evaluates exit triggers via ``premium_stop_exit()``, and when a
trigger fires it:

  1. Fetches the current mid price via ``client.snapshot()``.
  2. Places a sell-to-close limit order via ``client.close_position()``.
  3. Records the outcome via ``record_option_outcome()``.

Each position is handled in a fault-isolated try/except so that one failure
does not block the others.  The function never raises.

INTEGRATION CONTRACT
--------------------
- Only active when ``config.options_mode == "paper"``.  Returns ``[]`` for any
  other mode (off / shadow).
- Returns the list of ``option_outcomes`` ULIDs created this cycle (one per
  position that was closed).  The caller (engine) uses this to log activity
  but does not need to act on the ids further.
- ``current_conviction_for`` is an optional callable ``(idea_id: str) ->
  float | None`` that looks up the current cycle conviction for a given idea.
  When it returns ``None`` (idea not in the current cycle), ``original_conviction``
  from the open-position row is used as a fallback (no spurious reversal exits).

NEVER call this from the equity engine path; it is wired only from the options
section of the cycle.
"""
from __future__ import annotations

import datetime
import logging
import math
import sqlite3
from typing import Callable, Optional

from arbiter.config import Config
from arbiter.options.alpaca_options_client import AlpacaOptionsClient
from arbiter.options.exit import premium_stop_exit
from arbiter.options.outcomes import record_option_outcome
from arbiter.options.positions import list_open_positions

log = logging.getLogger(__name__)


def manage_option_positions(
    conn: sqlite3.Connection,
    client: AlpacaOptionsClient,
    *,
    config: Config,
    clock: str,
    current_conviction_for: Optional[Callable[[str], Optional[float]]] = None,
) -> list[str]:
    """Evaluate and close triggered open option positions.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open arbiter DB connection.
    client : AlpacaOptionsClient
        Initialised options client for snapshot + order submission.
    config : Config
        Frozen arbiter config.
    clock : str
        Current engine clock as a tz-aware UTC ISO string (e.g.
        ``"2026-06-25T10:00:00+00:00"``).  Used as ``as_of`` for trigger
        evaluation and as ``close_ts`` / ``created_at`` for the outcome row.
    current_conviction_for : callable | None
        Optional lookup ``(idea_id: str) -> float | None``.  When provided and
        returns a non-None value, that conviction is used for the reversal check.
        When absent or returning None, ``original_conviction`` from the open row
        is used (safe fallback: no reversal exits for ideas absent from the
        current cycle).

    Returns
    -------
    list[str]
        ULIDs of ``option_outcomes`` rows inserted this cycle.  Empty when no
        position was closed or when ``config.options_mode != "paper"``.
    """
    if config.options_mode != "paper":
        return []

    open_positions = list_open_positions(conn)
    if not open_positions:
        return []

    outcome_ids: list[str] = []

    for pos in open_positions:
        idea_id: str = pos["idea_id"]
        occ_symbol: str = pos["occ_symbol"]
        underlying: str = pos["underlying"]

        try:
            # --- resolve current conviction ----------------------------------
            original_conviction: float = float(pos["original_conviction"])
            if current_conviction_for is not None:
                looked_up = current_conviction_for(idea_id)
                current_conviction = (
                    looked_up if looked_up is not None else original_conviction
                )
            else:
                current_conviction = original_conviction

            # --- parse thesis horizon date -----------------------------------
            try:
                thesis_horizon_date = datetime.date.fromisoformat(
                    pos["thesis_horizon_date"]
                )
            except (ValueError, TypeError) as exc:
                log.error(
                    "manage_options.bad_horizon_date idea_id=%s occ=%s error=%s",
                    idea_id, occ_symbol, exc,
                )
                continue

            # --- evaluate exit triggers --------------------------------------
            reason = premium_stop_exit(
                conn,
                client,
                occ_symbol=occ_symbol,
                entry_premium=float(pos["entry_premium"]),
                contracts_qty=int(pos["contracts_qty"]),
                idea_id=idea_id,
                open_ts=pos["open_ts"],
                underlying=underlying,
                thesis_horizon_date=thesis_horizon_date,
                original_conviction=original_conviction,
                current_conviction=current_conviction,
                as_of=clock,
                config=config,
            )

            if reason is None:
                # No trigger — position stays open.
                continue

            # --- fetch current mid for the closing order ---------------------
            contracts_qty = int(pos["contracts_qty"])
            snap_map = client.snapshot([occ_symbol])
            snap = snap_map.get(occ_symbol, {})

            bid = snap.get("bid")
            ask = snap.get("ask")
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

            if current_mid is None or current_mid <= 0:
                log.warning(
                    "manage_options.no_close_mid idea_id=%s occ=%s reason=%s "
                    "— skipping close this cycle",
                    idea_id, occ_symbol, reason,
                )
                continue

            # --- place sell-to-close -----------------------------------------
            close_resp = client.close_position(
                occ_symbol=occ_symbol,
                contracts_qty=contracts_qty,
                limit_price=current_mid,
            )
            log.info(
                "manage_options.close_placed idea_id=%s occ=%s reason=%s "
                "broker_id=%s",
                idea_id, occ_symbol, reason, close_resp.get("id"),
            )

            # --- compute exit premium (mid × qty × 100) ---------------------
            exit_premium = current_mid * contracts_qty * 100.0

            # --- fetch IV at close from the same snapshot --------------------
            iv_at_close: float | None = snap.get("iv")
            if iv_at_close is not None:
                try:
                    iv_at_close = float(iv_at_close)
                    if not math.isfinite(iv_at_close):
                        iv_at_close = None
                except (TypeError, ValueError):
                    iv_at_close = None

            # underlying_close_price: we don't have an equity snap here, so we
            # record 0.0 as a sentinel; express.py / engine can pass the real
            # price when it wires this call.  The field is display-only for
            # alpha_bps; a 0.0 underlying_open_price guard in outcomes.py keeps
            # it safe.
            underlying_close_price = 0.0

            # --- record outcome -----------------------------------------------
            outcome_id = record_option_outcome(
                conn,
                shadow_id=pos.get("shadow_id"),
                idea_id=idea_id,
                underlying=underlying,
                occ_symbol=occ_symbol,
                side=pos["side"],
                open_ts=pos["open_ts"],
                close_ts=clock,
                close_reason=reason,
                entry_premium=float(pos["entry_premium"]),
                exit_premium=exit_premium,
                underlying_open_price=float(pos["underlying_open_price"]),
                underlying_close_price=underlying_close_price,
                delta_at_open=_nullable_float(pos.get("delta_at_open")),
                iv_at_open=_nullable_float(pos.get("iv_at_open")),
                iv_at_close=iv_at_close,
                contracts_qty=contracts_qty,
                created_at=clock,
            )
            outcome_ids.append(outcome_id)
            log.info(
                "manage_options.outcome_recorded idea_id=%s occ=%s "
                "reason=%s outcome_id=%s",
                idea_id, occ_symbol, reason, outcome_id,
            )

        except Exception:  # noqa: BLE001
            log.exception(
                "manage_options.position_error idea_id=%s occ=%s — skipping",
                idea_id, occ_symbol,
            )
            continue

    return outcome_ids


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _nullable_float(value: object) -> float | None:
    """Convert *value* to float, or None if it's None / not finite."""
    if value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None
