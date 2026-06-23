/**
 * layout.test.ts — unit tests for the pure computeLayout function.
 *
 * These tests run in Vitest with no DOM / THREE dependency — layout.ts
 * is a pure function that only uses d3-force-3d and the contract types.
 */
import { describe, expect, it } from "vitest";
import type { Cluster, Edge, Node } from "../../contract";
import { CLUSTER_ANCHOR, computeLayout, mergeLayout } from "../layout";

// ─── Helpers ─────────────────────────────────────────────────────────────────
function makeNode(id: string, cluster: Cluster): Node {
  return { id, type: "engine_part", label: id, cluster, meta: {} };
}

function makeEdge(source: string, target: string): Edge {
  return { id: `${source}-${target}`, source, target, kind: "decides" };
}

const CLUSTERS: Cluster[] = [
  "sources", "figures", "council", "core",
  "ideas", "execution", "market", "learning", "infra",
];

// ─── Tests ───────────────────────────────────────────────────────────────────
describe("computeLayout", () => {
  it("returns empty map for empty input", () => {
    const result = computeLayout([], []);
    expect(result.size).toBe(0);
  });

  it("assigns a position to every node", () => {
    const nodes = CLUSTERS.map((c, i) => makeNode(`n${i}`, c));
    const result = computeLayout(nodes, []);
    for (const n of nodes) {
      expect(result.has(n.id)).toBe(true);
      const pos = result.get(n.id)!;
      expect(pos).toHaveLength(3);
      expect(pos.every((v) => isFinite(v))).toBe(true);
    }
  });

  it("is deterministic: same seed → same positions", () => {
    const nodes = CLUSTERS.map((c, i) => makeNode(`n${i}`, c));
    const r1 = computeLayout(nodes, [], 42);
    const r2 = computeLayout(nodes, [], 42);
    for (const n of nodes) {
      expect(r1.get(n.id)).toEqual(r2.get(n.id));
    }
  });

  it("different seeds → different positions", () => {
    const nodes = CLUSTERS.map((c, i) => makeNode(`n${i}`, c));
    const r1 = computeLayout(nodes, [], 42);
    const r2 = computeLayout(nodes, [], 99);
    // At least one node must differ (statistically certain)
    const differs = nodes.some((n) => {
      const p1 = r1.get(n.id)!;
      const p2 = r2.get(n.id)!;
      return p1[0] !== p2[0] || p1[1] !== p2[1] || p1[2] !== p2[2];
    });
    expect(differs).toBe(true);
  });

  it("clusters are spatially separated: core vs figures centroid distance > 10", () => {
    // Build a realistic-ish set of nodes for each cluster
    const nodes: Node[] = [
      ...Array.from({ length: 8 }, (_, i) => makeNode(`core_${i}`, "core")),
      ...Array.from({ length: 15 }, (_, i) => makeNode(`fig_${i}`, "figures")),
    ];
    const result = computeLayout(nodes, []);

    const centroid = (ids: string[]): [number, number, number] => {
      let x = 0, y = 0, z = 0;
      for (const id of ids) { const p = result.get(id)!; x += p[0]; y += p[1]; z += p[2]; }
      return [x / ids.length, y / ids.length, z / ids.length];
    };

    const coreIds = nodes.filter((n) => n.cluster === "core").map((n) => n.id);
    const figIds  = nodes.filter((n) => n.cluster === "figures").map((n) => n.id);

    const cc = centroid(coreIds);
    const fc = centroid(figIds);

    const dist = Math.sqrt(
      (cc[0] - fc[0]) ** 2 + (cc[1] - fc[1]) ** 2 + (cc[2] - fc[2]) ** 2,
    );
    // Anchors are ~20 units apart; force layout should keep them separated
    expect(dist).toBeGreaterThan(10);
  });

  it("CLUSTER_ANCHOR.core is at origin", () => {
    expect(CLUSTER_ANCHOR.core).toEqual([0, 0, 0]);
  });

  it("handles edges between nodes without error", () => {
    const nodes = [
      makeNode("a", "core"),
      makeNode("b", "ideas"),
      makeNode("c", "execution"),
    ];
    const edges = [makeEdge("a", "b"), makeEdge("b", "c")];
    expect(() => computeLayout(nodes, edges)).not.toThrow();
    const result = computeLayout(nodes, edges);
    expect(result.size).toBe(3);
  });

  it("ignores edges with missing endpoints gracefully", () => {
    const nodes = [makeNode("x", "core")];
    const edges = [makeEdge("x", "missing"), makeEdge("also_missing", "x")];
    expect(() => computeLayout(nodes, edges)).not.toThrow();
    const result = computeLayout(nodes, edges);
    expect(result.has("x")).toBe(true);
  });

  it("handles a large node set (79 figures)", () => {
    const nodes = Array.from({ length: 79 }, (_, i) => makeNode(`fig_${i}`, "figures"));
    const result = computeLayout(nodes, []);
    expect(result.size).toBe(79);
    // No node should be NaN
    for (const pos of result.values()) {
      expect(pos.every(isFinite)).toBe(true);
    }
  });
});

describe("mergeLayout", () => {
  it("returns existing positions unchanged for known nodes", () => {
    const n = makeNode("n1", "core");
    const existing = new Map<string, [number, number, number]>([["n1", [1, 2, 3]]]);
    const result = mergeLayout(existing, [n], []);
    expect(result.get("n1")).toEqual([1, 2, 3]);
  });

  it("adds positions for new nodes without changing existing", () => {
    const n1 = makeNode("n1", "core");
    const n2 = makeNode("n2", "ideas");
    const existing = new Map<string, [number, number, number]>([["n1", [5, 5, 5]]]);
    const result = mergeLayout(existing, [n1, n2], []);
    expect(result.get("n1")).toEqual([5, 5, 5]);  // unchanged
    expect(result.has("n2")).toBe(true);           // new node added
  });

  it("is a no-op when no new nodes are present", () => {
    const n = makeNode("n1", "core");
    const existing = new Map<string, [number, number, number]>([["n1", [9, 9, 9]]]);
    const result = mergeLayout(existing, [n], []);
    // Returns the same map object (no copy needed)
    expect(result).toBe(existing);
  });
});
