# Cockpit Watchlist + Live Charts — Design & Build Plan

**Date:** 2026-06-29
**Status:** Design approved (decisions locked); ready for implementation-plan → build
**Scope:** `cockpit/` only — strictly read-only w.r.t. trading
**Source:** synthesized from 3 parallel sonnet brainstorms (full docs in scratchpad: `watchlist-data.md`, `watchlist-ux.md`, `watchlist-arch.md`)

## 1. Summary

Add a **personal watchlist with live charts** to the Arbiter Cockpit. A collapsed
search **icon** (top-right) expands into a search bar; the user adds tickers to a
personal watchlist and clicks a saved ticker to open a **chart box** showing a **live
chart (incl. pre-market & post-market)** plus **5-day, 1-month, 3-month, 6-month**
charts. The watchlist is per-browser (localStorage), entirely separate from arbiter's
trading watchlist, and never touches the engine.

## 2. Locked decisions (from the user)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Chart-box layout | **Big primary chart (tabbed Live/5D/1M/3M/6M) + always-visible thumbnail strip** of the 4 historical ranges |
| 2 | Chart style | **Candlesticks** (all ranges) |
| 3 | Extended hours | **On by default** for the Live chart (shaded pre/post bands + a toggle to regular-hours-only) |
| 4 | Data feed | **SIP — confirmed available** on this Alpaca account (probe returned a real 04:00 ET pre-market bar) |

## 3. Reconciled architecture decisions (controller calls, where the 3 brainstorms differed)

- **Endpoint + contract → adopt the DATA agent's `ChartSeries` design** (richer; the
  arch agent's `BarSeries` omitted the `live`/pre-post range the user explicitly wants):
  - `GET /chart/{symbol}?range=live|5d|1m|3m|6m` → `ChartSeries`. New module
    `cockpit/api/chart.py`, wired in `cockpit/api/main.py`, mirroring `cockpit/api/ticker.py`'s
    fail-closed pattern (never raises, never 404, empty candles on Alpaca failure).
  - Response (in `cockpit/api/contract.py` + mirrored in `cockpit/web/src/contract.ts`):
    ```python
    class Candle(BaseModel):
        t: str; o: float; h: float; l: float; c: float; v: float
        session: str          # "pre" | "regular" | "post"
    class ChartSeries(BaseModel):
        symbol: str
        range: str            # live | 5d | 1m | 3m | 6m
        candles: list[Candle] = []
        extended_available: bool = False   # True when pre/post bars present
        as_of: str
        alpaca_ok: bool = False
    ```
  - **Use `cfg.alpaca_data_base_url` (data.alpaca.markets), NOT `ex._base()`** (which is the
    trading API). Reuse `ex.http_get(url, ex._headers())` exactly as `ticker.py` does.
  - Range→Alpaca mapping: `live`=5Min `extended_hours=true` feed=sip (start 04:00 ET→now);
    `5d`=15Min; `1m/3m/6m`=1Day `adjustment=all`. Backend classifies each bar's `session`
    from its UTC timestamp (pre 08:00–13:30Z / regular 13:30–20:00Z / post 20:00–24:00Z,
    DST-aware via zoneinfo).
  - **Set `ALPACA_DATA_FEED=sip` in arbiter `.env`** (the existing default is `iex`); the
    chart endpoint requests `feed=sip` for the live range so pre/post bars actually return.
  - In-process TTL cache keyed by `(symbol, range)`: live 60s, 5d 3m, 1m/3m/6m 5–10m.
    Single-process uvicorn → a module-level dict is sufficient.
- **Charting library → `lightweight-charts`** (TradingView, ~50KB, Apache-2.0). Overrides the
  arch agent's SVG/recharts suggestion: Canvas2D and R3F's WebGL are independent contexts,
  chart canvases mount only when a box is open (not per constellation frame), and only
  lightweight-charts has native candlesticks + a gap-aware time axis (recharts has neither
  and is ~8× the bundle). Mounted via a thin `useChart()` `useEffect` hook (~40 lines).
- **Placement → top-RIGHT** (UX agent's grounded analysis), not top-center. Collapsed 32×32
  icon at `top:16, right:16` (the unused 32px gap beside the InspectionPanel's `right:48`);
  expands leftward to ~380px; auto-collapses when the InspectionPanel opens (same
  `inspectionOpen` occlusion guard `OptionsPanel` uses). The chart box opens at top-right; one
  right-side overlay at a time (opening a constellation node closes/yields the chart box).
- **Persistence → Zustand `persist` (localStorage)**, both agents agree. `zustand@^4.5.5` is
  already a dependency; the existing `cockpit/web/src/ui/store.ts` is the home. `partialize`
  so ONLY `watchlistSymbols` persists (never the constellation nav state). Key
  `"cockpit-watchlist"`.

## 4. Separation-from-trading guarantee

Arbiter's *trading* watchlist is `_DEFAULT_WATCHLIST` in `arbiter/arbiter/ingest/runner.py`
(a Python constant, never in SQLite, read only during ingest). The personal UI watchlist
lives **only** in browser localStorage; its only API calls are `GET /ticker/{symbol}`
(validate/name) and the new `GET /chart/{symbol}` (display). The cockpit API is GET-only
(`allow_methods=["GET"]`) and opens the DB `mode=ro`, so nothing here can write trading
state. The store slice carries a comment stating it must never be forwarded to ingest/engine.

## 5. Component tree (frontend, all new files under `cockpit/web/src/ui/`)

- `WatchlistBar.tsx` — top-level container (collapsed / expanded / chart-open); owns
  `top:16 right:16 zIndex:15`; consumes `inspectionOpen` + the watchlist store.
  - `TickerSearchInput` — debounced input; validates via `GET /ticker/{symbol}` (name!=null →
    known; null → soft "add anyway?").
  - `AutocompleteMenu` / `TickerSuggestion`
  - `SavedTickerList` → `SavedTickerChip` (click = open chart, × = remove)
- `WatchlistChartBox.tsx` — floating overlay (`top:16 right:16`, `width min(500px, 100vw-96px)`);
  rendered independently so the bar can collapse without killing the chart.
  - `ChartTimeframeTabs` (Live●/5D/1M/3M/6M + pre/post toggle)
  - `TickerChart` — the `lightweight-charts` wrapper (`useChart(symbol, range, prePost)`), `loading`/`error` states
  - `ChartThumbnailStrip` → `ChartThumbnail` (4 sparklines; click promotes to primary)

All reuse the existing theme tokens (`theme/theme.ts`: `T.panelBg`, `T.panelBorder`, `T.radius`,
`T.fontSans/fontMono`, `T.accent #7c83ff`, ok/bad colors, `motion.*`, `prefersReducedMotion()`)
and the `Badge` aesthetic. Full ARIA labels, keyboard flow, and ASCII mockups are in
`scratchpad/watchlist-ux.md`. Both components are imported into `CockpitUI.tsx` (already ~1500
lines — no inline additions), mounted alongside `<HUD>`/`<PositionsPanel>` with
`inspectionOpen={!!effectiveSelectedId}`.

## 6. Phased build plan (TDD; repo patterns: `pytest` offline fixtures for `cockpit/api`, `vitest` for `cockpit/web`)

**Phase 0 — Contract + feed config (coordination gate).**
Add `Candle` + `ChartSeries` to `contract.py` and `contract.ts` (atomic, field-name-frozen);
add `fetchChart(symbol, range)` to `cockpit/web/src/api.ts`; set `ALPACA_DATA_FEED=sip` in `.env`.
Test: `tsc --noEmit` green; `pytest cockpit/api/` green (no regressions).

**Phase 1 — Backend `/chart/{symbol}` endpoint.**
`cockpit/api/chart.py::build_chart_series(symbol, range)` (Alpaca bars per range, session
classification, TTL cache, fail-closed → empty `alpaca_ok=False`); wire `@app.get("/chart/{symbol}")`.
Test: `cockpit/api/test_chart.py` (offline, monkeypatched Alpaca): correct `ChartSeries`
shape per range; session tagging (pre/regular/post) from timestamps; `extended_available`
True only when pre/post bars present; graceful-empty on failure; symbol upper-cased; read-only
invariant holds. Checkpoint: `curl localhost:8910/chart/AAPL?range=live` parses.

**Phase 2 — Watchlist store slice.**
Add `watchlistSymbols` + `add/remove/has`, `activeWatchSymbol`, `activeRange` to `store.ts`
wrapped in `persist` with `partialize` (only symbols). Client-only `WatchlistItem` type in a
new `watchlist-types.ts` (not the frozen contract). Comment: never forward to trading.
Test: `src/ui/__tests__/watchlist.store.test.ts` — add/remove/dedupe; persist round-trip;
nav state (hoveredId) NOT persisted.

**Phase 3 — Search bar UI (`WatchlistBar`).**
Collapsed icon → expand → search/validate/add → saved chips; auto-collapse on `inspectionOpen`;
keyboard + ARIA per UX doc. Test: `vitest` — collapsed by default; expands on click; valid
symbol → chip (uppercased); invalid (name null / fetch error) → inline soft-unknown, no silent add.

**Phase 4 — Chart box (`WatchlistChartBox` + `lightweight-charts`).**
`npm i lightweight-charts`; big candlestick chart + tabs + thumbnail strip + pre/post toggle;
`fetchChart` on open/tab-change with a per-`(symbol,range)` `useRef` 60s cache; extended-hours
shaded bands; `extended_available=false` → "extended hours unavailable" note. Test: `vitest`
with mocked `fetchChart` — loading/error/data states; tab switch refetches; thumbnail promote;
close clears `activeWatchSymbol`. Checkpoint: click a chip → real candles render over the R3F canvas.

**Phase 5 — Polish.**
Pre/post band styling + toggle default-on; rate-limit/debounce guards; reduced-motion; final
full-suite run (`pytest cockpit/api -q`, `vitest run`, `tsc -b`) + manual smoke in the live cockpit.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Alpaca rate limits (multi-range fetching) | Per-`(symbol,range)` client cache (60s) + backend TTL + 400ms search debounce; 200 req/min is ample for a personal watchlist |
| Data base URL confusion | Use `cfg.alpaca_data_base_url`, never `ex._base()` (the trading URL) — `ticker.py` is the reference |
| 5-min bar lag on "live" | Acceptable for a decision-support dashboard; document it (true tick-level would need the WS stream) |
| `CockpitUI.tsx` bloat | New components in their own files, imported (precedent: Options/Positions panels) |
| Chart box vs InspectionPanel both top-right | One right-side overlay at a time; opening a node yields the chart box (or vice-versa) — finalize in Phase 4 |

## 8. Out of scope (YAGNI)

WebSocket/tick streaming; cross-device watchlist sync (localStorage is per-browser by design);
adding any write endpoint to the cockpit API; multi-worker cache (single-process deployment).
