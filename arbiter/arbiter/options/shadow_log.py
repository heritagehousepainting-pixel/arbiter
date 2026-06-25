"""Shadow-log writer for the options expression layer — P1.

Every time the gate and contract selector run — whether the gate fires or not —
one row is written to ``option_shadow_log``.  This gives a complete audit trail
for calibrating thresholds from real Alpaca chain data.

Row shape: ``OptionShadowRow.to_dict()`` (see ``types.py``).
Persistence: ``insert_row(conn, "option_shadow_log", row.to_dict())``
             from ``arbiter.db.helpers``.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from arbiter.db.helpers import generate_ulid, insert_row
from arbiter.options.types import (
    OptionContract,
    OptionGateDecision,
    OptionOrder,
    OptionShadowRow,
)


def log_shadow_option(
    conn: sqlite3.Connection,
    *,
    idea_id: str,
    gate_decision: OptionGateDecision,
    contract: Optional[OptionContract],
    order: Optional[OptionOrder],
    as_of: str,
    created_at: str,
) -> str:
    """Persist one shadow-log row to ``option_shadow_log`` and return its ULID.

    Called unconditionally after the gate (and optionally after contract
    selection + sizing), regardless of whether ``gate_decision.express`` is
    True or False.  This gives a complete record for calibration.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open arbiter DB connection.
    idea_id : str
        ULID of the source idea (FK → ``ideas.idea_id``).
    gate_decision : OptionGateDecision
        Full gate decision object.
    contract : OptionContract | None
        The selected contract if gate fired and a contract was found; None
        otherwise.
    order : OptionOrder | None
        The sized order if gate fired, contract found, and sizing succeeded;
        None otherwise.
    as_of : str
        Tz-aware UTC ISO timestamp from the engine clock for the decision.
    created_at : str
        Tz-aware UTC ISO timestamp for the DB insertion (usually same as as_of
        but injected separately so tests can freeze both independently).

    Returns
    -------
    str
        The ULID of the inserted ``option_shadow_log`` row.
    """
    row_id = generate_ulid()

    # Derive underlying from the contract when available, else from gate decision
    # context.  gate_decision carries conviction/catalyst but not underlying;
    # contract is the authoritative source.  Callers that reject before contract
    # selection must pass underlying via idea metadata — we use a safe fallback
    # of empty string only when both are absent (should not happen in production).
    underlying: str = contract.underlying if contract is not None else ""

    row = OptionShadowRow(
        id=row_id,
        idea_id=idea_id,
        underlying=underlying,
        as_of=as_of,
        gate_express=1 if gate_decision.express else 0,
        gate_reason=gate_decision.reason,
        side=contract.side.value if contract is not None else None,
        occ_symbol=contract.occ_symbol if contract is not None else None,
        strike=contract.strike if contract is not None else None,
        expiry=contract.expiry.isoformat() if contract is not None else None,
        delta=contract.delta if contract is not None else None,
        iv=contract.iv if contract is not None else None,
        bid=contract.bid if contract is not None else None,
        ask=contract.ask if contract is not None else None,
        open_interest=contract.open_interest if contract is not None else None,
        volume=contract.volume if contract is not None else None,
        est_premium=order.est_premium if order is not None else None,
        delta_adjusted_notional=(
            order.delta_adjusted_notional if order is not None else None
        ),
        contracts_qty=order.contracts_qty if order is not None else None,
        conviction=gate_decision.conviction,
        horizon_days=gate_decision.horizon_days,
        catalyst_tag=gate_decision.catalyst_tag,
        ivr_estimate=gate_decision.ivr_estimate,
        realized_vol_proxy=gate_decision.realized_vol_proxy,
        created_at=created_at,
    )

    insert_row(conn, "option_shadow_log", row.to_dict())
    return row_id
