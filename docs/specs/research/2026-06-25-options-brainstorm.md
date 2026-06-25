# Options Expression Layer — Brainstorm

**Date:** 2026-06-25  
**Status:** BRAINSTORM (agent output; feed to CRITIQUE then PLAN)

---

## Codebase Seam Map

Before the architectures, a precise map of where the existing seams live — the
architectures below hook into these exact points:

| Seam | Location | Role |
|---|---|---|
| `run_cycle()` in `_engine.py` | engine/_engine.py L381 | Orchestrates: opinions → fuse → decide → submit |
| `_bound_decide()` closure | engine/_engine.py L650 | Calls `decide()` → returns `PaperOrder | None` |
| `_bound_submit()` closure | engine/_engine.py L672 | Calls `submit_order()` → folds into `RiskBook` |
| `RiskBook` | policy/book.py | Immutable exposure accumulator, feeds decide() caps |
| `PaperOrder` | contract/seams.py | Frozen: ticker, side, qty (notional USD), horizon_bucket, exits |
| `AlpacaAdapter.place()` | execution/alpaca_adapter.py L173 | Equity-only, `POST /v2/orders` |
| `submit_order()` | execution/submit.py | Notional→shares, dedup, persist, audit |
| `outcome_runner.run_outcome_sweep()` | orchestrator/outcome_runner.py | End-of-cycle outcome attribution |
| `TrustLedger` | trust/ledger.py | Updates advisor weights from `ResolvedOutcome` |
| `Config` | config.py | Frozen; adding `[options]` section needs `_KNOWN_KEYS` + fields |

Key constraint: `AlpacaAdapter.place()` is equity-only (`POST /v2/orders`, qty in
shares). Options orders go to `POST /v2/orders` with `asset_class: "us_option"`
and OCC symbology — a different body shape. The adapter must be extended or a
new executor created.

---

## Architecture A — Post-Decide Overlay (Thin Sidecar)

### How it hooks in

After `_bound_decide()` produces a `PaperOrder | None` in `run_cycle`, a new
`_bound_express_option()` closure runs in the same cycle loop. It reads the
same `fusion_output` and `idea` that just passed through decide, then
independently decides whether to express that thesis as an option. This is a
**second callable after decide, before submit** — a sidecar function the engine
calls if the equity path produced an order (or if conviction cleared the
higher options threshold even without an equity order).

```
for idea in pending_ideas:
    fusion = fuse(...)
    equity_order = decide(fusion, idea)          # existing path
    option_shadow = express_option(fusion, idea)  # NEW — runs regardless
    if equity_order:
        submit(equity_order)
    if option_shadow:
        options_log(option_shadow)               # P1: shadow log only
```

The `express_option()` callable is constructed in `build_engine()` behind a
`config.options_enabled` flag. When disabled, it's a no-op lambda.

### Option selection logic

A `OptionsSelector` module (new `arbiter/options/selector.py`):

1. **Gate check** — `fusion.conviction >= config.options_min_conviction` (higher
   than equity's `_MIN_CONVICTION=0.05`; suggest 0.35+), `idea.horizon_days >= 60`,
   and catalyst type in `{form4_cluster, form13d, form13f_first_filing}`.
2. **Chain fetch** — `GET /v2/options/contracts?underlying_symbol=TICKER` via
   a new `OptionsChainClient` that wraps `AlpacaAdapter._headers()`. Filter:
   expiry ≥ `now + horizon_days`, strikes in ±30% of current price.
3. **Strike/delta policy** — target delta ≈ 0.60–0.70 for calls (ITM, equity-
   like delta, limited downside). For a LONG call: pick the strike where
   `delta >= 0.60` (sort by strike ASC for calls, DESC for puts). If no chain
   available → shadow log the miss, skip.
4. **IV cheapness gate** — IV rank (IVR) computed as
   `(current_IV - 52w_low_IV) / (52w_high_IV - 52w_low_IV)`. Gate: IVR ≤ 50
   (don't buy options in the top half of the IV range). Source: Alpaca's
   `/v2/options/contracts/{symbol}` returns `implied_volatility`. The 52-week
   IV range requires a separate data pull or approximation (see Hard Problems).
5. **Premium check** — `option_premium ≤ config.options_max_premium_pct × equity`
   (e.g. 0.5% of portfolio per contract slot).
6. **Contracts** — 1–3 contracts depending on conviction tier; each contract
   controls 100 shares.

### Gate criteria + thresholds

| Gate | Threshold (suggested) | Source |
|---|---|---|
| Conviction | ≥ 0.35 | `fusion.conviction` |
| Horizon | ≥ 60 days | `idea.horizon_days` |
| Catalyst type | insider cluster / activist 13D / first-filing 13F | `sig.source` |
| IVR | ≤ 50 | Computed from chain |
| Bid-ask spread | ≤ 10% of mid | Chain |
| Open interest | ≥ 500 contracts | Chain |
| Premium per position | ≤ 0.5% portfolio | Config cap |

### Delta-adjusted risk in RiskBook

Options delta-adjusted notional = `contracts × 100 × delta × underlying_price`.
This is folded into the existing `RiskBook.add(ticker, delta_adjusted_notional)`
after a shadow-log "would-have-submitted." In P2 (real paper orders), the fold
happens after an actual options fill confirmation. The `RiskBook` is already in
USD notional — delta-adjusted notional is the correct unit to express options
exposure alongside equity exposure.

A separate `options_budget_used` counter (not in RiskBook, just a running float
in the cycle closure) tracks actual premium spent, enforced against
`config.options_sleeve_pct × equity`.

### Outcome track isolation

A new `options_ideas` table (separate from `ideas`) stores every shadow-logged
or executed option expression with its entry IV, strike, expiry, premium paid,
and the `parent_idea_id` (foreign key to the originating equity idea). Outcome
is computed at expiry or early unwind against options P&L:

```
option_return = (exit_premium - entry_premium) / entry_premium
```

This outcome is written to a separate `options_outcomes` table. It feeds a
separate `OptionsPerformanceTracker` — NOT the equity `TrustLedger`. The equity
directional learning loop never sees options P&L.

### Shadow mode design

P1 (shadow): `express_option()` always returns an `OptionShadowLog` dataclass,
logged as a JSONL audit entry (`option.shadow_log`). No broker call. The shadow
log contains: ticker, direction (call/put), selected strike, expiry, IV at
decision time, mid premium, delta, IVR, conviction that triggered it, and
"would_have_premium_usd." The shadow log accumulates for weeks before P2 is
enabled.

P2 flip: a single config flag `options_paper_enabled = true` swaps the shadow
log path to a real `AlpacaOptionsAdapter.place()` call.

### Config / isolation story

New `[options]` section in `arbiter.toml`:
```toml
[options]
enabled = false                  # master toggle
paper_enabled = false            # P2: actually submit paper orders
min_conviction = 0.35
min_horizon_days = 60
sleeve_pct = 0.03                # 3% of equity budget cap for premiums
max_premium_pct = 0.005          # max 0.5% per position
max_ivr = 50                     # don't buy in top half of IV range
target_delta_min = 0.60
target_delta_max = 0.75
min_open_interest = 500
```

`options_enabled = false` → the `express_option` closure is a no-op. Zero
blast radius to the equity path.

### Pros
- Minimal blast radius. The entire options path is one additional callable in
  `_bound_submit`'s neighborhood; the equity flow is untouched.
- Inherits all existing safety gates naturally (paused engine → no option calls).
- Config toggle is a single bool; no schema migration risk.
- Shadow log is free (no broker dependency in P1).
- `RiskBook` delta-fold is surgical — 3 lines in the engine closure.

### Cons
- `express_option()` runs for every idea, even those with no chain. Chain fetches
  are a new network dependency inside the cycle — need a timeout + fail-closed
  guard.
- IV rank requires historical IV data; Alpaca may not provide a clean 52w IV
  range endpoint. May need approximation or skip.
- OCC symbology generation is non-trivial (format: `TICKER YYMMDDCNNNNN`).
- `AlpacaAdapter` must be extended or a companion `AlpacaOptionsAdapter` added
  for P2 — it currently speaks only equity.

---

## Architecture B — Parallel Options Engine (Fully Separate Process)

### How it hooks in

A completely independent daemon (`arbiter options-daemon`) reads from the same
SQLite DB (polling the `ideas` table for MONITORED equity ideas with conviction
above the options threshold), and independently decides whether to express as
options. No changes to `run_cycle`, `_bound_decide`, `_bound_submit`, or
`RiskBook`.

```
[Equity engine run_cycle] → persists idea to DB → [Options daemon polls DB] →
    → fetch chain → gate → shadow log / paper order
```

### Pros
- Zero coupling to the equity engine. The options daemon can crash without
  affecting equity trading.
- Can iterate and redeploy the options daemon independently.
- Database is already the shared durable store.

### Cons
- MAJOR: The `RiskBook` is in-memory and cycle-scoped. It does NOT persist to
  the DB between cycles. The options daemon cannot fold delta-adjusted notional
  into the equity cycle's RiskBook — the caps become meaningless across the two
  processes. This is a correctness failure: the combined gross exposure silently
  exceeds caps.
- The options daemon introduces a second SQLite writer — SQLite's WAL mode can
  handle concurrent readers/writer, but two concurrent writers need careful
  coordination (busy_timeout is already set but two writers racing on ideas/orders
  is fragile).
- Polling the DB for "qualifying ideas" requires reconstructing the conviction
  value from the persisted data — the live `fusion.conviction` is not durably
  stored today (only the order + outcome are).
- More operational complexity: two daemons, two health checks.
- The locked principle "zero impact on existing equity path" is satisfied, but
  the RiskBook-crossing problem violates the spirit of "delta-adjusted risk feeds
  the existing RiskBook/caps."

**This architecture fails the RiskBook requirement.** Rejecting it.

---

## Architecture C — Options as a New Advisor Channel (A4.options)

### How it hooks in

A new advisor `A4.options` produces `Opinion` objects with `advisor_id="A4.options"`
and a custom `payload` field indicating "express this as an option rather than
equity." The existing fusion / decide path runs. The decide() function detects
the A4.options contribution and routes the order to a parallel options submit
instead of equity submit.

### Why this is wrong

The seed plan explicitly states: "The expression layer does NOT vote." Options
are not a new signal — they are an execution modality. Routing options through
the advisor channel pollutes the trust/learning loop: `TrustLedger` would
accumulate A4.options outcomes alongside directional outcomes, which the locked
principle forbids. Fusion weights for A4.options would be meaningless. The
`horizon_bucket` typing on opinions doesn't map cleanly to option expiries.

**This architecture violates the locked principle "directional learning stays on
the EQUITY outcome."** Rejecting it.

---

## Recommended Architecture: A — Post-Decide Overlay

Architecture A is the recommendation. It is the only one that:

1. Satisfies all locked principles structurally (options don't touch the
   TrustLedger; learning stays on equity outcomes).
2. Hooks at the natural seam (`_bound_submit` neighborhood in `run_cycle`).
3. Feeds `RiskBook` with delta-adjusted notional in the same cycle scope where
   the book lives (in-memory, closure-scoped — the only place it CAN be fed).
4. Is fully togglable by a single config bool with zero blast radius.
5. Has a clean P1→P2 flip (shadow log → real order, one config change).

---

## The 4 Most Important Tradeoffs / Hard Decisions

### 1. IV rank without historical IV data

"IV rank" is the right cheapness signal but requires 52-week IV history for the
ticker. Alpaca's options API returns current IV from the chain snapshot but does
NOT provide historical IV series. Options:

- **Approximation (recommended for P1/shadow):** Use ATM straddle cost as a %
  of stock price (`straddle_pct = (call_mid + put_mid) / stock_price`) and
  compare to a hand-calibrated threshold (e.g. straddle_pct ≤ 0.08 for ≥60d
  options is "reasonable IV"). This sidesteps historical data but is imprecise.
- **External source:** CBOE Historical Volatility (free), or VIX index as a
  macro proxy for when not to buy options (e.g. VIX > 30 → elevated market IV,
  avoid unless thesis is very high conviction).
- **Shadow period role:** In P1 shadow mode, log IV at decision time for every
  considered option. After 4–8 weeks of shadow data, you have your OWN IV history
  for the tickers you care about. Use it to retroactively calibrate the IVR gate.

Recommendation: Start with straddle_pct heuristic in P1 shadow. Build ticker-
specific IV history from shadow logs. Wire proper IVR gate before P2.

### 2. Delta-adjusted RiskBook fold: when to fold

The RiskBook is immutable and updated per-submit in `_bound_submit`. For P1
shadow mode: fold the delta-adjusted notional into the RiskBook in the shadow
log path anyway (even with no real order). This keeps the cap accounting honest
for simulation purposes and validates that the options sleeve doesn't silently
push gross exposure over `max_gross_pct`. For P2 paper mode: fold only after a
confirmed options fill, same as equity.

The critical point: the fold is in the closure scope in `_engine.py`. The
`_book: list[RiskBook]` container must be updated from `express_option()`
the same way `_bound_submit` updates it — via `_book[0] = _book[0].add(...)`.
This requires passing the `_book` container reference into `express_option`.

### 3. Options outcome track: what to measure and how to attribute

Options P&L is path-dependent and nonlinear. The equity `ResolvedOutcome` uses
alpha_bps (SPY-beta-adjusted return). Options need a different metric:

- `option_pl_pct`: `(exit_premium - entry_premium) / entry_premium`
- `underlying_alpha_bps`: the equity return over the same window (to verify the
  directional thesis was right even if IV crushed the option)

The key design decision: store BOTH, but only feed `underlying_alpha_bps` to
the equity trust loop (to maintain the locked principle). The `option_pl_pct`
feeds a separate `OptionsPerformanceTracker` with its own stats (Sharpe of
the options sleeve, win rate, average IV crush cost). This tracker is display-
only in P1 and informs go/no-go for P3 scaling.

The `parent_idea_id` FK in `options_ideas` is the critical link: every option
expression traces back to the equity idea that spawned it, so you can directly
compare "equity outcome" vs "option outcome" for the same thesis.

### 4. AlpacaAdapter extension: new class vs. modified class

The current `AlpacaAdapter.place()` speaks equity only (`POST /v2/orders` with
`symbol`, `qty`, `side`). Options orders need:
- `symbol` = OCC symbol (e.g. `AAPL240119C00150000`)
- `asset_class` = `"us_option"`  
- `qty` in contracts (not shares)
- Different legs for complex orders (out of scope for v1 long-only)

Recommendation: Create a new `AlpacaOptionsAdapter` class (new file
`execution/alpaca_options_adapter.py`) that inherits or composes the existing
HTTP helpers but has an `options_place(intent: OptionsOrderIntent)` method.
Do NOT modify `AlpacaAdapter` — the equity adapter is tested, working, and the
modification blast radius is too high. The `OptionsOrderIntent` dataclass mirrors
`OrderIntent` but adds `occ_symbol`, `contracts`, `option_type` (call/put).

In P1 (shadow), `AlpacaOptionsAdapter` is only used for chain fetches
(`GET /v2/options/contracts`), not order submission. This is safe and allows
the chain-fetch plumbing to be tested before P2.

---

## File Map (Architecture A)

New files:
```
arbiter/options/__init__.py
arbiter/options/chain.py         # AlpacaOptionsChainClient: fetch + filter chain
arbiter/options/selector.py      # OptionsSelector: gate + strike/expiry policy
arbiter/options/shadow_log.py    # OptionShadowLog dataclass + log writer
arbiter/options/tracker.py       # OptionsPerformanceTracker (display-only P1)
arbiter/options/risk.py          # delta_adjusted_notional() helper
```
New execution:
```
arbiter/execution/alpaca_options_adapter.py  # P2: options order placement
```
DB migrations:
```
arbiter/db/migrations/NNNN_options_tables.sql
    → options_ideas (id, parent_idea_id FK, ticker, occ_symbol, strike, expiry,
                      direction, entry_premium, entry_iv, entry_delta, entry_ivr,
                      contracts, decision_conviction, shadow_only, created_at)
    → options_outcomes (id, options_idea_id FK, exit_premium, exit_iv,
                        option_pl_pct, underlying_alpha_bps, closed_at, reason)
```
Modified files:
```
config.py                 # [options] section: _KNOWN_KEYS + Config fields
engine/_engine.py         # build_engine() wires express_option callable;
                          # run_cycle() calls it post-decide + folds RiskBook
arbiter.toml              # [options] stanza (all defaults off)
```

---

## Phased Build Plan (P1 first)

**P1 — Shadow only (safe, no paper orders):**
1. `options/chain.py` — fetch chain from Alpaca, filter by expiry + OI + spread
2. `options/selector.py` — gate logic + strike/delta selection + straddle_pct IV check
3. `options/shadow_log.py` — `OptionShadowLog` dataclass + JSONL audit writer
4. `options/risk.py` — `delta_adjusted_notional()` helper
5. Config additions (`[options]`, all disabled by default)
6. Engine wiring: `express_option()` closure + RiskBook fold (shadow mode)
7. DB migration: `options_ideas` table only (no outcomes yet)
8. Tests: gate logic, selector unit tests with mocked chain, shadow log format

**P2 — Paper orders (after shadow validation):**
1. `execution/alpaca_options_adapter.py` — `place_option()` via Alpaca options API
2. `options/tracker.py` — `OptionsPerformanceTracker` reads `options_outcomes`
3. DB migration: `options_outcomes` table
4. Engine wiring: real submit path behind `config.options_paper_enabled`
5. Outcome sweep: hook that closes options_ideas at expiry and writes options_outcomes

**P3 — Evaluate edge, then scale or retire.**
