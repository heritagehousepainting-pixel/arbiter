# Arbiter — Master Build Plan

**Companion to** `2026-06-18-arbiter-decision-engine-design.md` · Status: **Planning only (nothing built)** · Date: 2026-06-18

This consolidates 14 parallel lane build-plans into one dependency-ordered plan. Each lane has its
own detailed task list, interfaces, and tests (produced separately); this document is the spine that
sequences them, locks the shared conventions, and records the cross-lane contracts.

---

## 0. The 14 build lanes

| # | Lane | Delivers | First lands |
|---|------|----------|-------------|
| 1 | Scaffold / config / CLI / dashboard | `arbiter` package, `Config`, CLI, shared ABCs (copied from stockbot), DB connection factory, dashboard skeleton, structlog | Phase 0 |
| 2 | Storage | `arbiter.db` (SQLite WAL) schema + access module, `audit.jsonl` writer, insert-only/supersede helpers | Phase 0 |
| 3 | Point-in-time data + prices | the `get(field,ticker,as_of)` choke point, Alpaca+Stooq price ingestion, backtest clock, beta, slippage | Phase 0–1 |
| 4 | Safety infrastructure | quorum gate, latching circuit breakers, broker-side kill switch, degradation ladder, idempotency, alerting | Phase 0 |
| 5 | A1 disclosure ingestion | SEC Form 4 + Congress adapters → normalized `filings` (10b5-1 excluded, 4/A supersede, ranges) | Phase 1 |
| 6 | A1 signal + scoring | cluster/opportunistic detection, dual signal-type + person scoring, leaderboard, emits `Opinion` | Phase 1 |
| 7 | A2 MiroFish adapter | self-host + egress firewall, async HTTP, run cache, `run_group_id`, breaker | Phase 4 |
| 8 | A3 tip layer + anti-manipulation | `TipSource` ABC, `UnverifiedTip`, source-diversity gate, account scorer, **volume-anomaly gate** | gates Phase 0–1; full A3 Phase 6 |
| 9 | Advisor contract + calibration | the frozen `Opinion` + validator + registry; Platt/isotonic calibration + cold-start `STANCE_BASE` | contract Phase 1; calibration Phase 3 |
| 10 | Fusion engine | log-opinion-pool, correlation deflation, effective-N + lone-bull tax, horizon bucketing, vetoes, `FusionOutput` | equal-weight Phase 1; deflation Phase 5 |
| 11 | Trust ledger | composite skill×calibration×coverage, caps/floors, shadow onboarding, decay, regime freeze, correlation matrix | Phase 3; correlation Phase 5 |
| 12 | Decision policy + execution | conviction→action, quarter-Kelly+caps+ADV cap, exits, idempotent orders, Alpaca executor, paper→live gate | paper Phase 1; gate Phase 7 |
| 13 | Idea lifecycle orchestrator | the `Idea` FSM, triage, provisional/revision, cycle runner with fault isolation, outcome-readiness sweep | basic Phase 1; revision Phase 4 |
| 14 | Evaluation + backtest | outcome labeling (SPY-beta alpha), point-in-time replay, walk-forward, deflated Sharpe, ablations, attribution, feedback loop | labeling Phase 2; backtest Phase 3 |

> **Note on numbering:** several lane plans referenced earlier "Lane N" labels from the *engine design*
> (its internal 1–8 lanes). In this build document the authoritative numbering is the **14 build lanes
> above**. Where a lane plan says e.g. "Lane 7 (system arch)" or "Lane 9 (DB)", read it as **build-lane 1
> (scaffold)** and **build-lane 2 (storage)** respectively.

---

## 1. Canonical project structure (resolves cross-lane drift)

The scaffold lane owns this; all other lanes conform. Flat package (matches `stockbot/src` convention),
**not** a nested `src/`:

```
poly_bot/
  polybot/            # existing
  stockbot/           # existing  (arbiter copies its Executor/SimExecutor/risk ABCs)
  arbiter/            # NEW
    pyproject.toml
    Makefile
    .env.example
    config/arbiter.toml
    arbiter/                 # the importable package
      config.py  logging_setup.py  metrics.py  types.py
      shared/                # copied ABCs: executor, sim_executor, risk_gate
      db/                    # connection, migrations/, helpers, queries/   (lane 2)
      data/                  # pit.py, clock.py, sources/, beta.py, slippage.py  (lane 3)
      safety/                # gate, quorum, breakers, kill_switch, alerting  (lane 4)
      ingest/                # edgar/, congress/, identity/, writer  (lane 5)
      signals/               # detection, scoring, enrichment, emit, leaderboard  (lane 6)
      adapters/mirofish/     # adapter, http_client, run_cache, egress whitelist  (lane 7)
      tips/  defenses/       # TipSource, UnverifiedTip, anomaly/diversity gates  (lane 8)
      contract/  calibration/   # Opinion + validator; Platt/isotonic  (lane 9)
      fusion/                # pool, correlation, dedup, veto, output  (lane 10)
      trust/                 # ledger, brier, coverage, caps, regime, correlation  (lane 11)
      policy/  execution/  gate/   # decision, sizing, exits; adapter, reconciler; criteria, ramp  (lane 12)
      orchestrator/          # idea, lifecycle, scheduler, triage, outcome_sweep  (lane 13)
      evaluation/            # outcome_labeler, backtest/, metrics/, attribution/, feedback/  (lane 14)
      web/                   # dashboard server + templates  (lane 1)
    tests/                   # mirrors package; each lane owns its subdir
    data/                    # runtime: arbiter.db, audit.jsonl, metrics.jsonl  (gitignored)
```

---

## 2. Locked conventions every lane inherits

These are non-negotiable invariants the lanes agreed on; any lane violating them is wrong:

1. **One point-in-time gateway.** All price/filing/news/trust reads go through `get(field, ticker, as_of)`
   (lane 3). No `get_latest()`, no bare `datetime.now()` outside `clock.py`. Enforced by CI grep.
2. **Insert-only storage.** No in-place `UPDATE` except the single `is_superseded` flag flip inside
   `supersede_row()`. Corrections = new rows with `supersedes_id`. `audit.jsonl` is authoritative if it and the DB diverge.
3. **Abstain is `None`, never `0.0`.** Abstaining opinions are excluded from the fusion pool.
4. **Fail-closed.** `LIVE_TRADING=false` default; safety gate unreachable → no trade; missing ADV → size 0.
5. **MiroFish isolation.** Called over local HTTP only (never `import mirofish` — AGPL); its network egress is
   firewall-restricted to structured/filing data (no news/social) so it stays independent of A3.
6. **Outcome label = SPY-beta-adjusted alpha** over the stated horizon, entry filing-date+1 open net of
   modeled slippage, point-in-time beta; ±25bps binary for display only. (Lane 14 owns; lanes 9 & 11 consume.)
7. **Idempotency = ULID primary key + `dedup_hash` UNIQUE** (`ticker+side+horizon+entry_date+advisor_sig`),
   checked against local ledger AND broker before submit; max 1 retry then halt+alert.
8. **Horizon buckets:** INTRADAY `<1d`, SHORT `1–30d`, MEDIUM `31–120d`, LONG `121–365d`. Fusion pools only
   *within* a bucket; a missing bucket is `NO_SIGNAL`, not 0.5.
9. **Calibration owns raw→probability; fusion owns pooling.** `STANCE_BASE` is the cold-start prior only.

---

## 3. Global build order (dependency DAG)

```
        ┌── L1 scaffold ──┐
        ▼                 ▼
   L2 storage ───────► L4 safety ─────────────┐
        │                 ▲                    │
        ▼                 │                    │
   L3 pit+prices          │                    │
        │                 │                    │
        ▼                 │                    ▼
   L9 contract ──► L5 ingest ──► L6 A1 scoring ──► (MVP leaderboard)
        │                                   │
        ▼                                   ▼
   L10 fusion (equal-wt) ◄──────────────────┘
        │
        ▼
   L13 orchestrator ──► L12 policy+exec (paper) ──► closed ideas
        │                                              │
        ▼                                              ▼
   L8 anomaly/diversity gates (early)            L14 outcome labeling (Phase 2)
                                                       │
                          ┌────────────────────────────┤
                          ▼                            ▼
                   L9 calibration (P3)          L11 trust ledger (P3)
                          └──────────┬───────────────┘
                                     ▼
                          L10 fusion trust-weighted (P3)
                                     │
                                     ▼
                          L7 MiroFish (P4) → L13 revision (P4)
                                     │
                                     ▼
                          L10 correlation deflation + L11 corr matrix (P5)
                                     │
                                     ▼
                          L8 full A3 (P6) → L12 paper→live gate (P7) → IBKR (P8)
```

**Critical path to a working MVP:** L1 → L2 → L3 → L9 (contract) → L5 → L6 → L10 (equal-weight) →
L13 (basic cycle) → L12 (paper). Plus L4 safety in parallel from the start.

---

## 4. Phase plan with entry gates

### Phase 0 — Safety scaffold (build the seatbelts first)
**Lanes:** L1 (scaffold/config/CLI/dashboard skeleton, shared ABCs, DB factory), L2 (schema + audit
writer), L4 (breakers, quorum, kill-switch webhook hosted **off-box**, alerting, idempotency), L3 (price
clients + backtest clock + `get()`), L8 (volume-anomaly + diversity gates wired but dormant).
**Exit:** a manually-constructed test order passes the full safety stack and appears in `audit.jsonl`;
kill-switch drill blocks orders within 60s with the Python process down; CI green (config strict-parse,
no-`get_latest`/no-`now()` lint, breaker latching).

### Phase 1 — MVP (the smallest inspectable result)
**Lanes:** L9 (Opinion contract + validator), L5 (Form 4 then Congress ingestion), L6 (signal detection +
dual scoring + **leaderboard**), L10 (equal-weight fusion), L13 (basic cycle runner, FSM, dedupe,
outcome-readiness sweep), L12 (paper policy + Alpaca-paper executor + exits + idempotency).
**Deferred/stubbed:** trust-learning (equal weights), MiroFish (`None`), A3 (stub), correlation math, live trading.
**Exit:** runs ≥7 days, ≥5 paper signals, all in audit log, leaderboard renders (signal-types AND people,
gate-failing rows grayed), no look-ahead violations in tests.

### Phase 2 — Outcome labeling + per-source accuracy
**Lanes:** L14 (outcome labeler: SPY-beta alpha, slippage, corporate-event/early-exit/reversal labels,
outcome store), L6 (per-source accuracy on the leaderboard), L8 (anomaly gate live in the position sweep).
**Entry gate:** ≥30 closed paper trades (est. 4–8 weeks).
**Exit:** can answer "A1 cluster-buy signals: X% accuracy, Y bps mean alpha over N trades"; disclosure-lag
cost surfaced (t+1 vs t+5 vs t+10 entry) — the differentiator.

### Phase 3 — Trust-weighted fusion + calibration
**Lanes:** L11 (composite trust, shrinkage, caps/floors, shadow onboarding, regime freeze), L9 (Platt/isotonic
calibration), L10 (trust-weighted pool).
**Entry gate:** ≥60 labeled outcomes; coverage-roster dependency (L14↔L11) resolved.
**Hard rule:** trust-weighted fusion must beat equal-weight on a holdout **and** beat the
naive-follow-every-insider baseline, or the system reverts to equal-weight (code, not judgment).

### Phase 4 — MiroFish (A2), shadow first
**Lanes:** L7 (self-host + egress firewall + adapter + run cache), L13 (triage gate, provisional/revision).
**Entry gate:** Phase 3 complete + ≥30 days A1 forward-test baseline. A2 runs **shadow** (weight 0) until ~30
resolved outcomes, hard-capped at 0.35 thereafter.

### Phase 5 — Correlation deflation
**Lanes:** L10 (effective-N + deflation active), L11 (rolling pairwise ρ + event-fingerprint collisions).
**Entry gate:** A1 + A2 active ≥30 days with ≥30 overlapping signals (else ρ stays at the 0.5 prior).

### Phase 6 — A3 news/X tip layer
**Lanes:** L8 (full `TipSource`, X adapter behind the paid-API decision, account/cluster scorer, shadow first).

### Phase 7 — Paper→live gate
**Lanes:** L12 (hash-locked criteria, staged 10→25→50→100% ramp, 30-day approval expiry).
**Entry gate (immutable, pre-committed):** ≥60 days, ≥30 closed trades, Sharpe ≥1.0, drawdown ≤8%, breakers
clear, kill-switch tested ≤30 days, manual `--approve-live`.

### Phase 8 — IBKR migration (only if Alpaca limits bind; may never happen)

---

## 5. Key cross-lane interface contracts (the seams)

| Producer → Consumer | Contract |
|---------------------|----------|
| L9 → all advisors | `Opinion` dataclass + `validate_opinion()`; advisors emit raw stance, never calibrated probs |
| advisors → L9 calibration → L10 | calibration `transform(raw, horizon) -> prob`; fusion consumes calibrated probs only |
| L11 → L10 | `WeightBundle` (per-advisor `AdvisorWeight` + CIs + correlation matrix); weights are log-pool weights (not a simplex) |
| L10 → L12 | `dict[HorizonBucket, FusionOutput]` (conviction, dispersion, effective_N, advisor_contributions, vetoes, cold_start) |
| L12 → L4 | calls `is_trading_allowed(account)` before every order; ADV cap is the last sizing transform |
| L13 → L7 | passes an `Idea` (ticker+thesis+horizon) to MiroFish; triage decides invoke/skip |
| L13 → L14 | emits `OUTCOME_READY` with the idea's original `as_of` + stated horizon |
| L14 → L9, L11 | `ResolvedOutcome` (SPY-beta alpha, binary, advisor confidence, abstain flag) — feeds calibration + trust |
| **L14/L13 → L11** | **eligible-idea roster** (which ideas each advisor *could* have opined on) — required for the coverage term; this is the one dependency that must be wired explicitly, not deferred |
| L8 → L10 | `source_fingerprint` for the correlation haircut; `VolumeAnomalyGate` used by L4 + L13 |

---

## 6. Cross-lane issues resolved in synthesis

1. **Package layout:** flat `arbiter/arbiter/...` (per scaffold lane), not L12's `src/` — L12 conforms.
2. **"Lane N" reference drift:** plans mixed engine-design lane numbers with build-lane numbers; §0 note
   maps them. Authoritative = the 14 build lanes.
3. **Storage ownership:** the DB module + all schemas live in build-lane 2; lanes that sketched their own
   tables (5,6,9,11,12,13,14) contribute migration fragments to `arbiter/db/migrations/` which the lane-2
   runner applies in order.
4. **Idempotency key:** unified to ULID PK + `dedup_hash` UNIQUE (L12 + L2 agree; L4's `uuid4` becomes the ULID).
5. **Coverage denominator:** flagged by L11 as a hard dependency on an eligible-idea roster from L13/L14 —
   elevated to a first-class Phase-3 entry requirement above (not a "future refinement").

## 7. Standing risks carried from the design (unchanged, still the watch-list)

The premise test (does A1 alpha survive the disclosure lag? — answered by the Phase-1 MVP's ~30 trades);
MiroFish is forward-test-only and un-backtestable; survivorship bias and look-ahead leakage are the
"silent killers" L14 guards with the no-look-ahead canary + naive-baseline gate; correlation priors and
several hand-tuned constants (lone-bull tax, conviction thresholds, 26-week half-life) need eval
validation before live capital; the slippage model is simple and must be upgraded before meaningful live size.

---

**Status: planning only. No code has been written. This plan sequences the 14 lane plans into one
buildable path; the MVP critical path (L1→L2→L3→L9→L5→L6→L10→L13→L12 + L4) is the place to start.**
