/**
 * layout.test.ts — unit tests for the pure computeLayout function.
 *
 * These tests run in Vitest with no DOM / THREE dependency — layout.ts
 * is a pure function that only uses d3-force-3d and the contract types.
 */
import { describe, expect, it } from "vitest";
import type { Cluster, Edge, Node } from "../../contract";
import { CLUSTER_ANCHOR, computeLayout, computeSpread, computeG, mergeLayout } from "../layout";

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
    const { positions: result } = computeLayout([], []);
    expect(result.size).toBe(0);
  });

  it("assigns a position to every node", () => {
    const nodes = CLUSTERS.map((c, i) => makeNode(`n${i}`, c));
    const { positions: result } = computeLayout(nodes, []);
    for (const n of nodes) {
      expect(result.has(n.id)).toBe(true);
      const pos = result.get(n.id)!;
      expect(pos).toHaveLength(3);
      expect(pos.every((v) => isFinite(v))).toBe(true);
    }
  });

  it("is deterministic: same seed → same positions", () => {
    const nodes = CLUSTERS.map((c, i) => makeNode(`n${i}`, c));
    const { positions: r1 } = computeLayout(nodes, [], { seed: 42 });
    const { positions: r2 } = computeLayout(nodes, [], { seed: 42 });
    for (const n of nodes) {
      expect(r1.get(n.id)).toEqual(r2.get(n.id));
    }
  });

  it("different seeds → different positions", () => {
    const nodes = CLUSTERS.map((c, i) => makeNode(`n${i}`, c));
    const { positions: r1 } = computeLayout(nodes, [], { seed: 42 });
    const { positions: r2 } = computeLayout(nodes, [], { seed: 99 });
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
    const { positions: result } = computeLayout(nodes, []);

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
    const { positions: result } = computeLayout(nodes, edges);
    expect(result.size).toBe(3);
  });

  it("ignores edges with missing endpoints gracefully", () => {
    const nodes = [makeNode("x", "core")];
    const edges = [makeEdge("x", "missing"), makeEdge("also_missing", "x")];
    expect(() => computeLayout(nodes, edges)).not.toThrow();
    const { positions: result } = computeLayout(nodes, edges);
    expect(result.has("x")).toBe(true);
  });

  it("handles a large node set (79 figures)", () => {
    const nodes = Array.from({ length: 79 }, (_, i) => makeNode(`fig_${i}`, "figures"));
    // Only asserts finiteness at scale — few ticks suffice (keeps the suite light).
    const { positions: result } = computeLayout(nodes, [], { ticks: 40 });
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

// ─── Step 1: computeSpread and computeG helpers ───────────────────────────────

describe("computeSpread", () => {
  it("two families: big UNLABELED banks stay compact, LABELED clusters get generous spacing", () => {
    // Refined design (user-approved 2026-06-26): figures/ideas are big hover-only
    // banks with SMALL per-node spacing so they stay compact (don't inflate the
    // world / zoom the camera out). Labeled clusters get GENEROUS per-node spacing
    // so their always-on names never stack.
    //
    // Canary: a 100-node bank must out-pack a 6-node labeled cluster — i.e. the
    // labeled cluster, with far fewer nodes, occupies a LARGER footprint.
    expect(computeSpread("figures", 100)).toBeLessThan(computeSpread("council", 6));
    expect(computeSpread("ideas",   100)).toBeLessThan(computeSpread("council", 6));

    // Banks are compact in absolute terms (figures@100 ≈ 6.0, ideas@100 ≈ 5.2).
    expect(computeSpread("figures", 100)).toBeLessThan(7.0);
    expect(computeSpread("ideas",   100)).toBeLessThan(7.0);

    // Labeled clusters are generous (council@6 ≈ 12.25, core@5 ≈ 8.94, market@6 ≈ 8.33).
    expect(computeSpread("council", 6)).toBeGreaterThan(10.0);
    expect(computeSpread("core",    5)).toBeGreaterThan(7.0);
    expect(computeSpread("market",  6)).toBeGreaterThan(7.0);
  });

  it("grows proportionally to sqrt of count above the MIN clamp (compact banks scale ∝ √count)", () => {
    // figures uses a small per-node (0.52) so MIN_SPREAD (6.0) binds below ~133
    // nodes. Above the clamp, the √count law holds: 4× count → ~2× spread.
    const s200 = computeSpread("figures", 200);
    const s800 = computeSpread("figures", 800);
    expect(s800 / s200).toBeGreaterThan(1.8);
    expect(s800 / s200).toBeLessThan(2.2);
  });

  it("clamps to minSpread for single-node clusters", () => {
    // Single node in any cluster must not collapse to zero
    const clusters: Cluster[] = [
      "sources", "figures", "council", "core", "ideas",
      "execution", "market", "learning", "infra", "options",
    ];
    for (const c of clusters) {
      expect(computeSpread(c, 1)).toBeGreaterThan(1.0);
    }
  });
});

// Adjacency + gap MIRROR the source (layout.ts ADJACENT_PAIRS, MIN_ZONE_GAP) so
// this is an independent canary of the zone-separation guarantee.
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
const MIN_ZONE_GAP = 2.0;

function anchorDist(a: Cluster, b: Cluster): number {
  const [ax, ay, az] = CLUSTER_ANCHOR[a];
  const [bx, by, bz] = CLUSTER_ANCHOR[b];
  return Math.sqrt((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2);
}

describe("computeG", () => {
  it("is ≥ 1, deterministic, and rises above 1 when generously-spaced labeled zones need room", () => {
    // Refined design: G is NOT pinned to 1. The generous labeled clusters
    // (council/core …) need real separation, so G naturally rises (~1.47 here).
    // That is the zone-separation guarantee at work — correct, not a regression.
    const counts = new Map<Cluster, number>([
      ["sources", 3], ["figures", 75], ["council", 5], ["core", 5],
      ["ideas", 12], ["execution", 5], ["market", 6],
      ["learning", 3], ["infra", 3], ["options", 3],
    ]);
    const g = computeG(counts);
    expect(g).toBeGreaterThan(1);            // labeled zones force expansion
    expect(computeG(counts)).toBe(g);        // deterministic (same input → same G)
  });

  it("CORE INVARIANT: after scaling by G, no adjacent spread-spheres overlap (sA+sB+gap ≤ dist·G)", () => {
    // This is the whole point of G. Check it across small AND large graphs.
    const scenarios: Map<Cluster, number>[] = [
      new Map([
        ["sources", 3], ["figures", 75], ["council", 5], ["core", 5],
        ["ideas", 12], ["execution", 5], ["market", 6],
        ["learning", 3], ["infra", 3], ["options", 3],
      ]),
      new Map([
        ["sources", 8], ["figures", 480], ["council", 12], ["core", 8],
        ["ideas", 212], ["execution", 10], ["market", 20],
        ["learning", 6], ["infra", 6], ["options", 8],
      ]),
    ];
    for (const counts of scenarios) {
      const g = computeG(counts);
      for (const [a, b] of ADJACENT_PAIRS) {
        const sA = computeSpread(a, counts.get(a) ?? 1);
        const sB = computeSpread(b, counts.get(b) ?? 1);
        const lhs = sA + sB + MIN_ZONE_GAP;
        const rhs = anchorDist(a, b) * g;
        // ≤ with a tiny float epsilon (the binding pair sits at equality).
        expect(lhs).toBeLessThanOrEqual(rhs + 1e-6);
      }
    }
  });

  it("rises further when a LABELED cluster grows (labeled zones drive G)", () => {
    // Under the refined design the binding constraint is a labeled pair
    // (council↔core), so growing the council cluster increases G; growing a
    // compact bank does not (its small footprint never becomes binding here).
    const base = new Map<Cluster, number>([
      ["sources", 3], ["figures", 75], ["council", 5], ["core", 5],
      ["ideas", 12], ["execution", 5], ["market", 6],
      ["learning", 3], ["infra", 3], ["options", 3],
    ]);
    const biggerCouncil = new Map(base).set("council", 14);
    const moreFigures = new Map(base).set("figures", 150);

    expect(computeG(biggerCouncil)).toBeGreaterThan(computeG(base));
    // Doubling the figures bank does NOT move G (banks stay compact, non-binding).
    expect(computeG(moreFigures)).toBe(computeG(base));
  });

  it("is always ≥ 1 regardless of counts", () => {
    const counts = new Map<Cluster, number>([
      ["sources", 1], ["figures", 1], ["council", 1], ["core", 1],
      ["ideas", 1], ["execution", 1], ["market", 1],
      ["learning", 1], ["infra", 1], ["options", 1],
    ]);
    expect(computeG(counts)).toBeGreaterThanOrEqual(1);
  });

  it("empty cluster map returns 1", () => {
    expect(computeG(new Map())).toBe(1);
  });
});

// ─── Step 2: computeLayout scaling invariants ─────────────────────────────────

describe("computeLayout — scaling invariants", () => {
  it("density invariance: figures min pairwise spacing stays ≥ 1.2 at 80 nodes", () => {
    const nodes = Array.from({ length: 80 }, (_, i) => makeNode(`f${i}`, "figures"));
    // 40 ticks already settles the compact bank (minPair ≈ 4.1); keeps the suite light.
    const { positions } = computeLayout(nodes, [], { ticks: 40 });
    const ids = nodes.map((n) => n.id);
    let minDist = Infinity;
    for (let i = 0; i < ids.length - 1; i++) {
      for (let j = i + 1; j < ids.length; j++) {
        const p1 = positions.get(ids[i])!;
        const p2 = positions.get(ids[j])!;
        const d = Math.sqrt(
          (p1[0]-p2[0])**2 + (p1[1]-p2[1])**2 + (p1[2]-p2[2])**2,
        );
        if (d < minDist) minDist = d;
      }
    }
    // figureRadius (collide) = 1.1; with force imperfection allow down to 1.2
    expect(minDist).toBeGreaterThanOrEqual(1.2);
    // UPPER-bound canary: figures must stay TIGHTLY packed (compact bank family),
    // not loosened toward labeled-cluster spacing. A regression that gave figures
    // the labeled footprint (larger collide radius and/or per-node spacing) would
    // push min spacing up toward a labeled cluster's (council@10 ≈ 11). Observed
    // figures@80 ≈ 4.62; 5.5 is a real canary without flakiness.
    expect(minDist).toBeLessThan(5.5);
  });

  it("label room: council nodes (labeled cluster) keep GENEROUS spacing at 10 nodes", () => {
    const nodes = Array.from({ length: 10 }, (_, i) => makeNode(`c${i}`, "council"));
    const { positions } = computeLayout(nodes, []);
    const ids = nodes.map((n) => n.id);
    let minDist = Infinity;
    for (let i = 0; i < ids.length - 1; i++) {
      for (let j = i + 1; j < ids.length; j++) {
        const p1 = positions.get(ids[i])!;
        const p2 = positions.get(ids[j])!;
        const d = Math.sqrt(
          (p1[0]-p2[0])**2 + (p1[1]-p2[1])**2 + (p1[2]-p2[2])**2,
        );
        if (d < minDist) minDist = d;
      }
    }
    // labeledRadius = 5.6 + generous per-node (5.0); council@10 min ≈ 11.
    // A regression to compact-bank packing would drop this toward ~4.6, so a
    // floor of 6.0 is a real canary (well clear of figure-style packing).
    expect(minDist).toBeGreaterThanOrEqual(6.0);
  });

  it("zone separation: figures and council centroids stay far apart at 2× node counts", () => {
    const nodes: Node[] = [
      ...Array.from({ length: 150 }, (_, i) => makeNode(`fig${i}`, "figures")),
      ...Array.from({ length: 10 },  (_, i) => makeNode(`c${i}`,   "council")),
    ];
    // Cluster-centroid separation converges fast under the strong anchor force;
    // 80 ticks is plenty and keeps this 160-node case light.
    const { positions } = computeLayout(nodes, [], { ticks: 80 });

    const centroid = (cluster: Cluster): [number, number, number] => {
      const pts = nodes
        .filter((n) => n.cluster === cluster)
        .map((n) => positions.get(n.id)!);
      const x = pts.reduce((s, p) => s + p[0], 0) / pts.length;
      const y = pts.reduce((s, p) => s + p[1], 0) / pts.length;
      const z = pts.reduce((s, p) => s + p[2], 0) / pts.length;
      return [x, y, z];
    };

    const fc = centroid("figures");
    const cc = centroid("council");
    const dist = Math.sqrt((fc[0]-cc[0])**2 + (fc[1]-cc[1])**2 + (fc[2]-cc[2])**2);

    // At 2× counts G > 1, anchors expand, so centroid distance must exceed base
    // spread radii sum (nodes should not intermix zones)
    const sF = computeSpread("figures", 150);
    const sC = computeSpread("council", 10);
    // Allow 4-unit tolerance for force imperfection
    expect(dist).toBeGreaterThan(sF + sC - 4);
  });

  it("scale source of truth: G from computeLayout equals G from computeG with same counts", () => {
    const nodes: Node[] = [
      ...Array.from({ length: 150 }, (_, i) => makeNode(`fig${i}`, "figures")),
      ...Array.from({ length: 5 },   (_, i) => makeNode(`c${i}`,   "council")),
      ...Array.from({ length: 5 },   (_, i) => makeNode(`co${i}`,  "core")),
    ];
    // G is computed BEFORE the force sim runs, so ticks are irrelevant here —
    // use ticks: 1 to keep this 160-node case from hogging the worker pool.
    const { G: layoutG } = computeLayout(nodes, [], { ticks: 1 });

    const counts = new Map<Cluster, number>();
    for (const n of nodes) counts.set(n.cluster, (counts.get(n.cluster) ?? 0) + 1);
    const directG = computeG(counts);

    expect(layoutG).toBe(directG);
  });
});

describe("computeLayout — warm-start (opts.initial)", () => {
  it("determinism: same seed + same initial → identical positions", () => {
    const nodes = CLUSTERS.map((c, i) => makeNode(`n${i}`, c));
    const { positions: base } = computeLayout(nodes, []);

    const r1 = computeLayout(nodes, [], { seed: 42, initial: base, ticks: 80 });
    const r2 = computeLayout(nodes, [], { seed: 42, initial: base, ticks: 80 });

    for (const n of nodes) {
      expect(r1.positions.get(n.id)).toEqual(r2.positions.get(n.id));
    }
  });

  it("warm-start stability: adding 1 node leaves existing nodes within delta=5", () => {
    const base = CLUSTERS.map((c, i) => makeNode(`n${i}`, c));
    const { positions: basePosMap } = computeLayout(base, [], { ticks: 200 });

    const withExtra: Node[] = [...base, makeNode("extra_idea", "ideas")];
    const { positions: newPosMap } = computeLayout(withExtra, [], {
      initial: basePosMap,
      ticks: 80,
    });

    const DELTA = 5.0;  // world units — tuned visually; strong anchor forces keep this tight
    for (const n of base) {
      const old = basePosMap.get(n.id)!;
      const neo = newPosMap.get(n.id)!;
      const d = Math.sqrt((old[0]-neo[0])**2 + (old[1]-neo[1])**2 + (old[2]-neo[2])**2);
      expect(d).toBeLessThan(DELTA);
    }
  });

  it("new node (not in initial) still gets a valid finite position", () => {
    const existingNodes = CLUSTERS.map((c, i) => makeNode(`n${i}`, c));
    const { positions: existingPosMap } = computeLayout(existingNodes, []);

    const newNode = makeNode("brand_new_trade", "market");
    const allNodes = [...existingNodes, newNode];
    const { positions } = computeLayout(allNodes, [], { initial: existingPosMap, ticks: 80 });

    const p = positions.get("brand_new_trade")!;
    expect(p).toHaveLength(3);
    expect(p.every(isFinite)).toBe(true);
  });

  it("warm-start across a G CHANGE: stable zones expand uniformly (nudge, not reshuffle)", () => {
    // Under the refined design, G is driven by the LABELED clusters, so to exercise
    // a real G change we grow the council cluster (not a compact bank). The other
    // clusters stay fixed and are the "stable" set we check.
    const stable: Node[] = [
      ...Array.from({ length: 60 }, (_, i) => makeNode(`f${i}`,  "figures")),
      ...Array.from({ length: 5 },  (_, i) => makeNode(`co${i}`, "core")),
      ...Array.from({ length: 6 },  (_, i) => makeNode(`m${i}`,  "market")),
      ...Array.from({ length: 3 },  (_, i) => makeNode(`s${i}`,  "sources")),
      ...Array.from({ length: 5 },  (_, i) => makeNode(`e${i}`,  "execution")),
    ];
    const cold: Node[] = [
      ...stable,
      ...Array.from({ length: 5 }, (_, i) => makeNode(`c${i}`, "council")),
    ];
    const { positions: coldPos, G: coldG } = computeLayout(cold, [], { ticks: 80 });

    // Warm-start: council grows 5 → 14 → G rises (binding council↔core pair).
    const hot: Node[] = [
      ...stable,
      ...Array.from({ length: 14 }, (_, i) => makeNode(`c${i}`, "council")),
    ];
    const { positions: hotPos, G: hotG } = computeLayout(hot, [], {
      initial: coldPos,
      ticks: 80,
    });

    // Sanity: G actually changed (this is what makes the test meaningful).
    expect(hotG).toBeGreaterThan(coldG);

    // Lever C expands every zone uniformly by G, so a stable node's NEW position
    // should track OLD * (hotG / coldG) — the whole structure scaling together.
    // "Nudge, not reshuffle" means a small residual around that scaled position
    // (far zones legitimately translate many units; the ratio test absorbs that).
    const ratio = hotG / coldG;
    const stableNonCouncil = stable.filter((n) => n.cluster !== "figures");
    const DELTA = 8.0;  // observed max ≈ 5.9; deterministic, so 8 is a safe non-flaky bound
    for (const n of stableNonCouncil) {
      const old = coldPos.get(n.id)!;
      const neo = hotPos.get(n.id)!;
      const d = Math.sqrt(
        (old[0] * ratio - neo[0]) ** 2 +
        (old[1] * ratio - neo[1]) ** 2 +
        (old[2] * ratio - neo[2]) ** 2,
      );
      expect(d).toBeLessThan(DELTA);
    }
  });
});
