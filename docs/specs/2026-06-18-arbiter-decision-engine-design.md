# Arbiter — Smart-Money Decision Engine

**Design spec** · Status: **Planning only (nothing built)** · Date: 2026-06-18

---

## 0. One-paragraph summary

`arbiter` is a local-first Python system that follows people whose trades are publicly
disclosed (SEC Form 4 corporate insiders + members of Congress), **scores how their picks
actually perform over time**, and combines that with other independent "advisors" — a
MiroFish agent-swarm that war-games an idea, and a live-news/X tip layer — into **one
decision engine** that decides, sizes, and (eventually) executes trades. **No single
advisor is "truth."** Truth is only what the decision engine concludes after weighing all
advisors by their proven track record. Everything is paper-first; real money is a later,
hard-gated phase.

---

## 1. Origin & key reality-checks (research that shaped this)

The concept began as a "smart-money tracker": follow disclosed trades, measure returns,
then auto-follow the proven winners (via Robinhood/Coinbase). Multi-agent research
surfaced several findings that materially changed the design:

- **Robinhood has no equity-trading API.** Execution path is **Alpaca (paper) → IBKR
  (live)**. Robinhood is out for stocks. (FINRA's pattern-day-trader $25k rule was
  eliminated June 2026, so small accounts aren't constrained.)
- **"Score people, then follow the winners" is only half-right.** For **Congress**, top
  performers reshuffle every year and the **~45-day disclosure lag erases most alpha** —
  demoted to a slow sector/theme signal. For **insiders**, the edge is **event-specific,
  not person-specific** (opportunistic cluster buys, conviction size, no 10b5-1 plan), so
  the engine scores **both signal-types AND people, side by side**.
- **Data is free and feasible** (SEC EDGAR, House/Senate disclosures) but has landmines:
  yfinance is broken (use Alpaca + Stooq), 10b5-1 plans must be filtered, Form 4/A
  amendments double-count if appended naively, Congress reports **ranges not amounts**,
  survivorship bias inflates everyone's returns, and entry must be scored at
  filing-date+1 (no look-ahead).
- **This already exists as a consumer product** (Autopilot ~$750M AUM, dub). The genuine
  differentiator is **proof-of-edge before copying** + surfacing the disclosure-lag cost
  nobody else shows.
- **MiroFish** (open-source AI swarm-simulation engine, GitHub `666ghj/MiroFish`, AGPL,
  ~v0.1, no published benchmarks) is **not a financial oracle** and not trustworthy as a
  standalone signal. But used correctly — as **one fallible advisor that war-games a
  single idea** and whose vote the engine discounts by track record — it fits. It is
  **firewalled to structured/filing data only** so it stays independent from the news/X
  advisor, and is **forward-test-only** (can't be cheaply backtested).

---

## 2. Architecture: a council of advisors → one decision engine

```
Advisors emit raw (score, confidence, horizon, as_of, source_fingerprint, run_group_id)
   → Calibration (per advisor): raw → calibrated probability   [Platt <200 outcomes, isotonic after]
   → Fusion (per horizon-bucket): log-opinion-pool, weighted by trust, deflated by correlation
   → Conviction = signal-strength × (effective_N / N)  − lone-bull tax
   → Policy: quarter-Kelly × hard caps × ADV-cap → paper order (gated)
   → Outcome labeled at horizon (SPY-beta-adjusted alpha) → feeds trust + calibration
```

Three tiers of provenance:
1. **Tip layer (radar):** live, noisy, unverified leads (X/news). A tip alone is worth zero.
2. **Verification layer (truth oracle):** disclosures + price/volume action. Nothing counts
   until corroborated here.
3. **Execution layer:** Alpaca paper → IBKR live. Acts only on verified + scored signals.

### The advisors

| ID | Advisor | Perspective | Clock |
|----|---------|-------------|-------|
| **A1** | Smart-money tracker (Form 4 insiders + Congress) | positioning ("what smart money is doing") | insiders ~2-day lag; Congress ~45-day lag |
| **A2** | MiroFish swarm | scenario/reaction ("how the crowd reacts to this idea") | ~15–20 min/run, on-demand, expensive |
| **A3** | Live-news/X tip layer | real-time catalyst | seconds–minutes (built later) |

More advisors can be added behind the same contract without changing the engine.

---

## 3. The decision engine (converged design)

### 3.1 Advisor contract (Lane 1)

Each opinion carries: directional `stance_score` ∈ [-1,1], `confidence` (with a **source
tag**: empirical / modeled / self-reported / none), `horizon`, `as_of` (**information
timestamp, not wall-clock**), `rationale`, `source_fingerprint` (for correlation
detection), and `run_group_id` (so a multi-opinion advisor like MiroFish isn't
double-counted).

- **Abstain is `None`, not `0.0`.** Absence of evidence ≠ confident neutral.
- Advisors emit a **raw** directional signal only. They do **not** output calibrated
  probabilities — calibration is owned downstream.

### 3.2 Calibration (Lane 3, upstream of fusion)

Per-advisor (and horizon-stratified for A3): raw score+confidence → **calibrated
probability**. Platt scaling under 200 outcomes, isotonic regression at 200+. A
hard-coded `STANCE_BASE` table is the **cold-start prior only**, replaced by the
calibrator once data exists. Calibration owns raw→probability; fusion owns pooling.

### 3.3 Fusion (Lane 2)

- **Log opinion pool**, weighted by trust, with **correlation deflation** and a
  **hard-veto** layer. Deliberately **not** a meta-learner (would overfit the tiny
  outcome set for a year+).
- **Per horizon-bucket** — never average a 1-day news view with a 60-day insider view.
- **Conviction = signal-strength × diversity-factor − lone-bull tax**, where
  `diversity_factor = effective_N / N` and
  `effective_N = 1 / (Σᵢ Σⱼ wᵢ wⱼ ρᵢⱼ)`. Three bots reading the same filing →
  effective_N ≈ 1.1 → conviction cut ~60%. A **lone-bull tax** fires when advisors are
  unanimous *and* correlated *and* a dissenter exists.
- Same-run opinions (same `run_group_id`, same bucket) are merged to **one** logical
  opinion; same-run opinions in *different* buckets are independent (the MiroFish
  short+medium case).

### 3.4 Trust-weighting (Lane 3)

- **Composite trust = geometric mean of skill × calibration × coverage**, recency-weighted.
  The **coverage** term defeats "abstain-on-hard-calls" gaming.
- Skill = recency-weighted inverse **Brier** on non-abstain predictions.
- **Weight caps:** sample-gated ceiling topping at **0.50**; **MiroFish hard-capped at
  0.35 forever**; negative-skill advisors → **0.00** + diagnostic hold (manual re-enable);
  thin-sample positive floor **0.02**.
- **Onboarding:** new advisors run **shadow mode** (recorded, zero live weight) until ~30
  resolved outcomes, then a probationary ramp.
- **Non-stationarity:** exponential forgetting, **26-week half-life**; weekly updates
  (≥5 new outcomes to trigger); **21-day freeze** on regime-change, then post-regime
  outcomes weighted 2×.

### 3.5 Decision policy & execution (Lane 4)

- **Sizing:** quarter-Kelly × hard caps. Caps: 5% per name, 20% per sector, 80% gross,
  20 open positions, plus a **2%-of-20-day-ADV liquidity cap** (so the engine can't move
  the signal it reads). Calibration confidence multiplies size (small until proven).
- **Fail-closed everywhere.** `LIVE_TRADING=false` default; Lane-8 unreachable → no trade.
- **Exits defined at entry** (stop-loss, horizon expiry, conviction-reversal), stored
  transactionally with the position; evaluated by a Lane-5 sweep (60s).
- **Idempotent orders:** ULID primary key + a `dedup_hash`
  (ticker+side+horizon+entry_date+advisor_signature) unique constraint; pre-submit check
  against local ledger **and** broker. Max **1 retry, then halt + alert.**

### 3.6 Idea lifecycle & async timing (Lane 5)

- An **`Idea`** object (ticker + thesis + horizon) with a state machine:
  NASCENT → GATHERING → PROVISIONAL_DECIDED → FINAL_DECIDED → EXECUTED → MONITORED →
  OUTCOME_READY → CLOSED (or ABANDONED).
- **Provisional decision** from fast advisors; **revised** when slow MiroFish reports.
  Post-execution revision rule: **EXIT** if conviction flips sign or drops <0.25,
  **REDUCE 50%** if <0.50, else **HOLD**. Never size up on revision.
- **MiroFish invoke/skip:** always for SWING/LONG; for DAY only if >30 min to entry;
  never for INTRADAY or a NEWS_CATALYST <5-min window.
- **Dedupe key = (ticker, horizon_bucket).** Concurrent ideas on one ticker across
  *different* buckets are allowed (capped at MAX_TICKER_EXPOSURE).
- **Congress horizon measured from filing date**, 7-day entry window, else expired-on-arrival.

### 3.7 Evaluation & feedback (Lane 6)

- **Point-in-time replay** only (no look-ahead). **MiroFish is forward-test-only + cached**
  (non-deterministic, expensive). Walk-forward (5+ windows), **deflated Sharpe**, ablations
  (each advisor must earn its place), and **paper trading as the true out-of-sample**.
- Attribution via Shapley for ≤5 advisors; simple per-advisor outcome tracking as the
  early baseline. Watch the correlation problem.

### 3.8 System architecture (Lane 7)

- **New sibling project `arbiter`** next to `polybot`/`stockbot`. **Reuse** stockbot's
  Executor/SimExecutor, risk-gate + kill switch, dashboard skeleton, experiment registry.
  **Build new:** advisor contract, fusion, trust ledger, idea/outcome stores, orchestrator.
- **MiroFish called over HTTP only** (AGPL isolation — never `import mirofish`).
- **Scheduled loop** (not a daemon); advisors invoked via a bounded thread pool with
  **fault isolation** (a timed-out/crashed advisor yields a null opinion, engine continues).
- Storage: **single `arbiter.db` (SQLite, WAL mode)** + append-only `audit.jsonl`
  (authoritative if they diverge). All data rows insert-only; corrections are new rows
  with `supersedes_id` + `is_superseded`.

### 3.9 Safety (Lane 8 + consolidated)

- **Quorum:** 2+ live advisors → 100% size; 1 → 25% (DEGRADED); 0 → no new positions (HALTED).
- **Circuit breakers** (latching, infrastructure-level, can't be disabled by advisor/fusion
  code): daily loss ≥2%, per-position −5% intraday, MiroFish 3× consecutive failures,
  A3 volume anomaly on a held name, any broker non-200, confidence-distribution shift >30%.
- **Kill switch:** **broker-side** (works even if the Python process is dead),
  phone-reachable, halts new orders but does **not** auto-close positions, **tested monthly**.
- **Degradation ladder** Levels 0–4; Levels 3–4 supersede.
- **Alerting** tiers info/warning/critical (critical → push + auto-pause), tested weekly.

---

## 4. Locked foundational definitions (the keystones)

### 4.1 Outcome label — "what is correct?"
**SPY-beta-adjusted alpha** over the advisor's stated horizon (NOT absolute return).
`alphaᵢ = Rᵢ(t0,t1) − betaᵢ · R_SPY(t0,t1)`, betaᵢ = 252-day rolling beta as of `t0−1`
(imputed to 1.0 + flagged if unavailable). Entry = **filing-date+1 open, net of modeled
slippage** (5bps + 0.5×spread). Continuous alpha (bps) drives trust; a **±25bps binary**
is for display only (within ±25bps = no-call). Early/partial exits, reversals, and
corporate events are each labeled explicitly and counted.

### 4.2 Point-in-time choke point
**Every** read of price/filing/news/trust routes through one interface
`get(field, ticker, as_of)`. No `get_latest()`. Same code path in live and backtest →
look-ahead is structurally impossible. Per-source `as_of`: Form 4 = filing timestamp;
Congress = disclosure date; price (execution) = next-day open; news = publish timestamp;
beta = 252-day window ending `as_of−1`.

### 4.3 Data-integrity invariants (non-negotiable)
- **No survivorship bias** — universe/prices include delisted/bankrupt/acquired tickers.
- **Form 4/A amendments supersede** (never append-and-double-count); direction-changing
  amendments invalidate the original signal.
- **10b5-1 plan trades excluded** at ingest (no informational content).
- **Congress amounts stored as ranges** (`amount_low`/`amount_high`); never midpoint-imputed.
- **Immutable history** — no in-place updates; corrections are superseding rows.

### 4.4 Horizon taxonomy (unified — orchestrator reconciliation)
Fusion pools only *within* a bucket; each advisor's default horizon lands in one bucket:

| Bucket | Range | Default home |
|--------|-------|--------------|
| INTRADAY | < 1 day | future fast A3 |
| SHORT | 1–30 days | news/X tips |
| MEDIUM | 31–120 days | Congress (from filing date) |
| LONG | 121–365 days | Form 4 insiders |

### 4.5 Execution stack
Prices: **Alpaca + Stooq** (not yfinance). Execution: **Alpaca paper → IBKR live**
(Robinhood has no stock API). Legal: trading on public disclosures is fine for personal
use; managing others' money triggers RIA rules — stay personal-use.

---

## 5. Build plan (phased, data-gated)

A 12-lane spec is un-buildable at once. Build seatbelts first, deliver value early, add
heavy machinery only when data justifies it.

- **Phase 0 — Safety scaffold.** Breakers, quorum gate, audit log, kill-switch webhook
  (hosted off-box), alerting, Alpaca paper account.
- **Phase 1 — MVP.** A1 (insiders + Congress) → equal-weight fusion → paper sim → CLI
  leaderboard. **No trust-learning, no MiroFish, no correlation math, no live.** Smallest
  thing that produces a real, inspectable result.
- **Phase 2 — Outcome labeling + per-source accuracy** (needs ~30 closed trades).
- **Phase 3 — Trust-weighted fusion** (needs ≥60 labeled outcomes; simple Bayesian Beta
  update, *not* a neural net; must beat equal-weight on holdout or revert).
- **Phase 4 — MiroFish (A2)** over HTTP, shadow-mode first.
- **Phase 5 — Correlation deflation** (only once A1+A2 actually overlap enough to measure).
- **Phase 6 — A3 news/X tip layer** (shadow first; volume-anomaly breaker on).
- **Phase 7 — Paper→live gate** evaluation → 10% live, manual ramp 10%→25%→50%→100%.
- **Phase 8 — IBKR migration** if Alpaca limits bind (may never be needed).

**Paper→live gate (hash-locked, immutable mid-run):** ≥60 days, ≥30 closed trades,
Sharpe ≥1.0, drawdown ≤8%, breakers clear, kill-switch tested ≤30 days, manual approval
(expires every 30 days).

**Do NOT build in v1 (stub these):** correlation matrix, multi-factor trust model, A3,
Robinhood, auto-close-on-kill, web UI, retry queues, per-advisor sizing.

---

## 6. Residual risks / open questions (deferred, not blocking)

- **The premise itself:** does A1 have tradeable alpha *after* the disclosure lag? The
  MVP's ~30 paper trades answer this; if not, revisit before Phase 2.
- Correlation estimates are noisy until signals overlap (default ρ=0.5 when sparse).
- Calibration has a cold-start window (~30 outcomes/advisor/bucket).
- Regime detection is single-indicator (fine for paper; harden before live).
- Slippage model is simple (upgrade to volume-adjusted impact before meaningful live capital).
- MiroFish whitelist drift — every data-source addition must pass a written independence check.
- 10b5-1 keyword detection is heuristic — review quarterly.
- 26-week trust half-life may be too slow for A3 (per-advisor override is a Phase-2 refinement).

---

## 7. Process note

This design was produced through structured brainstorming + two rounds of parallel
multi-agent design/audit (8 lane-designers in Round 1, 6 conflict-resolvers in Round 2),
converging when all 12 cross-lane seams were resolved. The full agent analyses are
summarized here; the engine is internally consistent and has a buildable MVP. **Status
remains planning only — no code has been written.**
