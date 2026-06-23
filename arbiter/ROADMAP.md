# Arbiter — Build Roadmap (execution)

Driving the `/goal` build of `docs/specs/2026-06-18-arbiter-*.md`. Source of truth for cross-lane
contracts is `INTERFACES.md`. Status legend: ⬜ not started · 🟡 in progress · ✅ done · 🔒 gated.

## How this build runs (parallel-agent waves)
The architecture is a strict dependency DAG, so agents run in **waves** — each wave parallelizes only
genuinely-independent modules that build against the frozen `INTERFACES.md`.

- **Wave A — Foundation (freeze contracts):** L1 scaffold/types/config, L2 storage/schema, L9 Opinion +
  seam dataclasses, L3 PIT gateway/clock. Small, must land coherently first.
- **Wave B — Fan-out (~20 agents):** every remaining module built in parallel against frozen interfaces —
  L4 safety, L3 price clients, L5 ingest, L6 signals, L8 gates, L10 fusion, L11 trust, L9 calibration,
  L12 policy/exec, L13 orchestrator, L14 evaluation, L7 MiroFish adapter.
- **Wave C — Integration:** wire the end-to-end Phase-1 paper-sim cycle; full suite green.
- **Audit loop ×2 (then /loop until clean):** function · wording · layout/design · security/correctness.
- **Review:** orchestrator decides done vs. another loop.

## Phase status (what "buildable now" means)
- **Phase 0 — Safety scaffold:** ⬜ buildable now (L1,L2,L3,L4,L8-dormant).
- **Phase 1 — MVP paper-sim:** ⬜ buildable now (L9,L5,L6,L10 equal-wt,L13,L12 paper).
- **Phase 2 — Outcome labeling:** 🔒 code buildable now (L14); *completion* needs ≥30 closed paper trades.
- **Phase 3 — Trust + calibration:** 🔒 code buildable now (L11,L9 calib); needs ≥60 labeled outcomes.
- **Phase 4 — MiroFish (A2):** 🔒 adapter buildable now (L7); needs self-host + 30 shadow outcomes.
- **Phase 5 — Correlation deflation:** 🔒 code buildable now (L10,L11); needs A1+A2 overlap data.
- **Phase 6 — A3 news/X:** 🔒 `TipSource`/gates buildable; full A3 needs paid-API decision.
- **Phase 7 — Paper→live gate:** 🔒 criteria code buildable now (L12); gate opens only on 60d/30-trade/Sharpe.
- **Phase 8 — IBKR:** 🔒 only if Alpaca binds.

Data/time-gated phases ship **real code in shadow/stub** per the plan — never fabricated history.

## Done-when (MVP target for the /loop)
1. `arbiter` package imports clean; `pytest` green across all lane test subdirs.
2. CI lints pass: no `get_latest()`, no `datetime.now()` outside `clock.py`, config strict-parse, breaker latching.
3. End-to-end paper-sim cycle runs: ingest (fixtured Form 4 + Congress) → signals → equal-weight fusion →
   orchestrator FSM → SimExecutor paper order → audit.jsonl entry → leaderboard renders.
4. Safety stack provably blocks an order when a breaker latches / quorum fails / kill-switch set.
5. No look-ahead: PIT canary test passes; outcome labeler reproduces a known alpha on fixtures.
