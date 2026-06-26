# Cockpit constellation — layout that scales with growth (design)

**Date:** 2026-06-26 · **Status:** BUILT + REFINED (user-approved after visual tuning)

> **Refinement note (2026-06-26).** After building the five levers we visually tuned
> the layout with the user and refined the intent. The headline changes from the
> original draft below:
> - **Lever A is now TWO families** (§A): big *unlabeled* banks (figures, ideas) stay
>   **compact** (small per-node spacing) so they don't inflate the world and zoom the
>   camera out; *labeled* clusters get **generous** per-node spacing so names never stack.
> - **G is no longer pinned to ~1** (§C). With generously-spaced labeled zones, `G`
>   naturally rises (~1.5 at realistic counts) to guarantee zone separation. That is the
>   mechanism working as intended, **not** a regression. `G` is driven by the binding
>   *labeled* pair (council↔core), so growing a compact bank does not move it.
> - **FitView frames the labeled SPINE only** (§F, new): the camera fits
>   council→core→execution→trades and the big banks (figures/ideas/sources) bleed off the
>   edges — the close, legible view the user prefers. The banks still render.
> - **View-aware vertical label de-clutter** (§G, new) in `NodeLabels`.
>
> The original draft is preserved below; sections updated by the refinement are marked.

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
- `scene/SceneRoot.tsx` — warm-start recompute on node-set change; expose the global
  scale; **spine-only FitView framing** (§F).
- `scene/Labels.tsx` — counter-scale label `distanceFactor` (lever E); per-cluster zone
  lift; **view-aware vertical de-clutter** (§G).
- `scene/__tests__/layout.test.ts` — extend with scaling/spacing assertions.

Out of scope: every other scene/UI file.

## The approach — five levers (+ two refinement mechanisms F, G)

### A. Density-preserving spread — TWO families *(refined)*
Replace the fixed `CLUSTER_SPREAD[c]` radius with a **count-aware** footprint so
**spacing-per-node stays constant** as a cluster grows (no clumping):

```
spread(c) = max(minSpread[c], perNodeSpacing[c] * sqrt(count_c))
```

Rationale: N nodes evenly filling a disk of radius R at per-node spacing s satisfy
`N·s² ≈ πR²`, so `R ∝ s·√N`. √count keeps projected density constant.

**Refinement — two families of `perNodeSpacing`:**
- **Big UNLABELED banks (figures, ideas)** — hover-only dots, no always-on labels. Use a
  **small** per-node (≈0.52) so the bank stays a **compact** dense cloud and does not
  inflate the world (which would force the camera to zoom out). Still grows ∝ √count, and
  `minSpread` (≈6 for figures) keeps small banks from collapsing.
- **LABELED clusters (council/sources/core/execution/market/infra/options)** — every node
  shows a name, so use a **generous** per-node (≈3–5) so labels never stack even at a close
  zoom. Net effect: a 6-node labeled cluster legitimately occupies a *larger* footprint
  than a 100-node figure bank.

### B. Label-aware collision *(refined constants)*
The collide force min-distance is cluster-aware so **labeled** nodes never let their labels
stack, while the dense unlabeled banks stay tightly packed:

```
forceCollide(d => LABELED_CLUSTERS.has(d.cluster) ? labeledRadius : figureRadius)
```

`LABELED_CLUSTERS` = clusters whose node types Labels.tsx renders always-on (council/
advisors, data sources, engine parts, exec parts, infra, trades). `learning` (outcome
nodes) and the figure/idea banks are **not** in the set. Refined constants:
`labeledRadius = 5.6` (generous label room), `figureRadius = 1.1` (tight banks).

### C. Flow preserved, zones kept apart — G as a separation GUARANTEE *(refined)*
Keep the `CLUSTER_ANCHOR` directions/shape exactly. Apply a single **global scale `G`**
that expands all anchors radially from the core enough to keep a minimum gap between every
pair of adjacent zones' spread-spheres:

```
G = max(1, max over adjacent pairs (A,B) of (spread(A) + spread(B) + MIN_ZONE_GAP) / anchorDist(A,B))
anchor'(c) = core + (anchor(c) - core) * G       // core is origin, so anchor'(c) = anchor(c) * G
```

This is the **core invariant**: for every adjacent pair, after scaling,
`spread(A) + spread(B) + gap ≤ anchorDist(A,B) · G` — spread-spheres never overlap.

**Refinement — `G` is NOT pinned to 1.** With the generous labeled spacing of §A, the
binding pair is a *labeled* one (council↔core), so `G` settles around **~1.5** at realistic
counts and **rises further when a labeled cluster grows**. Growing a compact *bank* does
**not** move `G` (its small footprint is never the binding constraint). A larger `G` is
visually neutral because FitView (§F) re-frames; its only job is zone separation.

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
As the world grows and `FitView` zooms, drei `<Html distanceFactor>` labels would change
on-screen size. Counter-scale `distanceFactor` by `G` (zone + node labels) so labels keep
their on-screen size. ZoneLabels also multiply their **world position** (anchor + a
per-cluster `ZONE_LIFT`) by `G` so they stay centered over their now-`G`-scaled clusters.
**Label style is unchanged** — only scale factor and position.

### F. Spine-only camera framing *(refinement, SceneRoot/FitView)*
The user's preferred view is **close**, reading council→core→execution→trades, with the
SMART MONEY bank bleeding off the left edge rather than the camera zooming out to contain
the whole world. So `FitView` computes its bounding box from a **`framePositions`** subset
that **excludes the big banks `{figures, ideas, sources}`** — it frames only the labeled
*spine*. The banks still render at full extent; they simply sit outside the framing box and
bleed past the viewport edges. This keeps the loved close-in composition stable even as the
banks grow (their growth no longer pushes the camera back).

### G. View-aware vertical label de-clutter *(refinement, NodeLabels)*
Even with §A/§B spacing, projection can put two node labels on top of each other for a given
camera angle. `NodeLabels` runs a **throttled `useFrame`** that reads the on-screen rects of
the always-on labels and pushes colliding ones **down** via `translateY` (DOM only). This
touches **no node/edge geometry, material, motion, or controls** — it only nudges the HTML
label elements so names stay readable. Purely a legibility overlay.

## Data flow
`layout.ts` computes positions **and** the global scale `G` from the node set (`G` is
returned alongside `positions` in `LayoutResult`). `SceneRoot`/`Labels` read that same `G`
the anchors were scaled by — single source of truth, no drift. `SceneRoot` also derives the
`framePositions` spine subset (§F) for FitView.

## Testing *(updated for the refined intent)*
`layout.test.ts` is pure (no React/THREE). The suite asserts the refined invariants:
1. **Two families:** a 100-node *bank* (figures/ideas) packs **tighter** than a 6-node
   *labeled* cluster — `computeSpread(figures,100) < computeSpread(council,6)`; banks stay
   compact in absolute terms, labeled clusters stay generous.
2. **√count growth:** above the `minSpread` clamp, 4× count → ~2× spread.
3. **Density invariance:** the figure bank keeps a tight min pairwise spacing (≥1.2) **and**
   stays tight (an upper-bound canary catches any loosening toward labeled spacing).
4. **Label room:** labeled-cluster nodes keep generous separation (a floor well above the
   compact-bank packing) so a regression to bank-style packing is caught.
5. **G core invariant:** for every adjacent pair, after scaling, `sA + sB + gap ≤ dist·G`
   — checked at small *and* large counts. `G ≥ 1`, deterministic, and **rises when a
   labeled cluster grows** (banks growing does not move it).
6. **Zone separation:** adjacent clusters' centroids stay disjoint at 2× counts.
7. **Determinism:** identical inputs → identical positions.
8. **Warm-start:** adding one node nudges existing nodes within a small delta; across a
   **G change** (grow a labeled cluster) the stable zones expand **uniformly** — each node
   tracks `old · (G_new / G_old)`, a gentle nudge, not a reshuffle.
9. **Scale source of truth:** `G` from `computeLayout` == `G` from `computeG`.

Plus visual verification: render synthetic small and large graphs, screenshot at 1512 +
compact, confirm compact banks, generous legible labeled zones, the close spine framing
with banks bleeding off-edge, no label stacking, no zone overlap — evidence, not assertion.

## Success criteria
- The labeled spine reads close and legible; the SMART MONEY / IDEAS banks stay compact and
  bleed off-edge rather than zooming the camera out.
- A grown graph reads with the **same** comfortable density: compact banks, generous
  non-stacking labels, clear gaps between zones (G guarantees separation).
- No regression to node/edge styles, hover-trace, motion, or controls.
