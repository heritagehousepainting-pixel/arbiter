"""``/node/{id}`` deep detail for the inspection panel.  OWNED BY LANE 1.

Dispatches on id prefix and returns a fully typed ``NodeDetail`` per node type:
- ``fig.<person_id>``   : people row + recent filings + person_scores (or "building…")
- ``A1.*`` / ``A2.*``   : recent opinions + trust_weights history
- ``idea.<idea_id>``    : ideas row + linked opinions + orders + outcomes
- ``trade.<TICKER>``    : live Alpaca position (degrades offline) + originating idea/figure
- ``src.*``             : filing counts / service liveness
- ``core.*``            : engine-part summary from heartbeat + breakers
- ``exec.*``            : exec-part summary (orders, reconciler)
- ``infra.*``           : daemon/kill-switch/alerting from heartbeat
- ``outcome.<id>``      : outcome row detail

Returns None for genuinely unknown ids (main.py turns it into 404).
Strictly read-only.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from .contract import NodeDetail, NodeType
from .db import DEFAULT_DB_PATH
from .state import _heartbeat

_ARBITER_PKG_ROOT = DEFAULT_DB_PATH.parents[1]  # repo root


def _alpaca_position(ticker: str) -> dict:
    """Fetch live Alpaca position for a ticker — degrades gracefully."""
    try:
        if str(_ARBITER_PKG_ROOT) not in sys.path:
            sys.path.insert(0, str(_ARBITER_PKG_ROOT))
        from arbiter.config import load_config  # noqa: PLC0415
        from arbiter.engine import build_executor  # noqa: PLC0415

        ex = build_executor(load_config())
        positions = ex.get_positions()
        if ticker in positions:
            p = positions[ticker]
            return {
                "ticker": ticker,
                "shares": getattr(p, "shares", None),
                "avg_price": getattr(p, "avg_price", None),
                "side": "short" if (getattr(p, "shares", 0) or 0) < 0 else "long",
                "source": "alpaca_live",
            }
    except Exception:
        pass
    return {"ticker": ticker, "shares": None, "avg_price": None,
            "source": "alpaca_offline"}


# --- per-type builders -------------------------------------------------------

def _figure_detail(conn: sqlite3.Connection, person_id: str) -> NodeDetail | None:
    """Figure: people row + recent filings + person_scores."""
    row = conn.execute(
        "SELECT person_id, canonical_name, source, created_at "
        "FROM people WHERE person_id = ?",
        (person_id,),
    ).fetchone()
    if row is None:
        return None

    name = str(row["canonical_name"])
    src = str(row["source"])

    # Recent filings
    filing_rows = conn.execute(
        "SELECT ticker, txn_type, shares, price, filing_ts, source "
        "FROM filings WHERE person_id = ? AND is_superseded = 0 "
        "ORDER BY filing_ts DESC LIMIT 20",
        (person_id,),
    ).fetchall()
    filings = [
        {
            "ticker": str(r["ticker"]),
            "txn_type": str(r["txn_type"]),
            "shares": r["shares"],
            "price": r["price"],
            "filing_ts": str(r["filing_ts"]),
            "source": str(r["source"]),
        }
        for r in filing_rows
    ]

    # person_scores (may be empty — cold start)
    score_row = conn.execute(
        "SELECT sample_count, accuracy, alpha_bps_avg, gate_pass, as_of "
        "FROM person_scores WHERE person_id = ? AND is_superseded = 0 "
        "ORDER BY created_at DESC LIMIT 1",
        (person_id,),
    ).fetchone()
    if score_row:
        score_summary = {
            "sample_count": score_row["sample_count"],
            "accuracy": score_row["accuracy"],
            "alpha_bps_avg": score_row["alpha_bps_avg"],
            "gate_pass": bool(score_row["gate_pass"]),
            "as_of": str(score_row["as_of"]),
        }
    else:
        score_summary = {"note": "building… (not enough outcomes yet)"}

    return NodeDetail(
        id=f"fig.{person_id}",
        type="figure",
        label=name,
        summary={
            "person_id": person_id,
            "canonical_name": name,
            "source": src,
            "n_filings": len(filings),
            "score": score_summary,
        },
        rows=filings,
    )


def _advisor_detail(conn: sqlite3.Connection, advisor_id: str) -> NodeDetail | None:
    """Advisor: recent opinions + trust_weights history."""
    # Recent opinions
    op_rows = conn.execute(
        "SELECT ticker, stance_score, confidence, as_of, idea_id, rationale "
        "FROM opinions WHERE advisor_id = ? AND is_superseded = 0 "
        "ORDER BY as_of DESC LIMIT 20",
        (advisor_id,),
    ).fetchall()
    opinions = [
        {
            "ticker": str(r["ticker"]),
            "stance_score": float(r["stance_score"]),
            "confidence": float(r["confidence"]),
            "as_of": str(r["as_of"]),
            "idea_id": str(r["idea_id"] or ""),
            "rationale": str(r["rationale"] or "")[:200],
        }
        for r in op_rows
    ]

    # trust_weights history (most recent rows)
    tw_rows = conn.execute(
        "SELECT weight, ci_low, ci_high, shadow, cap_reason, as_of "
        "FROM trust_weights WHERE advisor_id = ? "
        "ORDER BY as_of DESC LIMIT 10",
        (advisor_id,),
    ).fetchall()
    trust_history = [
        {
            "weight": float(r["weight"]),
            "ci_low": float(r["ci_low"]),
            "ci_high": float(r["ci_high"]),
            "shadow": bool(r["shadow"]),
            "cap_reason": str(r["cap_reason"] or ""),
            "as_of": str(r["as_of"]),
        }
        for r in tw_rows
    ]
    if not trust_history:
        trust_history = [{"note": "building… (not enough outcomes yet)"}]

    # Outcome summary
    oc_rows = conn.execute(
        "SELECT COUNT(*) as n, "
        "AVG(alpha_bps) as avg_alpha, "
        "SUM(CASE WHEN binary=1 THEN 1 ELSE 0 END) as wins "
        "FROM outcomes WHERE advisor_id = ? AND is_superseded=0",
        (advisor_id,),
    ).fetchone()

    # Current weight
    current_tw = conn.execute(
        "SELECT weight, shadow, cap_reason FROM trust_weights "
        "WHERE advisor_id = ? AND is_superseded=0 ORDER BY as_of DESC LIMIT 1",
        (advisor_id,),
    ).fetchone()

    summary = {
        "advisor_id": advisor_id,
        "current_weight": float(current_tw["weight"]) if current_tw else None,
        "shadow": bool(current_tw["shadow"]) if current_tw else None,
        "cap_reason": str(current_tw["cap_reason"] or "") if current_tw else None,
        "n_outcomes": int(oc_rows["n"]) if oc_rows else 0,
        "avg_alpha_bps": float(oc_rows["avg_alpha"]) if oc_rows and oc_rows["avg_alpha"] else None,
        "win_count": int(oc_rows["wins"]) if oc_rows and oc_rows["wins"] is not None else 0,
        "trust_history": trust_history,
    }

    return NodeDetail(
        id=advisor_id,
        type="advisor",
        label=advisor_id,
        summary=summary,
        rows=opinions,
    )


def _idea_detail(conn: sqlite3.Connection, idea_id: str) -> NodeDetail | None:
    """Idea: ideas row + linked opinions + orders + outcomes."""
    row = conn.execute(
        "SELECT idea_id, ticker, thesis, horizon_days, state, as_of, created_at, updated_state_at "
        "FROM ideas WHERE idea_id = ? AND is_superseded = 0",
        (idea_id,),
    ).fetchone()
    if row is None:
        return None

    ticker = str(row["ticker"])

    # Linked opinions
    op_rows = conn.execute(
        "SELECT advisor_id, stance_score, confidence, as_of, rationale "
        "FROM opinions WHERE idea_id = ? AND is_superseded = 0 "
        "ORDER BY as_of DESC LIMIT 20",
        (idea_id,),
    ).fetchall()
    opinions = [
        {
            "kind": "opinion",
            "advisor_id": str(r["advisor_id"]),
            "stance_score": float(r["stance_score"]),
            "confidence": float(r["confidence"]),
            "as_of": str(r["as_of"]),
            "rationale": str(r["rationale"] or "")[:200],
        }
        for r in op_rows
    ]

    # Orders linked to this idea
    ord_rows = conn.execute(
        "SELECT order_id, side, qty, status, created_at "
        "FROM orders WHERE idea_id = ? ORDER BY created_at DESC LIMIT 10",
        (idea_id,),
    ).fetchall()
    orders = [
        {
            "kind": "order",
            "order_id": str(r["order_id"]),
            "side": str(r["side"]),
            "qty": float(r["qty"]),
            "status": str(r["status"]),
            "created_at": str(r["created_at"]),
        }
        for r in ord_rows
    ]

    # Outcomes linked to this idea
    oc_rows = conn.execute(
        "SELECT id, advisor_id, alpha_bps, binary, label_kind, created_at "
        "FROM outcomes WHERE idea_id = ? AND is_superseded = 0 "
        "ORDER BY created_at DESC LIMIT 10",
        (idea_id,),
    ).fetchall()
    outcomes = [
        {
            "kind": "outcome",
            "id": str(r["id"]),
            "advisor_id": str(r["advisor_id"]),
            "alpha_bps": float(r["alpha_bps"]),
            "binary": int(r["binary"]),
            "label_kind": str(r["label_kind"]),
            "created_at": str(r["created_at"]),
        }
        for r in oc_rows
    ]

    return NodeDetail(
        id=f"idea.{idea_id}",
        type="idea",
        label=ticker,
        summary={
            "idea_id": idea_id,
            "ticker": ticker,
            "thesis": str(row["thesis"] or ""),
            "horizon_days": int(row["horizon_days"]),
            "state": str(row["state"]),
            "as_of": str(row["as_of"]),
            "created_at": str(row["created_at"]),
            "updated_state_at": str(row["updated_state_at"]),
        },
        rows=opinions + orders + outcomes,
    )


def _trade_detail(conn: sqlite3.Connection, ticker: str) -> NodeDetail:
    """Trade: live Alpaca position (degrades offline) + originating idea/figure."""
    position = _alpaca_position(ticker)

    # Find originating idea (most recent order for this ticker with idea_id)
    idea_row = conn.execute(
        "SELECT idea_id FROM orders WHERE ticker = ? AND idea_id IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    originating_idea = str(idea_row["idea_id"]) if idea_row else None

    # Find originating figure (via idea → opinions → advisor → figure filings)
    figure_info: dict = {}
    if originating_idea:
        fig_row = conn.execute(
            """
            SELECT p.person_id, p.canonical_name, p.source
            FROM filings f
            JOIN people p ON p.person_id = f.person_id
            WHERE f.ticker = ? AND f.is_superseded = 0
            ORDER BY f.filing_ts DESC LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        if fig_row:
            figure_info = {
                "person_id": str(fig_row["person_id"]),
                "canonical_name": str(fig_row["canonical_name"]),
                "source": str(fig_row["source"]),
            }

    # Recent orders for context
    ord_rows = conn.execute(
        "SELECT order_id, side, qty, status, created_at FROM orders "
        "WHERE ticker = ? ORDER BY created_at DESC LIMIT 10",
        (ticker,),
    ).fetchall()
    orders = [
        {
            "order_id": str(r["order_id"]),
            "side": str(r["side"]),
            "qty": float(r["qty"]),
            "status": str(r["status"]),
            "created_at": str(r["created_at"]),
        }
        for r in ord_rows
    ]

    side = position.get("side", "unknown")
    return NodeDetail(
        id=f"trade.{ticker}",
        type="trade",
        label=ticker,
        summary={
            "ticker": ticker,
            "shares": position.get("shares"),
            "avg_price": position.get("avg_price"),
            "side": side,
            "position_source": position.get("source"),
            "originating_idea": originating_idea,
            "originating_figure": figure_info or None,
        },
        rows=orders,
    )


def _src_detail(conn: sqlite3.Connection, node_id: str) -> NodeDetail:
    """Data source: filing counts / service liveness."""
    src_map = {
        "src.form4": "form4",
        "src.form13d": "form13d",
        "src.congress": "congress",
    }
    label_map = {
        "src.form4": "SEC Form 4 (insiders)",
        "src.form13d": "SEC 13D/13G (activists)",
        "src.congress": "Congress PTR",
        "src.alpaca": "Alpaca market data",
        "src.mirofish": "MiroFish A2 service",
    }
    label = label_map.get(node_id, node_id)

    if node_id in src_map:
        src = src_map[node_id]
        counts = conn.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN filing_ts >= datetime('now', '-7 days') THEN 1 ELSE 0 END) as recent "
            "FROM filings WHERE source = ? AND is_superseded = 0",
            (src,),
        ).fetchone()
        recent_rows = conn.execute(
            "SELECT ticker, person_id, txn_type, filing_ts FROM filings "
            "WHERE source = ? AND is_superseded = 0 ORDER BY filing_ts DESC LIMIT 10",
            (src,),
        ).fetchall()
        rows = [
            {"ticker": str(r["ticker"]), "person_id": str(r["person_id"]),
             "txn_type": str(r["txn_type"]), "filing_ts": str(r["filing_ts"])}
            for r in recent_rows
        ]
        summary = {
            "source": src,
            "total_filings": int(counts["total"]) if counts else 0,
            "recent_7d": int(counts["recent"]) if counts else 0,
        }
    elif node_id == "src.alpaca":
        hb = _heartbeat()
        summary = {
            "status": "live" if hb else "offline",
            "open_positions": hb.get("open_positions") if hb else None,
            "is_open": hb.get("is_open") if hb else None,
        }
        rows = []
    elif node_id == "src.mirofish":
        mf_count = conn.execute(
            "SELECT COUNT(*) FROM opinions WHERE advisor_id='A2.mirofish' AND is_superseded=0"
        ).fetchone()[0]
        summary = {
            "total_opinions": int(mf_count),
            "status": "live" if mf_count > 0 else "offline",
        }
        rows = []
    else:
        summary = {"node_id": node_id}
        rows = []

    return NodeDetail(id=node_id, type="data_source", label=label, summary=summary, rows=rows)


def _core_detail(conn: sqlite3.Connection, node_id: str) -> NodeDetail:
    """Engine part: summary from heartbeat + breakers."""
    hb = _heartbeat()
    label_map = {
        "core.fusion": "Fusion",
        "core.sizing": "Sizing (¼-Kelly/ADV)",
        "core.gates": "Gates (risk book)",
        "core.safety": "Safety (breakers)",
    }
    label = label_map.get(node_id, node_id)

    try:
        breakers = conn.execute(
            "SELECT breaker_name, latched, latched_at, reason FROM breaker_state"
        ).fetchall()
        breaker_summary = [
            {"name": str(r["breaker_name"]), "latched": bool(r["latched"]),
             "latched_at": str(r["latched_at"] or ""), "reason": str(r["reason"] or "")}
            for r in breakers
        ]
    except Exception:
        breaker_summary = []

    summary = {
        "node_id": node_id,
        "daemon_ok": hb is not None,
        "paused": hb.get("paused") if hb else None,
        "is_open": hb.get("is_open") if hb else None,
        "breakers": breaker_summary,
    }
    return NodeDetail(id=node_id, type="engine_part", label=label, summary=summary, rows=breaker_summary)


def _exec_detail(conn: sqlite3.Connection, node_id: str) -> NodeDetail:
    """Exec part: order counts + reconciler status."""
    label_map = {
        "exec.adapter": "Alpaca paper adapter",
        "exec.exit_monitor": "Exit monitor",
        "exec.reconciler": "Reconciler",
    }
    label = label_map.get(node_id, node_id)
    hb = _heartbeat()

    try:
        total_orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        filled_orders = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status='filled'"
        ).fetchone()[0]
        recent_orders = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE created_at >= datetime('now', '-7 days')"
        ).fetchone()[0]
        recent_rows = conn.execute(
            "SELECT order_id, ticker, side, qty, status, created_at FROM orders "
            "ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        rows = [
            {"order_id": str(r["order_id"]), "ticker": str(r["ticker"]),
             "side": str(r["side"]), "qty": float(r["qty"]),
             "status": str(r["status"]), "created_at": str(r["created_at"])}
            for r in recent_rows
        ]
    except Exception:
        total_orders = filled_orders = recent_orders = 0
        rows = []

    summary = {
        "node_id": node_id,
        "daemon_ok": hb is not None,
        "total_orders": total_orders,
        "filled_orders": filled_orders,
        "recent_7d": recent_orders,
    }
    return NodeDetail(id=node_id, type="exec_part", label=label, summary=summary, rows=rows)


def _infra_detail(node_id: str) -> NodeDetail:
    """Infra: daemon/kill-switch/alerting from heartbeat."""
    label_map = {
        "infra.daemon": "Market-hours daemon",
        "infra.killswitch": "Kill switch",
        "infra.alerting": "Alerting (ntfy)",
    }
    label = label_map.get(node_id, node_id)
    hb = _heartbeat()

    summary: dict = {"node_id": node_id}
    if hb:
        summary.update({
            "now": hb.get("now"),
            "is_open": hb.get("is_open"),
            "paused": hb.get("paused"),
            "open_positions": hb.get("open_positions"),
            "backoff_s": hb.get("backoff_s"),
            "iteration_kind": hb.get("iteration_kind"),
            "next_open": hb.get("next_open"),
            "next_close": hb.get("next_close"),
        })
    else:
        summary["status"] = "offline"

    return NodeDetail(id=node_id, type="infra", label=label, summary=summary, rows=[])


def _outcome_detail(conn: sqlite3.Connection, outcome_id: str) -> NodeDetail | None:
    """Outcome: single outcome row detail."""
    row = conn.execute(
        "SELECT id, idea_id, advisor_id, ticker, alpha_bps, binary, "
        "advisor_confidence, horizon_days, label_kind, created_at "
        "FROM outcomes WHERE id = ? AND is_superseded = 0",
        (outcome_id,),
    ).fetchone()
    if row is None:
        return None

    ticker = str(row["ticker"])
    alpha = float(row["alpha_bps"])
    label = f"{ticker} {'+' if int(row['binary']) >= 0 else ''}{int(alpha)}bps"

    return NodeDetail(
        id=f"outcome.{outcome_id}",
        type="outcome",
        label=label,
        summary={
            "id": outcome_id,
            "idea_id": str(row["idea_id"]),
            "advisor_id": str(row["advisor_id"]),
            "ticker": ticker,
            "alpha_bps": alpha,
            "binary": int(row["binary"]),
            "advisor_confidence": float(row["advisor_confidence"]),
            "horizon_days": int(row["horizon_days"]),
            "label_kind": str(row["label_kind"]),
            "created_at": str(row["created_at"]),
        },
        rows=[],
    )


# --- Prefix routing table ---------------------------------------------------

_NODE_TYPES: dict[str, NodeType] = {
    "fig": "figure",
    "A1": "advisor",
    "A2": "advisor",
    "A3": "advisor",
    "idea": "idea",
    "trade": "trade",
    "outcome": "outcome",
    "src": "data_source",
    "core": "engine_part",
    "exec": "exec_part",
    "infra": "infra",
}


def build_node_detail(conn: sqlite3.Connection, node_id: str) -> NodeDetail | None:
    """Return detail for *node_id*, or None if unknown (→ 404).

    Dispatches on the id prefix.
    """
    prefix = node_id.split(".")[0]

    # --- figure ---
    if prefix == "fig":
        person_id = node_id[4:]  # strip "fig."
        return _figure_detail(conn, person_id)

    # --- advisors ---
    if prefix in ("A1", "A2", "A3"):
        return _advisor_detail(conn, node_id)

    # --- ideas ---
    if prefix == "idea":
        idea_id = node_id[5:]  # strip "idea."
        return _idea_detail(conn, idea_id)

    # --- trades ---
    if prefix == "trade":
        ticker = node_id[6:]  # strip "trade."
        return _trade_detail(conn, ticker)

    # --- outcomes ---
    if prefix == "outcome":
        outcome_id = node_id[8:]  # strip "outcome."
        return _outcome_detail(conn, outcome_id)

    # --- data sources ---
    if prefix == "src":
        known_src = {"src.form4", "src.form13d", "src.congress", "src.alpaca", "src.mirofish"}
        if node_id not in known_src:
            return None
        return _src_detail(conn, node_id)

    # --- core engine parts ---
    if prefix == "core":
        known_core = {"core.fusion", "core.sizing", "core.gates", "core.safety"}
        if node_id not in known_core:
            return None
        return _core_detail(conn, node_id)

    # --- exec parts ---
    if prefix == "exec":
        known_exec = {"exec.adapter", "exec.exit_monitor", "exec.reconciler"}
        if node_id not in known_exec:
            return None
        return _exec_detail(conn, node_id)

    # --- infra ---
    if prefix == "infra":
        known_infra = {"infra.daemon", "infra.killswitch", "infra.alerting"}
        if node_id not in known_infra:
            return None
        return _infra_detail(node_id)

    # Unknown prefix → 404
    return None
