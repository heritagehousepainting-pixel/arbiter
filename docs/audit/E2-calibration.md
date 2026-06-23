# E2 — Calibration Audit

**Lane:** E2 (calibration) — READ-ONLY audit
**Date:** 2026-06-19
**Scope:** `arbiter/calibration/` — `calibrator.py`, `isotonic.py`, `platt.py`, `multi_advisor.py`, `stance_base.py`
**Orientation:** learning-loop spec §0 D5 (per-advisor `transform_for` seam, ≥15-nonzero-per-bucket apply gate, all-cold `is_cold_start`); INTERFACES.md §11.9 (calibration owns raw→prob) and §-fusion (pool.py consumes the seam).

## VERDICT: CHANGES REQUIRED (no P0; two P1 correctness/soundness defects)

The mechanical guards are good: predict_proba cannot emit NaN (both scalers hard-clamp; LogisticRegression/IsotonicRegression are stable), single-class and <2-sample fits raise and fall back to the prior, and the D5 ≥15 apply gate correctly keeps a fragile 2-point fit out of fusion. The cold-start prior table is sensible and conservative.

However, two real defects exist in **what gets fitted** and **how the calibrated value flows into conviction**:

1. **Fit X-values are reconstructed from the label, not the advisor's real stance — a leaky, over-confident fit.** `ResolvedOutcome` carries the true `stance_score ∈ [-1,1]` but `fit()` ignores it and uses `sign(binary)*advisor_confidence` as the "raw stance," making the regressor's input a deterministic function of its target. This manufactures near-separable data and inflates calibrated probabilities. (P1)
2. **Calibrated output is a probability in [0,1] but is summed into a signed signal-space [-1,1] in pool.py — the directional sign of a bearish advisor is lost once a model (or prior) is active.** A strongly bearish stance maps to ~0.10–0.40, a *positive* contribution; neutral maps to 0.50, not 0.0. (P1)

---

## FINDINGS

### P1 — Fit input is reconstructed from the outcome label (leaky / over-confident fit) — `calibrator.py:146-161`
The fit loop discards the real per-opinion stance and rebuilds a proxy:
```python
sign = 1.0 if outcome.binary == 1 else -1.0
raw_stance = sign * outcome.advisor_confidence   # X is a function of y
```
`ResolvedOutcome.stance_score` ("advisor's ACTUAL directional forecast in [-1,1]", `seams.py:184`) is exactly the X the calibrator should fit against, and it is available on every outcome. Using `sign(binary)*confidence` instead means:
- Winners always get a positive X, losers always a negative X **by construction**, regardless of what the advisor actually forecast.
- The logistic/isotonic fit sees artificially separable data and produces over-confident curves. Empirically, fitting 20 outcomes whose true stance was a constant +0.1 (advisor with no directional skill) yields `transform(+0.9)=0.89`, `transform(-0.9)=0.11` — a confident spread invented entirely from the label.
- This defeats the purpose of calibration (correcting the advisor's own scores); it instead measures `confidence`, which the prior table already accounts for.

**Why it matters:** the whole point of the seam is to learn the advisor's raw→prob mapping; fitting on a label-derived proxy makes the learned model spuriously confident and uncorrelated with the advisor's actual scoring behavior. This is the "over-confident fit" the audit was asked to find.

**Recommended action:** fit on `outcome.stance_score` (the real X) against `outcome.binary` (the y). Keep the no-call (binary==0) exclusion. If `stance_score` is ever absent/zero-defaulted in historical rows, gate on data availability rather than synthesizing X from y.

### P1 — Calibrated probability [0,1] is mixed into signed signal-space [-1,1]; bearish sign is lost — `pool.py:115-121`, `calibrator.py:227`, `multi_advisor.py:118-119`
`pool.py` does `contrib = w_norm * calibrated; signal_strength += contrib`, treating the calibrator's return as a signed signal in [-1,1]. But once any model OR the cold-start prior is active, `transform`/`predict_proba` returns **P(positive-alpha) ∈ [0,1]** (always non-negative; neutral = 0.50). Consequences:
- A maximally bearish advisor (stance −0.9) contributes ~0.10–0.40 — a **positive** push on `signal_strength`, i.e. it reads as mildly bullish.
- A neutral advisor contributes 0.50, a large positive bias, instead of 0.0.
- The mapping is only "passthrough/identity" under the Phase-1 `_IdentityCalibrator`/empty `MultiAdvisorCalibrator`; the moment a real `Calibrator` fits or even falls back to the STANCE_BASE prior, the contribution space silently changes from signed-[-1,1] to probability-[0,1], flipping/biasing conviction. Calibrated probability does not correctly flow into conviction.

**Why it matters:** this is "a calibrator applied with the wrong semantics" — directionally incorrect conviction the instant calibration goes live. Combined with the P1 above it can produce confident, wrong-signed sizing.

**Recommended action:** define one canonical contribution space at the seam. Either (a) have fusion map calibrated prob back to signed space via `2*p - 1` before weighting, or (b) keep calibrator output as probability and rework `pool.py` to aggregate probabilities (and re-center 0.5). Document the chosen space in INTERFACES.md §11.9 so every calibrator (identity, Platt, isotonic, prior) returns the SAME space. Currently identity returns [-1,1] and everything else returns [0,1].

### P2 — "passthrough-equivalent" is inconsistent across the gate's two branches — `multi_advisor.py:111-118`
The module docstring repeatedly promises gated/unknown advisors stay "PASSTHROUGH-EQUIVALENT," but the two fallback branches return different spaces:
- Unknown advisor (`cal is None`): returns **raw_stance** ∈ [-1,1] (true passthrough).
- Known-but-gated advisor (below 15 samples): returns `Calibrator(advisor_id).transform(...)` = the **STANCE_BASE prior** ∈ [0,1] (e.g. 0.11 for stance −0.9), NOT the raw stance.
So two opinions with identical stance get contributions in different spaces depending only on whether the advisor key exists. This compounds the P1 space-mismatch and makes the gate's behavior depend on registry membership.

**Recommended action:** pick one fallback space and use it in both branches (consistent with whatever resolution P1 chooses). Either both return raw stance, or both return the prior; do not split.

### P3 — Isotonic/Platt switch threshold (200) vs apply gate (15) leaves a wide thin-isotonic-free zone but no per-class minimum — `calibrator.py:42,46,177`
`_ISOTONIC_THRESHOLD=200` is on total non-zero outcomes per bucket; the apply gate is 15. Between 15 and 200 a Platt fit is applied. Platt on, say, 16 samples with a 15:1 class imbalance is still fragile (one class barely represented). The single-class guard (`len(set(y))<2`) only catches the degenerate all-one-class case, not severe imbalance.

**Recommended action:** consider a minimum-per-class count (e.g. ≥5 of each class) in addition to the total apply gate before applying a fitted model, and add a brief comment justifying 200 vs 15.

### P3 — `_MIN_FIT_SAMPLES=2` doc/behavior is fine but the property `is_cold_start` and the wiring gate use different definitions — `calibrator.py:46,90-100` vs `multi_advisor.py:75-88`
`Calibrator.is_cold_start` is "no fitted model at all" (model exists at 2 samples), while `MultiAdvisorCalibrator.is_cold_start` is "no advisor applied at ≥15." Both are individually correct and documented, but a reader wiring `FusionOutput.cold_start` could read the wrong one. No bug today (fusion reads the MultiAdvisor wrapper), just a footgun.

**Recommended action:** none required; optionally rename the base property `has_any_fitted_model` to remove ambiguity.

---

## CHECKS THAT PASSED (no finding)

- **No NaN / degenerate prob path.** `PlattScaler.predict_proba` returns `float(prob[1])` from sklearn (bounded (0,1)); `IsotonicScaler.predict_proba` clamps domain then `np.clip(...,0,1)`; `Calibrator.transform` hard-clamps `max(0,min(1,prob))`. No division, no exp overflow, no unguarded sklearn extrapolation. Verified empirically.
- **Thin-sample / single-class gating.** `fit()` skips buckets with `< _MIN_FIT_SAMPLES`; scalers raise `ValueError` on <2 samples or single class; `Calibrator.fit` catches `ValueError` and pops the model → prior fallback. The D5 ≥15 apply gate (`max_bucket_nonzero_outcomes`) correctly prevents a 2-point fit from shipping into fusion. Verified the 2-point fit is held out.
- **Stratification correctness.** `fit()` buckets by `bucket_for_days` into the 4 `HorizonBucket` members; `_n_outcomes` is rewritten for every bucket on each `fit()` call (no stale carry-over across re-fits). Per-advisor filtering on `advisor_id` is correct. STANCE_BASE covers all 4 buckets for every advisor type plus "*".
- **Prior sanity.** STANCE_BASE values are monotone in stance bin, centered at 0.50 for neutral, and conservatively bounded (0.38–0.63). `lookup_prior` falls back to "*" and to SHORT bucket safely. Sensible.
- **persist()** requires tz-aware `as_of`, supplies ULID PK + created_at explicitly (no silent-NULL PK). Fine.

---

## OPPORTUNITIES TO ADD

1. **Calibration quality metric / reliability check.** Nothing measures whether the fitted curve is actually better-calibrated than the prior (e.g. Brier score or ECE on a held-out slice). Add a post-fit reliability gate so a worse-than-prior fit is rejected even above 15 samples.
2. **Per-class minimum, not just total.** As P3 — track positive/negative counts per bucket and require a minimum of each before applying.
3. **Regularize the small-sample Platt fit.** `LogisticRegression(max_iter=1000)` uses default C=1.0; on near-separable thin data this still over-confidently saturates. Consider a stronger prior (smaller C) or a Bayesian/beta-binomial shrink toward the STANCE_BASE prior in the 15–200 regime.
4. **Single source of truth for contribution space.** Add an explicit assertion/test in fusion that every calibrator (identity, prior, Platt, isotonic, gated, unknown) returns the SAME documented space, to catch the P1/P2 space drift regressing.
5. **Use `stance_score` end-to-end.** Once P1 is fixed, add a fit-time test asserting that fitting on real stances reproduces a known reliability curve, and that constant-stance / no-skill advisors do NOT produce confident spreads.
