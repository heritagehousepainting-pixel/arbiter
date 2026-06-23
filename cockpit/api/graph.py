"""Build the STABLE constellation topology (``/graph``) from the live DB.

The stable skeleton = data sources → tracked figures → advisors → decision core
→ execution → infra, plus the feedback (learning-loop) edges.  Live ideas /
trades / outcomes are NOT here — they are dynamic bodies served by ``state.py``.

Read-only: every query goes through ``db.connect`` (mode=ro).
"""
from __future__ import annotations

import sqlite3

from .contract import Edge, Graph, Node

# --- Fixed structural nodes ------------------------------------------------
_DATA_SOURCES = [
    ("src.form4", "SEC Form 4 (insiders)"),
    ("src.form13d", "SEC 13D/13G (activists)"),
    ("src.form13f", "SEC 13F (fund managers)"),
    ("src.congress", "Congress PTR (House+Senate)"),
    ("src.alpaca", "Alpaca market data"),
    ("src.mirofish", "MiroFish A2 service"),
]
_ADVISORS = [
    ("A1.insider", "A1 · Insiders", "form4"),
    ("A1.congress", "A1 · Congress", "congress"),
    ("A1.activist", "A1 · Activists", "form13d"),
    ("A1.fund", "A1 · Funds", "form13f"),
    ("A2.mirofish", "A2 · MiroFish", "mirofish"),
    ("A3.news", "A3 · News", "news"),
]
_CORE_PARTS = [
    ("core.fusion", "Fusion"),
    ("core.sizing", "Sizing (¼-Kelly/ADV)"),
    ("core.gates", "Gates (risk book)"),
    ("core.safety", "Safety (breakers)"),
]
_EXEC_PARTS = [
    ("exec.adapter", "Alpaca paper adapter"),
    ("exec.exit_monitor", "Exit monitor (long+short)"),
    ("exec.reconciler", "Reconciler"),
]
_INFRA = [
    ("infra.daemon", "Market-hours daemon"),
    ("infra.killswitch", "Kill switch"),
    ("infra.alerting", "Alerting (ntfy)"),
]

# source id → advisor id (which data source feeds which advisor)
_SOURCE_TO_ADVISOR = {
    "src.form4": "A1.insider",
    "src.congress": "A1.congress",
    "src.form13d": "A1.activist",
    "src.form13f": "A1.fund",
    "src.mirofish": "A2.mirofish",
}
# filings.source value → data source node id (figure → source disclosure edge)
_FILING_SOURCE_TO_NODE = {
    "form4": "src.form4",
    "form13d": "src.form13d",
    "form13f": "src.form13f",
    "congress": "src.congress",
}
_FIGURE_KIND = {  # people.source → human label of the figure kind
    "form4": "insider",
    "form13d": "activist",
    "form13f": "fund manager",
    "congress": "politician",
}


def _figure_nodes(conn: sqlite3.Connection) -> tuple[list[Node], list[Edge]]:
    """Tracked figures (people) + their disclosure edges to a data source.

    Only people with at least one filing are surfaced (the ones we actually
    follow); each links to the data source its disclosures come through.
    """
    rows = conn.execute(
        """
        SELECT p.person_id, p.canonical_name, p.source,
               COUNT(f.id) AS n_filings,
               GROUP_CONCAT(DISTINCT f.source) AS filing_sources
        FROM people p
        JOIN filings f ON f.person_id = p.person_id AND f.is_superseded = 0
        GROUP BY p.person_id
        ORDER BY n_filings DESC
        """
    ).fetchall()
    nodes: list[Node] = []
    edges: list[Edge] = []
    for r in rows:
        fid = f"fig.{r['person_id']}"
        kind = _FIGURE_KIND.get(str(r["source"]), "insider")
        nodes.append(Node(
            id=fid, type="figure", label=str(r["canonical_name"] or r["person_id"]),
            cluster="figures",
            meta={"kind": kind, "n_filings": int(r["n_filings"] or 0),
                  "source": str(r["source"] or "")},
        ))
        for fs in str(r["filing_sources"] or "").split(","):
            src_node = _FILING_SOURCE_TO_NODE.get(fs.strip())
            if src_node:
                edges.append(Edge(id=f"e.disc.{r['person_id']}.{fs.strip()}",
                                  source=fid, target=src_node, kind="discloses"))
    return nodes, edges


def build_graph(conn: sqlite3.Connection) -> Graph:
    nodes: list[Node] = []
    edges: list[Edge] = []

    for sid, label in _DATA_SOURCES:
        nodes.append(Node(id=sid, type="data_source", label=label, cluster="sources"))
    for aid, label, _src in _ADVISORS:
        nodes.append(Node(id=aid, type="advisor", label=label, cluster="council",
                          meta={"future": False}))
    for cid, label in _CORE_PARTS:
        nodes.append(Node(id=cid, type="engine_part", label=label, cluster="core"))
    for eid, label in _EXEC_PARTS:
        nodes.append(Node(id=eid, type="exec_part", label=label, cluster="execution"))
    for iid, label in _INFRA:
        nodes.append(Node(id=iid, type="infra", label=label, cluster="infra"))

    fig_nodes, fig_edges = _figure_nodes(conn)
    nodes.extend(fig_nodes)
    edges.extend(fig_edges)

    # source → advisor (ingest)
    for sid, aid in _SOURCE_TO_ADVISOR.items():
        edges.append(Edge(id=f"e.ingest.{sid}", source=sid, target=aid, kind="ingest"))
    # advisor → fusion (fuses)
    for aid, _l, _s in _ADVISORS:
        edges.append(Edge(id=f"e.fuse.{aid}", source=aid, target="core.fusion", kind="fuses"))
    # core internal flow (decides)
    for a, b in [("core.fusion", "core.sizing"), ("core.sizing", "core.gates"),
                 ("core.gates", "core.safety")]:
        edges.append(Edge(id=f"e.dec.{a}.{b}", source=a, target=b, kind="decides"))
    # core → execution → market (submits/holds)
    edges.append(Edge(id="e.sub.core.adapter", source="core.safety",
                      target="exec.adapter", kind="submits"))
    edges.append(Edge(id="e.exec.monitor", source="exec.adapter",
                      target="exec.exit_monitor", kind="decides"))
    # infra gating the core
    edges.append(Edge(id="e.gate.kill", source="infra.killswitch",
                      target="core.safety", kind="gates"))
    edges.append(Edge(id="e.gate.daemon", source="infra.daemon",
                      target="core.fusion", kind="gates"))
    # learning loop: outcomes teach advisors (edge target; source is dynamic outcome)
    # represented structurally as advisor self-trust anchor for the scene.
    return Graph(nodes=nodes, edges=edges)
