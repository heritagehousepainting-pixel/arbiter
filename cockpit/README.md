# Arbiter Cockpit

A true-3D (React-Three-Fiber) **living map of the arbiter system** — data sources →
tracked figures (smart money) → council → decision core → trades → learning loop — that doubles
as a live, **strictly read-only** operations cockpit.

Design spec: `../docs/specs/2026-06-22-system-cockpit-design.md`.

## Safety
The cockpit NEVER writes to the trading system. The API opens `arbiter/data/arbiter.db` with
`mode=ro` (writes raise) and only calls read endpoints / read-only AlpacaAdapter methods. It is a
separate process; running or killing it has zero effect on trading.

## Run

API (read-only sidecar, port 8910) — uses the arbiter venv (has FastAPI):
```
cd /Users/jonathanmorris/poly_bot
arbiter/.venv/bin/uvicorn cockpit.api.main:app --port 8910 --reload
```

Web (3D UI, port 5173):
```
cd cockpit/web
npm install
npm run dev
```
Open http://localhost:5173.

## Layout
- `api/` — FastAPI sidecar. `contract.py` is the FROZEN DTO contract (mirror: `web/src/contract.ts`).
  `db.py` (read-only conn), `graph.py` (stable topology), `state.py` (live snapshot), `main.py` (routes).
- `web/` — Vite + React + TS + R3F. `src/contract.ts` (frozen types), `src/api.ts` (read client),
  `src/App.tsx` (scene).

## Build lanes (see spec §6)
1. API / data-mapping  2. Event stream (SSE)  3. Scene & rendering  4. Interaction & inspection
5. Aesthetic & motion polish. All build against the frozen contract.
