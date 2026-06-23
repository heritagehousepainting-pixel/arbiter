# Arbiter Cockpit — a living 3D map of the system (design)

**Date:** 2026-06-22  **Status:** approved in brainstorm, ready for implementation plan.

A true-3D (WebGL / React-Three-Fiber) **neural-constellation** that is simultaneously an
explanatory map of the whole arbiter system AND a live operations cockpit: the same nodes/edges
light up with real data. The spine of the piece is the thesis — **we follow named smart money
(politicians, insiders, activist funds), run their disclosed trades through our council (A1 +
MiroFish A2), and crystallize trades** — so the *people* are first-class.

## 0. Hard constraints (non-negotiable)
- **READ-ONLY.** The cockpit is a separate process that NEVER writes to `data/arbiter.db`
  or touches the trading path. It opens SQLite with `mode=ro` and only calls read endpoints.
  It is never imported by the daemon/engine. If it dies, trading is unaffected, and vice-versa.
- **No new instrumentation in the trading code.** The live event stream is derived by tailing the
  EXISTING audit/metrics JSONL + the daemon `heartbeat.json`. We do not edit engine code.
- **Offline-safe degradation.** Daemon down → "daemon offline"; Alpaca unreachable → positions
  show "stale"; empty learning tables → figure/advisor track-record renders "building…".

## 1. Architecture
Two processes:
- **`cockpit/api`** — a new read-only **FastAPI** sidecar on `:8910`. Reads: the SQLite DB
  (`mode=ro`), the daemon heartbeat JSON, and live **Alpaca** positions/account via the arbiter's
  existing `AlpacaAdapter` (read-only `get_positions`/`get_account` only). Tails the audit JSONL
  for the event stream.
- **`cockpit/web`** — a new **Vite + React + TypeScript + React-Three-Fiber** app on `:5173`.
  Builds the constellation from `/graph` once, polls `/state` every ~4s, subscribes to `/events`
  (SSE) for real-time pulses.

Data sources of truth (grounded in the real schema):
- **Figures:** `people` (n≈79: `person_id, canonical_name, source`), scored by `person_scores`
  (track record — currently EMPTY; render "building…").
- **Disclosures/trades-followed:** `filings` (`source` = form4|form13d|congress, `person_id`,
  `ticker`, `txn_type`, `shares`, `price`, `filing_ts`).
- **Advisors:** fixed set {A1.insider, A1.congress, A1.activist, A2.mirofish} (+A3 dim/"future");
  trust from `trust_weights` (has data) / `trust_advisor_scores` (empty → "building…").
- **Opinions:** `opinions` (stance_score, confidence, advisor_id, idea_id).
- **Ideas (lifecycle):** `ideas` (state FSM, ticker, thesis, horizon).
- **Orders/exec:** `orders` (side, qty, status, exits_json, idea_id). Exit vs opening via
  `exit_label_kind` presence (shared rule with the engine).
- **Positions/trades (LIVE):** **Alpaca** (`get_positions` → long/short, avg, qty; `get_account`
  → equity/P&L). NOT `sim_positions` (empty on paper).
- **Outcomes/learning:** `outcomes` (alpha_bps, binary, advisor_id, label_kind) → trust loop.
- **Infra:** heartbeat JSON (is_open, paused, open_positions, backoff), kill switch (config URL),
  breakers (`breaker_state`).

## 2. The read-only API contract (FROZEN — the agent handoff)
Pydantic DTOs in `cockpit/api/contract.py`, mirrored as TS types in `cockpit/web/src/contract.ts`.

- `GET /graph` → `{ nodes: Node[], edges: Edge[] }` (static topology / inventory).
  `Node = { id, type, label, cluster, meta }` where `type ∈ {data_source, figure, advisor,
  engine_part, idea, exec_part, trade, outcome, infra}`, `cluster ∈ {sources, figures, council,
  core, ideas, execution, market, learning, infra}`.
  `Edge = { id, source, target, kind }`, `kind ∈ {ingest, discloses, scores, fuses, decides,
  submits, holds, resolves, teaches, gates}`.
- `GET /state` → per-node dynamic values keyed by node id:
  `{ nodes: { [id]: { intensity: 0..1, status?, value?, label_extra? } }, account: {equity, daily_pl},
    health: {db, daemon, alpaca}, kill_switch: {halted}, as_of }`.
- `GET /events` (SSE) → `data: { ts, kind, node_ids[], payload }` per discrete event
  (kinds: fill, idea_new, idea_transition, opinion, cover, outcome, breaker, alert, heartbeat).
- `GET /node/{id}` → typed detail for the inspection panel (figure → recent filings + score;
  advisor → recent opinions + trust history; idea → thesis + opinions + orders + outcome;
  trade → shares/avg/uPL + originating idea/figure).
- `GET /health` → `{ db, daemon_fresh, alpaca, as_of }`.

## 3. The constellation (scene model)
3D star-field graph, clustered by role; the **decision core** is the densest/brightest region.
Layers (outer → inner → out):
**Data sources** → **Tracked figures** (politicians / insiders / activist funds — named nodes) →
**Advisors / council** → **Decision core** (Fusion → Sizing → Gates → Safety) → **Ideas**
(FSM-colored) → **Execution** (adapter, exit monitor, reconciler) → **Trades** (long=cool/up,
short=warm/down, glow=|P&L|) → **Outcomes** ⟲ feedback edges back to **advisor + figure trust**
(the visible learning loop).

Live encodings: brightness = signal strength/intensity; node size = trust weight / notional;
color = state (green bullish / red bearish; FSM palette for ideas); edge pulse = activity
(a particle travels the edge on the matching SSE event). Kill switch halted → the core visibly
clamps (desaturates/contracts). Daemon beacon pulses with the heartbeat.

The signature moment: a named figure's disclosed trade → filing → A1 scores → fuses with A2 →
core → idea → order → a crystallized **trade** → later an **outcome** travels back and re-sizes
that advisor + figure. The whole closed loop is the system.

## 4. Interaction
- **Orbit/zoom/pan** the constellation (R3F + drei controls). Reduced-motion + perf guardrails.
- **Hover** a node → tooltip (label + headline metric). **Click** → side **inspection panel**
  (`/node/{id}` detail). **Click an edge/particle** → explains that relationship.
- **Cluster focus**: select a layer (Figures / Council / Core / Trades / Learning) → camera eases
  to it, others dim. A "follow the money" guided path that walks one live idea end-to-end.
- **Legend / HUD**: equity, daily P&L, daemon status, kill-switch state, "systems online."

## 5. Tech stack
- Web: Vite + React 18 + TypeScript, **@react-three/fiber** + **@react-three/drei**, zustand for
  state, native EventSource for SSE. No Next. Lint: eslint + prettier. Test: vitest +
  @testing-library + a headless-safe scene smoke test.
- API: FastAPI + uvicorn, pydantic v2, stdlib sqlite3 (`mode=ro`), reuse `arbiter` package for
  config + AlpacaAdapter (read-only). Test: pytest + httpx against a fixture DB (NO network).

## 6. Build strategy — vertical slice first, then 5 parallel lanes
**Foundation (orchestrator, before fan-out):** scaffold `cockpit/{api,web}`, FREEZE
`contract.py` + `contract.ts`, and ship a thin runnable slice — `/graph` + `/state` from the real
DB (read-only), and an R3F scene that renders the constellation skeleton with the
**execution→trades** arc live (positions from Alpaca, polled). This proves perf + data wiring +
read-only safety on day one.

**Then 5 disjoint lanes (the agents), each owning distinct files against the frozen contract:**
1. **API / data-mapping** — implement all endpoints + the SQL→DTO mappers (`/graph` full
   inventory incl. figures, `/state`, `/node/{id}`, `/health`) + pytest on a fixture DB.
2. **Event stream** — the audit/heartbeat tailer → `/events` SSE; map raw audit lines to typed
   events; resilient to truncation/rotation; tests with a synthetic audit file.
3. **Scene & rendering** — the R3F constellation: node/edge meshes, layout (force/cluster), camera,
   instancing/perf, particle-flow on events; reduced-motion + zero-jank.
4. **Interaction & inspection** — hover/click, the inspection panel (`/node/{id}`), cluster focus,
   the guided "follow the money" path, HUD/legend.
5. **Aesthetic & motion polish** — the visual language (palette, bloom/glow, depth, materials),
   the kill-switch-clamp + learning-loop choreography, the "finished/premium" pass + a11y/perf audit.

Lanes 1–2 are backend (Python), 3–5 frontend (TS/R3F); all build against the frozen contract, so
they can run in parallel without colliding. A final audit+fix wave verifies end-to-end on live data.

## 7. Testing & success criteria
- API: read-only proven (open `mode=ro`, assert writes raise); all endpoints covered on a fixture
  DB; offline degradation paths tested; NO network in tests.
- Web: contract types compile against real payloads; scene renders the full `/graph`; an SSE event
  drives a visible pulse; reduced-motion path; no console errors.
- **Done = ** open `:5173`, see the whole system as a living constellation with the 79 figures →
  council → core → the 3 live trades (AMZN/T/UBER), click any node for detail, and watch a real
  fill/cover pulse through in real time — all without ever writing to the trading system.
