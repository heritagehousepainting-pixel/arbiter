# Robotics Watchlist (Observational Board) — Design

*Date: 2026-07-13 · Status: DRAFT (awaiting review) · Sub-project #2 of the robotics thematic-intelligence module*

## Context

The creator is macro-bullish on robotics as the next wave after AI and wants a standalone
robotics **thematic-intelligence module** in the arbiter/cockpit stack. The plan decomposes
into three pieces:

1. **The map** — DONE 2026-07-13. Deep sector research → `docs/specs/research/2026-07-13-robotics-map.md`
   (human map) + `docs/specs/research/robotics_map.json` (structured companies + supply-edge graph).
2. **The observational board (THIS SPEC)** — a tracked, tagged robotics universe visible in cockpit.
3. **The early-insight signal** — surfaces robotics developments pre-mainstream (later; sits on #2).

Decision already taken (brainstorming, 2026-07-13): the board is built **"both, sequenced"** —
display-only now, with a clean, *deliberate* seam for feeding the engine later. Universe:
**curated core + reference rows** (see §5).

## Key codebase finding (why this design is safe)

There are two unrelated things called "watchlist," wired to two different price paths:

- **Cockpit watchlist** — `cockpit/web/src/ui/watchlistStore.ts`: browser-local Zustand store,
  persisted to `localStorage` key `"cockpit-watchlist"`. Its own docstring: *"display-only …
  MUST NEVER be forwarded to arbiter's ingest runner or trading engine."* Charts come from
  read-only per-symbol endpoints `GET /ticker/{symbol}` and `GET /chart/{symbol}` that hit
  Alpaca live. The server holds **no** watchlist-membership state.
- **Ingest `_DEFAULT_WATCHLIST`** — `arbiter/arbiter/ingest/runner.py:53`, derived from
  `arbiter/arbiter/data/sectors.py::_SECTOR_BY_TICKER` via `covered_tickers()`. This decides
  which tickers get their **SEC filings scanned**; a qualifying filing (form4/congress/13d/13f)
  is what makes a ticker a trade `Idea` (`arbiter/arbiter/signals/detection.py:148` →
  `arbiter/arbiter/engine/_engine.py:596`). This is the real upstream path to trade-eligibility.

There is **no per-ticker tag/group/layer facility today** — the only per-ticker category is a
single, trade-load-bearing GICS string in `sectors.py` (feeds the 20% per-sector risk cap).
It must **not** be overloaded with robotics/layer tags.

**Consequence:** the observational board can live entirely in the cockpit display layer, which
is *structurally incapable* of making a symbol trade-eligible. This is the whole point.

## Goals

- A curated, layer-tagged robotics universe, visible as a dedicated cockpit panel.
- Live price + day/month change on the US-listed subset; foreign/private chokepoints shown as
  tagged **reference rows** (no live price) so they stay visible.
- Seeded from the already-generated `robotics_map.json` (layer + longevity + early-insight tags).
- **Zero changes to arbiter's trading path.** No new trade-eligibility, by construction.

## Non-goals (explicitly out of scope for this build)

- Any change to `sectors.py`, `_DEFAULT_WATCHLIST`, the engine, or ingest.
- Engine-wiring / trade-eligibility (that is a *separate, later, reviewed* change — see §7).
- The early-insight signal (#3).
- Writing to any database (the cockpit API DB is opened `mode=ro`).

## Architecture

Three additive pieces, all in the cockpit (nothing in `arbiter/`):

1. **Static roster module** — `cockpit/api/robotics_roster.py`. Mirrors the static-dict pattern
   of `sectors.py`: a pure, in-memory list of roster entries (no I/O, no network). Generated
   once from `robotics_map.json`, then hand-curated. Pure + deterministic so it is trivially
   testable and cannot fail at request time.
2. **Read-only endpoint** — `GET /robotics-watchlist`, registered in `cockpit/api/main.py`,
   returning the roster as JSON. No DB access, no membership state, no writes. DTO defined in
   `cockpit/api/contract.py` and mirrored in `cockpit/web/src/contract.ts`.
3. **Cockpit panel** — a new HTML-overlay component `cockpit/web/src/ui/RoboticsPanel.tsx`
   (sibling to `WatchlistBar.tsx`), mounted in `CockpitUI.tsx`, toggled from the existing UI
   chrome. Groups entries by layer; renders live price/change per priceable row (reusing the
   existing `fetchTickerDetail` / `/ticker` path); opens the existing `WatchlistChartBox` on
   click for a chart. Reference rows render tagged but without price/chart.

The user's personal `localStorage` watchlist (`watchlistStore.ts`) is left untouched and
remains a separate feature.

## Data model — roster entry

```
RoboticsRosterEntry {
  symbol: string          // display ticker; for reference rows may be a home-exchange symbol e.g. "6324.T"
  company: string
  layer: "compute" | "brain" | "components" | "integrator" | "deployment"
  formFactors: string[]   // e.g. ["humanoid","industrial"]
  longevity: "chokepoint" | "durable" | "commodity" | "hype-risk" | "unclear"
  earlyInsight: boolean    // ⭐ from robotics_map.json earlyInsightCandidates
  trigger?: string         // "trigger to watch" text, for early-insight rows
  priceable: boolean       // true → live chart via /ticker + /chart; false → reference row
  note?: string            // one-line context (what they do / why on the board)
}
```

`GET /robotics-watchlist` returns `{ generated: string, entries: RoboticsRosterEntry[] }`.

## Universe & selection (§5)

- **Charted core (~30-40, `priceable: true`):** map companies with a **US-listed / ADR** ticker
  that Alpaca can price, chosen for signal — the chokepoints, sector leaders, and any US-listed
  early-insight names. Spanning every layer. (e.g. NVDA, TSM, ARM, ISRG, SYM, HSAI, and peers —
  exact list resolved during implementation by validating each candidate against `GET /ticker`.)
- **Reference rows (`priceable: false`):** foreign-only chokepoints (Harmonic Drive, Nabtesco,
  Fanuc, Unitree, RoboSense, Korean NPUs) and **private** early-insight names (GSA/Rollvis,
  Skild, Physical Intelligence, Auterion, Neura, Mujin). Tagged and visible; no chart.
- **`priceable` is determined during curation** and verified by hitting `GET /ticker/{symbol}`
  at build time; the flag is baked into the static module so runtime never guesses.

## What's displayed

Per row: company + symbol, layer chip, form-factor chips, longevity badge, ⭐ early-insight
flag, and — for priceable rows — live price + day%/month% (from the existing `/ticker` path)
with a click-to-chart. Early-insight rows also surface their `trigger` text. Grouped by layer,
with the map's headline framing (two chokepoint spines) available as panel copy.

Framing note: this is a **tagged, navigable reference of the whole sector, priced where the
feed reaches** — not a wall of live tickers. Many of the most interesting nodes are foreign or
private and appear as reference rows by design.

## The clean seam for later — engine wiring (§7, OUT OF SCOPE)

When (and only when) the creator decides robotics names should become trade-eligible, the
`robotics_roster.py` list becomes the source for a **separate, reviewed** change that adds the
chosen US-listed tickers to `sectors.py::_SECTOR_BY_TICKER` (which flows into
`_DEFAULT_WATCHLIST` and starts scanning their filings). That is the real trade-eligibility
path, it is deliberate, and it is not part of this build. Documented here so the seam is
intentional rather than accidental.

## Error handling & edge cases

- Reference rows (`priceable: false`) never call `/ticker` or `/chart` — no error surface.
- Priceable rows reuse the existing cockpit fetch + error/empty handling in `WatchlistBar`/
  `WatchlistChartBox`; a symbol that unexpectedly fails to price degrades to a reference row
  (show tags, hide price) rather than erroring the panel.
- The endpoint is pure/static → cannot fail on DB or network; returns the same roster always.

## Testing

- **Python:** unit tests for `robotics_roster.py` (well-formed entries; every `layer`/`longevity`
  in the allowed enum; no duplicate symbols; every `earlyInsight` row has a `trigger`) and for
  the `GET /robotics-watchlist` endpoint (200, shape matches contract, read-only).
- **Frontend:** a `RoboticsPanel` test (renders grouped-by-layer; priceable rows request price,
  reference rows do not; ⭐ flag + trigger render) alongside the existing `CockpitUI.test.tsx`.
- **Guardrail test:** assert the roster/endpoint path imports nothing from `arbiter/` and the
  cockpit DB stays `mode=ro` — encodes the display-only invariant.

## File-by-file (design level)

- NEW `cockpit/api/robotics_roster.py` — static roster + accessor.
- EDIT `cockpit/api/main.py` — register `GET /robotics-watchlist`.
- EDIT `cockpit/api/contract.py` — `RoboticsRosterEntry` + response DTO.
- EDIT `cockpit/web/src/contract.ts` — mirror the DTO.
- NEW `cockpit/web/src/ui/RoboticsPanel.tsx` — the panel.
- EDIT `cockpit/web/src/ui/CockpitUI.tsx` — mount + toggle.
- NEW tests: `cockpit/api` roster + endpoint tests; `RoboticsPanel` test.
- (build-time) a throwaway generator that seeds `robotics_roster.py` from `robotics_map.json`;
  not shipped as runtime code.

## Open questions / caveats

- Exact charted-core ticker list is resolved at implementation time by validating candidates
  against `GET /ticker` (avoids baking in an unpriceable symbol).
- `robotics_map.json`'s long tail (251 of 443 records are unrefereed gap-fill) is lower
  confidence; curation draws from the **vetted** core (chokepoints, early-insight, named leaders),
  not the raw tail.
- Panel placement within the existing cockpit chrome (which is R3F 3D + HTML overlays) to be
  finalized in the plan, following the `WatchlistBar` overlay precedent.
