"""Option outcome recording — P2 separate track (isolated from equity).

ISOLATION CONTRACT
------------------
This module writes to ``option_outcomes`` ONLY.  It NEVER touches the equity
``outcomes`` table, ``run_outcome_sweep()``, or the trust/calibration paths.
``option_pl_pct`` is display-only; ``underlying_alpha_bps`` may be used for
direction-validation but must not be injected into advisor trust scores.

When to call
------------
``record_option_outcome()`` is called by the P2 exit manager (``exit.py``) when
a position is closed for any reason (premium stop, horizon expiry, conviction
reversal, expiry approach, or manual).
"""
from __future__ import annotations

import math
import sqlite3

from arbiter.db.helpers import generate_ulid, insert_row
from arbiter.options.types import OptionOutcomeRow, OptionSide


def record_option_outcome(
    conn: sqlite3.Connection,
    *,
    shadow_id: str | None,
    idea_id: str,
    underlying: str,
    occ_symbol: str,
    side: str,
    open_ts: str,
    close_ts: str,
    close_reason: str,
    entry_premium: float,
    exit_premium: float,
    underlying_open_price: float,
    underlying_close_price: float,
    delta_at_open: float | None,
    iv_at_open: float | None,
    iv_at_close: float | None,
    contracts_qty: int,
    created_at: str,
) -> str:
    """Record the outcome of a closed option position to ``option_outcomes``.

    Computes ``option_pl_pct`` and ``underlying_alpha_bps`` from the raw
    price inputs and persists one ``OptionOutcomeRow`` via ``insert_row()``.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open arbiter DB connection.
    shadow_id : str | None
        FK → ``option_shadow_log.id`` for the shadow row this resolves.
        None for P2 live positions that were never shadowed.
    idea_id : str
        FK → ``ideas.idea_id``.
    underlying : str
        Underlying equity ticker.
    occ_symbol : str
        OCC symbol of the closed contract.
    side : str
        ``"call"`` or ``"put"``.
    open_ts : str
        Tz-aware UTC ISO timestamp when the position was opened.
    close_ts : str
        Tz-aware UTC ISO timestamp when the position was closed.
    close_reason : str
        Short close-reason code (see ``OptionOutcomeRow.close_reason``).
    entry_premium : float
        Total premium paid to open the position (USD, positive value).
    exit_premium : float
        Total premium received on close (USD, positive value).
    underlying_open_price : float
        Underlying equity price at position open (for alpha computation).
    underlying_close_price : float
        Underlying equity price at position close.
    delta_at_open : float | None
        Contract delta at position open.
    iv_at_open : float | None
        Implied volatility at position open.
    iv_at_close : float | None
        Implied volatility at position close.
    contracts_qty : int
        Number of contracts held.
    created_at : str
        Tz-aware UTC ISO insertion timestamp.

    Returns
    -------
    str
        The ULID of the inserted ``option_outcomes`` row.

    Notes
    -----
    ``option_pl_pct`` computation
        ``(exit_premium - entry_premium) / entry_premium``.
        Guard: if ``entry_premium == 0`` the result is 0.0 (no divide-by-zero).
        Display-only — does NOT feed advisor trust scores.

    ``underlying_alpha_bps`` sign convention
        Raw underlying return = ``underlying_close_price / underlying_open_price - 1``.
        For a **CALL** (bullish): a rising underlying is a win → sign is +1 (raw return
        is already the correct direction; positive alpha = the underlying moved in our
        favour).
        For a **PUT** (bearish): a falling underlying is a win → raw return is NEGATED
        before multiplying by 10 000 so that a DOWN move produces a POSITIVE alpha_bps.
        Formula:
            direction_mult = -1.0 if side == "put" else +1.0
            underlying_alpha_bps = raw_return × direction_mult × 10_000

        This field is the ONLY bridge to the equity trust ledger (direction-validation
        only).  Positive ``underlying_alpha_bps`` means the underlying moved IN FAVOUR of
        the thesis, regardless of actual option P&L (which is path/IV-dependent).

    NEVER call this from ``run_outcome_sweep()`` or any equity learning path.
    """
    # --- option_pl_pct (guard /0) -------------------------------------------
    if entry_premium == 0.0 or (not math.isfinite(entry_premium)):
        option_pl_pct = 0.0
    else:
        option_pl_pct = (exit_premium - entry_premium) / entry_premium

    # --- underlying_alpha_bps (signed by direction) --------------------------
    # Raw underlying move over the hold period.
    if underlying_open_price == 0.0 or (not math.isfinite(underlying_open_price)):
        raw_return = 0.0
    else:
        raw_return = underlying_close_price / underlying_open_price - 1.0

    # CALL: +move is good (mult +1).  PUT: -move is good (mult -1).
    direction_mult = -1.0 if side == OptionSide.PUT.value else 1.0
    underlying_alpha_bps = raw_return * direction_mult * 10_000.0

    # --- build and persist the row -------------------------------------------
    row_id = generate_ulid()
    row = OptionOutcomeRow(
        id=row_id,
        shadow_id=shadow_id,
        idea_id=idea_id,
        underlying=underlying,
        occ_symbol=occ_symbol,
        side=side,
        open_ts=open_ts,
        close_ts=close_ts,
        close_reason=close_reason,
        entry_premium=entry_premium,
        exit_premium=exit_premium,
        option_pl_pct=option_pl_pct,
        underlying_alpha_bps=underlying_alpha_bps,
        delta_at_open=delta_at_open,
        iv_at_open=iv_at_open,
        iv_at_close=iv_at_close,
        contracts_qty=contracts_qty,
        created_at=created_at,
    )
    insert_row(conn, "option_outcomes", row.to_dict())
    return row_id
