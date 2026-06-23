# Goal: A3 strength-gate + T-ledger heal (plan → build → audit)

**Date:** 2026-06-22. Paper account; A3 is probationary/shadow.

## Phase 1 — (a) Heal the LOCAL_ONLY `T −1` ledger orphan  [orchestrator, live DB]
The local `orders` ledger nets to T = −1 (from the 2026-06-22 short-support T churn:
SELL3, BUY3, SELL5, BUY4 = −1) but the broker shows T flat (0). The reconciler (now
`abs(v)`-correct) honestly flags this as LOCAL_ONLY each cycle.
- Investigate the live T order rows; back up the DB first (`data/arbiter.db.pre-tledger-bak`).
- Reconcile the local filled-order ledger so net T = 0 (match the broker) via a targeted,
  minimal, audited change (retire/neutralize the orphan order(s); preserve outcomes/ideas).
- Verify: a fresh `reconciler.reconcile` is CLEAN for T. Done when the divergence is gone.
- Done myself (live trading DB; not an agent slice).

## Phase 2 — (b) A3 strength gate  [1 Sonnet agent]
A3 currently emits ~1 opinion per watchlist name each cycle (live: 10/10, stances 0.05–0.51).
Add a tuned strength gate so A3 only emits when the news signal is genuinely strong.
- `adapters/a3/pipeline.py`: after corroboration + stance/confidence, DROP opinions whose
  `|stance_score|` (and/or `confidence`) is below a threshold; emit only the strong ones.
- Tunable threshold (config field + default, e.g. `A3_MIN_STANCE ≈ 0.25`), analyzed against
  the observed live distribution. Free/deterministic, shadow-safe, no-lookahead preserved.
- Tests for above/below threshold; update any test that assumed every corroborated ticker emits.
- Keep full suite (~2432) + both linters green.

## Phase 3 — Audit loop (×2, orchestrator)
- Pass 1 (function/correctness): (a) reconciler clean; (b) gate actually reduces emission and
  keeps the strong signals; no-lookahead + insert-only clean; full suite green; A3 still
  shadow-safe + inert without key.
- Pass 2 (regression/honesty): re-run, confirm no regressions, live read-only re-check that A3
  now emits FEWER, stronger opinions, update memory + any SETUP notes.
