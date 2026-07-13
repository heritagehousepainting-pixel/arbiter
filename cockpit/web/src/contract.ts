// FROZEN contract — mirror of cockpit/api/contract.py. Keep in sync.
// The cockpit is strictly READ-ONLY against the trading system.

export type NodeType =
  | "data_source" | "figure" | "advisor" | "engine_part" | "idea"
  | "exec_part" | "trade" | "outcome" | "infra";

export type Cluster =
  | "sources" | "figures" | "council" | "core" | "ideas"
  | "execution" | "market" | "learning" | "infra" | "options";

export type EdgeKind =
  | "ingest" | "discloses" | "scores" | "fuses" | "decides"
  | "submits" | "holds" | "resolves" | "teaches" | "gates";

export interface Node {
  id: string;
  type: NodeType;
  label: string;
  cluster: Cluster;
  meta: Record<string, unknown>;
}

export interface Edge {
  id: string;
  source: string;
  target: string;
  kind: EdgeKind;
}

export interface Graph {
  nodes: Node[];
  edges: Edge[];
}

export interface NodeState {
  intensity: number; // 0..1
  status?: string | null;
  value?: number | null;
  label_extra?: string | null;
}

export interface Account {
  equity: number | null;
  daily_pl: number | null;
}

export interface Health {
  db: boolean;
  daemon: boolean;
  alpaca: boolean;
}

export interface KillSwitch {
  halted: boolean | null;
}

export interface State {
  nodes: Record<string, NodeState>;
  dynamic_nodes: Node[];
  dynamic_edges: Edge[];
  account: Account;
  health: Health;
  kill_switch: KillSwitch;
  as_of: string;
}

export type EventKind =
  | "fill" | "idea_new" | "idea_transition" | "opinion"
  | "cover" | "outcome" | "breaker" | "alert" | "heartbeat";

export interface CockpitEvent {
  ts: string;
  kind: EventKind;
  node_ids: string[];
  payload: Record<string, unknown>;
}

export interface NodeDetail {
  id: string;
  type: NodeType;
  label: string;
  summary: Record<string, unknown>;
  rows: Record<string, unknown>[];
}

export interface OpenPosition {
  ticker: string;
  side: "long" | "short";
  qty: number;
  avg_entry: number;
  current_price: number | null;
  market_value: number | null;
  cost_basis: number | null;
  unrealized_pl: number | null;
  unrealized_pl_pct: number | null; // ROI as a fraction (e.g. -0.012)
  day_change_pct: number | null;    // day % as a fraction (e.g. 0.0099 = 0.99 %)
}

export interface Portfolio {
  equity: number | null;
  cash: number | null;
  daily_pl: number | null;
  n_open: number;
  n_long: number;
  n_short: number;
  gross_exposure: number;
  net_exposure: number;
  total_cost_basis: number;
  total_unrealized_pl: number;
  total_unrealized_pl_pct: number | null;
}

export interface PositionsResponse {
  positions: OpenPosition[];
  portfolio: Portfolio;
  as_of: string;
  alpaca_ok: boolean;
}

export interface TickerDetail {
  symbol: string;                    // always upper-cased
  name: string | null;               // from GET /v2/assets/{symbol}
  month_return_pct: number | null;   // (latest_bar_close - oldest_bar_close) / oldest_bar_close
  day_change_pct: number | null;     // (latest_bar_close - prev_bar_close) / prev_bar_close
  current_price: number | null;      // echoed from latest daily bar close
  as_of: string;                     // UTC ISO timestamp of fetch
}

// --- Watchlist live charts (mirror of ChartSeries in contract.py) ------------
export type ChartRange = "live" | "5d" | "1m" | "3m" | "6m";

export interface Candle {
  t: string;          // ISO-8601 UTC bar-open timestamp
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
  session: "pre" | "regular" | "post";
}

export interface ChartSeries {
  symbol: string;
  range: ChartRange;
  candles: Candle[];
  extended_available: boolean;   // true when pre/post-market bars are present
  as_of: string;
  alpaca_ok: boolean;
}

// --- Options layer -----------------------------------------------------------
export type OptionsMode = "off" | "shadow" | "paper";

export interface OpenOptionPosition {
  id: string;
  idea_id: string;
  underlying: string;
  occ_symbol: string;
  side: "call" | "put";
  strike: number;
  expiry: string;
  contracts_qty: number;
  entry_premium: number;
  delta_at_open: number | null;
  iv_at_open: number | null;
  underlying_open_price: number;
  thesis_horizon_date: string;
  original_conviction: number;
  open_ts: string;
  dte: number | null;
  current_mid: number | null;
  unrealized_pl: number | null;
  unrealized_pl_pct: number | null;
}

export interface OptionShadowPlay {
  id: string;
  idea_id: string;
  underlying: string;
  as_of: string;
  gate_express: boolean;
  gate_reason: string;
  side: "call" | "put" | null;
  occ_symbol: string | null;
  strike: number | null;
  expiry: string | null;
  delta: number | null;
  iv: number | null;
  est_premium: number | null;
  delta_adjusted_notional: number | null;
  contracts_qty: number | null;
  conviction: number;
  horizon_days: number;
  catalyst_tag: string | null;
  ivr_estimate: number | null;
  created_at: string;
}

export interface OptionOutcomeRecord {
  id: string;
  idea_id: string;
  underlying: string;
  occ_symbol: string;
  side: "call" | "put";
  open_ts: string;
  close_ts: string;
  close_reason: string;
  entry_premium: number;
  exit_premium: number;
  option_pl_pct: number;
  underlying_alpha_bps: number;
  delta_at_open: number | null;
  iv_at_open: number | null;
  iv_at_close: number | null;
  contracts_qty: number;
  created_at: string;
}

export interface IVPoint {
  as_of: string;
  atm_iv: number;
  occ_symbol: string;
}

export interface IVSeries {
  underlying: string;
  points: IVPoint[];
  current_iv_rank: number | null;
  as_of: string;
}

export interface OptionsState {
  options_mode: OptionsMode;
  open_positions: OpenOptionPosition[];
  recent_shadow_plays: OptionShadowPlay[];
  recent_outcomes: OptionOutcomeRecord[];
  n_open: number;
  sleeve_used_pct: number | null;
  win_rate: number | null;
  avg_option_pl_pct: number | null;
  avg_underlying_alpha_bps: number | null;
  as_of: string;
}

// --- Robotics watchlist (mirror of RoboticsRosterEntry/RoboticsWatchlist in contract.py) ------
export type RoboticsLayer = "compute" | "brain" | "components" | "integrator" | "deployment";
export type RoboticsLongevity = "chokepoint" | "durable" | "commodity" | "hype-risk" | "unclear";

export interface RoboticsRosterEntry {
  symbol: string;
  company: string;
  layer: RoboticsLayer;
  longevity: RoboticsLongevity;
  priceable: boolean;
  form_factors: string[];
  early_insight: boolean;
  trigger: string | null;
  region: string | null;
  note: string | null;
}

export interface RoboticsWatchlist {
  generated: string;
  entries: RoboticsRosterEntry[];
}

// Cluster → accent color (lane 5 may refine into proper design tokens).
export const CLUSTER_COLOR: Record<Cluster, string> = {
  sources: "#5b8cff",
  figures: "#ffd166",
  council: "#06d6a0",
  core: "#ef476f",
  ideas: "#c77dff",
  execution: "#4cc9f0",
  market: "#ffffff",
  learning: "#80ed99",
  infra: "#8d99ae",
  options: "#f9a825",   // deep amber — options / volatility layer
};
