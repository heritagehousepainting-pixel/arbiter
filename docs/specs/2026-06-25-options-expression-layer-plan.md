# Options Expression Layer — seed plan

**Date:** 2026-06-25 · **Status:** FINALIZED (3-agent research/brainstorm/plan loop complete — build-ready)

Research/brainstorm/plan agent notes: `docs/specs/research/2026-06-25-options-{research,brainstorm,plan}.md`.
The finalized, build-ready synthesis is the last section of this file and supersedes the seed where they differ.

This is the *starting* plan the council of agents will pressure-test and perfect. It
captures the architecture agreed in discussion; everything under "Open questions" and
"Hard problems" is deliberately unresolved for the loop to solve.

## Goal
Add an **options "expression layer"** so that high-conviction directional theses the
existing council already produces can be expressed as **long-dated (≥2 month) long calls /
puts**, tested entirely in paper. This is NOT a new directional brain — it is a
leverage/expression overlay on theses that A1/A2/A3 already score and that the learning
loop already evaluates on the underlying equity.

## Locked principles (do not relitigate)
- **Long calls/puts ONLY.** Defined risk = premium paid. No short options, no naked legs,
  no assignment / unlimited-loss tail. (Spreads are out of scope for v1.)
- **EVERY option play is long-dated — ≥2 month expiries (LEAPS-style), no exceptions.**
  There is no short-dated play and no special "tier"; the only options the system ever
  buys are long-term calls/puts matched to the disclosure-signal horizons (the advisors
  already run 90–180 day horizons; options express the same slow thesis). The gate decides
  *whether* a thesis is worth expressing as an option at all — not which kind.
- **Directional learning stays on the EQUITY outcome.** Option P&L is nonlinear / IV-path
  dependent and must NOT pollute advisor trust scores. Options outcomes are a separate track.
- **Shadow-first.** Log "would-have-traded" with full reasoning before any paper order.
- **Isolated.** Zero impact on the existing equity path; separate budget + delta-adjusted
  risk caps; can be turned fully off by config.

## Architecture (seed — refine me)
```
council (A1 insider/activist/fund · A2 mirofish · A3 news)
   → idea + direction + conviction + horizon
   → decide (existing equity sizing/gating)
        ├─ equity order            (existing path, unchanged)
        └─ options-expression gate (NEW)
              → strike/expiry selection
              → SHADOW log  |  paper option order (later phase)
```
The expression layer sits **after `decide`**, reading the same thesis. It does not vote.

## The gate — "is this thesis option-worthy?" (refine the criteria + thresholds)
- Conviction above a (higher) threshold — council agreement + magnitude.
- Horizon ≥ 2 months.
- A catalyst with a timeframe (activist 13D, insider cluster buy, etc.).
- Liquid options chain (open interest / volume, acceptable bid-ask).
- **IV not extreme** — don't overpay; needs an IV-rank / cheapness check.
- Premium-aware: expected move must clear the option breakeven, not just be directional.

## Hard problems the loop MUST solve (the real work)
1. **Implied-volatility modeling.** "Right on direction, wrong on IV (IV crush)" is the
   classic options failure. How do we gauge option *cheapness* with the data we can get?
2. **Strike/expiry selection policy.** e.g. ~0.6–0.7 delta ITM calls (equity-like delta,
   limited downside) with expiry ≥ thesis horizon so theta is slow. Codify it.
3. **Risk accounting.** Feed **delta-adjusted notional** into the existing `RiskBook` /
   gross & sector caps, or the caps become meaningless and the book is silently over-levered.
4. **Data + execution.** Alpaca options: chains, greeks, IV availability, OCC symbology,
   order path, option buying power — on PAPER. The current `AlpacaAdapter` is equities-only.
5. **Outcome / learning separation.** Nonlinear, path-dependent P&L → a distinct outcome
   model and attribution track, isolated from the equity learning loop.
6. **Sizing / budget.** A small carved-out paper sleeve while testing.

## Open questions (need the user)
- **Definition of "3+ option play"** — 3× potential return? 3+ contracts? a conviction-score
  tier? This drives the entire gate.
- Size of the options paper sleeve (% of the book).

## Phasing (seed)
- **P1 — Shadow (build first, zero risk):** wire options data + the gate + selection; log
  "would-have-bought X" for every qualifying idea. Validate against real chains for weeks.
- **P2 — Paper execution:** long calls/puts only, small budget, delta-adjusted caps,
  separate outcome tracking.
- **P3 — Evaluate edge** before any scale. (Note the sequencing tension: options leverage an
  edge the equity learning loop has not yet *proven* — shadow keeps plumbing ready meanwhile.)

## Deliverable of the loop
A finalized architecture + phased build plan (files, interfaces, config flags, test
strategy, data flow, isolation guarantees) ready to hand to a build wave — consistent with
this repo's existing patterns (`engine/`, `execution/`, `trust/`, `data/`, migrations,
`docs/specs/`).

---

# FINALIZED ARCHITECTURE — loop synthesis (2026-06-25)

All three agents independently converged on the same shape, which gives high confidence.
Where they differed, the resolution is noted.

## Architecture (locked)
**Post-`decide` overlay.** A new `arbiter/options/` package exposes `express_option()`, called
in `engine/_engine.py::run_cycle` immediately AFTER the equity `decide`/submit path, reading the
same `fusion_output` + `idea`. It does NOT vote and does NOT alter equity behavior. Gated by
`config.options_mode ∈ {"off","shadow","paper"}`, **default `"off"` → total no-op**.

- **Separate executor** — new `AlpacaOptionsAdapter`/`AlpacaOptionsClient` (NOT a modified
  `AlpacaAdapter`): options need a different body (`asset_class="us_option"`, OCC symbol, qty in
  contracts), and the equity `place()` hardcodes an equity body (`alpaca_adapter.py:173-225`).
  Shares only HTTP helpers. In P1 `place()` raises `NotImplementedError` (chains-only).
- **Risk: delta-adjusted notional, never premium.** `policy/book.py` gets `add_option_delta()`;
  exposure = `|delta| × 100 × underlying_price × contracts` folds into the SAME in-memory
  `RiskBook` (`_book` closure container) so gross/sector/open-position caps stay binding. A $2k
  premium can be $13k of delta exposure — registering premium would silently bypass the caps.
- **Outcomes: fully isolated.** New `option_outcomes` table; store BOTH `option_pl_pct` (→ a
  display-only options tracker) and `underlying_alpha_bps` (the ONLY field linking to the equity
  `TrustLedger`, for direction-validation). Option rows NEVER reach `run_outcome_sweep()` — their
  nonlinear/IV-path P&L must not corrupt advisor trust scores.
- **Submit bypass** — contracts skip the `shares = floor(notional/limit_price)` conversion
  (`submit.py:263`) via a `presized_contracts` path (parallel to the existing exit `presized_shares`).
- **Config** — new `[options]` TOML section; keys MUST be pre-registered in `_KNOWN_KEYS`
  (`config.py` strict-parse rejects unknown keys otherwise).

## The gate — `options_expression_gate(...) -> OptionGateDecision`
Express a thesis as an option only when ALL hold (thresholds are config, tunable):
- conviction ≥ **1.5×** the equity entry threshold (high-conviction only);
- thesis horizon ≥ **60 days**;
- a catalyst tag is present (activist 13D, insider cluster buy, etc.);
- liquidity: open interest ≥ 100 and volume ≥ 10 on the chosen contract;
- **IV not extreme** (see IV section);
- premium-aware: expected 1σ move clears the breakeven by ≥ 5%.

There is NO "tier" and NO short-dated path — when the gate fires, the play is ALWAYS a long-dated
long call/put. The gate only decides *whether*, never *what kind*.

## Contract selection — `select_contract(...)`
- **Target delta 0.70–0.80 (deep ITM).** RESOLUTION of the 0.60–0.70 vs 0.70–0.80 split: take the
  deeper ITM per the research — max delta (equity-like), minimal extrinsic/theta, and the best
  IV-crush resistance for a slow disclosure thesis. (Tunable; revisit with shadow data.)
- **Expiry ≥ thesis_horizon + 30 days**, capped at horizon + 180d, and always ≥ 60d — the option
  must never expire during the expected holding period.
- Call for bullish theses, put for bearish.

## Exit (options track, separate from the equity `exit_monitor`)
- **Premium-based stop (e.g. close at −50% of premium)**, NOT a stock-price stop — option P&L is
  nonlinear. Plus horizon/expiry management and a conviction-reversal cover. Managed entirely on
  the options track so it can't entangle equity exit logic.

## Implied volatility — the data gap (resolved for P1)
Alpaca's snapshot gives *current* IV but no 52-week history, so an IV-rank gate has no source yet.
- **P1 proxy (zero new data):** realized volatility of the underlying (computable from existing
  equity PIT data) + an ATM-straddle-cost heuristic (`straddle/price ≤ threshold`).
- **From day 1:** a daily ATM-IV snapshot writes to a new `option_iv_history` table, so a proper
  IV-rank gate (target IVR < 0.40) is ready by P2 from data we accumulated ourselves.

## ✅ P0 SPIKE — RESOLVED (live read-only check, 2026-06-25)
The highest-urgency unknown is answered favorably — the design stands.
- **Account is options Level 3 approved** (`options_trading_level: 3`); we only need Level 1 for
  long calls/puts. No approval step needed.
- **Chains:** `paper-api /v2/options/contracts` returns contracts (symbol, type, strike, expiry,
  open_interest) on the free tier.
- **IV + greeks + quotes:** the **free `indicative` feed** of
  `data.alpaca.markets/v1beta1/options/snapshots` returns `impliedVolatility`, `greeks`
  (delta/gamma/theta/vega), and bid/ask. Verified on a 90d near-ATM AAPL call: IV 0.3844, delta
  0.8706, bid/ask 51.44/52.45. (Deep-ITM contracts expiring next day return null greeks — expected;
  the selector targets ≥60d so this is a non-issue.)
- **The paid `opra` feed is NOT available** (`"OPRA agreement is not signed"`). The free
  `indicative` feed is sufficient for shadow + paper; OPRA is only a future live-trading nicety.
- IMPLICATION: the IV gate has a real data source. We still get only *current* IV (no history), so
  IV-rank requires accumulating daily snapshots as planned (`option_iv_history`); the realized-vol
  proxy covers P1 cold-start. CONTRACT SELECTOR must read `delta` from the indicative snapshot
  (use `feed=indicative`).

## Phasing
- **P1 — Shadow (zero trading risk):** full path wired; every qualifying idea writes a
  "would-have-traded" row to `option_shadow_log`; `place()` is a hard `NotImplementedError`. Run for
  weeks; validate gate, contract selection, and IV-data quality against real chains. New package
  `arbiter/options/` (`types, gate, contract_selector, sizing, shadow_log, alpaca_options_client,
  iv_history`), migration `029_options_shadow.sql`, the `_engine.py` one-branch hook, and
  `policy/book.py::add_option_delta`. Offline-first tests; no-lookahead + insert-only linters clean.
- **P2 — Paper execution:** same path, `place()` live against Alpaca paper, gated by
  `options_mode="paper"` AND an explicit approval record (mirrors equity `gate_approvals`).
  Delta-fold goes live; `option_outcomes` + premium-stop exit management land here.
- **P3 — Evaluate (protocol, not a build):** after ≥30 closed option positions, decompose returns
  into direction × IV-crush × theta × timing. If average P&L is negative, auto-revert to shadow.

## Isolation guarantee
`options_mode="off"` (the default) makes the entire layer inert — zero behavioral change to the
equity decision/execution/learning paths. The layer can be killed instantly by flipping one flag.

## User decisions (RESOLVED 2026-06-25)
1. **Options sleeve budget = 35% of the paper book**, measured as **premium at risk** (max
   aggregate premium the options layer may hold). `sizing.py` enforces this ceiling. NOTE: this
   is a *premium* cap; actual market exposure is still bounded by the delta-adjusted-notional fold
   into `RiskBook` (gross/sector caps), so a full sleeve cannot exceed the book's risk limits.
2. **Target delta CONFIRMED: 0.70–0.80 (deep ITM).**

---

# BUILT — P1 + P2 (2026-06-25)

Built in one /goal+/loop session as foundation → parallel sonnet-agent waves → I owned
the integration spine + audits. **Full suite 2704 green; no-lookahead + insert-only + options
ruff clean; `options_mode="off"` (default) is a proven no-op.**

New package `arbiter/options/`: `types`, `alpaca_options_client` (chains + indicative IV/greeks +
buy `place()` + sell `close_position()`), `gate`, `contract_selector` (0.70–0.80 ITM), `sizing`
(35% premium sleeve), `iv_history` (IVR + realized-vol proxy), `shadow_log`, `positions`
(insert-only; openness = absence of an outcome), `outcomes` (isolated `option_outcomes`), `exit`
(premium-stop/horizon/reversal decision), `manage` (close loop), `express` (orchestrator).
Migrations 029 (shadow+iv_history), 030 (outcomes), 031 (positions). Engine hook in
`engine/_engine.py` via a `try/finally` `express` callback in `orchestrator/cycle.py`; delta-adjusted
notional folds into the live `RiskBook` in PAPER mode only (shadow never touches the equity book).

**P1 live-verified** against a real Alpaca chain (AAPL → Dec-2026 $250 call, 0.75 delta, shadow row).
**Bugs caught by live/lint during integration:** 2 no-lookahead violations (`date.today()`/`now()`),
and the `volume=None` filter that made the layer inert (Alpaca's contracts endpoint omits volume; OI
is now the binding liquidity check).

**Isolation / go-live:** entirely gated by `config.options_mode ∈ {off,shadow,paper}`, default
**off**. Flipping to `shadow` (log-only) or `paper` (real paper options orders) is a deliberate
user go-live step — NOT flipped. `paper` mode IS the approval gate (conscious config change), under
the same kill-switch/safety gate as equity.

**Known P2 follow-ups (non-blocking):** exit management runs at full-cycle cadence (premium-stop not
checked intraday in fast-iterations yet); reversal-exit uses original conviction (no per-cycle
re-fuse of held tickers); `open_options_premium` sleeve usage is summed from open positions.
