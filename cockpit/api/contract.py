"""FROZEN read-only API contract for the Arbiter Cockpit.

These DTOs are the single source of truth shared by every build lane.  The
TypeScript mirror lives at ``cockpit/web/src/contract.ts`` and MUST stay in sync.

Nothing here writes; the cockpit is strictly read-only against the trading
system (see ``db.py`` — SQLite opened ``mode=ro``).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# --- Taxonomy -------------------------------------------------------------
NodeType = Literal[
    "data_source",  # SEC EDGAR Form 4 / 13D-G, Congress PTR, Alpaca, MiroFish svc
    "figure",       # a tracked person/fund: politician | insider | activist
    "advisor",      # A1.insider / A1.congress / A1.activist / A2.mirofish / A3
    "engine_part",  # fusion / sizing / gates / safety (the decision core)
    "idea",         # a live Idea (lifecycle FSM)
    "exec_part",    # alpaca adapter / exit monitor / reconciler
    "trade",        # a crystallized position (long/short)
    "outcome",      # a labeled outcome feeding the learning loop
    "infra",        # daemon / kill switch / alerting
]
Cluster = Literal[
    "sources", "figures", "council", "core", "ideas",
    "execution", "market", "learning", "infra",
]
EdgeKind = Literal[
    "ingest",     # data source → advisor
    "discloses",  # figure → data source/filing
    "scores",     # figure/filing → advisor
    "fuses",      # advisor → core
    "decides",    # core internal flow
    "submits",    # core/exec → trade
    "holds",      # trade ↔ market
    "resolves",   # trade → outcome
    "teaches",    # outcome → advisor/figure trust (the feedback loop)
    "gates",      # safety/kill-switch → core
]


class Node(BaseModel):
    id: str
    type: NodeType
    label: str
    cluster: Cluster
    meta: dict = {}


class Edge(BaseModel):
    id: str
    source: str
    target: str
    kind: EdgeKind


class Graph(BaseModel):
    """Static topology — the 'everything we have' inventory, built once."""
    nodes: list[Node]
    edges: list[Edge]


class NodeState(BaseModel):
    intensity: float = 0.0       # 0..1 signal strength / brightness
    status: str | None = None    # e.g. graduated|shadow|suppressed|MONITORED|long|short
    value: float | None = None   # primary metric (weight, notional, P&L, ...)
    label_extra: str | None = None


class Account(BaseModel):
    equity: float | None = None
    daily_pl: float | None = None


class Health(BaseModel):
    db: bool = True
    daemon: bool = False
    alpaca: bool = False


class KillSwitch(BaseModel):
    halted: bool | None = None


class State(BaseModel):
    """Dynamic snapshot — lights up the static graph AND carries the live
    'bodies' (ideas / trades / outcomes) that spawn and despawn over time.

    ``nodes`` keys the STABLE graph nodes (from ``/graph``) by id with their
    current intensity/status.  ``dynamic_nodes`` / ``dynamic_edges`` are the
    transient entities the scene spawns: live Ideas, open Trades, and recent
    Outcomes, plus their edges (figure→idea, idea→trade, trade→outcome→advisor).
    """
    nodes: dict[str, NodeState] = {}
    dynamic_nodes: list[Node] = []
    dynamic_edges: list[Edge] = []
    account: Account = Account()
    health: Health = Health()
    kill_switch: KillSwitch = KillSwitch()
    as_of: str


EventKind = Literal[
    "fill", "idea_new", "idea_transition", "opinion",
    "cover", "outcome", "breaker", "alert", "heartbeat",
]


class Event(BaseModel):
    """One discrete live event, streamed over SSE to drive scene pulses."""
    ts: str
    kind: EventKind
    node_ids: list[str] = []
    payload: dict = {}


class NodeDetail(BaseModel):
    """Deep detail for the inspection panel (shape varies by node type)."""
    id: str
    type: NodeType
    label: str
    summary: dict = {}
    rows: list[dict] = []   # recent filings / opinions / orders / outcomes


# --- Open positions / portfolio (live, read-only from Alpaca) --------------
class OpenPosition(BaseModel):
    """One live open position with cost/share, current price, ROI and P&L."""
    ticker: str
    side: str                          # "long" | "short"
    qty: float                         # ABSOLUTE shares held
    avg_entry: float                   # cost per share
    current_price: float | None = None
    market_value: float | None = None  # signed broker market value
    cost_basis: float | None = None    # |qty| * avg_entry
    unrealized_pl: float | None = None       # $ (broker-signed; loss<0)
    unrealized_pl_pct: float | None = None   # ROI as a fraction (e.g. -0.012)


class Portfolio(BaseModel):
    """Aggregate stats about everything currently open."""
    equity: float | None = None
    cash: float | None = None
    daily_pl: float | None = None
    n_open: int = 0
    n_long: int = 0
    n_short: int = 0
    gross_exposure: float = 0.0        # Σ |market_value|
    net_exposure: float = 0.0          # Σ market_value (long − short)
    total_cost_basis: float = 0.0
    total_unrealized_pl: float = 0.0
    total_unrealized_pl_pct: float | None = None  # total uPL / total cost basis


class PositionsResponse(BaseModel):
    positions: list[OpenPosition] = []
    portfolio: Portfolio = Portfolio()
    as_of: str
    alpaca_ok: bool = False
