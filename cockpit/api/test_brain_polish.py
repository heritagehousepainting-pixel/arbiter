"""Brain-polish invariants: no orphan nodes, no dangling edges, option positions
become connected amber nodes. Guards the 2026-06-26 constellation polish."""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_ARBITER_ROOT = _REPO_ROOT / "arbiter"
if str(_ARBITER_ROOT) not in sys.path:
    sys.path.insert(0, str(_ARBITER_ROOT))

from cockpit.api.db import connect  # noqa: E402
from cockpit.api.graph import build_graph  # noqa: E402
from cockpit.api.node_detail import build_node_detail  # noqa: E402
from cockpit.api.state import build_state  # noqa: E402
from cockpit.api.test_options import _build_option_fixture_db  # noqa: E402


def _conn(tmp_path):
    db = tmp_path / "brain.db"
    _build_option_fixture_db(str(db))
    return connect(str(db))


def test_no_orphan_static_nodes(tmp_path):
    """Every static graph node is touched by at least one edge (no free-hangers)."""
    conn = _conn(tmp_path)
    g = build_graph(conn)
    touched = {e.source for e in g.edges} | {e.target for e in g.edges}
    orphans = [n.id for n in g.nodes if n.id not in touched]
    assert orphans == [], f"orphan static nodes: {orphans}"
    # the three that used to float are now wired
    for nid in ("src.alpaca", "exec.reconciler", "infra.alerting"):
        assert nid in touched


def test_open_option_becomes_connected_amber_node(tmp_path):
    """Open option positions render as 'options'-cluster nodes wired to opt.layer."""
    conn = _conn(tmp_path)
    st = build_state(conn)
    opt_nodes = [n for n in st.dynamic_nodes if n.cluster == "options"]
    assert len(opt_nodes) >= 1, "expected at least one open-option node"
    assert all(n.id.startswith("option_position.") for n in opt_nodes)
    opt_ids = {n.id for n in opt_nodes}
    # every option node is reachable from opt.layer (so it has a path into the brain)
    expressed = {e.target for e in st.dynamic_edges
                 if e.source == "opt.layer" and e.kind == "submits"}
    assert opt_ids <= expressed, f"option nodes not wired to opt.layer: {opt_ids - expressed}"


def test_no_dangling_dynamic_edges(tmp_path):
    """No dynamic edge points at a node that isn't rendered (no lines to nowhere)."""
    conn = _conn(tmp_path)
    g = build_graph(conn)
    st = build_state(conn)
    node_ids = {n.id for n in g.nodes} | {n.id for n in st.dynamic_nodes}
    dangling = [
        (e.id, e.source, e.target)
        for e in st.dynamic_edges
        if e.source not in node_ids or e.target not in node_ids
    ]
    assert dangling == [], f"dangling dynamic edges: {dangling[:5]}"


def test_option_position_node_detail(tmp_path):
    """Clicking an option node returns a detail with the option summary."""
    conn = _conn(tmp_path)
    st = build_state(conn)
    opt = next(n for n in st.dynamic_nodes if n.cluster == "options")
    detail = build_node_detail(conn, opt.id)
    assert detail is not None
    assert detail.summary.get("underlying")
    assert "dte" in detail.summary
    assert "occ_symbol" in detail.summary
