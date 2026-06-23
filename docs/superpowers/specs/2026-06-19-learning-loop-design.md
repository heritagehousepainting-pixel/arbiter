# Learning Loop — Design Spec (sub-project #4)

> Wire the **real trust ledger** (advisor weights learned from realized outcomes) and the
> **real calibrator** into fusion, replacing the hardcoded `EqualWeightBundle` + `PassthroughCalibrator`.
> This is the "self-taught" core: realized P&L → trust weights → conviction.
>
> **Status:** DESIGN ONLY. No implementation code in this document. Build via plan→build→audit waves
> against the frozen contracts in `arbiter/INTERFACES.md`.
> **Date:** 2026-06-19. Depends on #1/#2/#3 (BUILT) + Phase-2 persistence (DONE).

---

## 0. POST-AUDIT BINDING AMENDMENTS (these SUPERSEDE any conflicting text below)

The plan audit returned **GO-WITH-AMENDMENTS**. Binding; do not relitigate. (Baseline suite is **1831**,
not 1743 — fix the suite-health gate number. `trust_weights` lives in **`001_core.sql`**, not 011.)

**D0 — [P0] STRICT cutoff: exclude ALL current-cycle outcomes from the learning inputs.** The end-of-cycle
sweep is NOT the only outcome writer: the **exit monitor** (`engine.py` ~752) and the **reconcile close-out**
(`engine.py` ~461 / `exit_monitor.close_idea_on_sell_fill`) BOTH write `outcomes` rows with `created_at = now`
EARLIER in the same `run_cycle`, before the learning step. So a non-strict `created_at <= now` cutoff trains a
decision at T on outcomes resolved at T → same-cycle look-ahead. FIX: `load_outcomes_for_learning` MUST use a
STRICT cutoff `created_at < now` (equivalently, anchor to the prior cycle boundary), so nothing stamped at the
current cycle's `now` is visible to this cycle's trust/calibration. Update the design's PIT narrative to name ALL
THREE outcome writers. Add a test: close an idea via the exit monitor at T, assert it is NOT in
`load_outcomes_for_learning(as_of=T)` in the same cycle.

**D1 — [P1] Add `cap_reason` to `trust_weights` (new migration 025).** The resolver must distinguish
negative-skill suppression (`weight=0, shadow=True`, genuinely off) from onboarding/cold (floored to keep
trading) from the PERSISTED/warm-start path. `trust_weights` (001_core.sql) has no `cap_reason` column today.
Add `cap_reason TEXT` via an additive `ALTER TABLE` migration; `ledger`/`trust_store` writes it; the resolver
and warm-start read it. Without this, the negative-skill branch (D6) and its test can't be satisfied from the
persisted path.

**D2 — [P1] Backtest must recompute weights/calibrator EACH step (no cross-step cache).** A cached
`(WeightBundle, calibrators)` carries recency-decay computed at the OLD `as_of`; reused on a later step it is not
the weight the live system would have had at that step. Under `BacktestClock`, recompute every step (cheap at MVP
scale). Caching gated by `should_update` is allowed ONLY in live (`Clock`) mode. Drop any "reproduces weights
exactly" claim that the cache would violate.

**D3 — Cold/probationary floor is a FRACTION, not full 1.0 parity.** Emitting `weight=1.0` for every cold/shadow
live advisor lets an unproven (possibly garbage) advisor sit at parity with — or, post-normalization, dominate —
a fully-graduated proven advisor, and it gets worse with ≥3 advisors (two cold advisors outvote one proven). Set
`EQUAL_FLOOR` to a probationary fraction (config-exposed; default ~0.5) chosen against the composite-weight scale
so a COLD advisor can never exceed a fully-graduated high-trust advisor, while staying positive+non-shadow so the
bucket still trades (no deadlock). Keep the ramp toward the learned weight as the advisor graduates.

**D4 — Warm-start / backtest reads of `trust_weights` use the `as_of` WINDOW, not `is_superseded`.** For a live
restart warm-start, reading the latest live row (`is_superseded=0`) is fine. But for ANY backtest read,
`is_superseded` reflects the latest REAL run, not the replay's point in time → not PIT-safe. Backtest reads MUST
select `WHERE as_of <= T ORDER BY as_of DESC LIMIT 1` per advisor. Specify both read paths explicitly.

**D5 — Thin-sample calibrator gating.** `Calibrator._MIN_FIT_SAMPLES=2` yields a fragile/over-confident fit. The
wiring MUST NOT apply a fitted calibrator until a meaningful per-bucket sample exists (reuse the shadow/onboarding
gating, e.g. stay passthrough-equivalent until well above 2 outcomes); confirm `predict_proba` clamps prevent any
NaN/degenerate prob from shipping.

**Deferred to #5 (note, not built here):** outcome attribution is the horizon proxy `_advisor_id_for`
(≥180d→insider else congress). For the 2-advisor MVP (insider=180d, congress=90d, ideas minted per-source) this
is ~1:1 and acceptable for bootstrapping. It BREAKS once A2/multi-horizon advisors land — so **opinion-based
attribution (attribute the outcome to the advisor whose opinion actually drove the order, recoverable via the
order's `idea_id`) is a BINDING prerequisite of sub-project #5**, not #4.

**Build structure:** #4 adds `trust/weight_resolver.py` + `trust_store` helpers + migration 025 but also edits the
shared `engine.py` (fuse wiring, learning step) and `fusion/pool.py` (`transform_for` seam). ONE focused build
agent (TDD); audit follows.

---

## Goal

Make the engine learn. Each full `run_cycle` must:

1. Read the accumulated `outcomes` (realized SPY-beta alpha + binary labels) up to `as_of`.
2. Update the `TrustLedger` (per-advisor log-pool weights from recency-weighted Brier skill ×
   calibration × coverage, with shadow onboarding + caps) and fit the `Calibrator`.
3. Persist the resulting weights to `trust_weights`.
4. Pass the derived `WeightBundle` + real `Calibrator` into `fuse(...)`.

…while **guaranteeing the system keeps trading during the cold-start** so it generates the very
outcomes that graduate advisors out of shadow (the bootstrap), and **never letting a backtest
re-weighting at date T see an outcome resolved after T** (no-look-ahead).

Generalizes to N advisors so A2 (MiroFish, #5) slots in later with no fusion change.

---

## Current state (verified by reading the code)

- **Fusion is hardcoded equal-weight + passthrough.** `arbiter/engine.py` lines 798–800:
  ```python
  calibrator = PassthroughCalibrator()
  advisor_ids = list(self.advisor_map.keys())
  weight_bundle = EqualWeightBundle(advisor_ids)
  ```
  `_bound_fuse` (lines 811–825) calls `fuse(opinions, weight_bundle, calibrator)` per bucket.
- **`TrustLedger` is built but UNWIRED** (`arbiter/trust/ledger.py`). `.update(...)` returns a
  `WeightBundle | None`; gates:
  - **Dormancy:** returns `None` until ≥ `PHASE3_ACTIVATION_THRESHOLD = 60` total resolved outcomes.
  - **`should_update`:** needs ≥ `MIN_NEW_OUTCOMES = 5` new outcomes since last update (and not regime-frozen).
  - **Shadow onboarding:** weight 0 until ≥ `SHADOW_THRESHOLD = 30` non-abstain outcomes for that advisor;
    then a `RAMP_OUTCOMES = 10` linear ramp from 0 → composite (still `shadow=True` during the ramp);
    above 30+10 → `shadow=False`, weight = composite.
  - **Caps** (`_apply_caps`): negative skill → 0.0 + `shadow=True`; thin-sample floor 0.02 (when
    n<15 and not shadow); ceiling 0.50; MiroFish (A2.*) cap 0.35.
  - `update(...)` already takes `as_of` and uses `outcome_dates` for recency decay (no `datetime.now()`).
- **`Calibrator` is built but UNWIRED** (`arbiter/calibration/calibrator.py`). `.fit(outcomes)` fits
  per-(advisor, bucket); `.transform(raw_stance, horizon_days)` is the seam `fuse` calls via `pool.py`;
  `is_cold_start` is a property (True until a model is fit). Below `_MIN_FIT_SAMPLES = 2` non-zero
  per bucket it falls back to the `lookup_prior` (STANCE_BASE) — i.e. **passthrough-equivalent while cold**.
- **`fuse` excludes shadow / zero-weight advisors** (`pool.py` lines 80–82) and **skips the bucket
  if `norm_weights` is empty** (`engine.py` `fuse` lines 122–124). This is the deadlock surface.
- **Outcome sweep runs at the END of `run_cycle`** (`engine.py` lines 998–1008), AFTER `fuse`/decide/submit.
  So outcomes resolved this cycle are written after this cycle's fusion — they are naturally visible
  only on the NEXT cycle. Good for PIT.
- **`outcomes` table** carries `created_at` = the resolved timestamp (`outcome_store._outcome_to_row`).
  `query_outcomes(conn, advisor_id=...)` returns dicts ordered by `created_at ASC`, excludes superseded.
- **`trust_weights` table exists** (migration `001_core.sql`: id, advisor_id, weight, ci_low, ci_high,
  shadow, as_of, supersedes_id, is_superseded, created_at) but **nothing writes it today**.
- **Live advisors:** `A1.insider`, `A1.congress` (`self.advisor_map`). Form-4 (insider) is gated on
  `EDGAR_USER_AGENT`; congress runs always. The MVP outcome attribution stub (`_advisor_id_for`,
  engine line 995) maps horizon ≥180d → `A1.insider`, else `A1.congress`.

---

## Design decisions

### D1 — Bootstrap chicken-and-egg (THE CRUX): equal-weight floor → trust-weighted as advisors graduate

**Problem.** If we feed `fuse` a real ledger `WeightBundle` directly, every advisor starts `shadow=True`
(weight 0 < SHADOW_THRESHOLD), so `pool.py` excludes all of them, `norm_weights` is empty, and `fuse`
skips the bucket → no conviction → no orders → no fills → no outcomes → shadow never lifts. Deadlock.
The ledger is also **dormant** (returns `None`) until 60 total outcomes exist, and `should_update`
suppresses bundles until 5 new outcomes. So for the entire ramp-up the ledger gives us *nothing usable*.

**Decision — a per-advisor blend computed in the engine, never inside the ledger.** Each full cycle the
engine builds the `WeightBundle` it passes to `fuse` by **merging** the ledger's latest bundle with an
equal-weight floor, advisor by advisor. The rule (new module `arbiter/trust/weight_resolver.py`,
function `resolve_weight_bundle(...)`):

```
ledger_bundle = <latest persisted/computed bundle, or None if dormant/no-update>

for each advisor_id in live advisor_map:
    aw = ledger_bundle.weights.get(advisor_id) if ledger_bundle else None

    if aw is None or aw.shadow or aw.weight <= 0.0:
        # cold-start, in shadow, in ramp, negative-skill hold, or ledger dormant
        # → participate at the EQUAL-WEIGHT FLOOR so the advisor still trades.
        effective_weight = EQUAL_FLOOR            # constant 1.0 (raw log-pool)
        shadow_flag      = False                  # MUST be False so pool.py includes it
    else:
        # graduated (shadow=False, weight>0): use the learned trust weight,
        # but never let it fall below the floor (avoid a graduated-but-tiny
        # advisor being effectively muted relative to a still-cold sibling).
        effective_weight = max(aw.weight, EQUAL_FLOOR_GRADUATED)
        shadow_flag      = False

    emit AdvisorWeight(advisor_id, effective_weight, ci_low, ci_high, shadow=False)
```

Key points:

- **The bundle handed to `fuse` NEVER contains a shadow/zero advisor for a *live* advisor.** Shadow is a
  *ledger* concept (recorded-but-not-yet-trusted); the engine translates "shadow" into "trade at the
  equal-weight floor" so fusion always has ≥1 participant and the deadlock is structurally impossible.
- **Smooth transition.** While advisor X is in shadow it pools at weight `EQUAL_FLOOR` (1.0). The instant
  X graduates (ledger emits `shadow=False`, weight=composite, e.g. 0.30) the engine swaps X's raw weight
  to 0.30 while the still-cold sibling stays at 1.0. Because `pool.py` normalizes (`w_i / Σw`), this
  *is* the blend: a graduated low-trust advisor is automatically down-weighted relative to a cold sibling,
  a graduated high-trust advisor up-weighted. No separate "blend fraction" knob is needed — the floor +
  log-pool normalization produce a continuous handoff.
- **Constants** (define in `weight_resolver.py`, not magic numbers):
  - `EQUAL_FLOOR = 1.0` — raw log-pool weight a cold/shadow advisor trades at (matches `EqualWeightBundle`).
  - `EQUAL_FLOOR_GRADUATED = THIN_SAMPLE_FLOOR = 0.02` — a graduated advisor's floor (ledger already
    applies its own 0.02 thin-sample floor; we re-assert it so a graduated weight is never clamped to 0
    by the resolver). Note these two floors are intentionally different scales: a *cold* advisor pools at
    1.0 (full equal participation, because we have no skill estimate and must keep trading); a *graduated*
    advisor pools at its learned weight (typically 0.02–0.50), which is correctly << 1.0. This means the
    **first** advisor to graduate at a low composite (say 0.10) will be *down*-weighted vs a sibling still
    cold at 1.0 — which is the desired behavior: "we have evidence X is mediocre; trust the unproven one
    at least as much." Documented as an accepted asymmetry; flagged in Open Risks (R3).
- **Quorum unaffected.** The safety gate (`is_trading_allowed`, INTERFACES §8) still counts *live*
  advisors (advisors that emitted a non-None opinion this cycle), not weights. Bootstrap trading at the
  floor keeps `live_advisor_count` ≥ 1, so DEGRADED/HALTED logic is unchanged.

**Why not blend inside the ledger?** The ledger's shadow/ramp/caps are the *learning* contract and must
stay PIT-pure and advisor-agnostic (A2 reuses them verbatim). The "keep trading while cold" policy is an
*engine/fusion* concern. Keeping the blend in `weight_resolver.py` means the ledger is never weakened and
the bootstrap policy is a single, testable function.

**Files:** new `arbiter/trust/weight_resolver.py`; engine.py lines 798–800 replaced (see D2).

---

### D2 — Where/when in the cycle; cadence; order vs the outcome sweep

**Placement (full `run_cycle` only).** Insert a "learning step" in `run_cycle` **between** the active-idea
load and the fusion wiring — i.e. replace the hardcoded block at engine.py 798–800 with a call to a new
engine helper `self._build_learning_inputs(now)` that returns `(weight_bundle, calibrator)`. It runs once
per full cycle, before `_bound_fuse` closes over them.

**It reads outcomes, NOT writes them.** The learning step only *reads* `outcomes` (≤ `as_of`) and writes
`trust_weights`/`calibration_params`. The outcome **sweep stays exactly where it is** — at the END of
`run_cycle` (engine.py 998–1008), AFTER fuse/decide/submit. This ordering is the no-look-ahead guarantee
at the cycle granularity:

```
run_cycle(now):
    ... reconcile, exit monitor ...
    weight_bundle, calibrator = build_learning_inputs(now)   # reads outcomes resolved <= now
    fuse(...) using weight_bundle + calibrator               # decisions at `now`
    decide / submit
    ... snapshot positions ...
    outcome_runner.run_outcome_sweep(now)                    # writes NEW outcomes resolved at `now`
```

Because outcomes resolved *this* cycle are written *after* this cycle's `build_learning_inputs`, the
weights used at `now` are computed strictly from outcomes resolved on a **prior** cycle (≤ now − one
cycle), never from outcomes minted in the same cycle. This is both PIT-correct and intuitive (you can't
grade a decision using its own future result).

**Cadence — gate on new outcomes via `should_update`, cache otherwise.** Recomputing the ledger +
refitting the calibrator every cycle is wasteful (most cycles add 0 new resolved outcomes). The learning
step:

1. Loads the current `outcomes_by_advisor` (≤ now, see D3).
2. Calls `ledger.should_update(outcomes_by_advisor, as_of=now)`.
   - **If True:** call `ledger.update(...)`, persist the new bundle to `trust_weights`, re-`fit` the
     calibrators, persist `calibration_params`, cache `(bundle, calibrators, last_outcome_count)`.
   - **If False (no/insufficient new outcomes, or dormant):** reuse the cached `ledger_bundle` and fitted
     calibrators from the previous full cycle (or `None`/cold if never computed). No recompute.
3. Always run `resolve_weight_bundle(ledger_bundle, live_advisor_ids)` (D1) — cheap, deterministic, runs
   every cycle so a freshly-added advisor immediately gets the floor.

`TrustLedger` is **stateful** (`last_update_at`, `outcomes_at_last_update`), so it must be a long-lived
member of the engine: add `self.ledger: TrustLedger`, `self.calibrators: dict[str, Calibrator]`, and a
cache `self._learning_cache` in `build_engine` (D7). The daemon's **fast iterations do NOT call
`run_cycle`** (they only reconcile + check live stops, per #3), so the heavy ledger/calibrator work runs
only on the 2×/day full cycles — no perf concern in the hot loop.

**Files:** engine.py (new `_build_learning_inputs`, `__init__`/`build_engine` members); new
`arbiter/trust/weight_resolver.py`; new `arbiter/trust/trust_store.py` (persist/read `trust_weights`).

---

### D3 — No-look-ahead enforcement (critical for backtests)

**The cutoff anchor is `outcomes.created_at`** (the resolved timestamp written by the sweep). Every input
to the learning step must be filtered to `created_at <= as_of` where `as_of = self.clock.now()` (the
simulated as_of under `BacktestClock`).

**Mechanism — filter at the query, pass `as_of` to every learner.**

1. New helper `load_outcomes_for_learning(conn, *, as_of) -> dict[str, list[tuple[ResolvedOutcome, datetime]]]`
   (place in `trust_store.py`). It runs `query_outcomes(conn, advisor_id=a)` per advisor and then
   **drops every row with `created_at > as_of`**, reconstructs a `ResolvedOutcome` from the row, and pairs
   it with `datetime.fromisoformat(row["created_at"])` as the resolved date. `query_outcomes` already
   excludes superseded rows and orders by `created_at ASC`.
   - *Refinement to the store:* add an optional `as_of: datetime | None = None` kwarg to `query_outcomes`
     that appends `AND created_at <= ?` when provided, so the cutoff is enforced in SQL (defense in depth)
     rather than only in Python. Backwards-compatible (defaults to no filter).
2. `ledger.update(..., as_of=now)` — already takes `as_of` and uses it for recency decay
   (`brier.brier_skill_score` / `_decay_weight` clamp `as_of - outcome_date` to ≥0). With the rows
   pre-filtered to ≤ now, no future outcome can enter the BSS sum or the correlation matrix.
3. `Calibrator.fit(outcomes)` — does **not** take an as_of, so it MUST be fed the **already-cutoff**
   outcome list. The learning step passes the same pre-filtered per-advisor list to `fit`. (Note: the
   calibrator has no internal date awareness, so the cutoff is purely the caller's responsibility — this
   is the single most error-prone seam; the test suite asserts it directly, see Test T4.)

**Why this is backtest-safe.** Under `BacktestClock`, `clock.now()` returns the simulated as_of T. The
learning step filters `created_at <= T`, so re-weighting "as of T" sees only outcomes resolved on/before T.
A walk-forward backtest that steps T forward day-by-day will reproduce exactly the weights the live system
would have had on each date. The live path uses the real `Clock`, where `created_at <= now` is trivially
all rows.

**`check_no_lookahead.sh` stays clean.** The learning step adds no `datetime.now()` / `get_latest()` calls
— all timestamps come from `self.clock.now()`. `datetime.fromisoformat(...)` parsing the stored
`created_at` is not flagged (the script only forbids `datetime.now()`/`utcnow()`/`get_latest()`). The new
`trust_store.py` / `weight_resolver.py` must contain **no** `datetime.now()`.

**Files:** `trust_store.py` (`load_outcomes_for_learning`); `evaluation/outcome_store.py` (optional `as_of`
kwarg on `query_outcomes`); engine.py (passes `self.clock.now()` as `as_of`).

---

### D4 — WeightBundle construction from the ledger + correlation matrix (v1)

**Per-advisor weights.** `ledger.update(...)` returns a `WeightBundle` whose `weights` are already the
learned per-advisor log-pool `AdvisorWeight`s (composite trust after caps/ramp). The engine does **not**
re-derive them — it takes them as-is and runs them through `resolve_weight_bundle` (D1) to apply the
bootstrap floor and strip the shadow flag for live advisors.

**Eligible-idea roster (required input to `ledger.update`).** Coverage = opined / eligible. If the
eligible roster is empty, coverage → 0 → composite → 0 → all weights 0 (the loud-warning deadlock guarded
in `ledger.update`). v1 wiring:
- `eligible_by_advisor[advisor_id]` = the set of `outcomes.idea_id` attributed to that advisor up to
  `as_of` (i.e. every idea the advisor *did* produce an outcome on counts as eligible). This makes
  coverage ≈ 1.0 in v1 (advisors opine on every idea they're attributed). Acceptable for the 2-advisor
  MVP because the MVP `_advisor_id_for` stub attributes exactly one advisor per idea; coverage isn't yet a
  discriminating signal. **Flagged as a refinement (R2):** a real roster (all ideas in the advisor's
  ticker/horizon scope, including ones it abstained on) requires Lane-13 idea→advisor eligibility, which
  is out of scope here.

**Correlation matrix — v1 = ledger default, no engine override.** `ledger.update` already builds a
`CorrelationMatrix` from outcomes + `fingerprints_by_advisor` and writes it into the returned bundle. For
v1:
- Pass `fingerprints_by_advisor=None` (we don't yet thread opinion `source_fingerprint`s into the ledger).
  The ledger's `CorrelationMatrix.build` then uses its own sparse-sample behavior (the §5 0.5 prior /
  outcome-correlation estimate).
- **`resolve_weight_bundle` passes the ledger's `correlation_matrix` through UNCHANGED** for graduated
  advisors, but because the bootstrap path emits a bundle keyed by *live* advisor ids with floored weights,
  v1 ships the correlation matrix as the ledger produced it (possibly empty while dormant). When the
  ledger is dormant/None, the resolver emits an **empty** correlation matrix → `fusion/correlation.py`
  defaults missing off-diagonal pairs to ρ=0.0 → `effective_n ≈ N` (no deflation). This is the documented
  Phase-1-safe default and is correct for 2 weakly-related lanes (insider vs congress).
- **Refinement (R1):** real cross-advisor correlation from co-observed outcomes + `source_fingerprint`
  collisions (insider Form-4 and congress PTR on the same ticker/week ARE correlated). Deferred to the
  correlation-deflation sub-project; the seam is ready (`fingerprints_by_advisor` param already exists).

**Files:** `weight_resolver.py` (pass-through of corr matrix + floor on weights); engine `_build_learning_inputs`
(builds `eligible_by_advisor` from outcome idea_ids).

---

### D5 — Calibrator fit cadence + cold-start (drop-in for PassthroughCalibrator)

**One `Calibrator` per advisor, long-lived on the engine** (`self.calibrators: dict[str, Calibrator]`,
constructed in `build_engine` with the engine's `conn` so `.persist(as_of)` can write `calibration_params`).

**Fit cadence = same gate as the ledger.** Re-`fit` only when `should_update` fired this cycle (i.e. ≥5
new outcomes). On fit:
- `self.calibrators[a].fit(outcomes_a)` where `outcomes_a` is the **cutoff-filtered** list for advisor a
  (D3). `Calibrator.fit` self-filters by `advisor_id` and bucket, so passing the per-advisor list (or even
  all outcomes) is safe; passing the cutoff list is what enforces no-look-ahead.
- `self.calibrators[a].persist(as_of=now)`.

**The real Calibrator IS a drop-in for `PassthroughCalibrator`.** `fuse` only needs
`transform(raw_stance, horizon_days)` + `is_cold_start`. The real `Calibrator` exposes both. The engine
must hand `fuse` a **single** calibrator object, but `Calibrator` is per-advisor while `pool.py` calls
`calibrator.transform(op.stance_score, op.horizon_days)` once per opinion regardless of advisor.

**Resolution — a thin per-cycle `MultiAdvisorCalibrator` adapter** (new, in `arbiter/calibration/`):
wraps `dict[str, Calibrator]` and routes `transform` by the advisor of the opinion being pooled. BUT
`pool.py`'s `transform(raw_stance, horizon_days)` signature carries **no advisor_id**. Two options:

- **Chosen (v1):** keep the single-calibrator contract by giving `MultiAdvisorCalibrator.transform` the
  *fallback* behavior of the first-fit / a shared model is wrong for per-advisor. Instead, **extend the
  fusion seam minimally**: `pool.py` already iterates opinions and knows `op.advisor_id`; change its call
  to `calibrator.transform_for(op.advisor_id, op.stance_score, op.horizon_days)` with a default
  implementation `transform_for(self, advisor_id, s, h) = self.transform(s, h)` on the base/passthrough so
  the contract stays backward-compatible. `MultiAdvisorCalibrator.transform_for` dispatches to the right
  per-advisor `Calibrator`. `is_cold_start` on the adapter = True iff **every** wrapped calibrator is cold
  (so `FusionOutput.cold_start` flips False only once at least one advisor has a fitted model).
  - This is a **deliberate, documented amendment to the fusion seam** (additive method; existing
    `transform` untouched; `PassthroughCalibrator` gets a one-line `transform_for`). Note it in INTERFACES.
- **Rejected:** stuffing advisor_id into `horizon_days` or a thread-local — fragile, hidden coupling.

**Cold-start behavior is automatic and passthrough-equivalent.** Below `_MIN_FIT_SAMPLES = 2` non-zero
outcomes per (advisor, bucket), `Calibrator.transform` returns `lookup_prior(...)` (STANCE_BASE) — a
monotone map of stance, behaviorally the passthrough during cold-start. No special-casing in the engine:
while everything is cold, conviction is driven by raw stance exactly as it is today.

**Files:** new `arbiter/calibration/multi_advisor.py` (`MultiAdvisorCalibrator`); `fusion/pool.py`
(call `transform_for`); `fusion/engine.py` `PassthroughCalibrator` (+`transform_for` shim); INTERFACES note.

---

### D6 — Shadow→live transition & caps (wire, don't reimplement)

All of negative-skill→0, thin-sample floor, ceiling, MiroFish cap, shadow ramp already live in
`ledger._apply_caps` + `_shadow_ramp_weight` and run inside `ledger.update`. The engine does **not**
reimplement any of them. The only transition logic the engine adds is `resolve_weight_bundle` (D1), which:

- Treats `shadow=True` (cold OR in-ramp OR negative-skill-hold) as "trade at the equal floor, `shadow=False`
  in the emitted bundle." So a still-onboarding advisor keeps trading at floor 1.0.
- Treats `shadow=False, weight>0` (fully graduated) as "use the learned weight." This is the exact point a
  graduated advisor's *learned* weight starts influencing fusion — the ledger flips `shadow→False` after
  the 30+10 ramp, the resolver stops flooring it to 1.0 and starts passing its composite weight, and
  `pool.py` normalization re-balances the pool accordingly.

One subtlety to encode + test: a **negative-skill** advisor comes back from the ledger as `weight=0.0,
shadow=True`. Under D1 that maps to the equal floor (it keeps trading at 1.0) — which is **wrong**: a
demonstrably-harmful advisor should be *suppressed*, not floored back to full participation. **Decision:**
`resolve_weight_bundle` distinguishes the two shadow reasons. The ledger sets `shadow=True` for both
"onboarding" and "negative-skill hold," but only negative-skill also has a **completed sample** (n ≥
SHADOW_THRESHOLD) AND `composite`/BSS < 0. The resolver cannot see BSS directly, so we thread it through:
`ledger.update` already knows `is_negative_skill`; persist a `cap_reason` (the `trust_advisor_scores`
table has a `cap_reason` column — `"negative_skill"`). The resolver reads `cap_reason` from the persisted
weight row (D7) and, for `cap_reason == "negative_skill"`, emits **weight 0.0, `shadow=True`** (genuinely
muted) instead of the floor. Onboarding shadows still floor to 1.0.

- *Simplification accepted for v1:* if persisting/reading `cap_reason` through `trust_store` is heavier
  than desired, the fallback is: resolver floors all shadows to 1.0 **except** when a prior persisted
  row for that advisor shows it had already graduated (shadow=False) and has now regressed to weight 0 —
  but the clean path is the `cap_reason` thread. Build the `cap_reason` path; it's one column.

**Files:** `weight_resolver.py` (negative-skill suppression vs onboarding floor); `trust_store.py`
(persist + read `cap_reason`); reuse all ledger caps unchanged.

---

### D7 — Performance / persistence

**Avoid redundant recompute.** Covered by D2: `should_update` gates the heavy `ledger.update` +
`calibrator.fit`. On a no-new-outcome cycle the engine reuses the cached bundle/calibrators — O(1).
`resolve_weight_bundle` runs every cycle but is O(advisors) (2 today). Loading outcomes is one indexed
`SELECT` per advisor (≤ a few hundred rows in the MVP) — cheap; can be skipped entirely if a cheaper
"new outcome count since last update" probe (one `SELECT COUNT(*) WHERE created_at <= now`) shows no
growth, but the simple per-advisor load is fine at MVP scale.

**Persist to `trust_weights`.** New `arbiter/trust/trust_store.py`:
- `persist_weight_bundle(conn, bundle, *, as_of)` — one **insert-only** row per advisor into
  `trust_weights` (id ULID, advisor_id, weight, ci_low, ci_high, shadow, as_of, created_at), using
  `insert_row`. Supersede prior live rows for the same advisor via `supersede_row` (or leave append-only
  and read the latest by `as_of DESC` — choose append-only + read-latest to match the table's
  `supersedes_id`/`is_superseded` design; simplest is insert + mark prior `is_superseded`). Persist the
  **ledger's** bundle (the learned weights), not the floored engine bundle, so the table is an honest
  record of what was learned (the floor is a runtime trading policy, not a learned weight).
  - Also write the auditable decomposition to `trust_ledger_snapshots` + `trust_advisor_scores` +
    `trust_correlation_entries` (migration 011) if the build wants the full audit trail; v1 minimum is
    `trust_weights`.
- `load_latest_weight_bundle(conn, *, as_of) -> WeightBundle | None` — reads the most recent non-superseded
  `trust_weights` rows with `as_of <= now` (PIT for backtests) to **warm-start** the engine on process
  restart (so a restarted daemon doesn't fall back to all-cold until the next `should_update`). Correlation
  matrix is re-derived on the next `ledger.update`; the warm-start bundle ships an empty matrix.

**Calibrator persistence** uses the existing `Calibrator.persist(as_of)` → `calibration_params` (012).
sklearn models are re-fit from outcomes on restart (the table is metadata/audit only, per its docstring),
so on restart the engine re-`fit`s from the cutoff-filtered outcomes once `should_update` fires (or
eagerly on first full cycle after restart — acceptable, it's the 2×/day path).

**Daemon hot loop untouched.** Fast iterations (#3) never call `run_cycle`, so the ledger/calibrator/persist
work never runs in the 180s loop. Only the 09:45 / 15:30 ET full cycles do learning.

**Files:** new `arbiter/trust/trust_store.py`; engine `build_engine` (construct `self.ledger`,
`self.calibrators`, warm-start from `load_latest_weight_bundle`), `_build_learning_inputs`.

---

### D8 — Scope

**IN:** trust-weight + calibration wiring into fusion (D1–D7); the bootstrap floor; no-look-ahead cutoff;
`trust_weights` persistence; the `transform_for` seam addition; generalization to N advisors.

**OUT (note as future):**
- **MiroFish A2 (#5).** Not wired here, but the design is N-advisor: `resolve_weight_bundle` iterates
  `live advisor_map`, and the MiroFish 0.35 cap already lives in `_apply_caps`. A2 slots in by registering
  the advisor + threading its opinions/outcomes — no fusion change.
- **Real correlation deflation (R1).** `fingerprints_by_advisor` + co-observation correlation. Seam ready.
- **Real eligible-idea roster (R2).** Needs Lane-13 idea→advisor eligibility; v1 uses attributed-outcome
  idea_ids (coverage ≈ 1).
- **Regime-tracker freeze.** `should_update` already honors `regime_tracker.is_frozen`; v1 passes
  `regime_tracker=None` (never frozen). Wiring real regime detection is a separate task.

---

## Test strategy (OFFLINE — synthetic outcomes drive weight changes)

All tests live under `arbiter/tests/` mirroring the package, use `pytest`, **no network**, and drive the
ledger/calibrator with **synthetic `ResolvedOutcome` rows** inserted via `outcome_store.store_outcome`
into an in-memory/migrated SQLite conn, with a `BacktestClock` controlling `as_of`.

- **T1 — bootstrap trades at cold-start (the make-or-break).** Fresh DB, 0 outcomes. Build the learning
  inputs; assert `resolve_weight_bundle` emits both live advisors with `shadow=False`, weight = `EQUAL_FLOOR`,
  and that a `fuse(opinions, bundle, calibrator)` over both advisors returns a **non-empty** dict with a
  pooled bucket (i.e. NOT skipped). Assert `is_cold_start` True and conviction == equal-weight conviction.
  **This proves no deadlock.**
- **T2 — dormant ledger → still trades.** Insert 40 synthetic outcomes (< 60 activation threshold).
  `ledger.update` returns None. Assert the engine reuses the floor bundle and still produces orders/conviction.
- **T3 — graduation transition.** Insert ≥ 60 total outcomes, ≥ 40 non-abstain for `A1.insider` (past
  30+10 ramp) with strongly positive alpha (high BSS), and keep `A1.congress` below 30 (still shadow).
  Force `should_update` (or insert ≥5 new). Assert: insider comes back `shadow=False` with a learned weight
  in (0, 0.50]; congress floors to 1.0; the resolved bundle has insider's *learned* weight and congress at
  1.0; and after `pool.py` normalization insider's normalized weight reflects the blend (e.g. a high-BSS
  insider at 0.50 vs congress floor 1.0 gives insider ≈ 0.33 — verify the down/up-weighting direction).
- **T4 — no-look-ahead cutoff respected (backtest correctness).** Insert outcomes resolved at T-10 and
  T+10 (created_at on both sides of the backtest as_of T). Set `BacktestClock` to T. Assert
  `load_outcomes_for_learning(conn, as_of=T)` returns ONLY the ≤T rows, that the BSS/weights computed match
  a control run containing *only* the ≤T rows, and that they DIFFER from a run that (wrongly) includes the
  T+10 rows. Also assert `Calibrator.fit` was fed only ≤T outcomes (spy/capture the list).
- **T5 — negative-skill suppression vs onboarding floor.** One advisor with ≥30 non-abstain, strongly
  *negative* alpha (BSS<0 → ledger weight 0, `cap_reason="negative_skill"`). Assert the resolver emits it
  at **weight 0.0, shadow=True** (suppressed, excluded by `pool.py`) — NOT floored to 1.0. A second,
  onboarding (cold) advisor in the same bundle still floors to 1.0. Assert the bucket still trades on the
  cold advisor (no deadlock even with one advisor suppressed).
- **T6 — cadence / caching.** Two consecutive full cycles with no new outcomes between them: assert
  `ledger.update` is called at most once (spy), the cached bundle is reused, and `trust_weights` gets no
  duplicate insert on the no-change cycle.
- **T7 — calibrator drop-in + cold-start flag.** Assert `MultiAdvisorCalibrator.transform_for` routes per
  advisor; with all advisors cold, `is_cold_start` is True and `transform_for` ≈ STANCE_BASE prior
  (passthrough-equivalent); after fitting one advisor with ≥2 non-zero outcomes, `is_cold_start` flips
  False and that advisor's transform uses the fitted model.
- **T8 — persistence round-trip + warm start.** After a graduating cycle, assert `trust_weights` has the
  learned (not floored) rows; a fresh engine `load_latest_weight_bundle(as_of=now)` reconstructs the bundle;
  the warm-started engine produces the same resolved bundle without re-running `ledger.update`.
- **T9 — no-lookahead lint stays clean.** Run `bash scripts/check_no_lookahead.sh` in CI; new modules
  (`trust_store.py`, `weight_resolver.py`, `multi_advisor.py`) contain no `datetime.now()`.
- **Suite health:** full `pytest` stays green (~1743 baseline + new tests) and fast/offline.

---

## Out-of-scope (restated)

MiroFish A2 wiring; real correlation deflation; real eligible-idea roster; real regime detection; any
live-money path (`LIVE_TRADING` stays false); UI/leaderboard changes beyond what already reads `outcomes`.

---

## Open risks

- **R1 — correlation underestimate.** v1 ships ρ=0.0 (empty matrix) while dormant and the ledger's own
  estimate after. Insider Form-4 and congress PTR on the same name ARE correlated; treating them as
  independent inflates `effective_n` and conviction. Mitigated by the 2-advisor scale (small absolute
  error); fully fixed by the correlation-deflation sub-project (seam ready).
- **R2 — coverage ≈ 1 in v1.** Using attributed-outcome idea_ids as the eligible roster makes coverage a
  near-constant, so it doesn't yet penalize abstention-gaming. Fine for the MVP attribution stub (one
  advisor per idea); needs a real Lane-13 roster to become discriminating.
- **R3 — floor asymmetry (1.0 cold vs ≤0.50 graduated).** The first advisor to graduate at a low composite
  is *down*-weighted vs a still-cold sibling at 1.0. This is intended ("trust the unproven one at least as
  much as a proven-mediocre one") but is a policy choice; if it proves wrong, tune `EQUAL_FLOOR` or blend
  by `(1 - ramp_progress)` instead of a hard floor.
- **R4 — `transform_for` seam change.** Touching `pool.py` + the calibrator contract is a cross-lane event.
  Mitigated by making it purely additive (`transform_for` defaults to `transform`); existing tests must
  stay green. Must be recorded in INTERFACES.md as a deliberate amendment.
- **R5 — MVP advisor attribution stub.** `_advisor_id_for` attributes outcomes by horizon, not by which
  advisor actually emitted the winning opinion. Until real attribution lands, trust weights learn against a
  proxy label — directionally fine for bootstrapping, but the learned weights are only as good as the stub.
- **R6 — same-cycle PIT edge.** The guarantee relies on the sweep running strictly *after*
  `build_learning_inputs`. If a future refactor moves the sweep earlier, no-look-ahead breaks. The ordering
  must be asserted by a test (covered implicitly by T4 with a within-cycle outcome) and called out in a
  code comment at both call sites.

---

## File-change summary (for the build wave)

| File | Change |
|---|---|
| `arbiter/trust/weight_resolver.py` | **NEW** — `resolve_weight_bundle(ledger_bundle, live_ids)`; floor/shadow/negative-skill logic (D1, D6). |
| `arbiter/trust/trust_store.py` | **NEW** — `load_outcomes_for_learning(conn, as_of)`, `persist_weight_bundle(conn, bundle, as_of)`, `load_latest_weight_bundle(conn, as_of)` (D3, D7). |
| `arbiter/calibration/multi_advisor.py` | **NEW** — `MultiAdvisorCalibrator` wrapping `dict[str, Calibrator]`; `transform_for`, `is_cold_start` (D5). |
| `arbiter/evaluation/outcome_store.py` | `query_outcomes(..., as_of=None)` optional SQL cutoff (D3). |
| `arbiter/fusion/pool.py` | Call `calibrator.transform_for(op.advisor_id, …)` (additive seam) (D5). |
| `arbiter/fusion/engine.py` | `PassthroughCalibrator.transform_for` shim (D5). |
| `arbiter/engine.py` | Replace lines 798–800; add `self.ledger`/`self.calibrators`/`self._learning_cache`; new `_build_learning_inputs(now)`; warm-start in `build_engine`; keep sweep at end (D2, D7). |
| `arbiter/INTERFACES.md` | Note the additive `transform_for` seam amendment (R4). |
| `arbiter/tests/trust/`, `arbiter/tests/fusion/`, `arbiter/tests/engine/` | T1–T9. |
