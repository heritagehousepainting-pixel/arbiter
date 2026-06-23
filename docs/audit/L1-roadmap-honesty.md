# L1 — Roadmap Honesty Audit (stubbed-vs-claimed) + ADD Backlog

**Auditor lane:** L1 (read-only). **Date:** 2026-06-19.
**Scope:** Cross-check claims in `INTERFACES.md`, `ROADMAP.md`, `SETUP_NEEDED.md` against the actual code at `/Users/jonathanmorris/poly_bot/arbiter`. Classify every flagged component as **WIRED & WORKING**, **WIRED-BUT-INERT** (runs but produces a no-op / placeholder value), **CODE-ONLY (not wired)** (exists, tested, but nothing in the production path calls it), or **ABSENT**.

---

## Verdict (one line)

The build is **substantially more honest than most**: the docs explicitly self-label A2/A3/MiroFish/regime as shadow/dormant, and the things claimed "live for the MVP" (A1 insider+congress, equal→trust-weighted fusion, calibrator-in-fusion, Alpaca paper exec, Stooq fallback, outcome sweep) **are genuinely wired**. The honesty gaps are smaller-bore: a few features are present-and-tested but have **zero production caller** (regime tracker, the MiroFish triage path, the entire tips layer), and one cap is **silently inert** (sector cap always sees `"UNKNOWN"`). The system is real but **early** — it is a working 2-advisor paper trader whose "self-learning" loop is structurally complete but starved of the outcome data that would make trust/calibration actually move.

---

## Inventory table

| Component | Claimed status | Real status | Evidence (file:line) |
|---|---|---|---|
| **A1.insider / A1.congress advisors** | Live in MVP | **WIRED & WORKING** — both registered in `advisor_map` and run each cycle | `engine.py:1355-1357`, `engine.py:914` |
| **A2 MiroFish adapter** | Phase-4 shadow; "adapter buildable now, no endpoint" | **CODE-ONLY (not wired)** `[P3]` — full adapter+HTTP client+run-cache+egress exist and are tested, but **no production caller**: `maybe_invoke_mirofish`/`triage_mirofish` have zero non-test references, and `MIROFISH_ENDPOINT` is unset (client raises `MirofishUnavailable`). Honestly self-labeled "NOT wired" in engine docstring. | `engine.py:29`, `adapters/mirofish/adapter.py:153`, `orchestrator/triage.py:173` (no caller), `adapters/mirofish/http_client.py:51,145` |
| **A3 news/X tips layer** | Phase-6 dormant; "`TipSource`/gates buildable" | **CODE-ONLY (not wired)** `[P3]` — `TipSource` ABC, `UnverifiedTip`, diversity gate, account scorer all exist, but **no concrete `TipSource` adapter exists** and nothing outside `tips/` and its tests imports the package. Pure dormant scaffold. Honestly labeled "SHADOW / DORMANT". | `tips/source.py:1-4`, grep: only `tips/__init__.py` consumes it; no Twitter/Reddit adapter present |
| **Correlation deflation (fusion)** | Phase-1 ρ=0 placeholder; real ρ in Phase-5 | **WIRED-BUT-INERT (by design)** `[P2]` — `effective_n` double-sum is real and live, but in MVP the `WeightBundle.correlation_matrix` is empty so every off-diagonal ρ defaults to **0.0** → `effective_N ≈ N`, no deflation ever fires. The trust ledger *can* emit a real matrix (`to_bundle_dict`), but with only 2 advisors (both A1, same-family) and no outcome volume it stays empty. Honestly documented. | `fusion/correlation.py:63,103-110`, `trust/weight_resolver.py:141-142`, `trust/ledger.py:398-413` |
| **Regime tracker** | Phase-3 trust; "Wave-C wiring TBD" | **CODE-ONLY (not wired) — effectively DEAD** `[P2]` — `RegimeTracker`/`apply_regime_weights` fully implemented + tested and threaded as an *optional* `regime_tracker=None` param through `trust/ledger.py`, but it is **never constructed in production** (`RegimeTracker(` has zero non-test hits) and **no regime-detection feeder exists** ("who detects regime changes … is TBD"). Always `None` → freeze + 2× multiplier never apply. | `trust/regime.py:13-14`, `trust/ledger.py:132,268,352`, grep: no prod `RegimeTracker(` |
| **Calibrator-in-fusion (`transform_for` / MultiAdvisorCalibrator)** | Additive R4/D5 seam, live | **WIRED & WORKING** `[P2 on efficacy]` — `MultiAdvisorCalibrator` built per cycle and passed into `_fuse`; per-advisor `Calibrator`s fit + persisted. BUT `_MIN_FIT_SAMPLES = 2` is extremely low, and with near-zero closed outcomes the calibrators are **cold-start passthrough** in practice → identity transform today. Plumbing real; effect ≈ none until data accrues. | `engine.py:729-782,990,1002`, `calibration/calibrator.py:46,91` |
| **Trust ledger / weight resolver** | Phase-3 trust-weighted fusion | **WIRED-BUT-INERT-IN-PRACTICE** `[P2]` — `_build_learning_inputs` calls the real `TrustLedger.update`, persists `trust_weights`, resolves a `WeightBundle`. Structurally the self-learning loop is closed. But it is gated on `load_outcomes_for_learning`, which is ~empty (few/no closed paper trades) → weights collapse to the equal floor. Real code, no signal yet. | `engine.py:688-782,704-711`, `trust/weight_resolver.py` |
| **Outcome labeler / sweep** | Phase-2; "needs ≥30 closed trades" | **WIRED & WORKING** `[P3]` — `run_outcome_sweep` is called inside `engine.run_cycle` (`engine.py:1193`) AND the daemon (`runtime/daemon.py:257`); persists `ResolvedOutcome`s. (Supersedes an earlier memory note that outcomes were "not yet persisted / stateless.") Completion is *data-gated*, not code-gated. | `engine.py:1193-1203`, `runtime/daemon.py:251-257` |
| **Sector cap (20% per sector)** | Hard cap, enforced | **WIRED-BUT-INERT** `[P1]` — `decide(...)` in `engine.py` is called **without `sector_by_ticker`**, and `sector_by_ticker` is never populated anywhere in production → every ticker maps to `"UNKNOWN"`. Effect: all positions counted as **one** sector, so the "per-sector" cap silently degenerates into a single 20% bucket across the whole book. Conservative (won't over-concentrate) but the *diversification* feature does not exist. | `policy/decision.py:208,238`, `engine.py:1029-1041` (no sector arg), grep: `sector_by_ticker` only in `decision.py` |
| **Stooq fallback** | Backup/delisted price source | **WIRED & WORKING** `[P3]` — `build_price_gateway` assembles Alpaca-primary + Stooq-fallback via `_FallbackPriceAdapter`; `build_engine` uses it. (`SETUP_NEEDED` still says "⬜ verify" — reachability unconfirmed, but the wiring is real.) | `data/sources/_gateway.py:85-134`, `engine.py:1324,80` |
| **IBKR** | Phase-8, "only if Alpaca binds" | **ABSENT** (honestly) `[P3]` — no IBKR/`ib_insync` code at all. Correctly future-gated; no false claim. | grep: zero hits |
| **Alpaca paper execution** | Live behind `executor_backend=alpaca_paper` | **WIRED & WORKING** — `AlpacaAdapter`/`submit_order` path real; `LIVE_TRADING` stays false. (Memory: 7 live paper orders placed.) | `engine.py:1069-1088`, INTERFACES §9 |
| **Critical-alert auto-pause** | `SETUP_NEEDED #5`: "built+tested, NOT yet auto-fired from cycle loop" | **WIRED (now)** `[P3]` — contradicts the doc: `_fire_critical_alert` exists, `Engine.paused` latches and is persisted; broker-fatal path fires it (`engine.py:1083`). Doc is stale, code is ahead. | `engine.py:41,202-252,1083` |

---

## Biggest honesty gaps (ranked)

1. **`[P1]` Sector cap is a no-op.** Documented as a real "20% per-sector" hard cap (INTERFACES §9); in practice every name is `"UNKNOWN"`, so it's a single 20% book bucket. This is the one cap that is *claimed real but silently inert*.
2. **`[P2]` "Self-learning" is plumbed but starved.** Trust weights, per-advisor calibration, and correlation deflation are all wired into the live cycle — but with ~no closed outcomes and only 2 same-family advisors, every one of them is currently at its **cold-start / floor / ρ=0 identity**. The loop is honest and structurally complete; it just hasn't *learned anything yet*. A casual reader of ROADMAP ("Phase 3 — Trust + calibration") could over-read how adaptive the system is **today**.
3. **`[P2/P3]` Three tested-but-dead subsystems:** the regime tracker (never constructed, no detector), the MiroFish triage path (no caller, no endpoint), and the entire tips layer (no concrete source). All are honestly self-labeled shadow/dormant/TBD — so this is a *completeness* gap, not a *deception* gap. Stale spots: `SETUP_NEEDED #5` (auto-pause) understates the code.

---

## Ranked ADD backlog (value-to-mission: a self-learning smart-money trader)

> Ranked by marginal value to the mission, not by effort. P-tags reflect how much the *claim/mission* depends on it.

### Tier 1 — unlock the learning loop (the mission itself)
1. **`[P1]` Feed real outcomes faster: shrink the labeling horizon loop + backfill from history.** The single biggest lever. Trust/calibration/correlation are all inert purely for lack of closed-outcome volume. ADD a historical backfill harness (replay Form-4/Congress disclosures through the PIT gateway against real Alpaca/Stooq bars) to generate dozens-to-hundreds of *labeled* outcomes **without waiting calendar months**, so the calibrators and trust weights leave cold-start. This is what turns "plumbed" into "learning."
2. **`[P1]` Add a 3rd, genuinely-independent advisor family to make fusion non-trivial.** With only A1.insider + A1.congress (same disclosure family), diversity/correlation/lone-bull-tax machinery can never bind meaningfully. Standing up **MiroFish (A2)** for real (self-host + set `MIROFISH_ENDPOINT` + flip shadow→onboarding) is the highest-leverage *new signal*: the entire fusion-diversity apparatus only earns its keep with ≥3 independent voices.
3. **`[P1]` Real sector mapping.** Cheapest correctness win with outsized risk value. ADD a `sector_by_ticker` provider (a static GICS/SIC map from EDGAR data you already ingest, or an Alpaca/lookup table) and pass it into `decide(...)`. Turns a dead cap into a real diversification constraint before the book grows.

### Tier 2 — make the learning trustworthy
4. **`[P2]` Regime detector to feed `RegimeTracker`.** The freeze + 2× post-regime machinery is built and dead. ADD a lightweight detector (e.g. SPY trend / vol-regime / drawdown-state classifier on PIT data) that emits `RegimeChangeEvent`s, and wire a constructed `RegimeTracker` into `_build_learning_inputs`. Protects the trust ledger from mis-learning across regime breaks — directly serves "self-learning that doesn't fool itself."
5. **`[P2]` Raise `_MIN_FIT_SAMPLES` and add per-bucket calibration guards.** `=2` will fit noise. ADD a principled minimum (with the isotonic/Platt low/high-data split already present) so the calibrator doesn't "learn" from 2 points and inject overconfidence.
6. **`[P2]` Correlation from `source_fingerprint` overlap, live.** The ledger can compute ρ but has no data; the fingerprint field is already on every Opinion. ADD the cross-advisor fingerprint-overlap estimator into the cycle so correlation deflation becomes real the moment A2/A3 arrive (rather than a Phase-5 cliff).

### Tier 3 — operational maturity
7. **`[P2]` Concrete `TipSource` + the A3 diversity→opinion bridge.** The tips scaffold is complete but has no adapter and no path from a corroborated tip to a (shadow) Opinion. ADD one real source (even a cheap RSS/StockTwits poller) behind the existing diversity gate, in shadow, to start scoring account credibility against eventual outcomes.
8. **`[P3]` Live Sharpe/DD/exposure dashboard wired to the paper→live gate criteria.** The Phase-7 gate (`60d/≥30 trades/Sharpe≥1.0/DD≤8%`) is code-gated but there's no surfaced running tally of *where the system stands against it*. ADD it so the gate is observable, not just enforced.
9. **`[P3]` Doc-truth pass.** Update `SETUP_NEEDED #5` (auto-pause is wired) and the older "outcomes not persisted" memory; add an explicit "inert in MVP" note next to the sector cap so the one silent no-op is documented like the others.

---

## How far from "complete & best-version"?

**Architecturally:** ~80% — the DAG, contracts, safety stack, exec, and the *shape* of the learning loop are all real and coherent; this is not vaporware.
**Functionally as a self-learning trader:** ~30% — it is a **2-advisor, cold-start paper trader**. Every adaptive component (trust, calibration, correlation, regime) is wired but currently at identity/floor for lack of independent signals and labeled outcomes. The gap to "best version" is dominated by **(a) more independent advisors** and **(b) outcome-data volume to actually train on** — i.e. Tier-1 ADDs 1–3 are the unlock; everything else is refinement.
