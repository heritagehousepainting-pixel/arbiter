# Cockpit ↔ Options integration — charter / seed plan

**Date:** 2026-06-26 · **Status:** SEED (brainstorm → plan → build loop, parallel sonnet agents)

## Goal
Make the arbiter cockpit reflect the **options expression layer** alongside the existing
equity system — one dashboard, both worlds. Today the cockpit is equity-only (built before
options); the option data (`option_positions`, `option_shadow_log`, `option_outcomes`,
`option_iv_history`, `options_mode`) lands in the DB but is invisible.

## Two intertwined deliverables (equal weight)
1. **Data integration** — surface options through the cockpit's frozen-contract pipeline.
2. **Visual layout / UX** — the new options elements must be laid out with proper spacing,
   **nothing covered or overlapping**, sensible reading flow, and clean integration into the
   existing 3D constellation + panels. The user explicitly cares about this: *spacing, no
   occlusion, everything makes sense in flow.* A correct-but-ugly/cluttered result fails.

## Hard constraints (do not violate)
- **Strictly READ-ONLY.** The cockpit opens the DB `mode=ro`; it NEVER writes the trading
  system. Options surfacing must remain read-only (new read endpoints / read queries only).
- **Frozen-contract discipline.** DTOs live in `api/contract.py` and are mirrored in
  `web/src/contract.ts` — extend both in lockstep; the web client and API agree by contract.
- **Don't disturb the equity views.** Additive only — existing nodes/panels/flows keep working.
- Pattern files: `api/` = `contract.py`, `db.py` (ro conn), `graph.py` (topology), `state.py`
  (live snapshot), `node_detail.py`, `positions.py`, `events.py`, `main.py` (routes). `web/src/`
  = `contract.ts`, `api.ts`, `App.tsx`, `ui/CockpitUI.tsx` + scene components.

## What to surface (refine in brainstorm)
- An **options node** in the constellation (e.g. `A4.options` / `options`), lit by
  `options_mode` (off = dim/absent, shadow = one glow, paper = active), wired from the council
  → options expression.
- **Open option positions** — contract (OCC), side, delta, contracts, entry premium, current
  premium/PL, underlying, days-to-expiry, the thesis/idea it expresses.
- **Recent plays** — shadow ("would-have-traded") and paper, with the gate reasoning
  (conviction, catalyst, IV-rank/cold-start), strike/expiry/delta, sizing vs the 35% sleeve.
- **IV history** — the ATM-IV accumulation per ticker (the IV-rank gate's fuel).
- **Option outcomes** — kept VISUALLY SEPARATE from equity learning (option P&L must read as
  its own track; it does not feed advisor trust — reflect that separation in the UI).

## Open questions for the loop
- New top-level node vs a sub-cluster hanging off the existing core/trades?
- A dedicated options panel vs tabs/sections within the existing inspector?
- 3D placement that doesn't crowd the existing constellation; panel placement that doesn't
  occlude existing overlays at common window sizes (the cockpit has known fit/spacing screenshots:
  `cockpit-*.jpeg/png` at repo root — use them as the layout baseline).

## Deliverable of the loop
A finalized integration + layout plan (contract DTOs, API routes/state, web components +
their exact placement/spacing/z-order), then the build, then a layout-verification pass
(screenshots, no occlusion, readable flow) — consistent with the cockpit's existing patterns.

---

# SYNTHESIS — reconciled build spec (2026-06-26)

Both research docs in: docs/specs/research/2026-06-26-cockpit-options-{data,layout}.md.

## Resolved conflict — the node
Use **`opt.layer`** (the data plan): type `engine_part`, an EXECUTION-PATH WAYPOINT with edges
`core.safety →(decides)→ opt.layer →(submits)→ exec.adapter`. It is NOT an advisor — do NOT call
it `A4.options` and do NOT give it a `teaches`/learning edge (options is an expression layer, not
a brain). Intensity from `OPTIONS_MODE` (read env) + 7d `option_shadow_log` activity: off=0.05,
shadow=grows with rows, paper=0.7+. SPATIALLY, render it in the layout plan's amber (`#f9a825`)
"OPTIONS" zone, lower-right (~[28,-14,-4], below execution, clear of trades/market), with its
dynamic children (option positions/plays) clustering there.

## FROZEN DTO contract (both build agents honor EXACTLY; py in contract.py, ts mirror in contract.ts)
- `OpenOptionPosition`: option_positions cols + computed `dte`, `current_mid`(nullable), `unrealized_pl`(nullable).
- `OptionShadowPlay`: full option_shadow_log row incl `gate_express`(bool), `gate_reason`.
- `OptionOutcomeRecord`: option_outcomes cols incl `option_pl_pct` + `underlying_alpha_bps` (ISOLATED — separate route, no learning edge).
- `IVPoint`/`IVSeries`: per-ticker ATM-IV series + computed `current_iv_rank`(None if <30 pts).
- `OptionsState`: lists of the above + aggregates `win_rate`, `avg_option_pl_pct`, `avg_underlying_alpha_bps`, `sleeve_used_pct`, plus `options_mode`.

## Routes (read-only)
`GET /options`→OptionsState · `GET /options/iv/{ticker}`→IVSeries (empty not 404) · `GET /node/opt.layer`→NodeDetail (extend the `/node/{id}` prefix table with `opt`). `State.nodes["opt.layer"]` added in build_state. Openness of a position = no matching option_outcomes row (mirror arbiter/options/positions.py).

## Layout (from the layout plan — honor it)
`OptionsPanel`: 400px (clamp `min(400px, calc(100vw-96px))`), max 52vh, collapsible, `bottom:84 right:48`,
in a `flex column-reverse` with Walkthrough above (8px gap). InspectionPanel stays `top:16`.
OCCLUSION GUARD: when `selectedId != null` (InspectionPanel open) → OptionsPanel auto-collapses to its
36px header strip (useEffect). Option OUTCOMES are NOT in the panel body — only via InspectionPanel on an
`option_outcome.*` node (a muted footer affordance points there) — preserving separation from equity learning.
Reading flow: HUD → equity Positions (top) → constellation → OptionsPanel (bottom-right) → InspectionPanel on demand → Walkthrough (+ new opt.layer step).

## Build split
- **Backend agent**: contract.py DTOs + ITS contract.ts MIRROR (one agent owns both to keep them in sync) +
  graph.py (opt.layer node/edges, amber zone) + state.py (intensity + OptionsState) + options.py + options_detail.py +
  node_detail.py (`opt` prefix) + main.py routes + cockpit api tests.
- **Frontend agent**: api.ts client calls + OptionsPanel component + the 3D opt.layer node/zone rendering +
  InspectionPanel option-node detail + the occlusion auto-collapse + the Walkthrough opt.layer step.
- **Me (integrator)**: wire/verify, run the cockpit, layout-verification screenshots (≥1512px + compact),
  confirm no occlusion / clean spacing / correct flow; iterate; tests green.

CONSTRAINTS (unchanged): strictly READ-ONLY, additive (equity views untouched), frozen-contract py↔ts in lockstep.
