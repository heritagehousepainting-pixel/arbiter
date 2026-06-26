# Cockpit — click-to-expand ticker detail (design)

**Date:** 2026-06-26 · **Status:** APPROVED (design) → next: implementation plan

## Goal
In the cockpit's **Open Positions** panel, clicking a ticker (MS, LULU, AMZN, …)
expands that row to reveal the *stock's* info: full company name, today's price +
daily % up/down, and the stock's return over the past ~month. Works for any ticker
that appears (data-driven, not hardcoded).

## Hard constraints
- **Strictly READ-ONLY** (cockpit never writes the trading system; Alpaca read-only).
- **Additive** — existing Open Positions columns/rows/behavior unchanged.
- **Frozen contract** — DTOs in `api/contract.py` mirrored in `web/src/contract.ts`.

## UX
- The **ticker cell** is the only click target; clicking toggles an inline accordion
  expansion of that row (a `▾`/`▸` chevron marks state). **One** ticker open at a time;
  clicking another collapses the first. Click again to collapse.
- Detail content (the stock, distinct from the row's position ROI/P&L):
  - **Company name** — full (e.g. "Morgan Stanley").
  - **Today** — current price + **day %** (▲ green / ▼ red).
  - **1-Month** — stock price **return over the past ~month** (▲ green / ▼ red).
- **States:** while the detail fetch is in flight → "loading…"; any field Alpaca can't
  supply → "—" (graceful, never an error wall).

## Data sources (all reachable read-only)
- **Day %** is FREE — Alpaca's raw `/v2/positions` payload already carries
  `change_today` per held position (just not surfaced). Surface it on the row's data.
- **Current price** — already on `OpenPosition` (`current_price`).
- **Company name** — `GET {trading_base}/v2/assets/{symbol}` → `name`.
- **1-Month return** — daily bars from the Alpaca **data** API
  `GET {data_base}/v2/stocks/{symbol}/bars?timeframe=1Day&start=<~35d ago>&feed=<config feed|iex>&adjustment=all`:
  take the oldest close in the window as the ~1-month reference, compare to current
  price (or the latest bar close): `month_return_pct = (current - ref) / ref`.

## Backend
- **`OpenPosition` (contract.py + .ts):** add `day_change_pct: float | null` (fraction),
  populated from `change_today` in `positions.py` (graceful None when absent).
- **New DTO `TickerDetail`:** `{ symbol, name: str|null, month_return_pct: float|null,
  current_price: float|null, as_of }`. (Name + month are the only things not already on
  the row; `current_price` echoed for robustness.)
- **New route `GET /ticker/{symbol}` → `TickerDetail`** (read-only, **lazy** — only hit on
  expand, so unopened tickers cost nothing and the 5s poll is unaffected). New module
  `api/ticker.py::build_ticker_detail(symbol)` reusing the executor/data-API HTTP plumbing
  (mirror `positions.py`'s `build_executor(load_config())` pattern). Symbol validated/upper-cased;
  unknown symbol or unreachable Alpaca → `TickerDetail` with null fields (HTTP 200, not 404/500).
- `main.py`: register the route.

## Frontend
- **`api.ts`:** `fetchTickerDetail(symbol)` → `TickerDetail`.
- **`PositionsPanel.tsx`:** the `Row` becomes expandable — ticker cell is a button
  (keyboard-focusable, `aria-expanded`); local `openTicker` state in the panel (one open).
  On expand, call `fetchTickerDetail` once and **cache per session** (company name is static;
  month return is fine intraday). The expanded sub-row renders name / Today(price+day%) /
  1-Month, using `current_price` + `day_change_pct` from the position it already has plus
  `name` + `month_return_pct` from the detail. Colors reuse the existing `plColor` helper.

## Testing
- **Backend** (`api/test_api.py` or `api/test_ticker.py`): `/ticker/{symbol}` happy path
  (mocked assets + bars → name + month_return_pct), and the degrade paths (Alpaca down →
  null fields, HTTP 200; missing bars → null month). `day_change_pct` surfaced in `/positions`.
- **Web** (`PositionsPanel` test): clicking a ticker toggles expansion + `aria-expanded`;
  detail renders name/price/day%/month%; loading and "—" states; only one row open at a time.

## Success criteria
- Click any ticker in Open Positions → row expands with company name, today's price + day %,
  and 1-month return; click again collapses. Works for newly-appearing tickers.
- No extra load on the 5s poll (detail is lazy + cached). Read-only preserved. Existing
  Open Positions UI unchanged. tsc clean; backend + web tests green.
