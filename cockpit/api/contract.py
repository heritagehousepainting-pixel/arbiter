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
    "execution", "market", "learning", "infra", "options",
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
    day_change_pct: float | None = None      # day % as a fraction (e.g. 0.0099 = 0.99 %)


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


class TickerDetail(BaseModel):
    """Company name + 1-month return for one held ticker (lazy, per-expand)."""
    symbol: str                              # always upper-cased
    name: str | None = None                  # from GET /v2/assets/{symbol}
    month_return_pct: float | None = None    # (latest_bar_close - oldest_bar_close) / oldest_bar_close
    day_change_pct: float | None = None      # (latest_bar_close - prev_bar_close) / prev_bar_close
    current_price: float | None = None       # echoed from latest daily bar close
    as_of: str                               # UTC ISO timestamp of fetch


# --- Options layer -------------------------------------------------------
OptionsMode = Literal["off", "shadow", "paper"]


class OpenOptionPosition(BaseModel):
    """One live open option position (option_positions row with no matching outcome)."""
    id: str                                    # ULID PK from option_positions
    idea_id: str
    underlying: str                            # equity ticker, e.g. "AAPL"
    occ_symbol: str                            # OCC symbol
    side: str                                  # "call" | "put"
    strike: float
    expiry: str                                # ISO date string
    contracts_qty: int
    entry_premium: float                       # total USD premium paid to open
    delta_at_open: float | None = None
    iv_at_open: float | None = None
    underlying_open_price: float
    thesis_horizon_date: str                   # ISO date string
    original_conviction: float
    open_ts: str                               # UTC ISO timestamp
    # Computed fields (None when live price unavailable from DB)
    dte: int | None = None
    current_mid: float | None = None           # live mid-price (not stored in DB)
    unrealized_pl: float | None = None
    unrealized_pl_pct: float | None = None


class OptionShadowPlay(BaseModel):
    """One row from option_shadow_log — a would-have-traded evaluation."""
    id: str
    idea_id: str
    underlying: str
    as_of: str
    gate_express: bool                         # True = gate fired (would have traded)
    gate_reason: str                           # "OK" | "IV_RANK_TOO_HIGH" | etc.
    side: str | None = None                    # "call" | "put" | None
    occ_symbol: str | None = None
    strike: float | None = None
    expiry: str | None = None
    delta: float | None = None
    iv: float | None = None
    est_premium: float | None = None
    delta_adjusted_notional: float | None = None
    contracts_qty: int | None = None
    conviction: float
    horizon_days: float
    catalyst_tag: str | None = None
    ivr_estimate: float | None = None
    created_at: str


class OptionOutcomeRecord(BaseModel):
    """One closed option trade — display-only, isolated from equity learning."""
    id: str
    idea_id: str
    underlying: str
    occ_symbol: str
    side: str
    open_ts: str
    close_ts: str
    close_reason: str
    entry_premium: float
    exit_premium: float
    option_pl_pct: float         # (exit - entry) / entry; display only, NOT advisory trust
    underlying_alpha_bps: float
    delta_at_open: float | None = None
    iv_at_open: float | None = None
    iv_at_close: float | None = None
    contracts_qty: int
    created_at: str


class IVPoint(BaseModel):
    """One ATM-IV data point for a single underlying."""
    as_of: str
    atm_iv: float                # annualised, decimal (0.38 = 38%)
    occ_symbol: str


class IVSeries(BaseModel):
    """IV history series for one underlying ticker."""
    underlying: str
    points: list[IVPoint] = []
    current_iv_rank: float | None = None   # IVR ∈ [0,1]; None when <30 data points
    as_of: str


class OptionsState(BaseModel):
    """Complete options data snapshot, served at GET /options."""
    options_mode: OptionsMode = "off"
    open_positions: list[OpenOptionPosition] = []
    recent_shadow_plays: list[OptionShadowPlay] = []   # last 20, mixed express/reject
    recent_outcomes: list[OptionOutcomeRecord] = []    # last 20 closed trades
    # Aggregates (None when no data yet)
    n_open: int = 0
    sleeve_used_pct: float | None = None               # requires account equity; None offline
    win_rate: float | None = None
    avg_option_pl_pct: float | None = None
    avg_underlying_alpha_bps: float | None = None
    as_of: str


# --- Watchlist live charts (read-only; served at GET /chart/{symbol}) ---------
class Candle(BaseModel):
    """One OHLCV bar with its trading-session classification."""
    t: str              # ISO-8601 UTC bar-open timestamp
    o: float
    h: float
    l: float
    c: float
    v: float
    session: str        # "pre" | "regular" | "post"


class ChartSeries(BaseModel):
    """Chart data for one ticker + range. Fail-closed: empty candles on error."""
    symbol: str
    range: str                          # "live" | "5d" | "1m" | "3m" | "6m"
    candles: list[Candle] = []
    extended_available: bool = False    # True when pre/post-market bars are present
    as_of: str
    alpaca_ok: bool = False


# --- Robotics watchlist (display-only; see docs/specs/2026-07-13-robotics-watchlist-design.md) --
RoboticsLayer = Literal["compute", "brain", "components", "integrator", "deployment"]
RoboticsLongevity = Literal["chokepoint", "durable", "commodity", "hype-risk", "unclear"]


class RoboticsRosterEntry(BaseModel):
    """One curated robotics-universe row. DISPLAY-ONLY — never trade-eligible."""
    symbol: str                              # display ticker (home-exchange for reference rows)
    company: str
    layer: RoboticsLayer
    longevity: RoboticsLongevity
    priceable: bool                          # True → live price via /ticker; False → reference row
    form_factors: list[str] = []
    early_insight: bool = False              # ⭐ from robotics_map.json earlyInsightCandidates
    trigger: str | None = None               # "trigger to watch" text for early-insight rows
    region: str | None = None
    note: str | None = None


class RoboticsWatchlist(BaseModel):
    """The full curated roster, served at GET /robotics-watchlist (static, read-only)."""
    generated: str
    entries: list[RoboticsRosterEntry] = []
