# Robotics Watchlist (Observational Board) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a display-only "Robotics" board to the cockpit — a curated, layer-tagged robotics universe (live price where Alpaca can reach, tagged reference rows where it can't), seeded from `docs/specs/research/robotics_map.json`, with zero change to arbiter's trading path.

**Architecture:** Three additive cockpit pieces, nothing under `arbiter/`: (1) a pure static roster module `cockpit/api/robotics_roster.py`; (2) a read-only `GET /robotics-watchlist` endpoint returning Pydantic DTOs; (3) a `RoboticsPanel.tsx` overlay grouped by layer that reuses the existing `/ticker` fetch for prices and the existing `WatchlistChartBox` (via `setActiveWatchSymbol`) for charts. It lives in the layer whose own store comment forbids reaching arbiter, so it is display-only by construction.

**Tech Stack:** Python 3.14 / FastAPI / Pydantic v2 (cockpit API); React 18 / TypeScript / Zustand / Vite / Vitest (cockpit web). Tests: `pytest` (API), `vitest` (web).

## Global Constraints

- Cockpit is strictly READ-ONLY vs the trading system; the API DB is opened `mode=ro`. The roster endpoint must NOT open the DB at all.
- `cockpit/api/robotics_roster.py` MUST import nothing from `arbiter` (enforced by a test) — it is pure data.
- `contract.py` (Pydantic) and `contract.ts` (TypeScript) are a FROZEN mirrored pair and MUST stay in sync.
- Do NOT modify `sectors.py`, `_DEFAULT_WATCHLIST`, the engine, or ingest. Engine-wiring is explicitly out of scope (see spec §7).
- Layer enum (exact): `compute | brain | components | integrator | deployment`.
- Longevity enum (exact): `chokepoint | durable | commodity | hype-risk | unclear`.
- Run commands from the worktree root `/Users/jonathanmorris/poly_bot/.worktrees/robotics-watchlist`. Python: `/Users/jonathanmorris/poly_bot/arbiter/.venv/bin/python -m pytest cockpit/api/... `. Web: `cd cockpit/web && npm test`.
- Every code commit stages only robotics files; end messages with the standard Co-Authored-By / Claude-Session trailers.

---

### Task 1: Contract DTOs (Python + TypeScript mirror)

**Files:**
- Modify: `cockpit/api/contract.py` (append new section)
- Modify: `cockpit/web/src/contract.ts` (append mirror)
- Test: `cockpit/api/test_robotics_watchlist.py` (schema tests)

**Interfaces:**
- Produces (Python): `RoboticsRosterEntry`, `RoboticsWatchlist` Pydantic models.
- Produces (TS): `RoboticsLayer`, `RoboticsLongevity`, `RoboticsRosterEntry`, `RoboticsWatchlist` types.

- [ ] **Step 1: Write the failing schema test**

Create `cockpit/api/test_robotics_watchlist.py`:
```python
"""Tests for the display-only robotics watchlist (roster module + endpoint)."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ARBITER_ROOT = _REPO_ROOT / "arbiter"
for _p in (_REPO_ROOT, _ARBITER_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


class TestRosterEntrySchema:
    def test_minimal_entry(self):
        from cockpit.api.contract import RoboticsRosterEntry
        e = RoboticsRosterEntry(
            symbol="NVDA", company="Nvidia", layer="compute",
            longevity="chokepoint", priceable=True,
        )
        assert e.symbol == "NVDA"
        assert e.form_factors == []
        assert e.early_insight is False
        assert e.trigger is None

    def test_full_entry(self):
        from cockpit.api.contract import RoboticsRosterEntry
        e = RoboticsRosterEntry(
            symbol="6324.T", company="Harmonic Drive Systems", layer="components",
            form_factors=["humanoid", "industrial"], longevity="chokepoint",
            early_insight=True, trigger="Optimus mass-production ramp confirmations",
            priceable=False, region="Japan", note="strain-wave reducer near-monopoly",
        )
        assert e.priceable is False
        assert "humanoid" in e.form_factors

    def test_watchlist_wraps_entries(self):
        from cockpit.api.contract import RoboticsRosterEntry, RoboticsWatchlist
        wl = RoboticsWatchlist(
            generated="2026-07-13",
            entries=[RoboticsRosterEntry(symbol="NVDA", company="Nvidia",
                                         layer="compute", longevity="chokepoint", priceable=True)],
        )
        assert wl.generated == "2026-07-13"
        assert len(wl.entries) == 1
```

- [ ] **Step 2: Run it, verify it fails**

Run: `/Users/jonathanmorris/poly_bot/arbiter/.venv/bin/python -m pytest cockpit/api/test_robotics_watchlist.py::TestRosterEntrySchema -q`
Expected: FAIL — `ImportError: cannot import name 'RoboticsRosterEntry'`.

- [ ] **Step 3: Add the Python DTOs**

Append to `cockpit/api/contract.py`:
```python
# --- Robotics watchlist (display-only; see docs/specs/2026-07-13-robotics-watchlist-design.md) --
RoboticsLayer = Literal["compute", "brain", "components", "integrator", "deployment"]
RoboticsLongevity = Literal["chokepoint", "durable", "commodity", "hype-risk", "unclear"]


class RoboticsRosterEntry(BaseModel):
    """One curated robotics-universe row. DISPLAY-ONLY — never trade-eligible."""
    symbol: str                              # display ticker (home-exchange for reference rows)
    company: str
    layer: RoboticsLayer
    longevity: RoboticsLongevity
    priceable: bool                          # True → live price via /ticker; False → reference row
    form_factors: list[str] = []
    early_insight: bool = False              # ⭐ from robotics_map.json earlyInsightCandidates
    trigger: str | None = None               # "trigger to watch" text for early-insight rows
    region: str | None = None
    note: str | None = None


class RoboticsWatchlist(BaseModel):
    """The full curated roster, served at GET /robotics-watchlist (static, read-only)."""
    generated: str
    entries: list[RoboticsRosterEntry] = []
```

- [ ] **Step 4: Run it, verify it passes**

Run: `/Users/jonathanmorris/poly_bot/arbiter/.venv/bin/python -m pytest cockpit/api/test_robotics_watchlist.py::TestRosterEntrySchema -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Add the TypeScript mirror**

Append to `cockpit/web/src/contract.ts`:
```typescript
// --- Robotics watchlist (mirror of RoboticsRosterEntry/RoboticsWatchlist in contract.py) ------
export type RoboticsLayer = "compute" | "brain" | "components" | "integrator" | "deployment";
export type RoboticsLongevity = "chokepoint" | "durable" | "commodity" | "hype-risk" | "unclear";

export interface RoboticsRosterEntry {
  symbol: string;
  company: string;
  layer: RoboticsLayer;
  longevity: RoboticsLongevity;
  priceable: boolean;
  form_factors: string[];
  early_insight: boolean;
  trigger: string | null;
  region: string | null;
  note: string | null;
}

export interface RoboticsWatchlist {
  generated: string;
  entries: RoboticsRosterEntry[];
}
```

- [ ] **Step 6: Commit**

```bash
git add cockpit/api/contract.py cockpit/web/src/contract.ts cockpit/api/test_robotics_watchlist.py
git commit  # feat(cockpit): robotics watchlist DTOs (py + ts mirror)
```

---

### Task 2: Static roster module + curation

**Files:**
- Create: `cockpit/api/robotics_roster.py`
- Test: `cockpit/api/test_robotics_watchlist.py` (append `TestRosterData`, `TestRosterPurity`)
- Read (source data): `docs/specs/research/robotics_map.json`

**Interfaces:**
- Produces: `robotics_roster() -> list[dict]` (each dict matches `RoboticsRosterEntry` fields); `GENERATED: str`.

- [ ] **Step 1: Write the failing data-hygiene + purity tests**

Append to `cockpit/api/test_robotics_watchlist.py`:
```python
class TestRosterData:
    def test_roster_nonempty_and_validates(self):
        from cockpit.api.contract import RoboticsRosterEntry
        from cockpit.api.robotics_roster import robotics_roster
        rows = robotics_roster()
        assert len(rows) >= 25
        for r in rows:              # each row validates against the frozen DTO
            RoboticsRosterEntry(**r)

    def test_no_duplicate_symbols(self):
        from cockpit.api.robotics_roster import robotics_roster
        syms = [r["symbol"] for r in robotics_roster()]
        assert len(syms) == len(set(syms)), "duplicate symbols in roster"

    def test_every_layer_represented(self):
        from cockpit.api.robotics_roster import robotics_roster
        layers = {r["layer"] for r in robotics_roster()}
        assert layers == {"compute", "brain", "components", "integrator", "deployment"}

    def test_early_insight_rows_have_trigger(self):
        from cockpit.api.robotics_roster import robotics_roster
        for r in robotics_roster():
            if r.get("early_insight"):
                assert r.get("trigger"), f"{r['symbol']} early_insight without trigger"

    def test_has_both_priceable_and_reference_rows(self):
        from cockpit.api.robotics_roster import robotics_roster
        rows = robotics_roster()
        assert any(r["priceable"] for r in rows)        # US-listed charted core
        assert any(not r["priceable"] for r in rows)    # foreign/private reference rows


class TestRosterPurity:
    def test_module_imports_nothing_from_arbiter(self):
        """The display-only invariant: roster code cannot reach the trading system."""
        import ast
        from pathlib import Path
        src = Path(__file__).resolve().parent / "robotics_roster.py"
        tree = ast.parse(src.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert not a.name.startswith("arbiter"), f"imports {a.name}"
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("arbiter"), f"imports from {node.module}"
```

- [ ] **Step 2: Run it, verify it fails**

Run: `/Users/jonathanmorris/poly_bot/arbiter/.venv/bin/python -m pytest cockpit/api/test_robotics_watchlist.py::TestRosterData -q`
Expected: FAIL — `ModuleNotFoundError: cockpit.api.robotics_roster`.

- [ ] **Step 3: Generate the curated seed, then write the module**

First generate a candidate seed from the map (throwaway, run once):
```bash
/Users/jonathanmorris/poly_bot/arbiter/.venv/bin/python - <<'PY'
import json
m = json.load(open("docs/specs/research/robotics_map.json"))
early = {c["company"] for c in m.get("earlyInsightCandidates", [])}
trig = {c["company"]: c.get("watchFor") for c in m.get("earlyInsightCandidates", [])}
# candidates = named chokepoints + early-insight + companies with a ticker, deduped
for c in m["companies"]:
    if c.get("ticker") or c["name"] in early:
        print(c["layer"], "|", c.get("visibility"), "|", c.get("ticker"), "|", c["name"])
PY
```
Curation rules (apply to the printout): keep ~30-40 rows total; every layer present; `priceable=True` only for symbols Alpaca can resolve (US primary listing or US ADR — validate each in Step 3b); foreign-only and private names become `priceable=False` reference rows; set `early_insight`/`trigger` from `earlyInsightCandidates`; longevity from `longevityVerdicts`/`chokepoints` where available else `unclear`.

Create `cockpit/api/robotics_roster.py`:
```python
"""Curated, DISPLAY-ONLY robotics universe for the cockpit Robotics board.

Pure static data — NO imports from ``arbiter``, NO I/O, NO network (enforced by
``test_robotics_watchlist.py::TestRosterPurity``).  Seeded from
``docs/specs/research/robotics_map.json`` and hand-curated.  ``priceable=True``
rows are US-listed / ADR symbols the cockpit ``/ticker`` + ``/chart`` endpoints
can price; ``priceable=False`` rows are foreign-listed or private chokepoints
kept visible as tagged reference rows.

This module CANNOT make any symbol trade-eligible; it never touches
``sectors.py`` / ``_DEFAULT_WATCHLIST`` (see docs/specs/2026-07-13-robotics-watchlist-design.md §7).
"""
from __future__ import annotations

GENERATED = "2026-07-13"

# symbol, company, layer, longevity, priceable, form_factors, early_insight, trigger, region, note
_ROSTER: tuple[dict, ...] = (
    # --- compute ---
    {"symbol": "NVDA", "company": "Nvidia", "layer": "compute", "longevity": "chokepoint",
     "priceable": True, "form_factors": ["all"], "region": "US",
     "note": "Jetson Thor is the default robot-brain socket; compute+model+sim flywheel"},
    {"symbol": "TSM", "company": "TSMC", "layer": "compute", "longevity": "chokepoint",
     "priceable": True, "form_factors": ["all"], "region": "Taiwan (US ADR)",
     "note": "fab + CoWoS packaging — sold out through 2026; the sector's toll booth"},
    {"symbol": "ARM", "company": "Arm Holdings", "layer": "compute", "longevity": "durable",
     "priceable": True, "form_factors": ["all"], "region": "UK (US ADR)",
     "note": "CPU IP inside most robot SoCs; stood up a Physical AI unit"},
    {"symbol": "QCOM", "company": "Qualcomm", "layer": "compute", "longevity": "durable",
     "priceable": True, "form_factors": ["mobility", "humanoid"], "region": "US",
     "note": "Snapdragon Ride (AV) + edge compute challenger"},
    {"symbol": "688256.SS", "company": "Cambricon", "layer": "compute", "longevity": "unclear",
     "priceable": False, "form_factors": ["all"], "region": "China",
     "note": "domestic-GPU 'Nvidia substitute'; SMIC-node-constrained, state-backed"},
    # ... (full curated set produced in Step 3, spanning every layer) ...
)


def robotics_roster() -> list[dict]:
    """Return the curated roster as a list of plain dicts (one per RoboticsRosterEntry)."""
    return [dict(r) for r in _ROSTER]
```
**Step 3b (priceable validation):** with the cockpit API importable, for each `priceable=True` candidate run `build_ticker_detail(sym)` (offline is fine — you're checking the symbol is a plausible US listing, cross-check against a known-listings list); flip any that are clearly foreign-primary to `priceable=False`. Fill `_ROSTER` with the full curated set (≈30-40 rows) covering all five layers, ⭐ the `earlyInsightCandidates`, and the chokepoints as reference rows.

- [ ] **Step 4: Run it, verify it passes**

Run: `/Users/jonathanmorris/poly_bot/arbiter/.venv/bin/python -m pytest cockpit/api/test_robotics_watchlist.py -q`
Expected: PASS (schema + data + purity).

- [ ] **Step 5: Commit**

```bash
git add cockpit/api/robotics_roster.py cockpit/api/test_robotics_watchlist.py
git commit  # feat(cockpit): curated robotics roster (static, display-only)
```

---

### Task 3: Read-only `GET /robotics-watchlist` endpoint

**Files:**
- Modify: `cockpit/api/main.py`
- Test: `cockpit/api/test_robotics_watchlist.py` (append `TestRoboticsRoute`)

**Interfaces:**
- Consumes: `RoboticsWatchlist`, `RoboticsRosterEntry` (Task 1); `robotics_roster`, `GENERATED` (Task 2).
- Produces: HTTP `GET /robotics-watchlist -> RoboticsWatchlist`.

- [ ] **Step 1: Write the failing route test**

Append to `cockpit/api/test_robotics_watchlist.py` (reuse the `client` fixture pattern from `test_ticker.py` — copy the `fixture_db` + `client` fixtures into this file):
```python
class TestRoboticsRoute:
    def test_returns_200_and_shape(self, client):
        r = client.get("/robotics-watchlist")
        assert r.status_code == 200
        data = r.json()
        assert data["generated"]
        assert isinstance(data["entries"], list) and len(data["entries"]) >= 25
        e = data["entries"][0]
        for field in ("symbol", "company", "layer", "longevity", "priceable",
                      "form_factors", "early_insight", "trigger", "region", "note"):
            assert field in e, f"missing {field}"

    def test_is_static_no_db(self, client):
        """Endpoint must not depend on the DB — patch connect to explode; still 200."""
        from unittest.mock import patch
        with patch("cockpit.api.main.connect", side_effect=AssertionError("DB touched")):
            r = client.get("/robotics-watchlist")
        assert r.status_code == 200
```

- [ ] **Step 2: Run it, verify it fails**

Run: `/Users/jonathanmorris/poly_bot/arbiter/.venv/bin/python -m pytest cockpit/api/test_robotics_watchlist.py::TestRoboticsRoute -q`
Expected: FAIL — 404 (route not registered).

- [ ] **Step 3: Register the endpoint**

In `cockpit/api/main.py`, add to the contract import (line 15) `RoboticsWatchlist`, add `from .robotics_roster import GENERATED as ROBOTICS_GENERATED, robotics_roster`, and add the route:
```python
@app.get("/robotics-watchlist", response_model=RoboticsWatchlist)
def robotics_watchlist() -> RoboticsWatchlist:
    """Curated display-only robotics universe (static; never touches the DB or arbiter)."""
    return RoboticsWatchlist(generated=ROBOTICS_GENERATED, entries=robotics_roster())
```

- [ ] **Step 4: Run it, verify it passes**

Run: `/Users/jonathanmorris/poly_bot/arbiter/.venv/bin/python -m pytest cockpit/api/test_robotics_watchlist.py -q`
Expected: PASS (all classes).

- [ ] **Step 5: Commit**

```bash
git add cockpit/api/main.py cockpit/api/test_robotics_watchlist.py
git commit  # feat(cockpit): GET /robotics-watchlist read-only endpoint
```

---

### Task 4: Frontend client fn + RoboticsPanel component

**Files:**
- Modify: `cockpit/web/src/api.ts`
- Create: `cockpit/web/src/ui/RoboticsPanel.tsx`
- Test: `cockpit/web/src/ui/__tests__/RoboticsPanel.test.tsx`

**Interfaces:**
- Consumes: `RoboticsWatchlist` type (Task 1), `useWatchlistStore().setActiveWatchSymbol` (existing), `fetchTickerDetail` (existing).
- Produces: `fetchRoboticsWatchlist()`; `<RoboticsPanel inspectionOpen={boolean} />`.

- [ ] **Step 1: Add the client fn** (fold into this task)

In `cockpit/web/src/api.ts` add `RoboticsWatchlist` to the type import and:
```typescript
export const fetchRoboticsWatchlist = () => get<RoboticsWatchlist>("/robotics-watchlist");
```

- [ ] **Step 2: Write the failing component test**

Create `cockpit/web/src/ui/__tests__/RoboticsPanel.test.tsx` (mirror `WatchlistBar.test.tsx` harness: react-dom `createRoot` + `React.act`, `vi.mock("../../api")`, `matchMedia` shim):
```tsx
import React from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { createRoot, type Root } from "react-dom/client";
import { RoboticsPanel } from "../RoboticsPanel";
import { useWatchlistStore } from "../watchlistStore";

vi.mock("../../api", () => ({
  fetchRoboticsWatchlist: vi.fn(),
  fetchTickerDetail: vi.fn(),
  fetchChart: vi.fn(),
}));
import { fetchRoboticsWatchlist, fetchTickerDetail } from "../../api";

beforeAll(() => {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((q: string) => ({
      matches: false, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    })),
  });
});

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  useWatchlistStore.setState({ activeWatchSymbol: null });
  vi.mocked(fetchRoboticsWatchlist).mockReset();
  vi.mocked(fetchTickerDetail).mockReset();
});
afterEach(async () => { await React.act(async () => { root.unmount(); }); container.remove(); });

async function render(ui: React.ReactElement) { await React.act(async () => { root.render(ui); }); }

const ROSTER = {
  generated: "2026-07-13",
  entries: [
    { symbol: "NVDA", company: "Nvidia", layer: "compute", longevity: "chokepoint",
      priceable: true, form_factors: ["all"], early_insight: false, trigger: null,
      region: "US", note: "socket" },
    { symbol: "6324.T", company: "Harmonic Drive Systems", layer: "components",
      longevity: "chokepoint", priceable: false, form_factors: ["humanoid"],
      early_insight: true, trigger: "Optimus ramp", region: "Japan", note: "reducer" },
  ],
};

describe("RoboticsPanel", () => {
  it("collapsed by default (icon only)", async () => {
    vi.mocked(fetchRoboticsWatchlist).mockResolvedValue(ROSTER as never);
    await render(<RoboticsPanel inspectionOpen={false} />);
    expect(container.querySelector("[data-testid='robotics-icon-btn']")).not.toBeNull();
    expect(container.querySelector("[data-testid='robotics-panel-expanded']")).toBeNull();
  });

  it("expands and groups rows by layer after fetch", async () => {
    vi.mocked(fetchRoboticsWatchlist).mockResolvedValue(ROSTER as never);
    await render(<RoboticsPanel inspectionOpen={false} />);
    await React.act(async () => {
      (container.querySelector("[data-testid='robotics-icon-btn']") as HTMLButtonElement).click();
    });
    await React.act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const panel = container.querySelector("[data-testid='robotics-panel-expanded']");
    expect(panel).not.toBeNull();
    expect(panel?.textContent).toContain("Nvidia");
    expect(panel?.textContent).toContain("Harmonic Drive Systems");
    expect(panel?.textContent).toContain("COMPUTE");     // layer group header
    expect(panel?.textContent).toContain("COMPONENTS");
  });

  it("priceable row click sets activeWatchSymbol; reference row has no chart button", async () => {
    vi.mocked(fetchRoboticsWatchlist).mockResolvedValue(ROSTER as never);
    await render(<RoboticsPanel inspectionOpen={false} />);
    await React.act(async () => {
      (container.querySelector("[data-testid='robotics-icon-btn']") as HTMLButtonElement).click();
    });
    await React.act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const nvda = container.querySelector("[data-testid='robotics-row-NVDA'] button");
    expect(nvda).not.toBeNull();
    await React.act(async () => { (nvda as HTMLButtonElement).click(); });
    expect(useWatchlistStore.getState().activeWatchSymbol).toBe("NVDA");
    // reference row has no chart-opening button
    expect(container.querySelector("[data-testid='robotics-row-6324.T'] button")).toBeNull();
  });

  it("early-insight row shows the star and trigger", async () => {
    vi.mocked(fetchRoboticsWatchlist).mockResolvedValue(ROSTER as never);
    await render(<RoboticsPanel inspectionOpen={false} />);
    await React.act(async () => {
      (container.querySelector("[data-testid='robotics-icon-btn']") as HTMLButtonElement).click();
    });
    await React.act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const row = container.querySelector("[data-testid='robotics-row-6324.T']");
    expect(row?.textContent).toContain("★");
    expect(row?.textContent).toContain("Optimus ramp");
  });
});
```

- [ ] **Step 3: Run it, verify it fails**

Run: `cd cockpit/web && npm test -- RoboticsPanel`
Expected: FAIL — cannot resolve `../RoboticsPanel`.

- [ ] **Step 4: Implement `RoboticsPanel.tsx`**

Create `cockpit/web/src/ui/RoboticsPanel.tsx` — collapsed 🤖 icon ↔ expanded panel (mirror `WatchlistBar` tokens/occlusion pattern: absolute top:16 right:56 to sit left of the watchlist icon; `inspectionOpen` + `activeWatchSymbol` auto-collapse). On expand, `fetchRoboticsWatchlist()` once; group `entries` by `layer` in fixed order `["compute","brain","components","integrator","deployment"]`; render a group header (upper-cased layer) then one row per entry with `data-testid={`robotics-row-${symbol}`}`. A **priceable** row's symbol is a `<button>` calling `setActiveWatchSymbol(symbol)`; a **reference** row renders the symbol as plain text (no button). Early-insight rows prefix `★` and show the `trigger` in muted text. Longevity renders as a small colored badge. Full implementation follows the token/markup conventions in `WatchlistBar.tsx` verbatim (colors `T`, `motion`/`prefersReducedMotion`, click-outside + Esc collapse).

- [ ] **Step 5: Run it, verify it passes**

Run: `cd cockpit/web && npm test -- RoboticsPanel`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add cockpit/web/src/api.ts cockpit/web/src/ui/RoboticsPanel.tsx cockpit/web/src/ui/__tests__/RoboticsPanel.test.tsx
git commit  # feat(cockpit): RoboticsPanel overlay (grouped-by-layer, display-only)
```

---

### Task 5: Mount in CockpitUI + full green

**Files:**
- Modify: `cockpit/web/src/ui/CockpitUI.tsx` (import + mount next to `WatchlistBar`, ~line 1483)

**Interfaces:**
- Consumes: `<RoboticsPanel inspectionOpen={boolean} />` (Task 4).

- [ ] **Step 1: Add the import** — with the other `ui/` imports at the top of `CockpitUI.tsx`:
```tsx
import { RoboticsPanel } from "./RoboticsPanel";
```

- [ ] **Step 2: Mount it** next to the watchlist (after line 1484 `<WatchlistChartBox />`):
```tsx
      <RoboticsPanel inspectionOpen={!!effectiveSelectedId} />
```

- [ ] **Step 3: Typecheck + full frontend suite**

Run: `cd cockpit/web && npx tsc -b && npm test`
Expected: tsc clean; all suites pass (existing 138 + new RoboticsPanel tests). `CockpitUI.test.tsx` must still pass (RoboticsPanel fetch is mocked/again inert under its api mock — verify no new network call breaks it; if `CockpitUI.test.tsx`'s `vi.mock("../../api")` lacks `fetchRoboticsWatchlist`, add it to that mock).

- [ ] **Step 4: Full API suite (no regressions)**

Run: `/Users/jonathanmorris/poly_bot/arbiter/.venv/bin/python -m pytest cockpit/api -q`
Expected: new robotics tests pass; the 2 pre-existing unrelated failures (`test_opt_layer_summary_values`, `test_state_figure_nodes_lit`) remain the only failures — no NEW failures.

- [ ] **Step 5: Commit**

```bash
git add cockpit/web/src/ui/CockpitUI.tsx
git commit  # feat(cockpit): mount RoboticsPanel in the cockpit overlay
```

---

## Self-Review

**Spec coverage:** roster module (§Architecture-1 → Task 2), read-only endpoint (§Architecture-2 → Task 3), panel grouped by layer (§Architecture-3 → Tasks 4-5), data model (§Data model → Task 1), universe curated-core-plus-reference (§5 → Task 2 curation rules + `priceable`), display-only guardrail (§Testing → Task 2 `TestRosterPurity` + Task 3 `test_is_static_no_db`), reuse of `/ticker`+`/chart` (Task 4 via `setActiveWatchSymbol`), engine-wiring out of scope (Global Constraints — untouched). No gaps.

**Placeholder scan:** the only deferred item is the exact `_ROSTER` row set, which Task 2 Step 3 generates concretely from `robotics_map.json` with explicit curation rules + validation — not a vague placeholder. All test/impl code is complete.

**Type consistency:** `RoboticsRosterEntry`/`RoboticsWatchlist` field names identical across contract.py, contract.ts, roster dicts, and tests (`symbol, company, layer, longevity, priceable, form_factors, early_insight, trigger, region, note`). Endpoint path `/robotics-watchlist` consistent in main.py, api.ts, and tests. Store action `setActiveWatchSymbol` matches watchlistStore.ts.
