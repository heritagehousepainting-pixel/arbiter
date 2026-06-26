"""Build the dynamic ``/state`` snapshot from the live DB + Alpaca + heartbeat.

Lane 1 (API/data-mapping): deepens per-node intensities for ALL stable graph
nodes and builds the live dynamic-flow edges (figure→advisor, advisor→idea,
idea→trade, trade→outcome→advisor).

Strictly read-only.  All external reads (Alpaca, heartbeat, kill switch) are
best-effort and degrade gracefully so the cockpit works even when trading is
offline.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .contract import Account, Edge, Health, KillSwitch, Node, NodeState, State
from .db import DEFAULT_DB_PATH, db_reachable

# The arbiter Python package lives at <repo>/arbiter and is run via cwd
# (not pip-installed), so add <repo>/arbiter to sys.path for the read-only
# reuse of its config + AlpacaAdapter.  Done once, lazily, only when we touch
# Alpaca.
_ARBITER_PKG_ROOT = DEFAULT_DB_PATH.parents[1]  # <repo>/arbiter

_HEARTBEAT = DEFAULT_DB_PATH.parent / "arbiter-daemon.heartbeat"

# How many seconds of recent filings count as "recently active" for intensity.
_RECENT_SECONDS = 7 * 24 * 3600  # 7 days


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _heartbeat() -> dict | None:
    try:
        return json.loads(Path(_HEARTBEAT).read_text())
    except Exception:
        return None


def _alpaca_snapshot() -> tuple[Account, list[Node], list[Edge], bool]:
    """Live account + open positions as Trade nodes (read-only).  Returns
    (account, trade_nodes, trade_edges, alpaca_ok)."""
    try:
        import sys  # noqa: PLC0415
        if str(_ARBITER_PKG_ROOT) not in sys.path:
            sys.path.insert(0, str(_ARBITER_PKG_ROOT))
        from arbiter.config import load_config  # noqa: PLC0415
        from arbiter.engine import build_executor  # noqa: PLC0415

        ex = build_executor(load_config())
        acct = ex.get_account()
        positions = ex.get_positions()
    except Exception:
        return Account(), [], [], False

    nodes: list[Node] = []
    edges: list[Edge] = []
    for t, p in positions.items():
        short = p.shares < 0
        tid = f"trade.{t}"
        nodes.append(Node(
            id=tid, type="trade", label=t, cluster="market",
            meta={"shares": p.shares, "avg_price": p.avg_price,
                  "side": "short" if short else "long"},
        ))
        edges.append(Edge(id=f"e.hold.{t}", source="exec.adapter", target=tid, kind="submits"))
    return (
        Account(equity=getattr(acct, "equity", None), daily_pl=getattr(acct, "daily_pl", None)),
        nodes, edges, True,
    )


def _live_idea_nodes(conn: sqlite3.Connection) -> tuple[list[Node], list[Edge]]:
    rows = conn.execute(
        "SELECT idea_id, ticker, state, thesis, horizon_days FROM ideas "
        "WHERE is_superseded = 0 AND state NOT IN ('CLOSED','ABANDONED') "
        "ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    nodes: list[Node] = []
    edges: list[Edge] = []
    for r in rows:
        iid = f"idea.{r['idea_id']}"
        nodes.append(Node(
            id=iid, type="idea", label=str(r["ticker"]), cluster="ideas",
            meta={"state": str(r["state"]), "thesis": str(r["thesis"] or ""),
                  "horizon_days": r["horizon_days"]},
        ))
        edges.append(Edge(id=f"e.dec.idea.{r['idea_id']}", source="core.safety",
                          target=iid, kind="submits"))
    return nodes, edges


def _advisor_intensities(conn: sqlite3.Connection) -> dict[str, NodeState]:
    """Compute NodeState for all advisor nodes from trust_weights.

    Falls back to recent opinion activity if trust_weights is empty (cold start).
    """
    out: dict[str, NodeState] = {}
    try:
        rows = conn.execute(
            "SELECT advisor_id, weight, shadow, cap_reason FROM trust_weights "
            "WHERE is_superseded = 0"
        ).fetchall()
    except Exception:
        rows = []

    if rows:
        for r in rows:
            w = float(r["weight"] or 0.0)
            cap = r["cap_reason"] or ""
            status = "shadow" if r["shadow"] else ("capped" if cap else "active")
            out[str(r["advisor_id"])] = NodeState(
                intensity=max(0.0, min(1.0, w)),
                status=status,
                value=w,
                label_extra=cap if cap else None,
            )
    else:
        # Cold start: derive intensity from opinion volume in the past 7 days.
        cutoff = (_utcnow() - timedelta(seconds=_RECENT_SECONDS)).isoformat()
        try:
            opinion_rows = conn.execute(
                "SELECT advisor_id, COUNT(*) as n "
                "FROM opinions WHERE is_superseded=0 AND as_of >= ? "
                "GROUP BY advisor_id",
                (cutoff,),
            ).fetchall()
        except Exception:
            opinion_rows = []
        counts = {str(r["advisor_id"]): int(r["n"]) for r in opinion_rows}
        max_count = max(counts.values(), default=1) or 1
        for adv_id, cnt in counts.items():
            out[adv_id] = NodeState(
                intensity=min(1.0, cnt / max_count),
                status="active",
                value=float(cnt),
                label_extra="cold start",
            )
        # Ensure all known advisors appear (dim if no data)
        for adv_id in ("A1.insider", "A1.congress", "A1.activist", "A1.fund", "A2.mirofish"):
            if adv_id not in out:
                out[adv_id] = NodeState(intensity=0.05, status="active",
                                        label_extra="cold start")
    return out


def _data_source_intensities(conn: sqlite3.Connection) -> dict[str, NodeState]:
    """Intensity for data-source nodes from recent filing volume / service health."""
    out: dict[str, NodeState] = {}
    cutoff = (_utcnow() - timedelta(seconds=_RECENT_SECONDS)).isoformat()

    # filings-backed sources: form4, form13d, form13f, congress
    source_to_node = {
        "form4": "src.form4",
        "form13d": "src.form13d",
        "form13f": "src.form13f",
        "congress": "src.congress",
    }
    try:
        rows = conn.execute(
            "SELECT source, COUNT(*) as n FROM filings "
            "WHERE is_superseded=0 AND filing_ts >= ? GROUP BY source",
            (cutoff,),
        ).fetchall()
    except Exception:
        rows = []
    counts: dict[str, int] = {}
    for r in rows:
        src = str(r["source"])
        n = int(r["n"])
        counts[src] = n

    # Total recent filings to normalise intensities
    total = max(sum(counts.values()), 1)
    for src_key, node_id in source_to_node.items():
        n = counts.get(src_key, 0)
        # intensity: each source is relative to the max so we preserve relative richness
        out[node_id] = NodeState(
            intensity=min(1.0, (n / total) * 3.0) if total > 0 else 0.05,
            value=float(n),
            label_extra=f"{n} filings (7d)",
        )

    # alpaca: liveness from heartbeat open_positions
    hb = _heartbeat()
    if hb is not None:
        positions = int(hb.get("open_positions", 0))
        out["src.alpaca"] = NodeState(
            intensity=min(1.0, 0.4 + 0.2 * positions),
            status="live",
            value=float(positions),
            label_extra=f"{positions} open positions",
        )
    else:
        out["src.alpaca"] = NodeState(intensity=0.1, status="offline",
                                      label_extra="heartbeat missing")

    # mirofish: liveness from recent A2.mirofish opinions
    try:
        mf_count = conn.execute(
            "SELECT COUNT(*) FROM opinions WHERE advisor_id='A2.mirofish' "
            "AND is_superseded=0 AND as_of >= ?",
            (cutoff,),
        ).fetchone()[0]
    except Exception:
        mf_count = 0
    out["src.mirofish"] = NodeState(
        intensity=min(1.0, 0.1 + 0.05 * mf_count) if mf_count > 0 else 0.05,
        status="live" if mf_count > 0 else "offline",
        value=float(mf_count),
        label_extra=f"{mf_count} opinions (7d)",
    )
    return out


def _figure_intensities(conn: sqlite3.Connection) -> dict[str, NodeState]:
    """Intensity for figure nodes from recent filing activity."""
    cutoff = (_utcnow() - timedelta(seconds=_RECENT_SECONDS)).isoformat()
    try:
        rows = conn.execute(
            """
            SELECT p.person_id, p.source,
                   COUNT(f.id) AS n_filings
            FROM people p
            JOIN filings f ON f.person_id = p.person_id AND f.is_superseded = 0
            WHERE f.filing_ts >= ?
            GROUP BY p.person_id
            """,
            (cutoff,),
        ).fetchall()
    except Exception:
        return {}

    counts = {str(r["person_id"]): (int(r["n_filings"]), str(r["source"])) for r in rows}
    max_n = max((v[0] for v in counts.values()), default=1) or 1
    out: dict[str, NodeState] = {}
    for pid, (n, src) in counts.items():
        node_id = f"fig.{pid}"
        out[node_id] = NodeState(
            intensity=min(1.0, n / max_n),
            status=src,  # insider / activist / politician
            value=float(n),
            label_extra=f"{n} filings (7d)",
        )
    return out


def _engine_part_intensities(conn: sqlite3.Connection, hb: dict | None) -> dict[str, NodeState]:
    """Core engine parts: liveness from heartbeat + breaker state."""
    daemon_ok = hb is not None
    paused = hb.get("paused", False) if hb else False

    # Check breaker state
    try:
        breakers = conn.execute(
            "SELECT breaker_name, latched FROM breaker_state"
        ).fetchall()
    except Exception:
        breakers = []
    any_latched = any(bool(r["latched"]) for r in breakers)

    base_intensity = 0.8 if daemon_ok and not paused else 0.2
    safe_intensity = 0.3 if any_latched else base_intensity

    out: dict[str, NodeState] = {
        "core.fusion": NodeState(
            intensity=base_intensity,
            status="active" if daemon_ok and not paused else "offline",
        ),
        "core.sizing": NodeState(
            intensity=base_intensity,
            status="active" if daemon_ok and not paused else "offline",
        ),
        "core.gates": NodeState(
            intensity=base_intensity,
            status="active" if daemon_ok and not paused else "offline",
        ),
        "core.safety": NodeState(
            intensity=safe_intensity,
            status="latched" if any_latched else ("active" if daemon_ok else "offline"),
            label_extra="breaker latched" if any_latched else None,
        ),
    }
    return out


def _exec_part_intensities(conn: sqlite3.Connection, hb: dict | None) -> dict[str, NodeState]:
    """Execution cluster: liveness from heartbeat + recent orders."""
    daemon_ok = hb is not None
    try:
        recent_orders = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE created_at >= ?",
            ((_utcnow() - timedelta(seconds=_RECENT_SECONDS)).isoformat(),),
        ).fetchone()[0]
    except Exception:
        recent_orders = 0

    exec_intensity = min(1.0, 0.3 + 0.07 * recent_orders) if daemon_ok else 0.1
    return {
        "exec.adapter": NodeState(
            intensity=exec_intensity,
            status="live" if daemon_ok else "offline",
            value=float(recent_orders),
            label_extra=f"{recent_orders} orders (7d)",
        ),
        "exec.exit_monitor": NodeState(
            intensity=exec_intensity * 0.9,
            status="live" if daemon_ok else "offline",
        ),
        "exec.reconciler": NodeState(
            intensity=exec_intensity * 0.8,
            status="live" if daemon_ok else "offline",
        ),
    }


def _options_node_intensity(conn: sqlite3.Connection) -> dict[str, NodeState]:
    """Intensity for opt.layer from OPTIONS_MODE env + 7-day shadow_log activity.

    options_mode source: shared ``options._options_mode()`` (os.environ, then the
    arbiter .env the daemon loaded).  If it says "off" but shadow rows exist
    recently, we trust the DB activity (env may have changed without a restart).
    """
    from .options import _options_mode  # noqa: PLC0415

    options_mode = _options_mode()

    cutoff = (_utcnow() - timedelta(days=7)).isoformat()
    try:
        shadow_count = conn.execute(
            "SELECT COUNT(*) FROM option_shadow_log WHERE as_of >= ?",
            (cutoff,),
        ).fetchone()[0]
    except Exception:
        shadow_count = 0

    try:
        open_count = conn.execute(
            """
            SELECT COUNT(*) FROM option_positions p
            LEFT JOIN option_outcomes o
                ON o.idea_id = p.idea_id AND o.occ_symbol = p.occ_symbol
            WHERE o.id IS NULL
            """,
        ).fetchone()[0]
    except Exception:
        open_count = 0

    # If env says off but DB shows activity, promote to shadow
    effective_mode = options_mode
    if effective_mode == "off" and shadow_count > 0:
        effective_mode = "shadow"

    if effective_mode == "off":
        intensity = 0.05
        status = "off"
    elif effective_mode == "shadow":
        intensity = min(1.0, 0.3 + 0.05 * shadow_count)
        status = "shadow"
    else:  # paper
        intensity = min(1.0, 0.7 + 0.1 * open_count)
        status = "paper"

    return {
        "opt.layer": NodeState(
            intensity=intensity,
            status=status,
            value=float(open_count),
            label_extra=f"{shadow_count} shadow (7d)",
        )
    }


def _infra_intensities(hb: dict | None) -> dict[str, NodeState]:
    """Infra cluster: daemon + kill switch + alerting from heartbeat."""
    daemon_ok = hb is not None
    paused = hb.get("paused", False) if hb else False

    daemon_intensity = 0.9 if daemon_ok and not paused else (0.3 if daemon_ok else 0.05)
    return {
        "infra.daemon": NodeState(
            intensity=daemon_intensity,
            status="paused" if paused else ("live" if daemon_ok else "offline"),
            label_extra=(
                f"open={hb.get('is_open')}" if hb else "offline"
            ),
        ),
        "infra.killswitch": NodeState(
            intensity=0.8 if not paused else 1.0,
            status="halted" if paused else "armed",
            label_extra="HALTED" if paused else None,
        ),
        "infra.alerting": NodeState(
            intensity=0.5 if daemon_ok else 0.1,
            status="live" if daemon_ok else "offline",
        ),
    }


def _dynamic_flow_edges(conn: sqlite3.Connection) -> tuple[list[Node], list[Edge]]:
    """Build the live flow graph:
    - figure → its advisor (via filing source)
    - advisor → idea (via opinions.idea_id)
    - idea → trade (ticker match, most recent order)
    - trade → outcome (recent)
    - outcome → advisor (teaches edge)
    Also returns dynamic outcome nodes for recent outcomes.
    """
    dyn_nodes: list[Node] = []
    dyn_edges: list[Edge] = []

    # Map: filing source → advisor id
    source_to_advisor = {
        "form4": "A1.insider",
        "form13d": "A1.activist",
        "congress": "A1.congress",
    }

    # --- figure → advisor edges (top active figures, bounded) ---
    cutoff = (_utcnow() - timedelta(seconds=_RECENT_SECONDS)).isoformat()
    try:
        fig_rows = conn.execute(
            """
            SELECT p.person_id, f.source
            FROM people p
            JOIN filings f ON f.person_id = p.person_id AND f.is_superseded = 0
            WHERE f.filing_ts >= ?
            GROUP BY p.person_id, f.source
            ORDER BY COUNT(f.id) DESC
            LIMIT 30
            """,
            (cutoff,),
        ).fetchall()
    except Exception:
        fig_rows = []

    for r in fig_rows:
        pid = str(r["person_id"])
        src = str(r["source"])
        adv = source_to_advisor.get(src)
        if adv:
            eid = f"e.scores.{pid}.{adv}"
            dyn_edges.append(Edge(id=eid, source=f"fig.{pid}", target=adv, kind="scores"))

    # --- advisor → idea edges (via opinions) ---
    try:
        op_rows = conn.execute(
            """
            SELECT DISTINCT advisor_id, idea_id
            FROM opinions
            WHERE is_superseded = 0 AND idea_id IS NOT NULL
            ORDER BY as_of DESC
            LIMIT 50
            """,
        ).fetchall()
    except Exception:
        op_rows = []

    for r in op_rows:
        adv = str(r["advisor_id"])
        iid = r["idea_id"]
        if iid:
            eid = f"e.fuse.op.{adv}.{iid[:8]}"
            dyn_edges.append(
                Edge(id=eid, source=adv, target=f"idea.{iid}", kind="fuses")
            )

    # --- idea → trade edges (by ticker match to live orders) ---
    # Live trades (from Alpaca) may be referenced by ticker; we map idea→trade via ticker.
    try:
        idea_ticker_rows = conn.execute(
            """
            SELECT idea_id, ticker
            FROM ideas
            WHERE is_superseded = 0 AND state NOT IN ('CLOSED','ABANDONED')
            ORDER BY created_at DESC LIMIT 30
            """,
        ).fetchall()
    except Exception:
        idea_ticker_rows = []

    seen_trade_edges: set[str] = set()
    for r in idea_ticker_rows:
        iid = str(r["idea_id"])
        ticker = str(r["ticker"])
        trade_id = f"trade.{ticker}"
        eid = f"e.sub.idea.{ticker}"
        if eid not in seen_trade_edges:
            dyn_edges.append(Edge(id=eid, source=f"idea.{iid}", target=trade_id, kind="submits"))
            seen_trade_edges.add(eid)

    # --- outcome nodes + edges ---
    try:
        outcome_rows = conn.execute(
            """
            SELECT id, idea_id, advisor_id, ticker, alpha_bps, binary, label_kind, created_at
            FROM outcomes
            WHERE is_superseded = 0
            ORDER BY created_at DESC LIMIT 20
            """,
        ).fetchall()
    except Exception:
        outcome_rows = []

    for r in outcome_rows:
        oid = f"outcome.{r['id']}"
        ticker = str(r["ticker"])
        alpha = float(r["alpha_bps"] or 0.0)
        binary = int(r["binary"] or 0)
        label = f"{ticker} {'+' if binary >= 0 else ''}{int(alpha)}bps"
        dyn_nodes.append(Node(
            id=oid, type="outcome", label=label, cluster="learning",
            meta={
                "ticker": ticker,
                "alpha_bps": alpha,
                "binary": binary,
                "label_kind": str(r["label_kind"] or ""),
                "idea_id": str(r["idea_id"] or ""),
                "advisor_id": str(r["advisor_id"] or ""),
            },
        ))
        # trade → outcome
        trade_id = f"trade.{ticker}"
        dyn_edges.append(Edge(
            id=f"e.resolves.{r['id'][:8]}", source=trade_id, target=oid, kind="resolves",
        ))
        # outcome → advisor (teaches)
        adv = str(r["advisor_id"])
        dyn_edges.append(Edge(
            id=f"e.teaches.{r['id'][:8]}", source=oid, target=adv, kind="teaches",
        ))

    return dyn_nodes, dyn_edges


def build_state(conn: sqlite3.Connection) -> State:
    nodes: dict[str, NodeState] = {}

    hb = _heartbeat()
    daemon_ok = hb is not None
    ks = KillSwitch(halted=hb.get("paused") if hb else None)

    # --- advisor intensities (trust weights / cold-start opinions) ---
    nodes.update(_advisor_intensities(conn))

    # --- data source intensities (filing recency / service health) ---
    nodes.update(_data_source_intensities(conn))

    # --- figure intensities (recent filing activity) ---
    nodes.update(_figure_intensities(conn))

    # --- engine part intensities (heartbeat + breakers) ---
    nodes.update(_engine_part_intensities(conn, hb))

    # --- exec part intensities (heartbeat + orders) ---
    nodes.update(_exec_part_intensities(conn, hb))

    # --- infra intensities (daemon/kill-switch/alerting) ---
    nodes.update(_infra_intensities(hb))

    # --- options layer intensity (OPTIONS_MODE env + shadow_log activity) ---
    nodes.update(_options_node_intensity(conn))

    # --- dynamic nodes/edges ---
    idea_nodes, idea_edges = _live_idea_nodes(conn)
    account, trade_nodes, trade_edges, alpaca_ok = _alpaca_snapshot()
    flow_nodes, flow_edges = _dynamic_flow_edges(conn)

    # Deduplicate dynamic nodes by id (flow_nodes may overlap with trade_nodes)
    all_dynamic: dict[str, Node] = {}
    for n in idea_nodes + trade_nodes + flow_nodes:
        all_dynamic[n.id] = n

    # Deduplicate dynamic edges by id
    all_dyn_edges: dict[str, Edge] = {}
    for e in idea_edges + trade_edges + flow_edges:
        all_dyn_edges[e.id] = e

    return State(
        nodes=nodes,
        dynamic_nodes=list(all_dynamic.values()),
        dynamic_edges=list(all_dyn_edges.values()),
        account=account,
        health=Health(db=db_reachable(), daemon=daemon_ok, alpaca=alpaca_ok),
        kill_switch=ks,
        as_of=_now(),
    )
