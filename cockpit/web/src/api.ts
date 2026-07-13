// Read-only client for the cockpit sidecar.
import type {
  ChartRange,
  ChartSeries,
  CockpitEvent,
  Graph,
  IVSeries,
  NodeDetail,
  OptionsState,
  PositionsResponse,
  RoboticsSignals,
  RoboticsWatchlist,
  State,
  TickerDetail,
} from "./contract";

const BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8910";

async function get<T>(path: string): Promise<T> {
  const r = await fetch(`${BASE}${path}`);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return (await r.json()) as T;
}

export const fetchGraph = () => get<Graph>("/graph");
export const fetchState = () => get<State>("/state");
export const fetchNode = (id: string) => get<NodeDetail>(`/node/${encodeURIComponent(id)}`);
export const fetchPositions = () => get<PositionsResponse>("/positions");
export const fetchOptions = () => get<OptionsState>("/options");
export const fetchIvSeries = (ticker: string) => get<IVSeries>(`/options/iv/${encodeURIComponent(ticker)}`);
export const fetchTickerDetail = (symbol: string) =>
  get<TickerDetail>(`/ticker/${encodeURIComponent(symbol)}`);
export const fetchChart = (symbol: string, range: ChartRange) =>
  get<ChartSeries>(`/chart/${encodeURIComponent(symbol)}?range=${range}`);
export const fetchRoboticsWatchlist = () => get<RoboticsWatchlist>("/robotics-watchlist");
export const fetchRoboticsSignals = () => get<RoboticsSignals>("/robotics-signals");

/** Subscribe to the live SSE event stream (Lane 2 implements /events). */
export function subscribeEvents(onEvent: (e: CockpitEvent) => void): () => void {
  const es = new EventSource(`${BASE}/events`);
  es.onmessage = (msg) => {
    try {
      onEvent(JSON.parse(msg.data) as CockpitEvent);
    } catch {
      /* ignore malformed frame */
    }
  };
  return () => es.close();
}
