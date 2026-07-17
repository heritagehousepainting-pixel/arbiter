# Arbiter Unfreeze — Parole + Flow Engine (2026-07-17)

## Problem

Zero entry orders since 2026-07-10 despite a healthy daemon, an open safety
gate (NORMAL, no breaker trips), loose caps (20 positions, 80% gross), and
~$8.6k of $10k equity sitting in cash (~13% deployed vs the 75–85% target).

Diagnosed funnel (from `audit.jsonl`, `metrics.jsonl`, daemon log, live DB):

1. **Idea starvation** — 18 of the last 20 cycles processed 0 ideas.  Filing
   signals are bursty, and a decided idea blocks its `(ticker, bucket)` slot
   via dedupe until horizon expiry, so backlogs are one-shot.
2. **Opinion sparsity** — the one 170-idea cycle gathered only 6 opinions.
3. **Silent kill-points** — `decide()` drops sub-threshold conviction and
   `compute_size()` returns 0 with **no audit trail** (bare `continue`),
   making every freeze invisible.
4. **No deployment pressure** — nothing pushes toward the 75–85% deployed
   posture and nothing alerts when capital idles.

Trust state: every advisor sits at ledger weight 0/shadow (probationary),
which the resolver (`trust/weight_resolver.py`) maps to the EQUAL_FLOOR
(0.25) — so cold advisors DO trade; that mechanism is healthy.  A3.news's
`negative_skill` mute (assigned ~07-02 on n=7) has already lapsed.
A1.activist (48 outcomes, −614 bps avg) is heading toward a *correct*
`negative_skill` mute — which would bench the largest idea source outright.

## Goal

Aggressive-learning paper posture: trade most days, push toward 75–85%
deployment, maximize outcome data for the learning loop — **without**
bypassing fusion, caps, gates, breakers, attribution, or the kill switch.

## Design (4 stages, in build order)

### Stage 1 — Decision tracing

* `policy/decision.py::decide()` and `policy/sizing.py::compute_size()` gain
  an optional `trace: Callable[[str, dict], None] | None = None` parameter
  (pure functions stay pure; default None = today's behavior).
* Engine injects an audit-writing closure.  Every dead idea emits
  `decide.skip` to `audit.jsonl` with a reason code:
  - `no_opinions` — idea reached decide with no fused bucket output
  - `all_shadow` — every opinion excluded by pool (shadow/disabled)
  - `flat_conviction` — |conviction| < threshold (value included)
  - `size_zero_caps` — sizing clamped to 0 by caps/count gate
  - `size_zero_adv_missing` — ADV fail-closed path
  - `held_no_headroom` — held ticker skipped by `_addon_ok`
  - `dedupe_blocked` — idea slot blocked by an active idea
* One `cycle_funnel` audit event per cycle:
  `{ideas, with_opinions, fused, conviction_pass, sized, submitted}`.

### Stage 2 — Trust parole

* `trust/ledger.py`: `negative_skill` additionally requires
  `n_non_abstain >= SHADOW_THRESHOLD` (30).  A significantly-negative
  advisor below that sample gets `cap_reason="parole"` instead.
* `trust/weight_resolver.py`: `parole` → `EQUAL_FLOOR × PAROLE_FRACTION`
  (default 0.5 → weight 0.125), non-shadow — keeps trading small, keeps
  accruing outcomes.  Full-sample `negative_skill` stays hard-muted.
* Config knobs: `ARBITER_TRUST_PROBATION_FLOOR` (default 0.25),
  `ARBITER_TRUST_PAROLE_FRACTION` (default 0.5).

### Stage 3 — Re-decide flow (standing book)

* New revisit sweep in `run_cycle` (alongside the existing unexecuted /
  stuck-idea sweeps): `FINAL_DECIDED` ideas that (a) never produced an
  order, (b) are ≥ 1 day old, (c) horizon unexpired → superseded into a
  fresh `GATHERING` idea using existing supersede mechanics (attribution
  stays clean).  Capped at 50 ideas/day (config
  `ARBITER_IDEA_REVISIT_LIMIT`) to bound Finnhub load.
* Effect: filing backlogs re-fuse against fresh opinions daily; held names
  with headroom re-enter via the existing `_addon_ok` path.

### Stage 4 — Deployment pressure

* `compute_size()`: when conviction clears the bar and pre-floor size > 0,
  raise to `min_position_pct × equity` (config
  `ARBITER_MIN_POSITION_PCT`, default 0.02), then re-clamp by every
  existing headroom/count/ADV cap — the floor can never breach a cap.
* Daemon post-close sweep: deployment = 1 − cash/equity; below 50% for 3
  consecutive sessions → warning-tier ntfy alert including the latest
  `cycle_funnel` counts (visible why-idle from the phone).  Counter lives
  in `DaemonState` (in-memory; resets on restart — acceptable).

## Non-goals

* No change to conviction threshold (0.05) — tracing decides that later.
* No laptop-off catch-up scans (deferred).
* No change to options layer, cockpit, EXECUTOR_BACKEND, or go-live state.

## Testing

TDD per repo discipline; hermetic conftest (no live ntfy); each stage lands
as its own commit; full suite green before merge to main.
