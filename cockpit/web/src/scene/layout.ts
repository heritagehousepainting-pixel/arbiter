/**
 * layout.ts — pure, deterministic 3D force layout for the neural constellation.
 *
 * Rules:
 * - Cluster anchors define the topology: CORE at origin, DATA/FIGURES in the
 *   outer shell, COUNCIL between, IDEAS/EXECUTION/MARKET outward, LEARNING
 *   feedback loop high, INFRA low.
 * - We run a fixed number of force-simulation ticks from a seeded RNG, then
 *   freeze positions.  Subsequent calls with the same nodeIds produce the same
 *   result (no reshuffle on /state polls).
 * - Pure function: no React, no THREE.  Returns a LayoutResult { positions, G }.
 */

// d3-force-3d is a JS lib without bundled TS types; import via require-style.
import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  forceZ,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
} from "d3-force-3d";

import type { Cluster, Edge, Node } from "../contract";

// ─── Cluster anchors ────────────────────────────────────────────────────────
// Signed world-space positions.  The constellation reads outer→inner→outer:
//   sources/figures (shell, r≈28) → council (r≈15) → core (0,0,0)
//   → ideas/execution (outward, r≈12-20) → market (r≈22)
//   learning loops high+back, infra below.
// Read as a left→right PIPELINE so the flow is legible:
//   DATA (far left) → FIGURES → COUNCIL → CORE (center) → IDEAS → EXECUTION →
//   TRADES (far right). LEARNING loops over the top (feedback); INFRA below.
// NOTE: these are READ-ONLY inputs to computeG — do NOT modify.
export const CLUSTER_ANCHOR: Record<Cluster, [number, number, number]> = {
  sources:   [-46,  11, -5],
  figures:   [-30,  -1,  2],   // dense bank of smart-money names
  council:   [-14,   5, -2],
  core:      [  0,   0,  0],   // bright decision center
  ideas:     [ 14,  -4,  3],
  execution: [ 28,   4, -3],
  market:    [ 42,  -1,  2],   // crystallized trades
  learning:  [  2,  20,  5],   // feedback loop, up high
  infra:     [ -2, -18, -5],
  options:   [ 28, -14, -4],   // below Execution — options expression layer (amber zone)
};

// ─── Lever A: Density-preserving spread ─────────────────────────────────────
// Replaces the old fixed CLUSTER_SPREAD record.
// Formula: spread(c) = max(MIN_SPREAD[c], PER_NODE_SPACING[c] * sqrt(count))
// Two families ("keep close view" decision, 2026-06-26):
//   • BIG UNLABELED banks (figures, ideas) — hover-only dots, no always-on labels.
//     SMALL per-node spacing so they stay a compact dense bank and don't inflate
//     the world (which would zoom the camera out). They still grow ∝ √count.
//   • LABELED clusters (council/sources/execution/core/market/infra/options) —
//     every node shows a name, so GENEROUS per-node spacing + a large collide
//     radius (below) so labels never stack, even at a close zoom.
const PER_NODE_SPACING: Record<Cluster, number> = {
  sources:   3.2,   // labeled — "SEC 13D/13G (activists)" etc. need room
  figures:   0.52,  // big unlabeled bank — tight & compact; √480 ≈ 11
  council:   5.0,   // labeled — 6 advisor names must not stack (vertical fan)
  core:      4.0,   // labeled — Fusion/Sizing/Gates/Safety
  ideas:     0.52,  // big unlabeled bank (212) — tight & compact, hover-only
  execution: 3.2,   // labeled — Reconciler/Exit monitor/Options Layer/adapter
  market:    3.4,   // labeled — trade tickers (LULU/MS …) must not stack
  learning:  2.3,   // small, unlabeled outcomes
  infra:     3.0,   // labeled — daemon/kill switch/alerting
  options:   3.0,   // labeled — option position nodes
};

const MIN_SPREAD: Record<Cluster, number> = {
  sources:    3.0,
  figures:    6.0,
  council:    3.0,
  core:       2.5,
  ideas:      3.5,
  execution:  3.0,
  market:     3.0,
  learning:   2.5,
  infra:      2.5,
  options:    3.0,
};

/** Lever A: density-preserving cluster radius. sqrt(count) keeps per-node density constant. */
export function computeSpread(cluster: Cluster, count: number): number {
  return Math.max(
    MIN_SPREAD[cluster] ?? 2.5,
    (PER_NODE_SPACING[cluster] ?? 2.0) * Math.sqrt(Math.max(1, count)),
  );
}

// ─── Lever B: Label-aware collision radii ────────────────────────────────────
// Labeled clusters need more space so node labels never stack. This set tracks
// the clusters whose node types are always-on labeled in Labels.tsx LABEL_TYPES
// (advisor, engine_part, exec_part, data_source, infra, trade).
// figures cluster is NOT in this set — it stays tightly packed (no always-on labels).
// learning is NOT in this set — it holds "outcome" nodes, not a LABEL_TYPE.
export const LABELED_CLUSTERS = new Set<Cluster>([
  "council", "core", "execution", "sources", "infra", "market", "options",
]);
const labeledRadius = 5.6;   // generous: labeled nodes keep enough screen gap that names never stack
const figureRadius  = 1.1;   // tight packing for the big unlabeled banks (figures, ideas)

// ─── Lever C: Global zone scale G ────────────────────────────────────────────
// Adjacent cluster pairs whose spread spheres might overlap as clusters grow.
const ADJACENT_PAIRS: [Cluster, Cluster][] = [
  ["sources",   "figures"],
  ["figures",   "council"],
  ["council",   "core"],
  ["core",      "ideas"],
  ["ideas",     "execution"],
  ["execution", "market"],
  ["execution", "options"],
  ["core",      "learning"],
  ["core",      "infra"],
];
const MIN_ZONE_GAP = 2.0;   // minimum world-unit clearance between adjacent spread spheres

/**
 * Lever C: compute global scale G so adjacent cluster spread-spheres don't overlap.
 * G=1 at today's typical node counts; G>1 when clusters grow large enough to crowd.
 */
export function computeG(
  clusterCounts: Map<Cluster, number>,
  minGap = MIN_ZONE_GAP,
): number {
  let G = 1.0;
  for (const [a, b] of ADJACENT_PAIRS) {
    const cntA = clusterCounts.get(a) ?? 1;
    const cntB = clusterCounts.get(b) ?? 1;
    const sA = computeSpread(a, cntA);
    const sB = computeSpread(b, cntB);
    const [ax, ay, az] = CLUSTER_ANCHOR[a];
    const [bx, by, bz] = CLUSTER_ANCHOR[b];
    const dist = Math.sqrt((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2);
    if (dist > 0) G = Math.max(G, (sA + sB + minGap) / dist);
  }
  return G;
}

// ─── Seeded LCG PRNG ────────────────────────────────────────────────────────
// Deterministic pseudo-random numbers so positions are stable across hot-reload.
function makeLCG(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = Math.imul(1664525, s) + 1013904223;
    return (s >>> 0) / 4294967296;
  };
}

// ─── Types ───────────────────────────────────────────────────────────────────
interface SimNode {
  id: string;
  cluster: Cluster;
  x: number;
  y: number;
  z: number;
  fx?: number | null;
  fy?: number | null;
  fz?: number | null;
}

interface SimLink {
  source: string | SimNode;
  target: string | SimNode;
}

/** Options for computeLayout (Lever D: warm-start). */
export interface LayoutOpts {
  seed?: number;    // default 42
  ticks?: number;   // default 200; use 80 for warm-start re-settles
  initial?: Map<string, [number, number, number]>;  // warm-start seed
}

/** Return value: stable positions + the global scale G (single source of truth for labels). */
export interface LayoutResult {
  positions: Map<string, [number, number, number]>;
  G: number;
}

// ─── Main layout function ────────────────────────────────────────────────────
/**
 * Compute stable 3D positions for a set of nodes + edges.
 *
 * Levers applied:
 *   A — density-preserving spread (computeSpread replaces fixed CLUSTER_SPREAD)
 *   B — label-aware collision (labeledRadius vs figureRadius)
 *   C — global zone scale G (anchors scaled radially from core by G)
 *   D — warm-start: known nodes seed from opts.initial; new nodes use PRNG
 *
 * @param nodes   All nodes (graph.nodes ++ dynamic_nodes)
 * @param edges   All edges  (graph.edges ++ dynamic_edges)
 * @param opts    { seed?, ticks?, initial? }
 * @returns       { positions: Map<nodeId, [x,y,z]>, G: number }
 */
export function computeLayout(
  nodes: Node[],
  edges: Edge[],
  opts?: LayoutOpts,
): LayoutResult {
  if (nodes.length === 0) return { positions: new Map(), G: 1 };

  const { seed = 42, ticks = 200, initial } = opts ?? {};

  const rng = makeLCG(seed);

  // ── Lever C: compute G from cluster counts ───────────────────────────────
  const clusterCounts = new Map<Cluster, number>();
  for (const n of nodes) {
    clusterCounts.set(n.cluster, (clusterCounts.get(n.cluster) ?? 0) + 1);
  }
  const G = computeG(clusterCounts);

  // ── Build SimNodes ───────────────────────────────────────────────────────
  // Lever A: use computeSpread instead of fixed CLUSTER_SPREAD.
  // Lever C: scale anchors by G.
  // Lever D: use warm-start positions for known nodes, PRNG for new ones.
  //          IMPORTANT: consume 3 PRNG calls for EVERY node so the seed sequence
  //          for new nodes is deterministic regardless of warm-start status.
  const simNodes: SimNode[] = nodes.map((n) => {
    const count = clusterCounts.get(n.cluster) ?? 1;
    const spread = computeSpread(n.cluster, count);
    const [ax, ay, az] = CLUSTER_ANCHOR[n.cluster] ?? [0, 0, 0];

    // Always consume the 3 PRNG values (deterministic sequence for new nodes)
    const theta = rng() * Math.PI * 2;
    const phi = Math.acos(2 * rng() - 1);
    const r = spread * (0.4 + rng() * 0.6);

    // Lever D: warm-start from initial if available
    if (initial?.has(n.id)) {
      const [ix, iy, iz] = initial.get(n.id)!;
      return { id: n.id, cluster: n.cluster, x: ix, y: iy, z: iz };
    }

    // New node: seed near the (G-scaled) cluster anchor
    return {
      id: n.id,
      cluster: n.cluster,
      x: ax * G + r * Math.sin(phi) * Math.cos(theta),
      y: ay * G + r * Math.cos(phi),
      z: az * G + r * Math.sin(phi) * Math.sin(theta),
    };
  });

  const nodeById = new Map(simNodes.map((n) => [n.id, n]));

  // Build sim links — only include links where both endpoints exist
  const simLinks: SimLink[] = edges
    .filter((e) => nodeById.has(e.source) && nodeById.has(e.target))
    .map((e) => ({ source: e.source, target: e.target }));

  // d3-force-3d simulation (numDimensions=3).
  // NO forceCenter — it pulls every cluster back to the origin and collapses
  // the pipeline into a blob.  Links are WEAK (just a hint) so cross-cluster
  // edges (figure→core etc.) don't drag the zones together; the strong cluster
  // anchors keep each layer in its own readable region.
  // Lever B: cluster-aware collision radius (labeled vs figure).
  // Lever C: anchor targets scaled by G.
  const sim = forceSimulation(simNodes, 3)
    .force("charge", forceManyBody().strength(-9).distanceMax(22))
    .force(
      "link",
      forceLink(simLinks)
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        .id((d: any) => (d as SimNode).id)
        .distance(10)
        .strength(0.02),
    )
    // Lever B: labeled clusters get larger collision radius (label room)
    .force(
      "collide",
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      forceCollide((d: any) =>
        LABELED_CLUSTERS.has((d as SimNode).cluster) ? labeledRadius : figureRadius,
      ),
    )
    // Anchor clusters to their G-scaled positions
    .force(
      "cx",
      forceX((d: SimNode) => CLUSTER_ANCHOR[d.cluster][0] * G).strength(0.92),
    )
    .force(
      "cy",
      forceY((d: SimNode) => CLUSTER_ANCHOR[d.cluster][1] * G).strength(0.9),
    )
    .force(
      "cz",
      forceZ((d: SimNode) => CLUSTER_ANCHOR[d.cluster][2] * G).strength(0.9),
    )
    .stop();

  // Run fixed ticks (synchronous) — no animation loop
  for (let i = 0; i < ticks; i++) sim.tick();

  // Collect final positions
  const positions = new Map<string, [number, number, number]>();
  for (const sn of simNodes) {
    positions.set(sn.id, [sn.x ?? 0, sn.y ?? 0, sn.z ?? 0]);
  }
  return { positions, G };
}

/**
 * Convenience: re-run layout only when the node-id set changes.
 * Stable positions for known nodes; new dynamic nodes get fresh positions
 * appended without reshuffling existing ones.
 *
 * NOTE: kept for backward compatibility; SceneRoot uses warm-start computeLayout instead.
 */
export function mergeLayout(
  existing: Map<string, [number, number, number]>,
  nodes: Node[],
  edges: Edge[],
  seed = 42,
): Map<string, [number, number, number]> {
  const newNodes = nodes.filter((n) => !existing.has(n.id));
  if (newNodes.length === 0) return existing;

  // Compute positions for new nodes only, using a derived seed
  const { positions: patch } = computeLayout(newNodes, edges, { seed: seed + existing.size });
  const merged = new Map(existing);
  for (const [id, pos] of patch) merged.set(id, pos);
  return merged;
}
