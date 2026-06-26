# Cockpit Options Integration — Visual Layout / UX Plan

**Date:** 2026-06-26  
**Status:** DESIGN (read-only — no code changes)  
**Purpose:** Concrete placement, spacing, and interaction design for adding the options
expression layer to the arbiter cockpit. Feeds the build phase.

---

## 1. Current Layout Baseline

### 1.1 What is currently on screen

Synthesized from code + screenshots at 1512px and compact (~828px):

| Element | Position | Size | z-order |
|---|---|---|---|
| HUD ("Arbiter Cockpit" card) | top: 16, left: 16 | ~240px wide, auto-height | base |
| Open Positions panel | top: 16, left: 50% (centered) | 560px wide, up to 60vh | z: 5 |
| InspectionPanel (node detail) | top: 16, right: 48 | 320px wide, up to calc(100vh-32px) | base |
| Legend toggle + panel | bottom: 84, left: 16 | 222px wide | base |
| HoverTooltip | bottom: 84, left: 50% (centered) | ~200px, nowrap | z: 10 |
| "Follow the Money" button / Walkthrough | bottom: 84, right: 48 | 320px wide when open | base |
| 3D Canvas + constellation | full viewport | 100vw × 100vh | background |

### 1.2 3D constellation cluster anchors (world units)

From `layout.ts` `CLUSTER_ANCHOR`:

```
sources   [-46, 11, -5]   — far left, mid-high
figures   [-30, -1,  2]   — left bank (large dense cloud ~75 nodes)
council   [-14,  5, -2]   — mid-left
core      [  0,  0,  0]   — bright center
ideas     [ 14, -4,  3]   — mid-right, slight down
execution [ 28,  4, -3]   — right, slight up
market    [ 42, -1,  2]   — far right
learning  [  2, 20,  5]   — high center (feedback loop)
infra     [ -2,-18, -5]   — below center
```

Pipeline reads left → right in screen space:
`DATA → SMART MONEY → COUNCIL → CORE → IDEAS → EXECUTION → TRADES`
with `LEARNING LOOP` high above center and `INFRA` below.

### 1.3 Observed spacing hazards (from screenshots)

- At 1512px: the top-center Positions panel (560px) sits between the HUD (left) and
  Inspection panel (right: 48). With the Inspection panel at 320px + 48px right margin,
  and the HUD at 240px + 16px margin, the center panel is well-clear of both at 1512px.
- At compact (~828px): the Positions panel (560px) is centered and the bottom of it
  approaches the midpoint of the screen. HUD and Inspection panel can share the sides
  because they are each narrow.
- The bottom row (Legend left, Tooltip center, Walkthrough right) at `bottom: 84` leaves
  space above a macOS Dock and avoids the bottom edge.
- The constellation core renders roughly in the right-center 60% of the screen at 1512px.
  The upper-right quadrant (above the Execution / Trades cluster) and the lower-right
  quadrant are open sky.

---

## 2. Options Node — 3D Constellation Placement

### 2.1 Reasoning

The options expression layer logically sits **after** the council reaches conviction but
**before / alongside** execution. Options are not equity trades; they are a parallel
expression of the same thesis at a different risk profile. They connect:

- `core.fusion` → `A4.options` (the decision to express via options)
- `A4.options` → `option_position.*` (dynamic nodes, like `trade.*`)

There is no existing cluster that cleanly owns options without crowding. The two candidate
zones in world-space are:

- **Below `learning` / above `core`** — occupied by INFRA and the feedback loop; adds
  visual confusion about purpose.
- **Below `execution` and to the right of `core`** — currently clear sky in the
  lower-right of the constellation. This is adjacent to execution semantically and
  sits between execution and infra in screen space.

**Decision: add an `options` cluster anchored at approximately `[28, -14, -4]`.**

Rationale:
- Execution anchor is `[28, 4, -3]`. Moving down 18 world units (y = -14) places options
  directly below execution in 3D space and below it on screen — legible as "execution's
  derivative expression."
- `infra` is at `[-2, -18, -5]` — that is 30 world units to the left and 4 units lower.
  With collision forces the options cluster will not overlap infra.
- `market` (trades) is at `[42, -1, 2]` — offset 14 right, no y overlap.
- In every screenshot the region around screen coordinates (right 40%, bottom 35%) is
  empty sky. The options cluster will occupy that void.
- The zone label "OPTIONS" will appear 8 units above the cluster anchor (same lift
  convention as other clusters), in the region that currently has no labels.

### 2.2 New cluster definition

```typescript
// layout.ts addition
options: [28, -14, -4]   // below Execution, right of Infra
```

```typescript
// contract.ts addition
// Cluster type extends to include "options"
// CLUSTER_COLOR.options: "#f9a825"  (deep amber — options/volatility orange)
```

Spread radius: `4.5` (matching `execution`). With typically 2-5 option position nodes at
any time, no crowding.

### 2.3 Node population

- **Static node:** `A4.options` — type `advisor` (or new type `option_engine`), cluster
  `options`. Always present. Lit by `options_mode` (`off` = dim, `shadow` = warm glow,
  `paper` = bright amber).
- **Dynamic nodes:** `option_position.<id>` — type `trade` (reuse; same rendering path),
  cluster `options`. Appear when positions open.

### 2.4 Edges

- `core.fusion → A4.options` (kind: `decides`) — static graph edge
- `A4.options → option_position.*` (kind: `holds`) — dynamic edge (like equity trades)
- `option_position.* → option_outcome.*` (kind: `resolves`) — dynamic

Zone label: `OPTIONS` rendered at `[28, -6, -4]` (8 units above anchor).

---

## 3. 2D Overlay Panel — Options Panel Design

### 3.1 Core decision: dedicated bottom-right collapsible panel

**Not** a tab in the Inspection panel (that is node-specific, opened per click).  
**Not** injected into the Positions panel (that is equity-only; options are explicitly
meant to read as a separate track per the charter).  
**Yes:** a new standalone `OptionsPanel` component, bottom-right corner, collapsible.

This mirrors the design language of the Positions panel (top-center) and keeps the two
financial tracks visually separate.

### 3.2 Exact position and size

```
position: absolute
bottom:   84px     (same bottom row as Legend, Tooltip, Walkthrough)
right:    48px     (same right margin as InspectionPanel and Walkthrough)
width:    400px
maxHeight: 52vh    (generously below the InspectionPanel's top: 16)
```

**Why `bottom: 84, right: 48`?**  
- The Walkthrough panel / button also lives at `bottom: 84, right: 48` but is the size
  of a ~320px card. The OptionsPanel needs to coexist. Solution: the Walkthrough lives
  at `bottom: 84, right: 48` and the OptionsPanel is stacked immediately above it. See
  Section 3.3 for the stacking resolution.

**Why not top-right?**  
The InspectionPanel already owns `top: 16, right: 48` and can expand to full viewport
height. Putting OptionsPanel there would collide whenever a node is selected.

**Why not top-center?**  
The Positions panel owns that region at 560px width.

**Why not bottom-center?**  
The HoverTooltip occupies `bottom: 84, left: 50%`. A wide panel there would collide.

**Why not bottom-left?**  
The Legend lives there. Space is tight at compact widths.

### 3.3 Coexistence with the Walkthrough panel

The Walkthrough button/panel lives at `bottom: 84, right: 48` and when open is 320px
wide. The OptionsPanel is 400px wide.

Resolution — **vertical stacking, not horizontal**:

- OptionsPanel: `bottom: 84, right: 48` (anchored at bottom row, collapses to ~36px
  header when closed).
- When OptionsPanel is open, the Walkthrough button shifts up: it gets placed at
  `bottom: 84 + OptionsPanel.height + 8px gap`. This is achieved by giving the
  Walkthrough a dynamic `bottom` driven by a CSS variable or by placing both inside a
  flex column container:

```
[right-column container]
position: absolute
bottom: 84
right: 48
display: flex
flex-direction: column-reverse   ← bottom-up stacking
gap: 8px
align-items: flex-end
```

Children (bottom to top):
1. OptionsPanel (collapsed = 36px header strip, open = up to 52vh)
2. Walkthrough button / panel

This way the Walkthrough is always above the OptionsPanel, never behind it, regardless
of the OptionsPanel's open/closed state. When both the Walkthrough panel (320px wide,
~180px tall) and the OptionsPanel are open simultaneously, combined height is at most
52vh + 180 + 8 ≈ 58vh + 8. At 828px viewport height that is still within the upper 42%
of the viewport, well below the top-center Positions panel (which ends by ~60vh from top).

### 3.4 OptionsPanel internal structure

```
┌────────────────────────────────────────────┐  ← border-radius: 10
│ OPTIONS  [shadow|paper|off badge]  ▾ hide  │  ← header, sticky, 36px
├────────────────────────────────────────────┤
│ SLEEVE STATUS (collapsible stats strip)    │  ← 3 stats inline
│ 35% sleeve · used 12% · IV-rank gate: off  │
├────────────────────────────────────────────┤
│ OPEN OPTION POSITIONS                      │  ← SectionTitle style
│ [mini-table: contract, side, Δ, qty,       │
│  entry, current, DTE, P&L]                │
│ (empty: "no open option positions")        │
├────────────────────────────────────────────┤
│ RECENT PLAYS (last 5)                      │  ← SectionTitle
│ [mini-table: ticker, type, side, strike,   │
│  expiry, gate reason, shadow/paper, PL]    │
│ (empty: "building IV history…")            │
├────────────────────────────────────────────┤
│ IV HISTORY (per ticker, last 10)           │  ← SectionTitle, collapsible
│ [mini-table: ticker, ATM IV, date, rank]   │
└────────────────────────────────────────────┘
```

Scrollable body (`overflowY: auto`) so many positions never push the panel off-screen.
Max-height is `52vh` so at 900px it caps at ~468px, leaving headroom above the Dock.

**Option Outcomes** are NOT shown in the OptionsPanel body. They are intentionally
surfaced via the inspection panel when the user clicks an `option_outcome.*` node in the
constellation — maintaining visual separation from equity learning just as the charter
requires. A note in the OptionsPanel header ("click an outcome node for P&L history")
provides the affordance.

### 3.5 Color / visual identity

OptionsPanel uses the same design tokens as PositionsPanel:
- `panelBg: "rgba(8,10,18,0.93)"`
- `panelBorder: "1px solid #1c2233"`
- `radius: 10`

Options-specific accent: `#f9a825` (deep amber). Used for:
- `options_mode` badge background when active
- IV-rank bar fill
- "paper" mode glow border on the panel: `border: "1px solid rgba(249,168,37,0.30)"`
- The `A4.options` node color in constellation matches this

Shadow mode: `#8d99ae` badge (muted — it's not live)
Off mode: header dim, no amber, body collapsed by default

---

## 4. Reading Flow

The intended eye path through the cockpit:

```
1. HUD (top-left)
   "Is the system live? What is my equity?"

2. Open Positions (top-center)
   "What equity positions are open? Current P&L?"

3. Constellation (center)
   "How did these positions form? Smart money → Council → Core → Ideas → Execution"
   (orbit/zoom as needed)

4. OptionsPanel (bottom-right, collapsed by default)
   "Is the options expression layer active? Any open contracts?"
   (expand to see plays, IV history, gate status)

5. InspectionPanel (right, on click)
   "Drill into a specific node — advisor trust, idea thesis, option outcome"

6. Walkthrough (bottom-right, above OptionsPanel)
   "Follow the Money" guided tour — reinforces the flow
```

The equity track (2 → 3) and options track (4) are adjacent to each other on the right
half of the screen but clearly separated: equity is top (Positions panel) and the
constellation center; options is bottom-right. The flow never forces the eye to cross
from one track to the other in an ambiguous direction.

---

## 5. Responsiveness and Overflow Handling

### 5.1 Viewport ≥ 1200px (normal desktop / 1512px)

- All panels fit: HUD (240px) + left margin (16px) leaves 1256px for center+right.
- OptionsPanel (400px) at right: 48 ends at right-edge + 448px from right = well clear.
- InspectionPanel (320px) at right: 48 does NOT conflict with OptionsPanel because
  InspectionPanel anchors top (not bottom). They share the right column without overlap.
- At 1512px there is ~700px of unclaimed horizontal space in the center that the
  constellation uses.

### 5.2 Viewport 828px - 1199px (compact laptop / iPad landscape)

- OptionsPanel stays at bottom-right at 400px — still fits (viewport is 828px; right
  edge at 828 - 48 = 780px, left edge at 780 - 400 = 380px — clear of the center).
- Positions panel at 560px centered: left edge at (828-560)/2 = 134px, right edge at
  694px. OptionsPanel left edge at 380px. These DON'T overlap vertically (Positions top,
  Options bottom).
- InspectionPanel (if open) at width 320px, right: 48 → right edge at 780, left at 460.
  OptionsPanel left at 380, InspectionPanel left at 460 — they would overlap horizontally
  but they are top-anchored (InspectionPanel) vs bottom-anchored (OptionsPanel) so only
  conflict if one is very tall. InspectionPanel caps at calc(100vh - 32px) and
  OptionsPanel caps at 52vh — they can meet in the middle. Resolution: OptionsPanel
  collapses to its 36px header when an InspectionPanel is open (a `selectedId != null`
  signal auto-collapses the OptionsPanel). This is a single condition to implement.

### 5.3 Viewport < 828px (rare on this dashboard, but handled)

- OptionsPanel width clamps to `min(400px, calc(100vw - 96px))` via `maxWidth`.
- The right-column container (Walkthrough + OptionsPanel) stacks vertically at bottom-right
  and can scroll if combined height exceeds the viewport — but `maxHeight: 52vh` on
  OptionsPanel prevents runaway growth.

### 5.4 Many option positions / plays

- Body is `overflowY: auto`. At maxHeight 52vh (~468px at 900px) there is room for the
  stats strip (~32px) + section titles + ~8-10 table rows before scroll activates.
- If `option_positions.length > 8`, the user scrolls within the panel. The header
  remains sticky, showing the count: `OPTIONS · 3 contracts`.
- IV history starts collapsed behind a `▸ show` toggle (like PositionsPanel's "▾ hide").

---

## 6. ASCII Layout Mock — 1512px viewport

```
┌─ viewport 1512 × 900 ──────────────────────────────────────────────────────────────────────────────────────────┐
│                                                                                                                  │
│  ┌─ HUD ─────────────┐   ┌─ OPEN POSITIONS (560px) ──────────────────────────────────────┐                     │
│  │ ARBITER COCKPIT   │   │ OPEN POSITIONS · 2  equity  daily P&L  gross  net  unreal P&L  │                     │
│  │ equity $9994      │   │ Ticker Side Shares Cost/sh Current ROI P&L                     │                     │
│  │ daily P&L -5.52   │   │ AMZN   LONG   1    $239    $233   -2.8%  -$6.74               │                     │
│  │ db daemon alpaca  │   │ UBER   SHORT  1     $72     $71   +1.1%  +$0.75               │                     │
│  └───────────────────┘   └────────────────────────────────────────────────────────────────┘                     │
│                                                                                                                  │
│                         LEARNING LOOP                                                                            │
│                                                                   ●                                             │
│                                  ●     ●                                                                         │
│           DATA SOURCES                                                                                           │
│   ●                         SMART MONEY   THE COUNCIL                          EXECUTION                        │
│      ●●●●●●●●●●              ●●●●●●●    ●●●  ●●                                  ●                             │
│   ●●●●●●●●●●●●●             ●●●●●●●         ●●                  DECISION         ●                             │
│      ●●●●●●●●●●●            ●●●●●●●              ●●             CORE          ●●●    IDEAS     TRADES          │
│         ●●●●●●●             ●●●●              ●●●●●●●   ●●●                       ●●●●        ●                │
│            ●                ●●●               ●●●●●●●      ●●●●●●●                  ●●●      ●                 │
│                              ●                 ●●●                                     ●     ●                  │
│                                             INFRA                                                               │
│                              ●●                                     OPTIONS (NEW)                               │
│                              ●                                         ● A4.options                             │
│                                                                         ●  ●  (option positions)                │
│                                                                                                                  │
│  ┌─ LEGEND ──────────┐                  ┌─ HOVER TOOLTIP (center-bottom) ─┐   ┌─ right column ──────────────┐  │
│  │ [toggle: Hide]    │                  │ node.id  status  intensity       │   │ ┌─ WALKTHROUGH ──────────┐ │  │
│  │ Cluster Colors    │                  └──────────────────────────────────┘   │ │ Follow the Money ▶     │ │  │
│  │ Node Types        │                                                          │ └────────────────────────┘ │  │
│  └───────────────────┘                                                          │ ┌─ OPTIONS PANEL ────────┐ │  │
│  [bottom: 84, left: 16]                                                         │ │ OPTIONS  [paper] ▾ hide│ │  │
│                                                                                  │ │ sleeve 35% · used 12%  │ │  │
│                                                                                  │ │ IV-rank gate: active   │ │  │
│                                                                                  │ │ ─────────────────────  │ │  │
│                                                                                  │ │ OPEN OPTION POSITIONS  │ │  │
│                                                                                  │ │ AAPL 230C 1/17 LONG    │ │  │
│                                                                                  │ │ Δ 0.41  qty 2  DTE 23  │ │  │
│                                                                                  │ │ entry $4.20  P&L +$80  │ │  │
│                                                                                  │ │ ─────────────────────  │ │  │
│                                                                                  │ │ RECENT PLAYS (last 5)  │ │  │
│                                                                                  │ │ TSLA 400C shadow … [▸] │ │  │
│                                                                                  │ │ ─────────────────────  │ │  │
│                                                                                  │ │ IV HISTORY [▸ show]    │ │  │
│  [bottom: 84]                                                                    │ └────────────────────────┘ │  │
└─────────────────────────────────────────────────────────────────────────────────┴─────────────────────────────┘
                                                                                   [bottom: 84, right: 48]
```

---

## 7. ASCII Layout Mock — Compact (~828px) with InspectionPanel open

```
┌─ viewport 828 × 900 ────────────────────────────────────────────────────────────┐
│                                                                                  │
│  ┌─ HUD ─────────────┐   ┌─ POSITIONS (560px, scrolls) ─┐  ┌─ INSPECT (320px)─┐│
│  │ ARBITER COCKPIT   │   │ OPEN POSITIONS · 2           │  │ [X]              ││
│  │ equity / P&L      │   │ stats strip                  │  │ trade            ││
│  │ db daemon alpaca  │   │ [table rows]                 │  │ AMZN LONG        ││
│  └───────────────────┘   └──────────────────────────────┘  │ avg $239 → ...   ││
│                                                              │ P&L -$6.74       ││
│              [ constellation — 3D scene ]                   │ [scroll]         ││
│                                                              │                  ││
│                                                              │                  ││
│                                                              └──────────────────┘│
│                                                                                  │
│  ┌─ LEGEND ──────────┐      ┌─ TOOLTIP ────┐      ┌─ right column ───────────┐ │
│  │ [Hide]            │      │ hoveredId    │      │ ┌─ WALKTHROUGH ─────────┐│ │
│  │ (collapsed)       │      └──────────────┘      │ │ Follow the Money ▶    ││ │
│  └───────────────────┘                            │ └───────────────────────┘│ │
│                                                   │ ┌─ OPTIONS (COLLAPSED) ─┐│ │
│                                                   │ │ OPTIONS [off]  ▸ show ││ │
│                                                   │ └───────────────────────┘│ │
│                                                   └──────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────────┘
```

When `selectedId != null` (InspectionPanel open), OptionsPanel auto-collapses to its
36px header strip. No overlap, no content hidden.

---

## 8. OptionsPanel Component Specification

### 8.1 File

`cockpit/web/src/ui/OptionsPanel.tsx`  
Registered in `CockpitUI.tsx` alongside `PositionsPanel` and `Walkthrough`.

### 8.2 Props

```typescript
interface OptionsPanelProps {
  /** When truthy, auto-collapse the panel body to avoid InspectionPanel overlap */
  inspectionOpen: boolean;
}
```

### 8.3 State

```typescript
const [open, setOpen] = useState(true);
// Auto-collapse when inspection panel opens
useEffect(() => {
  if (inspectionOpen) setOpen(false);
}, [inspectionOpen]);
```

### 8.4 CockpitUI render order (right column container)

```tsx
<div style={{
  position: "absolute",
  bottom: 84,
  right: 48,
  display: "flex",
  flexDirection: "column-reverse",
  gap: 8,
  alignItems: "flex-end",
}}>
  <OptionsPanel inspectionOpen={!!effectiveSelectedId} />
  <Walkthrough path={walkPath} />
</div>
```

Remove the `position: absolute, bottom: 84, right: 48` from `Walkthrough` and let the
container manage that.

### 8.5 OptionsPanel internal sections

All share the same `SectionTitle`, `KV`, `MiniTable` primitives already in `CockpitUI.tsx`.

**Header strip** (always visible, 36px):
```
OPTIONS  [mode-badge]  [n_positions dot]  ▾ hide / ▸ show
```
- mode-badge: amber "PAPER" | muted "SHADOW" | dim "OFF"
- n_positions: amber dot + count when > 0

**Stats strip** (show when open, ~32px):
```
sleeve 35%  ·  used: 12%  ·  IV-rank gate: [active|building|off]
```

**Open Option Positions** table:

| col | content |
|---|---|
| Contract | "AAPL 230C 1/17" (ticker + strike + callput + expiry) |
| Side | LONG / SHORT badge |
| Delta | e.g. 0.41 |
| Qty | contracts |
| Entry | entry premium per contract |
| Current | current mid |
| DTE | days to expiry |
| P&L | unrealized P&L, colored |

**Recent Plays** table (shadow + paper):

| col | content |
|---|---|
| Ticker | |
| Type | "shadow" (muted) or "paper" (amber) |
| Strike/Exp | condensed |
| Gate reason | conviction, IV-rank, cold-start — as a badge |
| P&L | for paper plays with outcomes |

**IV History** (collapsed by default, ▸ show toggle):

| col | content |
|---|---|
| Ticker | |
| ATM IV | as % |
| Date | |
| Rank | IV-rank percentile |

**Option Outcomes note** (footer, 1 line):
```
Click an option outcome node in the constellation for P&L history →
```
Styled in muted italic 11px.

---

## 9. Cluster / Legend Update

The Legend panel already auto-iterates `CLUSTER_COLOR` and `CLUSTER_LABELS`. Adding the
`options` cluster to those maps is the only change needed for Legend to display it.

```typescript
// contract.ts additions:
// Cluster type: add "options"
// CLUSTER_COLOR.options = "#f9a825"

// CockpitUI.tsx additions:
// CLUSTER_LABELS.options = "Options Expression"
// NODE_TYPE_DESCRIPTIONS additions (if new option_engine type used)

// layout.ts additions:
// CLUSTER_ANCHOR.options = [28, -14, -4]
// CLUSTER_SPREAD.options = 4.5
```

Zone label in `Labels.tsx`:
```typescript
ZONE_NAME.options = "OPTIONS"
// Rendered at [28, -6, -4] (lift = 8)
```

---

## 10. Walkthrough — Options Step

Add one step to the walkthrough path **after** step 5 (Execution) and before step 6
(Live Trade):

```typescript
{
  nodeId: "A4.options",
  label: "A4 · Options",
  clusterHint: "options",
  narration:
    "When conviction is high and IV-rank allows, the options expression layer papers or executes a matched option position — a leveraged expression of the same thesis, tracked separately from equity.",
},
```

This is a single array insert in `buildWalkthrough()`. The step counter ticks from 8
to 9 steps. No visual change to the walkthrough panel itself.

---

## 11. Verification Checklist (build phase)

After the build is complete, take a series of browser screenshots and verify each item.

### Screenshot 1: Default state, 1512px, no node selected, OptionsPanel open, options_mode = paper

- [ ] HUD visible top-left, not truncated
- [ ] Positions panel visible top-center, not obscured
- [ ] OptionsPanel visible bottom-right at correct size (400px)
- [ ] Walkthrough button visible immediately ABOVE OptionsPanel (not behind it)
- [ ] Legend visible bottom-left; tooltip zone bottom-center is clear
- [ ] Options cluster nodes visible in constellation lower-right quadrant
- [ ] Zone label "OPTIONS" renders without overlapping "EXECUTION" or "INFRA" labels
- [ ] No panel overlaps another panel

### Screenshot 2: Default state, 1512px, InspectionPanel open (click any trade node)

- [ ] InspectionPanel visible top-right (top: 16, right: 48)
- [ ] OptionsPanel auto-collapsed to 36px header strip
- [ ] No pixel overlap between InspectionPanel and OptionsPanel
- [ ] Positions panel still visible top-center (unaffected)

### Screenshot 3: Compact (~828px), no node selected, OptionsPanel open

- [ ] OptionsPanel width ≤ calc(100vw - 96px)
- [ ] OptionsPanel does not extend left past center of viewport
- [ ] Walkthrough button above OptionsPanel (vertical stack)
- [ ] Bottom row total height (Walkthrough + OptionsPanel) < 60vh

### Screenshot 4: OptionsPanel with 5+ option positions (force mock data)

- [ ] Table rows scroll within panel body (overflowY: auto active)
- [ ] Header "OPTIONS · 5 contracts" visible and sticky
- [ ] Panel does not grow past 52vh
- [ ] No content is clipped without scroll affordance

### Screenshot 5: options_mode = off

- [ ] A4.options node in constellation is dim (low intensity)
- [ ] OptionsPanel header badge shows "OFF" in muted gray
- [ ] OptionsPanel body collapses by default (default open = false when mode = off)
- [ ] No amber glow on panel border

### Screenshot 6: Walkthrough — options step

- [ ] Step narration for A4.options displays correctly
- [ ] A4.options node highlights in constellation
- [ ] Walkthrough panel is above OptionsPanel in right column (no overlap)

### Screenshot 7: IV History expanded

- [ ] IV History section visible within panel at correct font size
- [ ] Scrollable if > 5 tickers
- [ ] No overflow outside panel border

### Spacing checks (all screenshots)

- [ ] 16px margin on HUD (left edge matches left: 16)
- [ ] 48px margin on InspectionPanel right edge
- [ ] 48px margin on OptionsPanel right edge
- [ ] 84px bottom margin on all bottom-row elements
- [ ] 8px gap between Walkthrough and OptionsPanel in right column
- [ ] Panel border-radius 10px matches existing panels

---

## 12. Summary Table

| Decision | Choice | Why |
|---|---|---|
| Options node 3D position | `[28, -14, -4]` (below Execution) | Clear sky, semantically adjacent to execution, away from infra |
| Options cluster color | `#f9a825` (deep amber) | Options/volatility convention; distinct from all 9 existing clusters |
| Options panel type | Dedicated standalone panel (not a tab/section) | Charter: visual separation of options P&L track from equity |
| Options panel anchor | bottom-right in a flex column with Walkthrough | Right column is the natural home; stacking avoids horizontal crowding |
| Options panel size | 400px wide, 52vh max | Fits at 828px+ without touching Positions or HUD |
| InspectionPanel conflict | Auto-collapse OptionsPanel when selectedId != null | Prevents any possible pixel overlap at compact widths |
| Option outcomes display | Via InspectionPanel on node click only | Charter: option outcomes explicitly separated from equity learning |
| Walkthrough | Add 1 new step for A4.options after Execution step | Minimal change, maintains tour continuity |
| Legend | Auto-updated via CLUSTER_COLOR / CLUSTER_LABELS map extension | Zero extra code in Legend component |
