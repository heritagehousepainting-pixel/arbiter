# E1 — Trust-Ledger Math Audit

**Lane:** E1 (trust-ledger statistical correctness)
**Auditor:** READ-ONLY (no source/test/config modified)
**Date:** 2026-06-19
**Scope:** `arbiter/trust/ledger.py`, `arbiter/trust/brier.py` (+ `coverage.py`, `contract/seams.py` for field contracts). Attribution (E3), weight-resolver/fusion (E4), A1-stance sensitivity (E5) explicitly out of scope.

---

## VERDICT

**The trust math is mostly sound, with one P1 degenerate-input blow-up and one P1 dead-code floor that silently defeats a stated INTERFACES guarantee.** The post-#5a Brier/BSS is statistically correct *when inputs are in range*: scoring the real `stance_score × confidence` forecast against the realized `binary` genuinely lets a confidently-wrong advisor earn BSS < 0, so negative-skill suppression is now reachable (the #5a fix is real). The geometric-mean composite, the 182-day half-life, the shadow ramp, and the cap ordering all compose correctly. The two material risks are (1) `advisor_confidence`/`stance_score` are never clamped on the path into Brier, so an out-of-range input drives `p_hat` outside [0,1] and BSS to large negative values (−8 observed at confidence=2), and (2) the thin-sample floor is unreachable, so cold advisors it was meant to keep trading are instead zeroed. Neither blocks a paper run, but both should be fixed before real capital and before MiroFish onboarding leans on the 30-outcome shadow gate.

---

## FINDINGS

### P1 — Unbounded confidence/stance escapes [0,1], blowing up BSS — `brier.py:117` — degenerate-input blow-up
`p_hat = _stance_to_prob(outcome.stance_score * outcome.advisor_confidence)` assumes `stance_score ∈ [-1,1]` and `advisor_confidence ∈ [0,1]`, but **neither field is clamped anywhere on the path** (`outcome_labeler.py:135-136` passes both straight through unvalidated; `seams.py:183-184` are bare floats; `brier.py` does no clipping). If `stance_score × confidence` exceeds 1 in magnitude, `_stance_to_prob` returns a value outside [0,1], `bs = (p_hat - p_outcome)²` exceeds 1, and `BSS = 1 - bs/0.25` goes far below the natural −3 floor. Verified numerically: `confidence=2, stance=1, binary=-1` → `p_hat=1.5`, `BS=2.25`, **BSS=-8.0**. Because `is_negative_skill = bss < 0`, a single out-of-range row can permanently mark an advisor negative-skill (weight→0 + diagnostic hold), and because the Brier is recency-*weighted*, a recent bad row dominates. The math is correct for in-range inputs; the defect is the missing boundary contract.
**Why it matters:** a mis-scaled or buggy upstream confidence (e.g. a 0-100 scale instead of 0-1) silently and irreversibly suppresses an advisor, with no clip to catch it.
**Recommended action (E5/labeler owns the field; E1 should defend at the boundary):** clamp in `_stance_to_prob`/`recency_weighted_brier` — `p_hat = np.clip(_stance_to_prob(np.clip(stance,-1,1) * np.clip(conf,0,1)), 0.0, 1.0)` — and/or assert range at `ResolvedOutcome` construction. Minimal, math-preserving fix is the `np.clip` on `p_hat`.

### P1 — Thin-sample floor (0.02) is dead code — `ledger.py:112-113` — stated INTERFACES guarantee never fires
`_apply_caps` applies the floor only when `n_outcomes < THIN_SAMPLE_THRESHOLD (15)` **and** `not is_shadow`. But an advisor is non-shadow only after the ramp completes, which requires `n_non_abstain ≥ SHADOW_THRESHOLD (30) + RAMP_OUTCOMES (10) = 40`. Since `n_non_abstain ≤ n_outcomes`, a non-shadow advisor always has `n_outcomes ≥ 40`, so `n_outcomes < 15` is impossible. Verified exhaustively: **no (total, non-abstain) pair satisfies `total<15 ∧ non_abstain≥40`.** Every still-cold advisor is `is_shadow=True` (floor skipped) and gets weight 0, not 0.02. INTERFACES.md:157 advertises "floor 0.02" and the resolver (D1/D6, `cap_reasons`) is built to tell a floored-cold advisor apart from a muted negative-skill one — but the floor that distinction relies on never executes.
**Why it matters:** the design intent ("keep a thin/cold advisor lightly trading at 0.02 rather than fully muting") is silently void; cold and suppressed advisors are indistinguishable at the weight level.
**Recommended action:** decide intent. Either (a) raise `THIN_SAMPLE_THRESHOLD` above 40 / apply the floor to the *ramped* (shadow-graduated-but-still-low-sample) band, or (b) if the intent is genuinely "shadow until 40, then full weight, no floor band," delete `THIN_SAMPLE_FLOOR`/`THIN_SAMPLE_THRESHOLD` and the INTERFACES line so the contract matches reality.

### P2 — `should_update` new-outcome count is reset-blind and add-only — `ledger.py:258-265` — undercount/zero-count after replays or removals
New outcomes are counted as `max(0, len(records) - prev)` per advisor against `outcomes_at_last_update`. This silently mishandles two cases: (1) if an advisor's record list *shrinks* between updates (relabel/early-exit correction, dedup, backfill replacing rows), the per-advisor delta is clamped to 0, so genuinely-changed outcomes never trigger an update; (2) a *content* change to an existing outcome (e.g. a relabel from `early_exit`→`reversal`, or an `alpha_bps` correction flipping `binary`) with the same row count produces delta 0 and is never re-scored. The gate only senses list-length growth, not content churn.
**Why it matters:** label corrections (a real path — `label_kind` includes `reversal`/`corporate_event`/`partial`) can leave a stale WeightBundle in force indefinitely.
**Recommended action:** track a content hash or a monotonic last-seen outcome cursor per advisor rather than a bare count, or include "any advisor whose record-set hash changed" in the new-count.

### P2 — CI bounds are a fixed ±20%, not a confidence interval — `ledger.py:384-387` — mislabeled / sample-blind
`ci_low = composite*0.8`, `ci_high = composite*1.2` is a fixed multiplicative band independent of sample size, BSS variance, or coverage. It is not a Wilson/bootstrap interval despite the "Wilson-like" comment and the `ci_low/ci_high` naming. A 30-outcome advisor and a 3000-outcome advisor with equal composite get identical CI width. The band is also asymmetric in a way that doesn't reflect Brier uncertainty, and it collapses to [0,0] when composite=0 (fine) but never widens for thin samples (wrong — thin samples should have *wider* CIs). The code comments honestly flag this as a Phase-3 placeholder ("Wave-C: replace with proper bootstrap CI").
**Why it matters:** any downstream consumer (E4 fusion) that reads `ci_low`/`ci_high` as a real uncertainty band will under-discount thin-sample advisors. Acceptable *as a labeled stub*; a latent bug if fusion trusts it.
**Recommended action:** keep as stub but rename to `weight_band_lo/hi` or gate its use, OR implement a sample-size-aware band (e.g. width ∝ 1/√n_effective using the decay-weighted effective sample size already available from `total_weight`).

### P3 — `compute_composite_trust` accepts `regime_tracker` but never uses it — `ledger.py:132,179` — silent no-op parameter
`compute_composite_trust` takes `regime_tracker` and its docstring says "Optional RegimeTracker to apply post-regime 2× weights," but the function never references it; regime weighting (`apply_regime_weights`, imported at `ledger.py:56`) is applied nowhere in the composite path. The 2× post-regime multiplier described in the docstring does not happen here. (Whether it *should* live here vs. in E4 fusion is an E4 question, but the dangling param + misleading docstring is an E1 cleanliness issue and a correctness trap for the next editor.)
**Recommended action:** either wire `apply_regime_weights` into the composite (if regime adjustment is meant to be a trust-term) or drop the parameter and the docstring claim so the seam is honest.

### P3 — No-call (`binary==0`) skip in Brier but counted in coverage — `brier.py:104-109` + `coverage.py:58-63` — mild coverage-gaming residue
Brier correctly *skips* `binary==0` rows (the ±25bps no-call band) to avoid the free BS=0 perfect score — this is sound. But coverage counts that same idea as "opined" (any non-abstained row whose `idea_id ∈ eligible_set`). So an advisor who emits many near-zero / market-ambiguous stances pays no Brier penalty yet still earns full coverage credit for those ideas. The #5a comment explicitly motivates skipping no-calls in Brier to avoid skill inflation; the symmetric coverage credit partially re-opens that door (coverage↑ with zero skill exposure). Magnitude is bounded (coverage is one of three geometric-mean terms, capped at 1.0), so this is minor.
**Recommended action:** consider whether `binary==0` non-abstained rows should count toward coverage's `opined_count`, or document that no-call opinions are intentionally coverage-credited (they *were* genuine assigned calls).

---

## VERIFIED-SOUND (no finding)

- **#5a Brier forecast is real and statistically correct.** Scoring `p_hat = (stance·conf+1)/2` against `p_outcome ∈ {0,0.5,1}` makes BS≤0.25 ⇏ always-true; confidently-wrong (s=1,c=1,b=−1) → BS=1 → **BSS=−3**, so negative-skill suppression is genuinely reachable (was structurally unreachable under the old binary-reconstructed forecast). Confidence correctly pulls `p_hat` toward 0.5 (low-conf wrong is penalized less). ✔
- **BSS floor is −3 for in-range inputs** (max in-range BS=1, BS_REF=0.25). Bounded — no infinite blow-up *given the P1 clamp is added*. ✔
- **Composite = geometric mean (skill·cal·cov)^(1/3)** is correctly implemented, with explicit zero-short-circuit (any zero term → 0) and `np.clip` to [0,1]. Geometric mean is the right choice (one weak leg drags the whole score; can't be averaged away). Negative skill clamped to 0 before the mean. ✔
- **Half-life 182d (26 weeks):** `w=2^(-days/182)` gives exactly 0.5 at 182d, ~0.249 at 365d. Sensible for a trading-signal trust horizon; `delta_days` floored at 0 (no future-dated negative decay). ✔
- **Shadow ramp composes correctly:** n<30→(0,shadow); 30→(0,shadow) (ramp_progress 0); 31→(0.04·comp band... =composite·0.1); 39→composite·0.9; 40→full composite, shadow off. Continuous, monotone, no jump or >composite overshoot. ✔
- **Cap ordering is correct and no weight exceeds its cap:** negative-skill early-returns 0.0 before any floor; ceiling 0.50 then MiroFish 0.35 applied as successive `min()`, so MiroFish can never exceed 0.35 and no advisor exceeds 0.50. Ramp output ≤ composite ≤ 1, then capped — no path produces weight > cap. ✔
- **Empty-roster guard** (`ledger.py:320`) correctly warns loudly that an unwired roster collapses coverage→0→all weights 0. Good defensive logging (not a silent deadlock). ✔
- **`total_weight==0 → None`** in Brier and the `None`→full-shadow handling in `update` correctly avoid division-by-zero on all-abstain advisors. ✔

---

## OPPORTUNITIES TO ADD

1. **Boundary assertions on `ResolvedOutcome`** — validate `stance_score ∈ [-1,1]`, `advisor_confidence ∈ [0,1]`, `binary ∈ {-1,0,1}` at construction (frozen dataclass `__post_init__`). Closes the P1 blow-up at the source and documents the contract Brier silently assumes.
2. **Effective sample size surfacing** — `recency_weighted_brier` already computes `total_weight` (the decay-weighted N). Returning/exposing it would let CI become sample-aware (fixes P2-CI), let `should_update` reason about effective vs raw counts, and let the 30-outcome shadow gate be expressed in *effective* outcomes (a cluster of stale outcomes shouldn't graduate an advisor).
3. **Document the basis for the magic constants.** SHADOW_THRESHOLD=30, RAMP_OUTCOMES=10, THIN_SAMPLE_THRESHOLD=15, MIN_NEW_OUTCOMES=5, PHASE3_ACTIVATION_THRESHOLD=60 appear only in code. Only "30 shadow outcomes" has a spec anchor (ROADMAP Phase 4). A one-line rationale each (statistical power target for 30? why 15 if it's dead?) would prevent the P1-dead-code class of drift.
4. **BSS lower-clamp** — even with input clamping, consider clamping BSS at a documented floor (e.g. −1 or −3) for downstream stability and to make "how negative" comparable across advisors with different recency profiles.
5. **A degenerate-sample test matrix** — single-outcome advisor, all-abstain, all-no-call, one out-of-range confidence row, exactly-30 / exactly-40 ramp boundaries, and a record-set that shrinks between updates. These exercise every edge above.
