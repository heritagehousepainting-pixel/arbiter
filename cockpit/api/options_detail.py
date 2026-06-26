"""``/node/opt.layer`` deep detail for the options execution waypoint.

Called from ``node_detail.py`` when prefix == "opt".  Returns a ``NodeDetail``
with:
  summary: options_mode, n_open, shadow_count_7d, outcome_count, aggregate stats
  rows: last 10 shadow plays (mixed express/reject); each dict has kind="shadow_play"
        so the inspector can type-switch.

Read-only.
"""
from __future__ import annotations

import sqlite3

from .contract import NodeDetail
from .options import _list_open_positions, _options_mode


def build_options_node_detail(conn: sqlite3.Connection) -> NodeDetail:
    """Return NodeDetail for the opt.layer node."""
    options_mode = _options_mode()

    # Open position count
    open_positions = _list_open_positions(conn)
    n_open = len(open_positions)

    # 7-day shadow count
    try:
        shadow_count_7d = conn.execute(
            "SELECT COUNT(*) FROM option_shadow_log "
            "WHERE as_of >= datetime('now', '-7 days')"
        ).fetchone()[0]
    except Exception:
        shadow_count_7d = 0

    # Total outcome count
    try:
        outcome_count = conn.execute(
            "SELECT COUNT(*) FROM option_outcomes"
        ).fetchone()[0]
    except Exception:
        outcome_count = 0

    # Aggregate stats from all outcomes
    try:
        agg = conn.execute(
            """
            SELECT
                COUNT(*) AS n,
                SUM(CASE WHEN option_pl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                AVG(option_pl_pct) AS avg_pl_pct,
                AVG(underlying_alpha_bps) AS avg_alpha_bps
            FROM option_outcomes
            """
        ).fetchone()
        if agg and int(agg["n"]) > 0:
            n_total = int(agg["n"])
            win_rate: float | None = float(agg["wins"]) / n_total
            avg_option_pl_pct: float | None = float(agg["avg_pl_pct"])
            avg_alpha_bps: float | None = float(agg["avg_alpha_bps"])
        else:
            win_rate = avg_option_pl_pct = avg_alpha_bps = None
    except Exception:
        win_rate = avg_option_pl_pct = avg_alpha_bps = None

    # Last 10 shadow plays (rows), mixed express/reject
    try:
        shadow_rows = conn.execute(
            """
            SELECT id, underlying, as_of, gate_express, gate_reason,
                   side, strike, expiry, conviction, ivr_estimate, created_at
            FROM option_shadow_log
            ORDER BY created_at DESC
            LIMIT 10
            """
        ).fetchall()
    except Exception:
        shadow_rows = []

    rows = [
        {
            "kind": "shadow_play",
            "id": str(r["id"]),
            "underlying": str(r["underlying"]),
            "as_of": str(r["as_of"]),
            "gate_express": bool(r["gate_express"]),
            "gate_reason": str(r["gate_reason"]),
            "side": str(r["side"]) if r["side"] is not None else None,
            "strike": float(r["strike"]) if r["strike"] is not None else None,
            "expiry": str(r["expiry"]) if r["expiry"] is not None else None,
            "conviction": float(r["conviction"]),
            "ivr_estimate": float(r["ivr_estimate"]) if r["ivr_estimate"] is not None else None,
            "created_at": str(r["created_at"]),
        }
        for r in shadow_rows
    ]

    return NodeDetail(
        id="opt.layer",
        type="engine_part",
        label="Options Layer",
        summary={
            "options_mode": options_mode,
            "n_open": n_open,
            "shadow_count_7d": shadow_count_7d,
            "outcome_count": outcome_count,
            "win_rate": win_rate,
            "avg_option_pl_pct": avg_option_pl_pct,
            "avg_underlying_alpha_bps": avg_alpha_bps,
            "note": (
                "Option outcomes are isolated from equity learning — "
                "they do NOT feed advisor trust weights."
            ),
        },
        rows=rows,
    )


def build_option_position_detail(
    conn: sqlite3.Connection, position_id: str,
) -> NodeDetail | None:
    """NodeDetail for a single open option-position node (option_position.<id>).

    Reuses ``_list_open_positions`` so the dte / current-mid / unrealized-P&L are
    computed identically to the OptionsPanel. Returns None (→ 404) if the id isn't
    an open position.
    """
    for p in _list_open_positions(conn):
        if p.id != position_id:
            continue
        cp = "C" if str(p.side).lower() == "call" else "P"
        try:
            strike_lbl = str(int(p.strike)) if float(p.strike).is_integer() else str(p.strike)
        except (TypeError, ValueError):
            strike_lbl = str(p.strike)
        return NodeDetail(
            id=f"option_position.{position_id}",
            type="engine_part",
            label=f"{p.underlying} {strike_lbl}{cp}",
            summary={
                "underlying": p.underlying,
                "occ_symbol": p.occ_symbol,
                "side": p.side,
                "strike": p.strike,
                "contracts_qty": p.contracts_qty,
                "entry_premium": p.entry_premium,
                "delta_at_open": p.delta_at_open,
                "dte": p.dte,
                "unrealized_pl": p.unrealized_pl,
                "idea_id": p.idea_id,
            },
            rows=[],
        )
    return None
