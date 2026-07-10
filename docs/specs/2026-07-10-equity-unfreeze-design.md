# Equity Unfreeze + Deploy-More — Design (2026-07-10)

## Problem

Arbiter (live on `alpaca_paper`, $10k) stopped opening **new equity positions** after
**2026-06-26** and deployed only ~$1,570 (15.7%) of capital. Forensics (two agents +
direct DB/log verification) found the equity-entry freeze is caused by two DB-state
conditions — **not** risk caps (gross cap is 0.50 with 15.7% used; position cap 12 with 7 held),
and **not** a dead engine (daemon healthy, cycles running 11–12×/day):

1. **Dedupe horizon-lock.** A "no-trade" decision still advances the idea to `FINAL_DECIDED`
   (`orchestrator/cycle.py:293`), which counts as an *active* idea (only `CLOSED`/`ABANDONED`
   are terminal — `orchestrator/idea_store.py:50`). Dedupe then skips any new idea on that
   `(ticker,bucket)` (`cycle.py:191`) until the idea's full **90–180 day** horizon elapses
   (`orchestrator/outcome_runner.py:197`). Result: **196 never-executed ideas lock 169 tickers
   until 2026-09-20 → 12-29** (verified), **16,302** dedupe skips, cycles running with ~0 fresh ideas.
2. **Advisor muted on a thin sample.** `A3.news` (the main fresh-idea source — 94% of opinions)
   was demoted to `weight=0` ("negative_skill") on 2026-07-02 off **67** outcomes. Its ideas then
   fuse to conviction 0.0 → "no trade" → which *re-locks* the ticker (feeds cause #1). Root bug is an
   **asymmetry** in `trust/ledger.py:534`: graduation is significance-gated
   (`is_significant_skill`: `ci_low>0 AND n_eff≥MIN_EFFECTIVE_N`), but demotion-to-muted is a bare
   point estimate `is_negative_skill = brier_skill_score < 0.0` with **no CI / no effective-n guard**.

Separately fixed this session (context, not part of this spec): `ALPACA_DATA_FEED=sip`→403 blinded
the exit monitor + options layer (now `iex` + hardened in `data/current_price.py`); Anthropic credits
were exhausted (topped up).

## Decisions (locked with owner)

| # | Decision | Choice |
|---|----------|--------|
| 1 | The 169 locked tickers | Free them (achieved automatically by the dedupe fix — no DB surgery) |
| 2 | Capital deployment target | **80% deployed / 20% reserve** |
| 3 | Fractional shares | **Disabled** — whole-share only (prepping for live-trading realism) |
| 4 | Advisor muting | Significance-gate demotion (symmetric w/ graduation); un-mute follows automatically |

## Design

### Component 1 — Dedupe: short cooldown, not a full-horizon lock
Decouple the **dedupe lock** (should be short) from **outcome labeling** (must stay full-horizon for
counterfactual learning). Introduce `dedupe_cooldown_days` (default **3 trading days**; tunable via
config). A never-executed `FINAL_DECIDED` idea blocks its `(ticker,bucket)` only while younger than the
cooldown; past it, it no longer blocks new ideas — **but remains `FINAL_DECIDED`** so
`outcome_runner` still labels its outcome at full horizon (learning signal preserved). Held ideas
(`EXECUTED`/`MONITORED`) still block for their horizon (no double-buying; also backstopped by the
existing held-ticker skip). Change sites: the dedupe predicate (`is_duplicate`) and the active-idea
loader feeding it. **The 169 currently-locked tickers are all older than the cooldown → they free
themselves on the next cycle, no DB write.**

### Component 2 — Learning loop: significance-gate the demotion
In `trust/ledger.py`, replace `is_negative_skill = bss < 0.0` with a symmetric significance test:
mute (negative_skill) **only when `ci_high < 0.0 AND n_eff ≥ MIN_EFFECTIVE_N`** — i.e. the advisor is
significantly *worse* than chance and well-sampled. Reuse `skill_ci = bootstrap_skill_ci(...)` and
`n_eff = effective_sample_size(...)` already computed in the same loop (currently below line 534 —
reorder so the gate can read them). Add `is_significant_negative_skill(ci_high, n_eff)` mirroring
`is_significant_skill`. Below the bar → not muted → advisor floored at `trust_equal_floor` (0.25) via
the existing `_apply_caps`, so it keeps trading and keeps accruing outcomes to learn from.
**Un-mute is automatic:** the next `TrustLedger.update()` recomputes weights from scratch, so
A3.news / A1.activist / A1.congress re-evaluate under the fair gate with no DB surgery.

- **Empirical check at build time (do NOT assume):** compute `is_significant_negative_skill` for
  A3.news's 67 outcomes. Expected: insignificant (CI straddles 0 and/or n_eff too low) → floats.
  If it is *genuinely* significantly negative, it correctly stays muted — **stop and flag to the user**
  rather than force-un-mute a proven-bad signal (that would violate the learning principle).

### Component 3 — Deploy 80%: lift the ceiling AND make it reachable (config only)
`arbiter/.env`:
- `ARBITER_MAX_GROSS_PCT` `0.50 → 0.80`
- `ARBITER_MAX_OPEN_POSITIONS` `12 → 20` (the code/TOML default; needed because at 5%/name you need
  ~16 funded names to reach 80%)
- add `ARBITER_ALLOW_FRACTIONAL=0` (disable fractional — Decision 3)

Unchanged: per-name 5% (`max_position_pct`), sector 20%, ADV 2%, quarter-Kelly sizing. Deployment to
80% is a *ceiling*, realized only as `(#qualifying ideas) × (quarter-Kelly size)`; Components 1+2
supply the ideas. If deployment still lags after unfreeze, raising sizing aggressiveness (e.g.
half-Kelly) is a **follow-up lever, out of scope here**. Accept: whole-share + $500 per-name cap means
names priced above ~$500/share are skipped (honest for a live $10k account).

### Component 4 — Guardrails that STAY (non-goals)
No change to: stop-losses (restored + hardened this session), per-name / sector / ADV liquidity caps,
circuit breakers, kill-switch (fail-closed), Kelly fraction, safety gates. Aggressive ≠ reckless.

### Component 5 — Activation & verification
One daemon restart loads everything (feed hardening + Component 1 & 2 code + Component 3 `.env`).
Acceptance criteria (verify on live cycles, read-only):
- `trust_weights`: A3.news `weight > 0` (floored) after the next trust update — OR a documented,
  flagged "significantly-bad" verdict.
- Dedupe skips per cycle drop sharply; `ideas_processed` > 0 on normal cycles.
- Within a few market-hours cycles, **≥1 new equity entry** appears in `orders` (first since 06-26).
- Gross exposure trends up from 15.7% toward the 80% ceiling over subsequent sessions.
- No regression: exits still evaluate (stop-losses live), no new tracebacks, breakers unlatched.

## Risks
- **Un-muting a truly-bad advisor** → mitigated by the significance gate + the mandatory empirical check.
- **Faster capital deployment right after unfreeze** → bounded by per-name 5% / sector 20% / breakers /
  stop-losses; paper money; this is the intended "testing phase" behavior.
- **Cooldown too short → idea churn** (re-generating the same idea) → a few-days cooldown balances
  freshness vs churn; tune via the config knob.
- **Whole-share + 80% target** → universe narrows (high-priced names skipped); deployment may lag if
  many fresh ideas are high-priced. Monitor; revisit sizing lever only if needed.

## Rollout
Code + config only; **no manual DB mutation**. Order: land Component 1 & 2 code (with tests) →
set Component 3 `.env` → restart daemon → verify acceptance criteria over the next cycles.
Rollback = revert the commit + restore prior `.env` values + restart.
