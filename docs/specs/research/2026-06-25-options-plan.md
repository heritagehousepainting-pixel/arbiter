# Options Expression Layer — Phased Build Plan

**Date:** 2026-06-25
**Status:** PLAN (output of 3-agent loop; ready for build wave)
**Replaces / refines:** `docs/specs/2026-06-25-options-expression-layer-plan.md`

---

## 0. Principles (locked — not relitigated here)

- Long calls / puts ONLY. Defined risk = premium paid. No shorts, no spreads, no assignment tail.
- Expiry ≥ 2 months. Matched to disclosure-signal horizons (90–180 days).
- Directional learning stays on the EQUITY outcome. Option P&L never touches advisor trust scores.
- Shadow-first. Log "would-have-traded" before any paper order.
- Zero impact on the equity path; fully off with one config flag.

---

## 1. Architecture (refined)

```
council (A1 insider/activist/fund · A2 mirofish · A3 news)
   → FusionOutput (conviction + horizon + advisor_contributions)
   → decide()  [existing equity path — UNCHANGED]
        ├─ PaperOrder (equity)              EXISTING PATH, zero touch
        └─ options_expression_gate()        NEW — reads same FusionOutput
              ├─ gate check (threshold, horizon, catalyst, liquidity, IV)
              │     └─ returns OptionGateDecision (allowed | why_not)
              ├─ select_contract()           pick strike/expiry
              │     └─ returns OptionContract (OCC symbol, delta, iv_rank, …)
              ├─ size_option()               premium-aware notional budget
              │     └─ returns OptionOrder (premium_usd)
              ├─ delta_risk_hook()           fold delta-notional into RiskBook
              └─ SHADOW log (P1) | paper option order (P2)
```

The expression layer is a **pure side path**: it reads `FusionOutput` and
`RiskBook` already available in the engine cycle. It never modifies the
equity decision, never changes advisor weights, and can be disabled by a
single `options_enabled = false` config flag.

---

## 2. Solved: Hard Problems from the Seed Plan

### 2.1 IV / Cheapness Modeling (Hard Problem 1)

IV crush is the primary failure mode. We cannot build a full vol surface model
in this layer; instead we use a pragmatic "don't overpay" filter:

**IV Rank (IVR):** `IVR = (current IV − 52-week low IV) / (52-week high IV − 52-week low IV)`.
IVR < 40 is "cheap"; IVR > 70 is "expensive — skip". This is a binary gate, not a
precise model.

**Data source:** Alpaca's options chain endpoint (`GET /v1beta1/options/chains`)
returns `implied_volatility` per contract. We store the per-ticker 52-week
IV high/low in a small `option_iv_history` table, updated each shadow cycle.
Cold start: skip IV check for first 4 weeks of shadow; log `iv_rank=NULL` so
we can see the distribution before enforcing the gate.

**Premium-to-move filter:** Gate only passes when
`underlying_price × expected_move_pct > breakeven_price − underlying_price`
(i.e. the expected 1σ move over the thesis horizon clears the breakeven).
Expected move proxy: `IV × sqrt(days/365)`.

### 2.2 Strike / Expiry Selection Policy (Hard Problem 2)

**Strike:** Target **0.60–0.70 delta** ITM call (BUY conviction) / ITM put (SELL
conviction). This gives equity-like directional participation with defined risk.
Selection: from the chain returned by Alpaca, filter contracts with
`expiry >= (as_of + thesis_horizon_days)` and `abs(delta) in [0.60, 0.70]`;
take the contract closest to delta=0.65 as the canonical pick.

If no contract in the delta range exists (thin chain), fall back to ATM.
If still none, skip and log `reason=no_qualifying_contract`.

**Expiry:** Minimum `as_of + thesis_horizon_days`, capped at 180 days beyond
thesis horizon to avoid liquidity deserts. Prefer the first expiry cycle that
satisfies the minimum.

**OCC symbol construction:** `{TICKER}{YYMMDD}{C|P}{STRIKE×1000:08d}`
e.g. `AAPL251220C00200000`. Alpaca uses this format natively.

### 2.3 Risk Accounting (Hard Problem 3)

**Delta-adjusted notional** is folded into the existing `RiskBook` so equity
caps remain binding:

```python
delta_notional = abs(contract.delta) * contract.multiplier * underlying_price * n_contracts
risk_book = risk_book.add(ticker, delta_notional)   # same RiskBook.add() call
```

The `RiskBook` is passed to `decide()` AFTER the options path runs per ticker,
so the equity path sees the option's delta exposure as already committed. This
is conservative: a 0.65-delta call on 100 shares counts as 65 equity-equivalent
shares. The option *premium* is drawn from a separate carved-out budget (see §2.6).

**Separate option gross cap:** An `options_max_gross_pct` config field
(default 5% of portfolio equity, i.e. the maximum total premium deployed)
acts as a secondary budget. It is checked by the gate before each option order.
It is deliberately small — the options sleeve is for learning, not scale.

### 2.4 Data + Execution (Hard Problem 4)

Alpaca supports options on paper accounts via:
- Chains: `GET /v1beta1/options/chains/{underlying_symbol}` (returns contracts
  with greeks incl. delta, IV)
- Snapshots: `GET /v1beta1/options/snapshots/{symbol}` (real-time IV, OI, volume)
- Orders: `POST /v2/orders` with `asset_class = "us_option"` and OCC symbol

The **existing `AlpacaAdapter` is NOT modified**. A new `AlpacaOptionsClient`
wraps only the options-specific endpoints (chain fetch, greeks, option order).
It is a separate class so the equity path's `Executor` abstraction is
completely unchanged.

In P1 (shadow) the `AlpacaOptionsClient` is used only for chain fetches
(read-only). The `place()` call is NEVER invoked in P1 — shadow records the
"would-have" order only.

### 2.5 Outcome / Learning Separation (Hard Problem 5)

Option P&L is nonlinear and path-dependent — it cannot be compared to the
equity alpha_bps that drives advisor trust. Two separate tracks:

**Track A (existing, unchanged):** `outcomes` table — equity alpha_bps → trust.
**Track B (new):** `option_outcomes` table — option P&L in USD and return
multiples. Used ONLY for options-strategy evaluation (is the expression layer
adding edge vs. just buying equity?). No trust score update flows from Track B.

The `option_outcomes` table stores:
- `entry_premium_usd`, `exit_premium_usd` (or mark-to-market at expiry)
- `realized_pl_usd`, `return_multiple`
- `underlying_move_pct` (did direction work?)
- `iv_crush_factor` (IV at entry vs. exit — attribution)
- `idea_id` (link back to the originating idea for cross-referencing)

### 2.6 Sizing / Budget (Hard Problem 6)

**Option sleeve budget:** `options_sleeve_pct` config field, default 2.0% of
portfolio equity. Maximum total premium deployed across ALL open option
positions = `portfolio_equity × options_sleeve_pct`.

**Per-order size:** 1–5 contracts, targeting ≤ 1% of portfolio equity in
premium per position. At $10k portfolio: max $100 premium per option position,
$200 total sleeve. Intentionally tiny for P1/P2 validation.

**Contract count formula:**
```python
max_premium = min(portfolio_equity * max_option_position_pct,
                  remaining_sleeve_budget)
n_contracts = max(1, int(max_premium / (contract.ask_price * 100)))
```

---

## 3. Phased Build Plan

### Phase P1 — Shadow (zero trading risk)

**Goal:** Log "would-have-bought X at $Y on date Z" for every qualifying idea.
Validate gate criteria, contract selection, IV data quality, and budget
accounting against real chains for weeks before any money moves.

**Config flag:** `OPTIONS_ENABLED` env var / `options.enabled` TOML key.
Default: `false`. Set to `"shadow"` for P1. Setting `"paper"` unlocks P2.
The gate returns `OptionGateDecision(allowed=False)` immediately when the flag
is `false` or absent — zero code executes on the options path.

**Files to CREATE:**

| File | Responsibility |
|------|----------------|
| `arbiter/options/__init__.py` | Package marker |
| `arbiter/options/gate.py` | `OptionGateDecision` dataclass + `options_expression_gate()` — evaluates conviction threshold, horizon floor, catalyst tag, IV rank, liquidity check, premium-to-move clearance |
| `arbiter/options/contract_selector.py` | `OptionContract` dataclass + `select_contract()` — fetches chain, picks 0.60–0.70 delta contract at target expiry |
| `arbiter/options/sizing.py` | `OptionOrder` dataclass + `size_option()` — contract count sizing within budget |
| `arbiter/options/shadow_log.py` | `log_shadow_option()` — inserts into `option_shadow_log` table; the ONLY I/O in P1 |
| `arbiter/options/alpaca_options_client.py` | `AlpacaOptionsClient` — chain fetch + greeks (GET only in P1; place() stubbed to raise `NotImplementedError`) |
| `arbiter/options/iv_history.py` | `IVHistory` — read/write `option_iv_history` table; computes IVR from rolling window |
| `arbiter/options/types.py` | All options-specific types (`OptionGateDecision`, `OptionContract`, `OptionOrder`, `OptionSide`) |

**Files to MODIFY:**

| File | Change |
|------|--------|
| `arbiter/config.py` | Add `[options]` TOML section; new fields: `options_mode` ("off"/"shadow"/"paper"), `options_min_conviction`, `options_min_horizon_days`, `options_max_iv_rank`, `options_sleeve_pct`, `options_max_position_pct`, `options_delta_target`, `options_delta_tolerance` |
| `arbiter/engine/_engine.py` | After equity decide loop, call `run_options_shadow_cycle()` when `config.options_mode != "off"` |
| `arbiter/orchestrator/cycle.py` | Thread options shadow calls alongside equity cycle (same pattern as existing A2 threading) |
| `arbiter/policy/book.py` | `RiskBook.add_option_delta()` — dedicated method that tracks delta-notional in a separate `_option_delta` dict to keep option vs equity exposure attributable |

**DB migrations to ADD (P1):**

`029_options_shadow.sql` — creates:
- `option_shadow_log` — one row per "would-have" option considered
- `option_iv_history` — per-ticker IV high/low tracking (rolling 52w)

```sql
CREATE TABLE IF NOT EXISTS option_shadow_log (
    id                  TEXT PRIMARY KEY,       -- ULID
    idea_id             TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    occ_symbol          TEXT NOT NULL,
    option_side         TEXT NOT NULL,          -- "call" | "put"
    expiry_date         TEXT NOT NULL,          -- ISO date
    strike              REAL NOT NULL,
    delta               REAL NOT NULL,
    iv_rank             REAL,                   -- NULL during cold-start window
    bid_price           REAL NOT NULL,
    ask_price           REAL NOT NULL,
    underlying_price    REAL NOT NULL,
    n_contracts         INTEGER NOT NULL,
    estimated_premium   REAL NOT NULL,          -- ask × 100 × n_contracts
    conviction          REAL NOT NULL,
    gate_passed         INTEGER NOT NULL,       -- 1 if gate cleared, 0 if not
    gate_reject_reason  TEXT,
    as_of               TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS option_iv_history (
    id              TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    iv_sample       REAL NOT NULL,
    sample_date     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_option_iv_history_ticker_date
    ON option_iv_history (ticker, sample_date);
```

**Key interfaces (P1):**

```python
# arbiter/options/types.py
@dataclass(frozen=True)
class OptionGateDecision:
    allowed: bool
    reject_reason: str | None           # None when allowed=True
    conviction: float
    iv_rank: float | None

@dataclass(frozen=True)
class OptionContract:
    occ_symbol: str
    ticker: str
    expiry_date: date
    strike: float
    option_side: OptionSide             # "call" | "put"
    delta: float
    iv: float
    iv_rank: float | None
    bid: float
    ask: float
    open_interest: int
    volume: int

@dataclass(frozen=True)
class OptionOrder:
    order_id: str                        # ULID
    contract: OptionContract
    n_contracts: int
    estimated_premium_usd: float        # ask × 100 × n_contracts
    idea_id: str


# arbiter/options/gate.py — public signature
def options_expression_gate(
    ticker: str,
    fusion: FusionOutput,
    *,
    as_of: datetime,
    config: Config,
    risk_book: RiskBook,
    portfolio_equity: float,
    iv_history: IVHistory,
    catalyst_tag: str | None = None,    # e.g. "activist_13d", "insider_cluster"
) -> OptionGateDecision:
    ...


# arbiter/options/contract_selector.py — public signature
def select_contract(
    ticker: str,
    option_side: OptionSide,            # derived from fusion.conviction sign
    *,
    as_of: datetime,
    min_expiry_days: int,               # = config.options_min_horizon_days
    target_delta: float,                # = config.options_delta_target
    delta_tolerance: float,             # = config.options_delta_tolerance
    client: AlpacaOptionsClient,
    iv_history: IVHistory,
) -> OptionContract | None:
    ...


# arbiter/options/shadow_log.py — public signature
def log_shadow_option(
    order: OptionOrder | None,
    gate_decision: OptionGateDecision,
    *,
    conn: sqlite3.Connection,
    as_of: datetime,
    audit_path: str | None = None,
) -> str:
    ...
```

**Gate criteria (codified):**

```python
MIN_CONVICTION_MULTIPLIER = 1.5   # default: 1.5× the equity _MIN_CONVICTION (0.05)
                                   # → options threshold = 0.075
# Configurable via options_min_conviction
MIN_HORIZON_DAYS = 60             # ≥ 2-month thesis (configurable)
MAX_IV_RANK = 0.40                # don't buy expensive vol (configurable)
MIN_OPEN_INTEREST = 100           # basic liquidity floor
MIN_VOLUME = 10                   # basic liquidity floor
BREAKEVEN_BUFFER = 1.05           # expected move must be 5% beyond breakeven
```

**Catalyst tag enrichment:** The engine already knows the advisor source
(insider cluster, activist 13D, fund add). The cycle passes a `catalyst_tag`
derived from the `advisor_contributions` dict key with the highest weight.
Gate requires `catalyst_tag is not None` in strict mode (configurable; relaxed
during shadow for data collection).

**P1 isolation guarantee:** The `AlpacaOptionsClient.place()` raises
`NotImplementedError("Shadow mode: option orders are not executed in P1")`.
The only I/O is (a) GET chain/snapshot (read-only) and (b) INSERT into
`option_shadow_log` (local SQLite). The equity path is NOT modified —
`run_options_shadow_cycle()` is called AFTER `run_cycle()` returns so it
can never block or corrupt the equity cycle.

**Test strategy (P1, offline-first):**

```
tests/options/
  conftest.py          — fake chain fixture (static JSON, mirrors Alpaca schema)
  test_gate.py         — conviction threshold, horizon floor, IV rank, liquidity
                         gates; parametrize allowed/rejected combinations
  test_contract_selector.py
                       — delta-in-range selection, no-contract fallback, expiry
                         floor enforcement
  test_sizing.py       — budget cap, contract count formula, sleeve depletion guard
  test_shadow_log.py   — DB insert, idempotency (same idea_id + occ_symbol → no dup)
  test_iv_history.py   — IVR computation, cold-start NULL path
  test_alpaca_options_client.py
                       — monkeypatched http_get; verifies URL construction and
                         response parsing; verifies place() raises NotImplementedError
  test_options_integration.py
                       — wires gate → selector → size → log end-to-end with fake
                         chain; verifies nothing on the equity path changes
```

No live network calls in any test. `AlpacaOptionsClient` has injectable
`http_get` (same pattern as `AlpacaAdapter`). The `no_lookahead` and
`insert_only` linters apply unchanged (option_shadow_log uses insert_row).

---

### Phase P2 — Paper Execution

**Goal:** Place real paper option orders through Alpaca after weeks of shadow
validation confirming gate logic and contract selection are sound.

**Unlock condition:** `options_mode = "paper"` AND explicit approval record in
`option_gate_approvals` table (mirrors `gate_approvals` pattern for equity).

**Files to CREATE:**

| File | Responsibility |
|------|----------------|
| `arbiter/options/executor.py` | `OptionExecutor` — wraps `AlpacaOptionsClient.place()`; dedup via `option_orders` table; same retry/reject contract as equity `submit_order` |
| `arbiter/options/position_store.py` | `OptionPositionStore` — reads open option positions, tracks cost basis, marks unrealized P&L |
| `arbiter/options/exit_monitor.py` | `OptionExitMonitor` — closes options at thesis expiry or on hard stop (50% premium loss) |

**Files to MODIFY:**

| File | Change |
|------|--------|
| `arbiter/options/alpaca_options_client.py` | Enable `place()` — posts to `/v2/orders` with `asset_class=us_option` |
| `arbiter/options/gate.py` | Add `paper_approval_check()` step before place |
| `arbiter/engine/_engine.py` | Route to `OptionExecutor.place()` when `options_mode == "paper"` |

**DB migrations (P2):**

`030_option_orders.sql` — creates `option_orders` table (mirrors equity
`orders` schema but adds options-specific columns: `occ_symbol`, `option_side`,
`strike`, `expiry_date`, `n_contracts`, `premium_per_contract`).

`031_option_positions.sql` — creates `option_positions` and `option_outcomes` tables.

**Safety constraints (P2):**
- `options_sleeve_pct` budget strictly enforced before place; no override.
- Delta-notional folds into `RiskBook` immediately after fill, before any
  further equity decisions that cycle.
- Hard stop: 50% premium loss on any single position → auto-close (close-only
  order, `time_in_force=gtc`).
- P2 inherits all existing circuit breakers; `BrokerError` from options path
  also triggers engine pause.

---

### Phase P3 — Evaluate Edge

**Goal:** Run the options track long enough to answer: "Does the expression layer
add edge beyond the equity outcome already captured by the learning loop?"

**P3 is NOT a build phase.** It is an evaluation protocol:

1. Query `option_outcomes` for all closed positions (>= 30 samples per catalyst
   tag).
2. Compare `return_multiple` to a counterfactual: what would equity alpha_bps
   have been over the same horizon?
3. Attribute IV crush separately (`iv_crush_factor`) — a direction-correct but
   IV-crush-damaged option is a different failure mode from a wrong direction.
4. Gate to scale: if P&L is negative on average across ≥ 30 positions, options
   mode reverts to "shadow" (automatic, via config). Upside = widen sleeve.

**Files to CREATE (P3):**

| File | Responsibility |
|------|----------------|
| `arbiter/options/attribution.py` | `attribute_option_outcomes()` — decomposes returns into direction × IV × theta × timing |
| `arbiter/evaluation/option_backtest.py` | `run_option_backtest()` — replays shadow log to compute counterfactual P&L vs equity path |

---

## 4. Data Flow Diagram

```
Engine cycle (per ticker):
  1. FusionOutput → decide() → PaperOrder (equity path, UNCHANGED)
  2. FusionOutput → options_expression_gate()
       ↓ gate passes
  3. → AlpacaOptionsClient.get_chain()    [live Alpaca GET, read-only]
       ↓ contract found
  4. → select_contract() → OptionContract
  5. → size_option() → OptionOrder
  6. → delta_risk_hook() → risk_book.add_option_delta()   [caps updated]
  7P1. → log_shadow_option() → option_shadow_log [SQLite INSERT]
  7P2. → OptionExecutor.place() → option_orders [SQLite INSERT + Alpaca POST]
```

---

## 5. Config Changes Summary

New TOML section `[options]`:

```toml
[options]
mode = "off"                  # "off" | "shadow" | "paper"
min_conviction = 0.075        # 1.5× equity threshold
min_horizon_days = 60
max_iv_rank = 0.40
sleeve_pct = 0.02             # 2% of portfolio equity = max total premium
max_position_pct = 0.01       # 1% per position
delta_target = 0.65
delta_tolerance = 0.05        # accept 0.60–0.70
min_open_interest = 100
min_volume = 10
require_catalyst_tag = false  # true in production; false during shadow ramp
iv_cold_start_days = 28       # skip IV gate for first N days
```

Env-var overrides follow the existing `_env_float`, `_env_str`, `_env_bool`
pattern. New `_KNOWN_KEYS["options"]` set added to `_validate_toml()`.

---

## 6. Isolation / Safety Matrix

| Concern | Guarantee |
|---------|-----------|
| Equity path | Zero modifications to `decide()`, `submit_order()`, `FusionOutput`, `PaperOrder`. Options code called AFTER equity cycle returns. |
| Advisor trust | `option_outcomes` table never written to `outcomes`. No trust score update from options P&L. |
| Config off-by-default | `options_mode` defaults to `"off"`. All options code is behind `if config.options_mode != "off"` guards. |
| P1 budget | `AlpacaOptionsClient.place()` raises `NotImplementedError`. No money moves in P1. |
| P2 budget | `options_sleeve_pct` checked before every place; delta-notional folds into existing `RiskBook` caps. |
| DB isolation | Options tables (`option_shadow_log`, `option_orders`, `option_positions`, `option_outcomes`, `option_iv_history`) are separate from equity tables. No foreign keys cross the boundary. |
| Test hermeticity | `AlpacaOptionsClient` has injectable `http_get`/`http_post`; no live Alpaca calls in tests. `conftest.py` fake chain fixture mirrors real Alpaca schema. |
| Linter compliance | All option DB writes use `insert_row()`. No UPDATE/DELETE. `option_shadow_log` is insert-only by design (corrections = new row with `supersedes_id`). |

---

## 7. P1 Build Sequence (suggested wave order)

1. **Types + config** — `options/types.py`, config fields, migration `029_options_shadow.sql`
2. **IV history** — `options/iv_history.py` + `test_iv_history.py` (pure DB, no Alpaca)
3. **Alpaca options client** — `options/alpaca_options_client.py` + `test_alpaca_options_client.py` (monkeypatched, offline)
4. **Contract selector** — `options/contract_selector.py` + `test_contract_selector.py`
5. **Gate** — `options/gate.py` + `test_gate.py`
6. **Sizing** — `options/sizing.py` + `test_sizing.py`
7. **Shadow log** — `options/shadow_log.py` + `test_shadow_log.py`
8. **Engine wiring** — modify `engine/_engine.py` + `orchestrator/cycle.py` + `test_options_integration.py`
9. **RiskBook delta extension** — `policy/book.py` + extend `test_book.py`

Each step has its own test file and is independently reviewable. Steps 1–7 have
NO modifications to existing production files (except config). Only step 8
touches the engine and cycle, and only by adding an `if config.options_mode !=
"off"` branch after the existing equity path.
