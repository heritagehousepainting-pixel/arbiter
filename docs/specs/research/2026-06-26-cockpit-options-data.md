# Cockpit Options Data — Integration Design

**Date:** 2026-06-26
**Status:** PLAN (data/architecture half — visual half is a separate deliverable)
**Author:** read-only brainstorm agent (no code modified)

---

## 1. Scope and constraints recalled

- **Read-only.** The cockpit opens `arbiter.db` via `?mode=ro`. Every query here is a SELECT.
- **Additive only.** Existing equity nodes/edges/routes are never touched.
- **Frozen-contract discipline.** New Python DTOs in `api/contract.py` are mirrored in `web/src/contract.ts`; the two files move in lockstep.
- Options data lives in four tables: `option_positions`, `option_shadow_log`, `option_outcomes`, `option_iv_history`. All currently have 0 rows (system not yet in shadow/paper mode), so the design must degrade gracefully to empty/dim states.
- `options_mode` ("off" | "shadow" | "paper") lives in `arbiter/arbiter/config.py` and is read from the `OPTIONS_MODE` env var at daemon startup. It is NOT written into the DB. It also is NOT in the heartbeat file (`_hb()` in `daemon.py` does not include it). This matters for surfacing (see §4).

---

## 2. Files to add / modify

### Add (new files)

| File | Role |
|---|---|
| `cockpit/api/options.py` | New module: all read-only SQL queries against the four options tables + the `build_options_state()` function; mirrors `positions.py` pattern |
| `cockpit/api/options_detail.py` | `build_options_node_detail()` — detail for the `opt.layer` node and per-underlying IV drill |

### Modify (extend existing)

| File | Change |
|---|---|
| `cockpit/api/contract.py` | Add new DTOs: `OpenOptionPosition`, `OptionShadowPlay`, `OptionOutcomeRow`, `IVPoint`, `IVSeries`, `OptionsState` |
| `cockpit/api/graph.py` | Add `opt.layer` node to `_DATA_SOURCES`-adjacent lists + structural edges from `core.safety` and `exec.adapter` into `opt.layer` |
| `cockpit/api/state.py` | Add `_options_node_intensity()` call; include `opt.layer` in `nodes` dict of returned `State` |
| `cockpit/api/node_detail.py` | Add `opt` prefix to routing table + delegate to `build_options_node_detail()` |
| `cockpit/api/main.py` | Add `GET /options` route returning `OptionsState`; add `GET /options/iv/{ticker}` returning `IVSeries` |
| `cockpit/web/src/contract.ts` | Mirror all new DTOs as TypeScript interfaces; add `fetchOptions()` and `fetchIV(ticker)` to `api.ts` |
| `cockpit/web/src/api.ts` | Add `fetchOptions` and `fetchIV` exports |

---

## 3. Options node in the constellation graph

### Node identity

```
id:      "opt.layer"
type:    "engine_part"          # fits the constellation taxonomy — it IS an engine layer
label:   "Options Layer"
cluster: "execution"            # hangs in the execution cluster, between core.safety and exec.adapter
meta:    { "options_mode": "<off|shadow|paper>" }
```

**Why `execution` cluster, not a new cluster?** The options layer is not a data source, advisor, or figure — it is an expression/execution subsystem. The execution cluster already contains `exec.adapter`, `exec.exit_monitor`, and `exec.reconciler`. Options expression is the fourth peer: it takes a sized idea and adds a derivative expression on top. Adding a brand-new cluster just for one node would disturb the ring layout more than necessary.

**Why `engine_part` type, not a new type?** `engine_part` is the right semantic — it is a decision-layer part (gate + sizing + lifecycle). Introducing a new `NodeType` literal would require changes in many rendering places for no rendering gain.

### Structural edges (added to `graph.py`)

```python
# core.safety → opt.layer  (decides — the gate sits after the safety check)
Edge(id="e.dec.core.opt", source="core.safety", target="opt.layer", kind="decides")

# opt.layer → exec.adapter  (submits — paper orders go through the Alpaca adapter)
Edge(id="e.sub.opt.adapter", source="opt.layer", target="exec.adapter", kind="submits")
```

These two edges mirror the equity flow (`core.safety → exec.adapter`) but add the options gate as a decision waypoint. In shadow mode the `submits` edge is still valid conceptually (shadow evaluates what *would* have been submitted) and lets the visual show the full evaluation path even when no real orders flow.

### Node intensity (in `state.py` → `_options_node_intensity()`)

Read from `option_shadow_log` + `option_positions` + config-inferred mode:

```
options_mode = "off"    → intensity 0.05,  status "off"
options_mode = "shadow" → intensity derived from recent shadow rows (past 7d)
options_mode = "paper"  → intensity 0.8 + open position count boost
```

Since `options_mode` is not in the heartbeat, the cockpit resolves it by:
1. Reading the `OPTIONS_MODE` env var directly (same source the daemon uses) via `os.environ.get("OPTIONS_MODE", "off")` inside `_options_node_intensity()`.
2. Cross-checking with DB activity: if shadow rows exist recently but env says "off", trust the DB (safer — env may have changed without daemon restart).

Intensity formula:
```python
cutoff = utcnow - 7d
shadow_count = SELECT COUNT(*) FROM option_shadow_log WHERE as_of >= cutoff
open_count   = len(list_open_positions_ro(conn))  # same LEFT JOIN logic as positions.py

if options_mode == "off" and shadow_count == 0:
    intensity = 0.05
elif options_mode == "shadow" or shadow_count > 0:
    intensity = min(1.0, 0.3 + 0.05 * shadow_count)
elif options_mode == "paper":
    intensity = min(1.0, 0.7 + 0.1 * open_count)
```

`meta` carries `options_mode` so the front end can colour-code or label the node differently (off = grey, shadow = amber glow, paper = full colour).

---

## 4. New DTOs

### Python (`cockpit/api/contract.py` additions)

```python
# --- Options layer -----------------------------------------------------------

OptionsMode = Literal["off", "shadow", "paper"]


class OpenOptionPosition(BaseModel):
    """One live open option position (from option_positions, not yet in option_outcomes)."""
    id: str                          # ULID PK from option_positions
    idea_id: str
    underlying: str                  # equity ticker, e.g. "AAPL"
    occ_symbol: str                  # OCC symbol, e.g. "AAPL240119C00150000"
    side: str                        # "call" | "put"
    strike: float
    expiry: str                      # ISO date string
    contracts_qty: int
    entry_premium: float             # total USD premium paid to open
    delta_at_open: float | None
    iv_at_open: float | None
    underlying_open_price: float
    thesis_horizon_date: str         # ISO date string
    original_conviction: float
    open_ts: str                     # UTC ISO timestamp
    # Computed / enriched fields (None when market closed / data unavailable)
    dte: int | None = None           # calendar days to expiry as of query time
    current_mid: float | None = None # live mid-price (requires Alpaca options client)
    unrealized_pl: float | None = None  # (current_mid - entry_per_contract) * qty * 100
    unrealized_pl_pct: float | None = None


class OptionShadowPlay(BaseModel):
    """One row from option_shadow_log — a would-have-traded evaluation."""
    id: str
    idea_id: str
    underlying: str
    as_of: str
    gate_express: bool               # True = gate fired (would have traded)
    gate_reason: str                 # "OK" | "IV_RANK_TOO_HIGH" | "CONVICTION_TOO_LOW" | etc.
    side: str | None                 # "call" | "put" | None
    occ_symbol: str | None
    strike: float | None
    expiry: str | None
    delta: float | None
    iv: float | None
    est_premium: float | None
    delta_adjusted_notional: float | None
    contracts_qty: int | None
    conviction: float
    horizon_days: float
    catalyst_tag: str | None
    ivr_estimate: float | None
    created_at: str


class OptionOutcomeRecord(BaseModel):
    """One closed option trade from option_outcomes — display-only, isolated from equity learning."""
    id: str
    idea_id: str
    underlying: str
    occ_symbol: str
    side: str
    open_ts: str
    close_ts: str
    close_reason: str                # "premium_stop" | "horizon_expiry" | "conviction_reversal" | etc.
    entry_premium: float
    exit_premium: float
    option_pl_pct: float             # (exit - entry) / entry — display only, NOT advisory trust
    underlying_alpha_bps: float      # equity move bps — the direction-bridge field
    delta_at_open: float | None
    iv_at_open: float | None
    iv_at_close: float | None
    contracts_qty: int
    created_at: str


class IVPoint(BaseModel):
    """One ATM-IV data point for a single underlying."""
    as_of: str
    atm_iv: float                    # annualised, decimal (0.38 = 38%)
    occ_symbol: str


class IVSeries(BaseModel):
    """IV history series for one underlying ticker."""
    underlying: str
    points: list[IVPoint] = []
    current_iv_rank: float | None = None   # IVR ∈ [0,1] from option_iv_history; None < 30d data
    as_of: str


class OptionsState(BaseModel):
    """Complete options data snapshot, served at GET /options."""
    options_mode: OptionsMode = "off"
    open_positions: list[OpenOptionPosition] = []
    recent_shadow_plays: list[OptionShadowPlay] = []   # last 20, mixed express/reject
    recent_outcomes: list[OptionOutcomeRecord] = []    # last 20 closed trades
    # Aggregate stats (None when no data)
    n_open: int = 0
    sleeve_used_pct: float | None = None  # est_premium_total / equity (requires account)
    win_rate: float | None = None         # fraction of outcomes with option_pl_pct > 0
    avg_option_pl_pct: float | None = None
    avg_underlying_alpha_bps: float | None = None
    as_of: str
```

### TypeScript mirror (`cockpit/web/src/contract.ts` additions)

```typescript
export type OptionsMode = "off" | "shadow" | "paper";

export interface OpenOptionPosition {
  id: string;
  idea_id: string;
  underlying: string;
  occ_symbol: string;
  side: "call" | "put";
  strike: number;
  expiry: string;
  contracts_qty: number;
  entry_premium: number;
  delta_at_open: number | null;
  iv_at_open: number | null;
  underlying_open_price: number;
  thesis_horizon_date: string;
  original_conviction: number;
  open_ts: string;
  dte: number | null;
  current_mid: number | null;
  unrealized_pl: number | null;
  unrealized_pl_pct: number | null;
}

export interface OptionShadowPlay {
  id: string;
  idea_id: string;
  underlying: string;
  as_of: string;
  gate_express: boolean;
  gate_reason: string;
  side: "call" | "put" | null;
  occ_symbol: string | null;
  strike: number | null;
  expiry: string | null;
  delta: number | null;
  iv: number | null;
  est_premium: number | null;
  delta_adjusted_notional: number | null;
  contracts_qty: number | null;
  conviction: number;
  horizon_days: number;
  catalyst_tag: string | null;
  ivr_estimate: number | null;
  created_at: string;
}

export interface OptionOutcomeRecord {
  id: string;
  idea_id: string;
  underlying: string;
  occ_symbol: string;
  side: "call" | "put";
  open_ts: string;
  close_ts: string;
  close_reason: string;
  entry_premium: number;
  exit_premium: number;
  option_pl_pct: number;
  underlying_alpha_bps: number;
  delta_at_open: number | null;
  iv_at_open: number | null;
  iv_at_close: number | null;
  contracts_qty: number;
  created_at: string;
}

export interface IVPoint {
  as_of: string;
  atm_iv: number;
  occ_symbol: string;
}

export interface IVSeries {
  underlying: string;
  points: IVPoint[];
  current_iv_rank: number | null;
  as_of: string;
}

export interface OptionsState {
  options_mode: OptionsMode;
  open_positions: OpenOptionPosition[];
  recent_shadow_plays: OptionShadowPlay[];
  recent_outcomes: OptionOutcomeRecord[];
  n_open: number;
  sleeve_used_pct: number | null;
  win_rate: number | null;
  avg_option_pl_pct: number | null;
  avg_underlying_alpha_bps: number | null;
  as_of: string;
}
```

---

## 5. API routes

### `GET /options` → `OptionsState`

The primary options snapshot. Polled by the client on the same cadence as `/state` (every few seconds).

**Wiring:** `main.py` imports `build_options_state` from `api/options.py`. Opens `connect()` read-only, calls `build_options_state(conn)`, closes. Returns `OptionsState`.

### `GET /options/iv/{ticker}` → `IVSeries`

IV history for one underlying — used to power a per-ticker IV chart in the options inspector panel. Ticker is URL-encoded (e.g. `/options/iv/AAPL`).

**Wiring:** `main.py` imports `build_iv_series` from `api/options.py`. Returns 200 with empty `points` list when no data; never 404 for a valid-looking ticker.

### `GET /node/opt.layer` → `NodeDetail`

The existing `/node/{id}` route catches this via the new `opt` prefix in `node_detail.py`'s routing table. Returns a `NodeDetail` with:
- `summary`: `options_mode`, `n_open`, `shadow_count_7d`, `outcome_count`, recent aggregate stats (same fields as `OptionsState` top-level aggregate)
- `rows`: last 10 shadow plays (mixed express/reject), most useful for a quick inspector

---

## 6. SQL queries (all read-only SELECTs)

### Open positions (from `api/options.py`)

```sql
-- Mirrors list_open_positions() from arbiter/options/positions.py exactly
-- An open position = option_positions row with NO matching option_outcomes row
SELECT p.*
FROM option_positions AS p
LEFT JOIN option_outcomes AS o
    ON o.idea_id    = p.idea_id
   AND o.occ_symbol = p.occ_symbol
WHERE o.id IS NULL
ORDER BY p.open_ts
```

`current_mid` and `unrealized_pl` cannot be computed from the DB alone (they require the Alpaca options snapshot). For the cockpit's read-only purpose, these fields are set to `None` — the front end shows "live price unavailable" when they're null. This is consistent with how the equity `/positions` route degrades when Alpaca is unreachable.

### Recent shadow plays

```sql
SELECT id, idea_id, underlying, as_of,
       gate_express, gate_reason, side, occ_symbol,
       strike, expiry, delta, iv,
       est_premium, delta_adjusted_notional, contracts_qty,
       conviction, horizon_days, catalyst_tag, ivr_estimate, created_at
FROM option_shadow_log
ORDER BY created_at DESC
LIMIT 20
```

### Recent option outcomes

```sql
SELECT id, idea_id, underlying, occ_symbol, side,
       open_ts, close_ts, close_reason,
       entry_premium, exit_premium, option_pl_pct,
       underlying_alpha_bps,
       delta_at_open, iv_at_open, iv_at_close,
       contracts_qty, created_at
FROM option_outcomes
ORDER BY created_at DESC
LIMIT 20
```

### Aggregate stats (for OptionsState top-level fields)

```sql
-- Win rate and avg P&L from all option outcomes
SELECT
    COUNT(*) AS n,
    SUM(CASE WHEN option_pl_pct > 0 THEN 1 ELSE 0 END) AS wins,
    AVG(option_pl_pct) AS avg_pl_pct,
    AVG(underlying_alpha_bps) AS avg_alpha_bps
FROM option_outcomes
```

`sleeve_used_pct` requires account equity (from Alpaca) plus SUM of `entry_premium` for open positions. Follow the same degradation pattern as `positions.py`: if Alpaca is unreachable, set to `None`.

### IV history series

```sql
SELECT as_of, atm_iv, occ_symbol
FROM option_iv_history
WHERE underlying = ?
ORDER BY as_of ASC
LIMIT 365   -- at most one year back; one row per day
```

### IV rank (current)

```sql
-- IVR = fraction of the past 52 weeks where current ATM IV > historical ATM IV
-- Computed in Python from the time series (mirrors iv_history.iv_rank() exactly)
-- Minimum 30 rows required; return None otherwise
SELECT atm_iv
FROM option_iv_history
WHERE underlying = ?
  AND as_of >= datetime('now', '-365 days')
ORDER BY as_of ASC
```

Then in Python:
```python
if len(rows) < 30:
    current_iv_rank = None
else:
    current_iv = rows[-1]["atm_iv"]
    current_iv_rank = sum(1 for r in rows if r["atm_iv"] < current_iv) / len(rows)
```

### 7-day shadow count (for `opt.layer` intensity and node detail)

```sql
SELECT COUNT(*) FROM option_shadow_log
WHERE as_of >= datetime('now', '-7 days')
```

---

## 7. State integration: how `opt.layer` is lit

In `state.py`, add a call to a new private function alongside the existing intensity builders:

```python
# In build_state():
nodes.update(_options_node_intensity(conn))
```

`_options_node_intensity(conn)` returns `{"opt.layer": NodeState(...)}` using the formula in §3. The `options_mode` value is read inside this function from `os.environ.get("OPTIONS_MODE", "off")` — the same env var the daemon uses — so it's always in sync without DB pollution. If the env is "off" but shadow rows exist (mode was recently changed), the intensity reflects the shadow activity rather than the env value.

---

## 8. `node_detail.py` routing extension

Add `"opt"` to `_NODE_TYPES` and the dispatch table:

```python
# In _NODE_TYPES:
"opt": "engine_part",

# In build_node_detail():
if prefix == "opt":
    if node_id != "opt.layer":
        return None
    return _options_layer_detail(conn)  # in options_detail.py
```

`_options_layer_detail(conn)` builds a `NodeDetail` with:
- `summary`: options_mode, n_open positions, shadow_count_7d, outcome_count, aggregate stats
- `rows`: last 10 shadow plays (each dict has kind="shadow_play" so the inspector can type-switch)

---

## 9. Events stream extension (optional, non-blocking)

The `events.py` audit-log mapper can optionally recognize option audit events to pulse `opt.layer`. The arbiter engine logs option gate/sizing decisions to `audit.jsonl` under event names like `option.shadow_logged`, `option.position_opened`, `option.position_closed`. If those event names exist:

```python
# In _map_audit_line():
if event_name in ("option.shadow_logged", "option.position_opened", "option.position_closed"):
    return Event(
        ts=ts,
        kind="fill",   # reuse "fill" — it lights up the node correctly
        node_ids=["opt.layer"],
        payload=payload,
    )
```

This is additive and can be wired later without touching anything else. If the event names don't exactly match, the mapper silently ignores them (existing behavior).

---

## 10. Isolation guarantee: option outcomes vs. equity outcomes

The `option_outcomes` table is entirely separate from the equity `outcomes` table. The cockpit design enforces this isolation at every layer:

1. **Separate DTOs:** `OptionOutcomeRecord` (new) vs. existing equity `outcome` node — different Python classes, different TS interfaces.
2. **Separate routes:** `/options` (new) vs. `/state` and `/node/{id}` (existing equity).
3. **No cross-linkage in graph edges:** option outcome nodes are NOT added as dynamic `outcome`-type nodes in `State.dynamic_nodes`. They live only in `OptionsState.recent_outcomes` from `/options`.
4. **Separate constellation representation:** the `opt.layer` node accumulates option activity; equity outcome nodes (dynamic, cluster="learning") continue to teach the advisor graph. The `teaches` edge never points to `opt.layer`.

This means the visual will have two clearly distinct tracks: the existing learning loop (equity outcomes → advisor weights) and the options track (options outcomes → display-only in the options panel).

---

## 11. `api.ts` additions

```typescript
export const fetchOptions = () => get<OptionsState>("/options");
export const fetchIV = (ticker: string) =>
  get<IVSeries>(`/options/iv/${encodeURIComponent(ticker)}`);
```

These are consumed by the (yet-to-be-designed) options panel component. They're additive — nothing in the existing `App.tsx` or `CockpitUI.tsx` imports or calls them yet.

---

## 12. Degradation table

| Scenario | Behavior |
|---|---|
| `OPTIONS_MODE=off`, no DB rows | `opt.layer` dim (intensity 0.05, status "off"); `/options` returns empty lists, null aggregates |
| `OPTIONS_MODE=shadow`, no rows yet | `opt.layer` intensity 0.3, status "shadow"; lists empty |
| `OPTIONS_MODE=shadow`, rows accumulating | Intensity grows with shadow_count; plays show in recent_shadow_plays |
| `OPTIONS_MODE=paper`, open positions | Intensity 0.7+; open_positions populated; current_mid=null (no live options price from DB) |
| DB unreachable | Existing `db_reachable()` path; `/options` returns 503 or OptionsState with empty data |
| `option_iv_history` < 30 rows | `current_iv_rank=None`; series still returned with available points |

---

## Summary

**Node:** `opt.layer` — type `engine_part`, cluster `execution`, wired `core.safety → opt.layer → exec.adapter` with `decides`/`submits` edges. Intensity derived from shadow activity + `OPTIONS_MODE` env var. Lives in `graph.py` static topology; intensity computed in `state.py`.

**New DTOs (py + ts):** `OpenOptionPosition`, `OptionShadowPlay`, `OptionOutcomeRecord`, `IVPoint`, `IVSeries`, `OptionsState`. All in `contract.py` / `contract.ts`. Option outcomes are explicitly isolated from equity outcomes — separate types, separate route, no `teaches` edge.

**New routes:** `GET /options` → `OptionsState`; `GET /options/iv/{ticker}` → `IVSeries`. Both in `main.py`, implemented in new `api/options.py`. Node detail for `opt.layer` handled via the existing `/node/opt.layer` route with a new prefix entry in `node_detail.py` (delegating to new `api/options_detail.py`).

**Core SQL:** open positions via the same LEFT JOIN absence pattern as `arbiter/options/positions.py`; shadow plays and outcomes by recency limit; IV rank computed in Python from the 365-day series (mirrors `iv_history.iv_rank()` without importing the arbiter package).

**Files to add:** `cockpit/api/options.py`, `cockpit/api/options_detail.py`
**Files to modify:** `contract.py`, `graph.py`, `state.py`, `node_detail.py`, `main.py`, `web/src/contract.ts`, `web/src/api.ts`
