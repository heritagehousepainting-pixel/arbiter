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
 * - Pure function: no React, no THREE.  Returns a plain Map<id, [x,y,z]>.
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

// Radii of intra-cluster spread (world units) — figures are dense, core tight
const CLUSTER_SPREAD: Record<Cluster, number> = {
  sources:   4.5,
  figures:   11.0,  // many nodes → allow room
  council:   4.5,
  core:      4.0,
  ideas:     5.5,
  execution: 5.0,
  market:    4.5,
  learning:  4.0,
  infra:     4.0,
  options:   4.5,   // typically 2-5 option position nodes
};

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

// ─── Main layout function ────────────────────────────────────────────────────
/**
 * Compute stable 3D positions for a set of nodes + edges.
 *
 * @param nodes      All nodes (graph.nodes ++ dynamic_nodes)
 * @param edges      All edges  (graph.edges ++ dynamic_edges)
 * @param seed       Integer seed for the PRNG (default 42)
 * @param ticks      Force-simulation iterations (default 200)
 * @returns          Map<nodeId, [x, y, z]>
 */
export function computeLayout(
  nodes: Node[],
  edges: Edge[],
  seed = 42,
  ticks = 200,
): Map<string, [number, number, number]> {
  if (nodes.length === 0) return new Map();

  const rng = makeLCG(seed);

  // Build sim nodes with initial positions near the cluster anchor
  const simNodes: SimNode[] = nodes.map((n) => {
    const [ax, ay, az] = CLUSTER_ANCHOR[n.cluster] ?? [0, 0, 0];
    const spread = CLUSTER_SPREAD[n.cluster] ?? 4;
    // fibonacci-sphere-like: deterministic but spread
    const theta = rng() * Math.PI * 2;
    const phi = Math.acos(2 * rng() - 1);
    const r = spread * (0.4 + rng() * 0.6);
    return {
      id: n.id,
      cluster: n.cluster,
      x: ax + r * Math.sin(phi) * Math.cos(theta),
      y: ay + r * Math.cos(phi),
      z: az + r * Math.sin(phi) * Math.sin(theta),
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
    .force("collide", forceCollide(1.9))
    // Anchor clusters: STRONG X/Y/Z pull so each layer holds its region.
    .force(
      "cx",
      forceX((d: SimNode) => CLUSTER_ANCHOR[d.cluster][0]).strength(0.92),
    )
    .force(
      "cy",
      forceY((d: SimNode) => CLUSTER_ANCHOR[d.cluster][1]).strength(0.9),
    )
    .force(
      "cz",
      forceZ((d: SimNode) => CLUSTER_ANCHOR[d.cluster][2]).strength(0.9),
    )
    .stop();

  // Run fixed ticks (synchronous) — no animation loop
  for (let i = 0; i < ticks; i++) sim.tick();

  // Collect final positions
  const result = new Map<string, [number, number, number]>();
  for (const sn of simNodes) {
    result.set(sn.id, [sn.x ?? 0, sn.y ?? 0, sn.z ?? 0]);
  }
  return result;
}

/**
 * Convenience: re-run layout only when the node-id set changes.
 * Stable positions for known nodes; new dynamic nodes get fresh positions
 * appended without reshuffling existing ones.
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
  const patch = computeLayout(newNodes, edges, seed + existing.size);
  const merged = new Map(existing);
  for (const [id, pos] of patch) merged.set(id, pos);
  return merged;
}
