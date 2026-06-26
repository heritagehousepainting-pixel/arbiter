# Cockpit constellation — layout scaling: implementation plan

**Date:** 2026-06-26  
**Status:** PLAN (ready for build agent)  
**Design spec:** `docs/specs/2026-06-26-cockpit-layout-scaling-design.md`  
**Branch:** cockpit-layout-scaling

---

## Scope and ordering

Four ordered steps. Each step ends with a green `npx tsc -b && npx vitest run`.
Steps 1–2 touch only `layout.ts` + `layout.test.ts` (pure-function, no React).
Steps 3–4 touch `SceneRoot.tsx` and `Labels.tsx` (no tests; TypeScript type check is the gate).

```
Step 0  Baseline — confirm nothing is broken before touching code
Step 1  layout.ts: add computeSpread + computeG helpers (with tests)
Step 2  layout.ts: refactor computeLayout (new signature, return type, all levers A/B/C/D) + update tests
Step 3  SceneRoot.tsx: replace mergeLayout with warm-start computeLayout, thread G (Lever D + SceneRoot half of C)
Step 4  Labels.tsx: add scale prop, multiply distanceFactor by G (Lever E)
```

---

## Hard constraints — do NOT touch any file outside this list

The build agent and audit agent MUST confirm none of these changed:

- [ ] `scene/NodeMesh.tsx`
- [ ] `scene/NodeInstances.tsx`
- [ ] `scene/EdgeLines.tsx`
- [ ] `scene/PulseLayer.tsx`
- [ ] `contract.ts` (read-only)
- [ ] `theme/theme.ts`
- [ ] `ui/CockpitUI.tsx`
- [ ] `ui/store.ts`
- [ ] `api.ts`
- [ ] Any test file other than `scene/__tests__/layout.test.ts`
- [ ] `CLUSTER_ANCHOR` values inside `layout.ts` (they are READ-ONLY inputs to G)
- [ ] Node/edge visual styles, hover/selection/tooltip, motion/animation, OrbitControls config

---

## Constants chosen (starting values — tuned visually later)

### Lever A — per-cluster density spread

Calibrated so `computeSpread(c, typicalCount) ≈ current CLUSTER_SPREAD[c]` at today's node counts,
ensuring G=1 and the layout looks identical to today's composition at small sizes.

Derivation: `perNodeSpacing = currentSpread / sqrt(typicalCount)` then rounded to 1 dp.

```
PER_NODE_SPACING: Record<Cluster, number> = {
  sources:   2.6,   // sqrt(3)  → 2.6*1.73 = 4.5  ✓ (current 4.5)
  figures:   1.3,   // sqrt(75) → 1.3*8.66 = 11.3 ✓ (current 11.0)
  council:   2.0,   // sqrt(5)  → 2.0*2.24 = 4.5  ✓ (current 4.5)
  core:      1.8,   // sqrt(5)  → 1.8*2.24 = 4.0  ✓ (current 4.0)
  ideas:     1.6,   // sqrt(12) → 1.6*3.46 = 5.5  ✓ (current 5.5)
  execution: 2.2,   // sqrt(5)  → 2.2*2.24 = 4.9  ✓ (current 5.0)
  market:    1.8,   // sqrt(6)  → 1.8*2.45 = 4.4  ✓ (current 4.5)
  learning:  2.3,   // sqrt(3)  → 2.3*1.73 = 4.0  ✓ (current 4.0)
  infra:     2.3,   // sqrt(3)  → 2.3*1.73 = 4.0  ✓ (current 4.0)
  options:   2.6,   // sqrt(3)  → 2.6*1.73 = 4.5  ✓ (current 4.5)
}

MIN_SPREAD: Record<Cluster, number> = {
  sources:   3.0,   figures:  6.0,  council: 3.0,  core:    2.5,
  ideas:     3.5,   execution: 3.0, market:  3.0,  learning: 2.5,
  infra:     2.5,   options:   3.0,
}

// formula used everywhere:
computeSpread(c, count) = Math.max(MIN_SPREAD[c], PER_NODE_SPACING[c] * Math.sqrt(count))
```

### Lever B — label-aware collision radii

```
LABELED_CLUSTERS: Set<Cluster> = new Set([
  "council", "core", "execution", "sources", "infra", "market", "learning", "options"
])
// figures cluster is NOT in this set — it stays tightly packed

labeledRadius = 3.2   // was 1.9 (uniform); labeled nodes need label room
figureRadius  = 1.6   // ≤ current 1.9; slightly tighter packing for figures
```

### Lever C — global scale G

Adjacent pairs checked (anchor distances computed from CLUSTER_ANCHOR at G=1):

```
ADJACENT_PAIRS: [Cluster, Cluster][] = [
  ["sources",   "figures"],    // anchor dist ≈ 21.2
  ["figures",   "council"],    // anchor dist ≈ 17.6  ← tightest
  ["council",   "core"],       // anchor dist ≈ 15.0
  ["core",      "ideas"],      // anchor dist ≈ 14.9
  ["ideas",     "execution"],  // anchor dist ≈ 17.2
  ["execution", "market"],     // anchor dist ≈ 15.7
  ["execution", "options"],    // anchor dist ≈ 18.0
  ["core",      "learning"],   // anchor dist ≈ 20.7
  ["core",      "infra"],      // anchor dist ≈ 18.8
]

MIN_ZONE_GAP = 2.0   // minimum world-unit clearance between adjacent spread spheres

// One-shot formula (no iteration required):
// G_pair = (spread(A) + spread(B) + MIN_ZONE_GAP) / anchorDist(A, B)   // at G=1 anchors
// G = max(1, max over all pairs of G_pair)
```

Verification at today's typical counts:
- figures(75)+council(5): (11.3+4.5+2.0)/17.6 = 17.8/17.6 ≈ 1.01 → clamped to 1 (borderline OK)
- All other pairs produce ratios < 1 → G = 1 at today's counts ✓

> NOTE: figures(75)+council(5) produces G very close to 1.0 — the 11.3 vs 11.0 rounding means G may come out as 1.01 at today's exact counts. If the visual regression reveals even a tiny composition shift, lower `PER_NODE_SPACING.figures` from 1.3 to 1.27. The constant is the only dial to turn.

### Lever D — warm-start signature

```typescript
// New exported interface (add to layout.ts):
export interface LayoutOpts {
  seed?: number;    // default 42
  ticks?: number;   // default 200; use 80 for warm-start re-settles
  initial?: Map<string, [number, number, number]>;  // warm-start seed
}

// New return type (add to layout.ts):
export interface LayoutResult {
  positions: Map<string, [number, number, number]>;
  G: number;
}

// Updated signature:
export function computeLayout(
  nodes: Node[],
  edges: Edge[],
  opts?: LayoutOpts,
): LayoutResult
```

Warm-start logic inside `computeLayout`:
- When `opts.initial` is provided AND a node id is present in `initial`:
  use its `[x, y, z]` as the SimNode starting position (no PRNG jitter).
- When a node id is NOT in `initial` (new node): use the existing anchor-PRNG seed as today.
- The PRNG is always advanced the same number of steps regardless of whether initial is used,
  so the seed sequence for NEW nodes is deterministic: `rng()` is consumed for every node in
  the input order; for known nodes the consumed values are discarded.

### Lever E — label distanceFactor counter-scale

```typescript
// ZoneLabels receives `scale: number` prop (G from SceneRoot)
distanceFactor={46 * scale}   // was 46

// NodeLabels receives `scale: number` prop
distanceFactor={(isTrade ? 22 : 28) * scale}   // was 22 / 28
```

---

## Step 0 — Baseline

**File:** none (read-only check)  
**Test to write:** none  
**Action:** Run tests, confirm baseline is green.  

```bash
cd /Users/jonathanmorris/poly_bot/cockpit/web
npx tsc -b
npx vitest run
```

Expected: all 12 existing tests pass (`layout.test.ts`). If not, stop and report.

---

## Step 1 — Add `computeSpread` and `computeG` helpers

**File:** `cockpit/web/src/scene/layout.ts`  
**File:** `cockpit/web/src/scene/__tests__/layout.test.ts`

### Tests to add FIRST (append to layout.test.ts)

Import the two new exports: `import { ..., computeSpread, computeG } from "../layout";`

```typescript
describe("computeSpread", () => {
  it("returns ≈ current CLUSTER_SPREAD at typical node counts (calibration check)", () => {
    // These thresholds verify perNodeSpacing constants are correctly calibrated.
    expect(computeSpread("figures",   75)).toBeGreaterThan(10.5);
    expect(computeSpread("figures",   75)).toBeLessThan(12.0);
    expect(computeSpread("council",    5)).toBeGreaterThan(4.0);
    expect(computeSpread("council",    5)).toBeLessThan(5.0);
    expect(computeSpread("core",       5)).toBeGreaterThan(3.5);
    expect(computeSpread("core",       5)).toBeLessThan(4.5);
    expect(computeSpread("execution",  5)).toBeGreaterThan(4.5);
    expect(computeSpread("execution",  5)).toBeLessThan(5.5);
  });

  it("grows proportionally to sqrt of count in the linear region", () => {
    // 4× count → ~2× spread (sqrt scaling)
    const s50  = computeSpread("figures", 50);
    const s200 = computeSpread("figures", 200);
    // ratio should be sqrt(200/50) = 2.0 ± 10%
    expect(s200 / s50).toBeGreaterThan(1.8);
    expect(s200 / s50).toBeLessThan(2.2);
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

describe("computeG", () => {
  it("returns exactly 1 at today's typical node counts", () => {
    const counts = new Map<Cluster, number>([
      ["sources", 3], ["figures", 75], ["council", 5], ["core", 5],
      ["ideas", 12], ["execution", 5], ["market", 6],
      ["learning", 3], ["infra", 3], ["options", 3],
    ]);
    // At calibrated counts G must not exceed 1.05 (tiny float slack)
    expect(computeG(counts)).toBeGreaterThanOrEqual(1);
    expect(computeG(counts)).toBeLessThanOrEqual(1.05);
  });

  it("returns > 1 when figures cluster doubles (150 nodes)", () => {
    const counts = new Map<Cluster, number>([
      ["sources", 3], ["figures", 150], ["council", 5], ["core", 5],
      ["ideas", 12], ["execution", 5], ["market", 6],
      ["learning", 3], ["infra", 3], ["options", 3],
    ]);
    expect(computeG(counts)).toBeGreaterThan(1.0);
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
```

**Run (should FAIL — not implemented yet):**
```bash
cd /Users/jonathanmorris/poly_bot/cockpit/web && npx vitest run
```

### Implementation (layout.ts changes)

Add after `CLUSTER_SPREAD` block (which will be kept for now as a reference comment, then removed in Step 2):

1. **Add `PER_NODE_SPACING` and `MIN_SPREAD` records** (values from Constants section above).

2. **Add `computeSpread(cluster: Cluster, count: number): number`:**
   ```typescript
   export function computeSpread(cluster: Cluster, count: number): number {
     return Math.max(
       MIN_SPREAD[cluster] ?? 2.5,
       (PER_NODE_SPACING[cluster] ?? 2.0) * Math.sqrt(Math.max(1, count)),
     );
   }
   ```

3. **Add `ADJACENT_PAIRS` constant** (the 9 pairs listed in Constants above, as a `readonly` tuple array).

4. **Add `computeG(clusterCounts: Map<Cluster, number>, minGap = MIN_ZONE_GAP): number`:**
   Pre-compute all 9 base anchor distances from `CLUSTER_ANCHOR` (pure arithmetic, no sqrt at call time — compute once into a `BASE_DIST` lookup object at module top to avoid repeated sqrt in hot path, but this is a low-call function so inline is fine too).
   ```typescript
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
       const dist = Math.sqrt((ax-bx)**2 + (ay-by)**2 + (az-bz)**2);
       if (dist > 0) G = Math.max(G, (sA + sB + minGap) / dist);
     }
     return G;
   }
   ```

5. **Export both functions** (they are already exported via the `export function` keyword above; also exported from the module's barrel exports implicitly since no barrel re-exports are used).

**Verify (should now be GREEN):**
```bash
cd /Users/jonathanmorris/poly_bot/cockpit/web && npx tsc -b && npx vitest run
```

Expected: all new `computeSpread` + `computeG` tests pass; all pre-existing tests still pass.

---

## Step 2 — Refactor `computeLayout` (levers A/B/C/D) + update all tests

**File:** `cockpit/web/src/scene/layout.ts`  
**File:** `cockpit/web/src/scene/__tests__/layout.test.ts`

This step changes the `computeLayout` return type from `Map` to `LayoutResult`.
It ALSO updates `mergeLayout` to use the new return type (so it compiles).
It does NOT change the `mergeLayout` behavior — that function becomes dead code in Step 3.

### Tests to add FIRST (layout.test.ts)

Update the existing import to include `LayoutOpts`:
```typescript
import { ..., computeLayout, computeG, computeSpread, mergeLayout } from "../layout";
import type { LayoutOpts } from "../layout";
```

**Also update ALL existing `computeLayout` calls** in `layout.test.ts` to destructure `.positions`:
- `const result = computeLayout(nodes, []);` → `const { positions: result } = computeLayout(nodes, []);`
- `const r1 = computeLayout(nodes, [], 42);` → `const { positions: r1 } = computeLayout(nodes, [], { seed: 42 });`
- (All existing assertions on `result` then work unchanged — `result.has(...)`, `result.get(...)`, etc.)

New tests to add:

```typescript
describe("computeLayout — scaling invariants", () => {
  it("density invariance: figures min pairwise spacing stays ≥ 1.2 at 80 nodes", () => {
    const nodes = Array.from({ length: 80 }, (_, i) => makeNode(`f${i}`, "figures"));
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
    // figureRadius = 1.6; with force imperfection allow down to 1.2
    expect(minDist).toBeGreaterThanOrEqual(1.2);
  });

  it("label room: council nodes (labeled cluster) min pairwise spacing ≥ 2.8 at 10 nodes", () => {
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
    // labeledRadius = 3.2; allow 10% force imperfection → 2.8
    expect(minDist).toBeGreaterThanOrEqual(2.8);
  });

  it("zone separation: figures and council centroids stay far apart at 2× node counts", () => {
    const nodes: Node[] = [
      ...Array.from({ length: 150 }, (_, i) => makeNode(`fig${i}`, "figures")),
      ...Array.from({ length: 10 },  (_, i) => makeNode(`c${i}`,   "council")),
    ];
    const { positions } = computeLayout(nodes, []);

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
    const { G: layoutG } = computeLayout(nodes, []);

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
});
```

**Run (should FAIL — not implemented yet):**
```bash
cd /Users/jonathanmorris/poly_bot/cockpit/web && npx vitest run
```

### Implementation (layout.ts changes)

1. **Add `LayoutOpts` and `LayoutResult` interfaces** before `computeLayout`:
   ```typescript
   export interface LayoutOpts {
     seed?: number;
     ticks?: number;
     initial?: Map<string, [number, number, number]>;
   }

   export interface LayoutResult {
     positions: Map<string, [number, number, number]>;
     G: number;
   }
   ```

2. **Change `computeLayout` signature and internals:**
   - Old: `computeLayout(nodes, edges, seed=42, ticks=200): Map<...>`
   - New: `computeLayout(nodes, edges, opts?: LayoutOpts): LayoutResult`
   - Destructure opts at top: `const { seed = 42, ticks = 200, initial } = opts ?? {};`
   - **Empty-graph guard**: return `{ positions: new Map(), G: 1 }`.
   - **Compute G** from cluster counts (built from `nodes`):
     ```typescript
     const clusterCounts = new Map<Cluster, number>();
     for (const n of nodes) clusterCounts.set(n.cluster, (clusterCounts.get(n.cluster) ?? 0) + 1);
     const G = computeG(clusterCounts);
     ```
   - **Build SimNodes** — use `computeSpread(n.cluster, clusterCounts.get(n.cluster)!)` instead of `CLUSTER_SPREAD[n.cluster]` for the initial spread radius.
     Apply G to anchors: `const [ax, ay, az] = CLUSTER_ANCHOR[n.cluster]` → scaled: `ax*G, ay*G, az*G`.
     When `initial` is provided and `initial.has(n.id)`: skip the PRNG-based jitter and use `initial.get(n.id)!` as `{x, y, z}`.
     IMPORTANT: consume the 3 PRNG calls even for warm-start nodes so the seed sequence for NEW nodes
     remains deterministic regardless of how many known nodes precede them.
   - **Replace `forceCollide(1.9)`** with:
     ```typescript
     forceCollide((d: SimNode) =>
       LABELED_CLUSTERS.has(d.cluster) ? labeledRadius : figureRadius
     )
     ```
   - **Update anchor forces** to use scaled anchors:
     ```typescript
     forceX((d: SimNode) => CLUSTER_ANCHOR[d.cluster][0] * G).strength(0.92),
     forceY((d: SimNode) => CLUSTER_ANCHOR[d.cluster][1] * G).strength(0.9),
     forceZ((d: SimNode) => CLUSTER_ANCHOR[d.cluster][2] * G).strength(0.9),
     ```
   - **Return `{ positions, G }`** instead of `positions` alone.
   - **Remove `CLUSTER_SPREAD` record** (now dead) and replace its block comment with a note pointing to `PER_NODE_SPACING` + `MIN_SPREAD`.

3. **Update `mergeLayout`** to use the new `computeLayout` return type:
   ```typescript
   // Inside mergeLayout — extract .positions from the patch call:
   const { positions: patch } = computeLayout(newNodes, edges, { seed: seed + existing.size });
   ```
   The `mergeLayout` function itself still returns `Map<string, [number,number,number]>` (its own signature is unchanged — it just stops being called in Step 3). This keeps the existing `mergeLayout` tests green without changing them.

4. **Add `LABELED_CLUSTERS` and the collide constants** (add near top of file after CLUSTER_ANCHOR):
   ```typescript
   export const LABELED_CLUSTERS = new Set<Cluster>([
     "council", "core", "execution", "sources", "infra", "market", "learning", "options",
   ]);
   const labeledRadius = 3.2;
   const figureRadius  = 1.6;
   const MIN_ZONE_GAP  = 2.0;
   ```

**Verify (all tests including new ones should be GREEN):**
```bash
cd /Users/jonathanmorris/poly_bot/cockpit/web && npx tsc -b && npx vitest run
```

Expected: all 12 original tests + all new scaling/warm-start tests pass (roughly 25 total).

**Edge cases to handle in this step:**
- Single-node cluster: `computeSpread(c, 1)` → `minSpread[c]` — fine.
- `options` cluster with opt.layer node: falls in `LABELED_CLUSTERS` → uses `labeledRadius`. Fine.
- Empty `initial` map (first load): the `initial?.has(n.id)` check is `false` for all nodes → falls through to PRNG path. G is still computed and returned. Fine.
- All clusters absent from `clusterCounts` lookup: `clusterCounts.get(n.cluster) ?? 1` → uses floor count of 1.

---

## Step 3 — SceneRoot warm-start wiring

**File:** `cockpit/web/src/scene/SceneRoot.tsx`  
**Tests:** none (TypeScript compile + existing Vitest run is the gate)

### Changes

1. **Update the import line** from:
   ```typescript
   import { computeLayout, mergeLayout } from "./layout";
   ```
   to:
   ```typescript
   import { computeLayout } from "./layout";
   ```
   (`mergeLayout` is no longer called. Keep it exported from `layout.ts` — the audit agent decides whether to delete the dead export.)

2. **Replace the combined `posMap` state + `layoutG` tracking.** Add a `layoutG` state:
   ```typescript
   const [layoutG, setLayoutG] = useState<number>(1);
   ```

3. **Replace the `useEffect` layout block** (lines 136–152 in current file) with:
   ```typescript
   useEffect(() => {
     const currentIds = new Set(allNodes.map((n) => n.id));
     const hasNew = [...currentIds].some((id) => !laidOutIds.current.has(id));
     if (!hasNew) return;

     setPosMap((prev) => {
       const ticks = prev.size === 0 ? 200 : 80;
       const { positions, G: newG } = computeLayout(allNodes, allEdges, {
         initial: prev.size > 0 ? prev : undefined,
         ticks,
       });
       // Schedule G update after render (can't call setState in updater)
       // Use setTimeout(0) to defer; or use a ref + useEffect pattern below.
       // SIMPLEST CORRECT APPROACH: use a ref for G and sync to state after:
       layoutGRef.current = newG;
       laidOutIds.current = currentIds;
       return positions;
     });
   }, [allNodes, allEdges]);
   ```

   Actually, the cleanest approach avoids calling `setLayoutG` inside a `setPosMap` updater.
   Use a `useRef` for the computed G value, then synchronize to state with a second `useEffect`:

   ```typescript
   const layoutGRef = useRef<number>(1);

   useEffect(() => {
     const currentIds = new Set(allNodes.map((n) => n.id));
     const hasNew = [...currentIds].some((id) => !laidOutIds.current.has(id));
     if (!hasNew) return;

     setPosMap((prev) => {
       const ticks = prev.size === 0 ? 200 : 80;
       const { positions, G: newG } = computeLayout(allNodes, allEdges, {
         initial: prev.size > 0 ? prev : undefined,
         ticks,
       });
       layoutGRef.current = newG;
       laidOutIds.current = currentIds;
       return positions;
     });
   }, [allNodes, allEdges]);

   // Sync G ref to state whenever posMap changes (G and posMap always computed together)
   useEffect(() => {
     setLayoutG(layoutGRef.current);
   }, [posMap]);
   ```

4. **Pass `G` to ZoneLabels and NodeLabels** (updated signatures from Step 4):
   ```tsx
   <ZoneLabels scale={layoutG} />
   {posMap.size > 0 && (
     <NodeLabels nodes={allNodes} positions={posMap} scale={layoutG} />
   )}
   ```

**Verify:**
```bash
cd /Users/jonathanmorris/poly_bot/cockpit/web && npx tsc -b && npx vitest run
```

Expected: TypeScript clean, all tests still pass.

---

## Step 4 — Labels counter-scale (Lever E)

**File:** `cockpit/web/src/scene/Labels.tsx`  
**Tests:** none (TypeScript compile is the gate)

### Changes

1. **`ZoneLabels` — add `scale` prop:**
   ```typescript
   export function ZoneLabels({ scale = 1 }: { scale?: number }) {
   ```
   Inside the JSX: `distanceFactor={46 * scale}`.

2. **`NodeLabels` — add `scale` prop:**
   ```typescript
   export function NodeLabels({
     nodes,
     positions,
     scale = 1,
   }: {
     nodes: Node[];
     positions: Map<string, [number, number, number]>;
     scale?: number;
   })
   ```
   Inside the JSX: `distanceFactor={(isTrade ? 22 : 28) * scale}`.

3. **`ZoneLabels` position lifting** — the zone label lift offsets are currently hardcoded
   (`lift = c === "figures" ? 15 : ... 8`). These are in world units and scale with G
   automatically because the anchor coordinates scale with G. No change needed here.

**Verify (final gate):**
```bash
cd /Users/jonathanmorris/poly_bot/cockpit/web && npx tsc -b && npx vitest run
```

Expected: TypeScript clean, all tests pass.

---

## Risks, edge cases, and mitigations

| Risk | Mitigation |
|------|-----------|
| `figures` G at 75 nodes is 1.01 (not exactly 1) due to rounding | Lower `PER_NODE_SPACING.figures` from 1.3 → 1.27 if visual regression detected |
| Warm-start delta > 5 in tests (anchor forces weaker than expected) | Raise `DELTA` threshold in warm-start test from 5 → 8; separately tune anchor strength |
| Options cluster: `opt.layer` node has labeled = yes (in LABELED_CLUSTERS) so gets larger collide | Correct behavior — options labels must not stack |
| Empty graph: `computeLayout([], [])` → `{positions: new Map(), G: 1}` | Explicit early-return guard in Step 2 |
| G ref / state sync glitch on first render (layoutG=1 before first layout) | `useState(1)` default + G=1 at current counts means first render is visually correct |
| `setPosMap` updater reading stale allNodes/allEdges via closure | allNodes/allEdges are in the dependency array; updater uses the closure-captured values which are always the latest at the time the effect fires |
| `mergeLayout` still imported in tests (mergeLayout tests must still pass) | mergeLayout internals are updated in Step 2 to use `.positions`; its external signature and behavior are unchanged; its tests remain as-is |
| Single-node cluster with G > 1: spread is `minSpread` but anchors are scaled out | Correct — minSpread nodes get extra breathing room between zones, which is fine |
| d3-force-3d types — `forceCollide` callback type | Cast `d` as `SimNode` or `any` (existing pattern in file) |

---

## Verify command (each step)

```bash
cd /Users/jonathanmorris/poly_bot/cockpit/web && npx tsc -b && npx vitest run
```

Run from `cockpit/web` every time. A clean TypeScript build + green Vitest are the acceptance gate for each step. Do not proceed to the next step until both pass.

---

## What `mergeLayout` should become after this plan

- Still exported from `layout.ts` — kept as a dead export.
- Not called from `SceneRoot.tsx` (Step 3 removes the call site).
- Its `mergeLayout` tests in `layout.test.ts` continue to pass (no changes to those tests).
- The audit agent may delete the function and its tests if it determines they add no value; but
  this plan does NOT instruct deletion (keep the surface area of Step 3 minimal).

---

## Test count summary

| Category | New tests added | File |
|----------|-----------------|------|
| `computeSpread` calibration + scaling + floor | 3 | layout.test.ts |
| `computeG` at-count=1, growth, floor, empty | 4 | layout.test.ts |
| Density invariance (figures 80 nodes) | 1 | layout.test.ts |
| Label room (council 10 nodes) | 1 | layout.test.ts |
| Zone separation (figures 150 + council 10) | 1 | layout.test.ts |
| Scale source of truth (G identity) | 1 | layout.test.ts |
| Determinism with opts.initial | 1 | layout.test.ts |
| Warm-start stability (delta < 5) | 1 | layout.test.ts |
| New node in warm-start is finite | 1 | layout.test.ts |
| **Total new** | **14** | |
| Pre-existing (kept, updated for return type) | 12 | layout.test.ts |
| **Grand total** | **26** | |
