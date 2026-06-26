# Cockpit constellation — layout that scales with growth (design)

**Date:** 2026-06-26 · **Status:** APPROVED (design) → next: implementation plan

## Goal
The cockpit's 3D "neural constellation" is loved as-is. As the system tracks **more
figures, more council channels, more ideas, more trades**, the layout must keep
**even, readable spacing** and a **legible left→right flow** — so you can always see
everything and trace how it connects. Today it crowds: clusters cram into fixed-radius
spheres, labeled council/data-source nodes stack, and the dense "SMART MONEY" bank is a
blob with knots.

## Hard constraints (do NOT change)
Purely a **spatial-layout** change. These are untouched:
- Node styles/materials/sizes (`NodeMesh`, `NodeInstances`), the segmented edge style
  (`EdgeLines`, `PulseLayer`), all glow/bloom/theme tokens.
- Hover/selection behavior — the segmented connection lines, click-to-trace edge
  highlighting, tooltips. All motion/animation. Orbit/zoom camera controls.
- The recognizable composition: the left→right pipeline
  `DATA → SMART MONEY → COUNCIL → CORE → IDEAS → EXECUTION → TRADES`, with
  `LEARNING` up, `INFRA` down, `OPTIONS` lower-right.

## Files in scope
- `scene/layout.ts` — the force layout (core of the work).
- `scene/SceneRoot.tsx` — call the warm-start recompute on node-set change; expose the
  global scale.
- `scene/Labels.tsx` — counter-scale label `distanceFactor` only (lever E).
- `scene/__tests__/layout.test.ts` — extend with scaling/spacing assertions.

Out of scope: every other scene/UI file.

## The approach — five levers

### A. Density-preserving spread (core fix)
Replace the fixed `CLUSTER_SPREAD[c]` radius with a **count-aware** footprint so
**spacing-per-node stays constant** as a cluster grows (no clumping):

```
spread(c) = max(minSpread[c], perNodeSpacing[c] * sqrt(count_c))
```

Rationale: N nodes evenly filling a disk of radius R at per-node spacing s satisfy
`N·s² ≈ πR²`, so `R ∝ s·√N`. √count keeps projected density constant. `perNodeSpacing`
and `minSpread` are per-cluster; exact constants tuned against real screenshots in the
verification loop.

### B. Label-aware collision
The collide force min-distance becomes cluster-aware so **labeled** nodes never let
their labels stack, while the dense unlabeled figures stay tightly packed:

```
forceCollide(d => LABELED_CLUSTERS.has(d.cluster) ? labeledRadius : figureRadius)
```

`LABELED_CLUSTERS` = the node types Labels.tsx renders always-on (council/advisors,
data sources, engine parts, exec parts, infra, trades). `labeledRadius > 1.9` (current);
`figureRadius ≤ 1.9`. Tuned visually.

### C. Flow preserved, zones kept apart
Keep the `CLUSTER_ANCHOR` directions/shape exactly. Apply a single **global scale `G`**
that expands all anchors radially from the core **only enough** to keep a constant gap
between adjacent zones' spread-spheres as clusters grow:

```
G = max(1, growthHeadroom(maxSpread, tightestAnchorGap))
anchor'(c) = core + (anchor(c) - core) * G
```

Because the camera auto-fits (below), a uniform `G` is visually neutral at the macro
level — its job is purely to stop a grown cluster (e.g. SMART MONEY) from overrunning a
neighbor (COUNCIL). The composition stays the one you approved, just roomier.

### D. Gentle deterministic re-settle
On a data change (node-id set changes), recompute the **full** layout deterministically,
**warm-started** from current positions:
- Known nodes seed their x/y/z from the previous position map.
- New nodes anchor-seed as today (seeded PRNG around their cluster anchor).
- Run fewer ticks (~80 vs 200) so existing nodes **nudge** to make room rather than
  reshuffle. Same inputs → same output.

New signature: `computeLayout(nodes, edges, opts?: { seed?, ticks?, initial?: Map })`.
`SceneRoot` calls this with `initial = prev posMap`, replacing the append-only
`mergeLayout`. The existing `FitView` already re-frames the whole constellation when
`positions.size` changes, so growth stays fully in view.

### E. Constant label legibility
As the world grows and `FitView` zooms out, drei `<Html distanceFactor>` labels would
shrink on screen. Counter-scale `distanceFactor` by `G` (zone + node labels) so labels
keep their on-screen size. **Label style is unchanged** — only its scale factor. (If
undesired, drop this lever; rest stands.)

## Data flow
`layout.ts` computes positions **and** the global scale `G` from the node set. `G` is
exported (e.g. returned alongside positions, or a small `computeLayoutScale(nodes)`
helper) so `SceneRoot`/`Labels` read the same value the anchors were scaled by — single
source of truth, no drift.

## Testing
Extend `layout.test.ts` (pure function, no React/THREE — already the pattern):
1. **Density invariance:** min intra-cluster pairwise spacing does not collapse when a
   cluster's count doubles (footprint grew with √count).
2. **Label room:** labeled-cluster nodes keep ≥ `labeledRadius` separation.
3. **Zone separation:** adjacent clusters' bounding spheres stay disjoint at small and
   large counts (no overlap of SMART MONEY into COUNCIL, etc.).
4. **Determinism:** identical inputs → identical positions.
5. **Warm-start stability:** adding one node leaves existing nodes within a small delta
   of their prior positions (gentle nudge, not reshuffle).
6. **Scale source of truth:** `G` used for anchors == `G` exposed to labels.

Plus visual verification: render synthetic small and large graphs (e.g. 6 vs 30 council,
75 vs 200 figures, 6 vs 20 trades), screenshot at 1512 + compact, confirm even spacing,
legible flow, no label stacking, no zone overlap — evidence, not assertion.

## Success criteria
- A small graph looks essentially identical to today (composition preserved).
- A grown graph (2–3× nodes per cluster) reads with the **same** comfortable density:
  no knots, no stacked labels, clear gaps between zones, whole thing in frame.
- No regression to node/edge styles, hover-trace, motion, or controls.
