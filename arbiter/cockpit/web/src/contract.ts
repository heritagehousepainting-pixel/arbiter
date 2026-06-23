// FROZEN contract — mirror of cockpit/api/contract.py. Keep in sync.
// The cockpit is strictly READ-ONLY against the trading system.

export type NodeType =
  | "data_source" | "figure" | "advisor" | "engine_part" | "idea"
  | "exec_part" | "trade" | "outcome" | "infra";

export type Cluster =
  | "sources" | "figures" | "council" | "core" | "ideas"
  | "execution" | "market" | "learning" | "infra";

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
};
