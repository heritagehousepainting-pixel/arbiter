# Options Expression Layer — Research Findings

**Date:** 2026-06-25
**Role:** Research agent in 3-agent planning loop (RESEARCH → BRAINSTORM → PLAN)
**Read-only.** No production code written.
**Companion seed plan:** `docs/specs/2026-06-25-options-expression-layer-plan.md`

---

## 1. Hard Constraints

### 1.1 Alpaca Paper Options Support

**Confidence: ASSUMED-FROM-KNOWLEDGE + NEEDS-LIVE-CHECK.**

Alpaca added options trading support in late 2023 (broker API v2). Key facts:

| Item | Status | Confidence |
|---|---|---|
| Options available on paper accounts | YES — Alpaca explicitly markets paper options trading | ASSUMED-FROM-KNOWLEDGE |
| Account approval level required | Level 2 (defined risk / long options only) is needed for long calls/puts | ASSUMED-FROM-KNOWLEDGE |
| Paper accounts start at level 0 | Must be manually upgraded in the Alpaca dashboard | **NEEDS-LIVE-CHECK** |
| API base URL for options orders | Same `paper-api.alpaca.markets` base — `/v2/orders` with `asset_class: "us_option"` | ASSUMED-FROM-KNOWLEDGE |
| OCC option symbology | Standard OCC format: `AAPL240119C00150000` (ticker + date YYMMDD + C/P + 8-digit strike×1000) | ASSUMED-FROM-KNOWLEDGE |
| Multi-leg orders | NOT in scope (seed plan: spreads out of scope for v1) | — |
| Single-leg long calls/puts | Supported — `side: "buy"` + `type: "limit"` or `"market"` | ASSUMED-FROM-KNOWLEDGE |
| Options buying power (OBP) | Separate from equity buying power on Alpaca accounts | NEEDS-LIVE-CHECK |
| Options chain data (GET /v2/options/contracts) | Available in Alpaca's broker API; requires a data subscription or paper-tier access | NEEDS-LIVE-CHECK |
| Greeks from Alpaca data API | IV, delta, gamma, theta, vega available in `/v2/options/contracts/{symbol}` snapshots | ASSUMED-FROM-KNOWLEDGE |
| Open interest and volume | Available in the contracts endpoint | ASSUMED-FROM-KNOWLEDGE |
| Bid-ask spread for options | Available in snapshot quotes | ASSUMED-FROM-KNOWLEDGE |

**Hard blocker to verify first:** Does the paper account auto-provision for options, or does it require a manual application step in the Alpaca dashboard? This is the prerequisite for any P2 (paper execution) work.

**Config constraint:** `config.py` currently allowlists `paper-api.alpaca.markets` and loopback only (`_PAPER_HOST`, `_LOOPBACK_HOSTS`). The options data endpoint (`data.alpaca.markets`, already in `alpaca_data_base_url`) and the options trading endpoint (same paper base) are already permitted by the existing allow-list — no config surgery needed for the URL itself.

### 1.2 Executor Interface is Equities-Only

**VERIFIABLE-FROM-CODE.**

`arbiter/shared/executor.py` (line 1, comment): *"options-specific logic removed; equity-only interface kept."* The `OrderIntent` dataclass (`executor.py:26-30`) has no `asset_class` field; `AlpacaAdapter.place()` (`alpaca_adapter.py:173-225`) hardcodes the Alpaca `/v2/orders` body without `asset_class`, `option_type`, `expiration_date`, or `strike_price` fields. Any option order needs a parallel or extended order-placement path — the current `place()` cannot be extended in-place without breaking the equity path.

### 1.3 RiskBook Tracks Notional USD Only

**VERIFIABLE-FROM-CODE.**

`policy/book.py` — `RiskBook` holds `{ticker: usd_notional}`. For options, the economically meaningful exposure is **delta-adjusted notional** (option_delta × 100 × option_price × contracts × underlying_price), NOT premium paid. A call with delta=0.65 on 100 shares of a $200 stock has $13,000 of delta-adjusted exposure but might cost only $2,000 in premium. If the option premium is registered in the book as notional, the caps ($2k vs $13k) will be silent and the book will be silently under-measuring exposure. **This is the most dangerous integration risk.**

The seed plan identifies this (hard problem #3): "Feed delta-adjusted notional into the existing RiskBook." The fix requires that `_seed_risk_book()` and the cycle's `add()` call both receive delta-adjusted notional for options positions, which requires knowing the live delta at position inception.

### 1.4 Outcome Labeler is Equity Alpha Only

**VERIFIABLE-FROM-CODE.**

`evaluation/outcome_labeler.py:label()` computes `alpha_bps = log(exit/entry) - beta × log(spy_exit/spy_entry)` — a linear log-return formula. Options P&L is nonlinear (the payoff is a convex function of the underlying), path-dependent (theta decay runs daily even if direction is correct), and vol-dependent (IV crush can make a directionally correct position lose money). Using the equity labeler on option P&L would produce garbage alpha values that would poison advisor trust scores with nonsensical signals. **This is the correctness argument for "directional learning stays on the equity outcome" (seed plan locked principle #3).**

### 1.5 Config is Strictly Parsed — Any New Config Key Must be Declared

**VERIFIABLE-FROM-CODE.**

`config.py:113-125` — `_validate_toml()` raises `ConfigError` on unknown TOML keys. `_KNOWN_KEYS` covers `["core", "sizing", "storage", "alpaca", "edgar", "finnhub", "alerting", "daemon"]`. A new `[options]` section **must** be added to `_KNOWN_KEYS` or all options-related config will raise `ConfigError` on startup.

---

## 2. Integration Seams (with file:line refs)

The options expression layer sits **after `decide()`** in `_engine.py`. Here are the exact seams it touches:

### Seam A — After `_bound_decide` returns a PaperOrder (ENTRY POINT)
**File:** `arbiter/engine/_engine.py:650-760`

The expression gate reads `fusion.conviction`, `fusion.bucket`, `idea.horizon_days`, and `ticker`. It needs access to these to decide "is this thesis option-worthy?" The gate runs conceptually between `_bound_decide` returning an order and `_bound_submit` being called. In practice it needs a separate code path that:
1. Reads the same fusion output (already available in the `_bound_fuse` / cycle scope).
2. Calls the option chain data endpoint to check liquidity/IV.
3. Logs or (P2) submits the option order.

The equity path and option path are PARALLEL after `_bound_fuse` produces a `FusionOutput` — neither depends on the other's result.

### Seam B — RiskBook folding (EXPOSURE ACCOUNTING)
**File:** `arbiter/engine/_engine.py:741-750`

```python
_book[0] = _book[0].add(order.ticker, float(order.qty))
```

This fold happens after equity submit succeeds. For options, the fold must use **delta-adjusted notional** (described in §1.3 above), not premium paid. A separate `_options_book` (or extending RiskBook with a `delta_adjusted_notional` path) is needed so that options exposure is tracked correctly without contaminating the equity path.

### Seam C — AlpacaAdapter.place() (EXECUTION PATH)
**File:** `arbiter/execution/alpaca_adapter.py:173-225`

The `place()` method hardcodes an equity order body. For options it would need:
- `asset_class: "us_option"`
- `symbol`: OCC symbol string (e.g. `AAPL240119C00150000`) — NOT the equity ticker
- `qty`: number of contracts (integer)
- Options do NOT use `qty` as dollar notional; the sizing/submit path's notional→shares conversion (`submit.py:263`) also does not apply

A separate `place_option(intent: OptionOrderIntent)` method is the right design (not extending `place()`).

### Seam D — submit_order() notional→shares conversion (MUST BE BYPASSED)
**File:** `arbiter/execution/submit.py:258-283`

```python
shares = math.floor(notional / limit_price)
```

This equity-specific notional→shares conversion MUST NOT run for options. Option sizing is in contracts (typically 1–5 contracts for a shadow/paper sleeve), not shares. The `presized_shares` kwarg (`submit.py:183-188`) already provides a bypass for exit SELLs — the same bypass (or a new path) serves options entries.

### Seam E — Outcome tracking isolation
**Files:** `evaluation/outcome_labeler.py`, `evaluation/outcome_store.py`, `trust/ledger.py`

Option outcomes must go into a SEPARATE table (`option_outcomes`) or be tagged with `instrument_type="option"` in a new column, and must NEVER flow into the existing `outcomes` table that feeds the trust ledger. The trust ledger reads `outcomes` via `outcome_runner.run_outcome_sweep()` (`_engine.py:847-858`). Option rows in that table would produce nonlinear alpha values that corrupt advisor weights.

### Seam F — Shadow logging (P1 deliverable)
No existing "shadow log" table — a new `option_shadow_log` table (migration) is needed with: `ticker`, `underlying_price`, `strike`, `expiry`, `option_type` (call/put), `occ_symbol`, `contracts`, `premium`, `iv`, `delta`, `iv_rank`, `conviction`, `horizon_days`, `as_of`, `gate_passed`, `gate_reason`.

### Seam G — Config (new [options] section)
**File:** `arbiter/config.py:89-110`

New keys required (at minimum):
- `options_enabled` (bool, default False)
- `options_budget_pct` (float, the carved-out sleeve as % of equity)
- `options_min_conviction` (float, higher bar than equity `_MIN_CONVICTION=0.05`)
- `options_min_horizon_days` (int, ≥60)
- `options_max_contracts` (int, hard cap per trade)
- `options_max_iv_rank` (float, IV gate ceiling, e.g. 0.50)
- `options_target_delta` (float, e.g. 0.65 for ITM bias)
- `options_min_open_interest` (int, liquidity floor)

---

## 3. Options Pricing Fundamentals (Relevant to the Gate)

### 3.1 Implied Volatility and IV Rank

**Implied volatility (IV)** is the market's consensus forward-looking volatility embedded in an option's price (solved from Black-Scholes given the market price). It is NOT historical volatility.

**IV Rank (IVR)** = `(current_IV - 52w_low_IV) / (52w_high_IV - 52w_low_IV)`. A score of 0 = cheapest IV in the past year; 1 = most expensive. For a buyer of options:
- **Low IVR (< 0.30):** Cheap options — good time to buy long options.
- **High IVR (> 0.60):** Expensive options — IV crush risk is severe; avoid buying.

**IV Percentile (IVP)** is an alternative: what % of the past year's IV readings are below the current IV. Similar interpretation.

For arbiter's use: the seed plan correctly identifies "IV not extreme" as a gate criterion. With obtainable data (Alpaca options snapshots include `implied_volatility`), computing a rolling IV rank over 252 trading days is feasible but requires storing historical IV per ticker/expiry or using ATM-IV as a proxy. The cheapest correct proxy: track the at-the-money (ATM) IV of a reference 3-month contract over time and compare current reading to the trailing 252-day range.

**Data availability:** Alpaca's options data API returns `implied_volatility` per contract in the snapshot. **ASSUMED-FROM-KNOWLEDGE; needs a live API test.**

### 3.2 The "Right on Direction, Wrong on IV" Failure Mode

This is the most dangerous options failure. Example:

- Thesis: NVDA will rise 20% over 90 days (correct).
- Buy: 90-day ATM call when IV=80% (high, near earnings).
- What happens: NVDA rises 15%. But IV collapses from 80% → 30% post-earnings.
- Result: The call is WORTH LESS than when purchased despite the underlying moving in the right direction.

The collapse of IV (vega × ΔIV > delta × ΔS) destroys the position's value. This is "IV crush."

**Detection / avoidance with obtainable data:**
1. **IV Rank gate:** If IVR > 0.50 (elevated), reject. Prefer IVR < 0.30 for buying.
2. **Avoid buying into known catalyst events** (earnings dates, FDA rulings) unless the thesis IS the catalyst. For arbiter's signals (13D/Form-4 disclosure), the catalyst is already public — IV may already be elevated from speculation. Check: is IV spiking BECAUSE of the filing we just saw? If so, the market has already priced it and buying now is buying elevated IV.
3. **Long-dated (LEAPS-style) options partially mitigate IV crush:** A 6-month option has much lower vega exposure per unit time than a 1-month option. A 10-point IV drop hurts a 6-month option far less than a 1-month option.

### 3.3 Why Long-Dated Options Are Preferred for Slow Directional Signals

For arbiter's signals (90–180 day horizons):

- **Theta (time decay):** Daily theta for a 6-month option is roughly 1/6th the daily theta of a 1-month option for the same strike. Time is not the enemy on a long-dated option — it is manageable.
- **Breakeven math:** A 6-month call needs the stock to rise above `strike + premium` by expiry. With ≥60-day expiry, the daily breakeven bar is much lower.
- **Delta stability:** A deep-ITM option (delta ≈ 0.80) moves almost dollar-for-dollar with the stock. The deeper ITM it is, the more it behaves like a levered stock position.
- **IV sensitivity (vega):** Long-dated options have higher vega, meaning they're MORE sensitive to IV changes. But the RELATIVE impact of a 10-point IV drop is smaller for a longer option because the baseline time value is larger.

**Practical delta targeting:**
- **~0.70-0.80 delta** (ITM calls/puts): Highest delta exposure, least extrinsic value, closest to a levered stock position. Theta is minimal as % of premium.
- **~0.50 delta** (ATM): More balanced between intrinsic and extrinsic; IV crush risk higher as a % of price.
- The seed plan's "0.6–0.7 delta ITM calls" is directionally correct. For the risk-defined, equity-like-directional mandate, **0.70–0.80 delta with ≥90-day expiry** is closer to optimal.

### 3.4 Breakeven Math

For a long call at strike K, premium C:
- **Breakeven at expiry** = K + C (underlying must exceed this to profit)
- **Expected move vs. breakeven:** If the council has a +20% conviction over 90 days and the stock is at $100, strike at $95 (delta~0.75) with $12 premium → breakeven at $107 (7% move needed). This is well inside the expected move. The gate should reject if the breakeven requires more than the council's expected move to achieve.
- A rough expected move from the option chain itself: ATM straddle price ≈ market's expected 1-sigma move over the life of the option.

---

## 4. Best Practices: Expressing Slow Directional Signals via Long-Dated Options

From standard practice in systematic options trading:

1. **Strike selection:** Target the 0.70-0.80 delta strike for the chosen expiry. At this delta the option is well-ITM, has minimal extrinsic value (theta is slow), and moves nearly dollar-for-dollar with the underlying.

2. **Expiry selection:** Choose the next listed monthly expiry that is at least 30 days BEYOND the thesis horizon. This ensures there is no expiry pressure during the expected holding period. For a 90-day thesis, choose the 4-month expiry (≈120 days); for 180 days, the 7-month expiry.

3. **Liquidity filter:**
   - Open interest ≥ 100 contracts on the target strike/expiry
   - Bid-ask spread ≤ 5% of mid (i.e., (ask-bid)/mid < 0.05)
   - Volume ≥ 10 contracts on the day (for paper, this is advisory only)

4. **IV check:** IVR < 0.40 at time of entry. This is a hard gate, not a soft preference.

5. **Position sizing:** For a paper sleeve, 1–3 contracts max per idea, with total premium at risk ≤ `options_budget_pct × equity / max_concurrent_ideas`.

6. **Exit rules (distinct from equity):**
   - Stop: exit if premium falls 50% (hard premium stop, not stock price stop)
   - Horizon: close no later than 30 days before expiry to avoid gamma/theta acceleration
   - Profit: close at 2× premium paid (lock the gain)
   - Reversal: council conviction reversal still applies (same as equity path)

---

## 5. Open Technical Risks

### Risk 1 — Delta-adjusted notional for RiskBook (HIGH)
If options are registered by premium in `RiskBook`, the sector/gross caps become meaningless because a $2k premium can control $13k of delta exposure. **This is a silent safety bypass.** Requires either extending `RiskBook` to carry a parallel `delta_adjusted` accumulator, or creating a separate `OptionsRiskBook`. Either path requires also seeding the delta-adjusted exposure from any live option positions at cycle start in `_seed_risk_book()` (`safety_ops.py:201-218`).

### Risk 2 — Options-specific exit monitor (MEDIUM)
The current exit monitor (`execution/exit_monitor.py`) is equity-centric: it checks price against stop_loss, horizon_expiry, and conviction. Options need different exits: premium value (50% stop on premium paid), days-to-expiry floor (30-day cutoff), and the same conviction reversal. A separate `exit_monitor_options.py` or branching logic is needed.

### Risk 3 — OCC symbol generation + expiry selection algorithm (MEDIUM)
The current codebase has no calendar of listed option expiries. To select the right expiry, you need to know what monthly (or weekly) expirations are actually listed for a given ticker. This must come from the Alpaca options chain API (`GET /v2/options/contracts?underlying_symbols=AAPL`). Without this, selecting expiry is a guessing game. The implementation must call the chain API, filter to ≥ thesis_horizon + 30 days, and pick the nearest qualifying expiry.

### Risk 4 — IV rank computation requires historical IV storage (MEDIUM)
Computing IV rank requires trailing 252-day IV data. Alpaca's real-time snapshot gives current IV but does not (as far as known) serve historical IV time-series for an arbitrary contract. Options will need: (a) a daily cron that snapshots the ATM IV for each watchlist ticker and stores it in a new `option_iv_history` table, OR (b) use a proxy like VIX/sector ETF implied vol, OR (c) use the 30-day historical vol of the underlying as a rough IV-rank substitute (computable from the existing equity PIT data). **Option (c) is the lowest-friction P1 approach; (a) is correct for P2.**

### Risk 5 — The PaperOrder contract is equity-specific (LOW-MEDIUM)
`contract/seams.py:PaperOrder` has `ticker: str` (equity ticker), `qty: float` (notional USD), `side: OrderSide`, and `exits` (equity-style). Options need a different order type with `occ_symbol`, `contracts: int`, `option_type`, `strike`, `expiry`, `premium`. Creating a parallel `OptionShadowRecord` (for P1 shadow) and `OptionPaperOrder` (for P2 execution) is cleaner than extending `PaperOrder`.

### Risk 6 — Options position in get_positions() (LOW)
`AlpacaAdapter.get_positions()` (`alpaca_adapter.py:321-340`) calls `/v2/positions` and returns `PositionSnapshot(ticker, shares, avg_price)`. Options positions appear as separate entries in the Alpaca broker (with OCC symbols as the "ticker"). `_seed_risk_book()` iterates `get_positions()` — without filtering, options positions would be passed to `position_market_value()` which tries to call `current_price(ticker)` on an OCC symbol, which will return None and fall back to avg_price (the premium paid, not delta-adjusted exposure). This is Risk 1 in disguise at the seed step.

---

## 6. Unknowns Needing a Live Check

| Unknown | Where to Check | Urgency |
|---|---|---|
| Does the paper account support options without a manual approval step? | Alpaca dashboard → Paper account → Account level | HIGH — gates all P2 work |
| What data subscription tier is required for `/v2/options/contracts` snapshots on paper? | Alpaca dashboard → Subscriptions | HIGH |
| Does `/v2/options/contracts` return IV and greeks for paper accounts? | Curl test against paper account | HIGH |
| What is the exact OCC symbol format accepted by Alpaca's paper trading endpoint? | Alpaca options trading docs | MEDIUM |
| Does `/v2/positions` return options positions with OCC symbols? | Test with a paper options position | MEDIUM |
| Is there a rate limit on the options chain endpoint that would block per-cycle calls? | Alpaca rate limit docs / live test | MEDIUM |
| Does historical IV exist anywhere in Alpaca's data API, or must we track it ourselves? | Alpaca data API docs → historical options data | MEDIUM |

---

## 7. Architecture Implications Summary

The five facts that most constrain the architecture:

1. **The executor contract is equity-only** — options need a parallel execution path (`OptionAlpacaAdapter` or `place_option()` extension), NOT an extension of the equity `OrderIntent`. Mixing them risks the equity path's idempotency and notional-unit assumptions.

2. **RiskBook must use delta-adjusted notional for options** — premium-paid notional silently bypasses all exposure caps. Delta must be fetched from the chain at entry time and stored with the shadow/order record.

3. **Outcome tracking MUST be isolated** — option P&L is nonlinear and must never enter the `outcomes` table that drives the trust ledger. A separate `option_outcomes` table (or `instrument_type` column with strict filtering in `outcome_runner`) is mandatory.

4. **IV rank computation needs historical IV storage or a proxy** — the gate's "IV not extreme" criterion requires a rolling context window. For P1 shadow, historical realized volatility of the underlying (already computable from equity PIT data) is the zero-new-data proxy. For P2+, a daily ATM IV snapshot cron is required.

5. **IV check on Alpaca paper needs live verification** — Alpaca's paper options data availability (whether `/v2/options/contracts` returns IV and greeks without a premium data subscription) is the single most important unknown. If greeks require a paid data tier, the IV gate has no data source on paper and must use historical vol proxy throughout.

6. **Config strict-parse will reject any undeclared options section** — the `_validate_toml()` function raises `ConfigError` on unknown keys; a new `[options]` TOML section must be added to `_KNOWN_KEYS` in `config.py:89-110` before any options config can be loaded.

7. **Submit pipeline's notional→share conversion must be bypassed for options** — `submit.py:263` computes `shares = floor(notional / limit_price)`, which is meaningless for options contracts. Either a `presized_contracts` path (parallel to existing `presized_shares` bypass) or a wholly separate submission function is needed.
