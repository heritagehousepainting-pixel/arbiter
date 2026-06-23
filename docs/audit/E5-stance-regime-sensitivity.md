# E5 — A1 positive-stance & regime sensitivity of negative-skill suppression

**Lane:** E5 (READ-ONLY audit). **Date:** 2026-06-19. **Auditor scope:** soundness of the
learning signal, not the plumbing.
**Files in scope:** `arbiter/signals/emit.py`, `arbiter/evaluation/outcome_labeler.py`,
`arbiter/trust/brier.py`, `arbiter/trust/ledger.py`, `arbiter/trust/regime.py`,
`arbiter/data/beta.py`, `arbiter/trust/weight_resolver.py`.
**Spec orientation:** `docs/superpowers/specs/2026-06-19-real-attribution-design.md` §0 E4.

---

## VERDICT

**CONFIRMED — the negative-skill suppression branch CAN trip on a market-wide drawdown /
flat regime rather than on true advisor skill.** A1 emits only positive (BUY) stances floored
at 0.1 (`emit.py:129`). The Brier forecast `p_hat` is built from `stance_score × confidence`
(`brier.py:117`), so a *confident long* is a high-probability up-bet. Beta adjustment removes
the market's *mean* drift from `alpha_bps` but it does NOT make a long-only advisor's
decisive-up rate exceed the confidence-implied break-even — under a mean-zero idiosyncratic
alpha distribution the decisive outcomes split ~50/50, and a confident A1 advisor scores
**BSS ≈ −0.5** and is muted (`ledger.py:340-344` → `weight_resolver.py:109-118`). The
suppression threshold is governed by the advisor's *stance magnitude and confidence*, not its
skill. The recency-weighted aggregate and the 30-sample shadow gate dampen single-call noise
but do NOT correct this directional bias. The regime machinery that was meant to mitigate this
(`trust/regime.py`) is **dead code** — never constructed, never called. Verdict: the learned
weights for a long-only advisor reflect **regime/market-direction as much as skill** until a
regime control or short signals (A2/A3) land. This is the known sensitivity E4 told us to
document; it is real and quantified below.

---

## Evidence / numerical grounding (`.venv/bin/python`)

**Confidence-amplified break-even (long-only).** For a long with fixed `p_hat`, BSS=0 requires
a decisive-up rate equal to the break-even win-rate:

| stance / confidence | p_hat | break-even up-rate for BSS=0 |
|---|---|---|
| 0.1 / 0.5 (floor)   | 0.525 | 0.512 |
| 0.5 / 0.6           | 0.650 | 0.575 |
| 0.9 / 0.8           | 0.860 | **0.680** |

A confident A1 long must be right (decisive-up) **68% of the time** just to break even.

**Beta-adjusted idiosyncratic alpha (mean-zero band, σ=300bps over horizon), stance 0.9 /
conf 0.8:**

| true idiosyncratic edge | decisive-up rate | BSS |
|---|---|---|
| 0 bps    | 0.510 | **−0.49** (suppressed) |
| +50 bps  | 0.579 | **−0.29** (suppressed) |
| +100 bps | 0.645 | **−0.10** (suppressed) |
| +200 bps | 0.763 | +0.24 |
| +400 bps | 0.924 | +0.70 |

A genuinely *skilled* long with a real +50–100 bps edge is still **suppressed**, because its
confident stance demands a ~68% up-rate that mean-zero residual alpha can't supply. Suppression
fires on *the gap between confidence and the beta-residual up-rate*, not on negative skill.

---

## FINDINGS

`[P1] — Confident long-only stance makes BSS a directional bet, not a skill measure — arbiter/trust/brier.py:117 (with arbiter/signals/emit.py:129) — `
`p_hat = _stance_to_prob(stance_score × confidence)` with a stance floored at 0.1 and never
negative turns every A1 score into a bet that the name goes UP. The break-even up-rate scales
with confidence (0.51 at the floor → 0.68 at stance 0.9/conf 0.8). In a flat or down regime
where beta-adjusted decisive outcomes are ~symmetric, a confident A1 advisor earns BSS<0 and is
muted regardless of skill. **Recommended:** until short signals exist, decouple the *suppression*
decision from stance magnitude — e.g. gate negative-skill on a *direction-agnostic* skill
metric (sign-accuracy of alpha vs. the advisor's directional call, or BSS computed against a
stance normalized to a fixed reference confidence), and reserve confident-stance penalties for
reward sizing, not for the mute branch.

`[P1] — Regime mitigation is entirely dead code; the 2× post-regime reweight and 21-day freeze never engage — arbiter/trust/regime.py (whole module) + arbiter/trust/ledger.py:56,268,352 — `
`apply_regime_weights` is imported (`ledger.py:56`) but **never called anywhere** in the tree
(grep confirms zero call sites outside its def). `RegimeTracker` is **never constructed with any
`RegimeChangeEvent`** in production code (grep: no `RegimeTracker(...)` / `RegimeChangeEvent(...)`
outside tests). `compute_composite_trust` accepts `regime_tracker` (`ledger.py:132,352`) but the
parameter is unused in the BSS path — `brier.py` has no regime awareness at all. So the freeze
(`is_frozen`) is always False and the post-regime fast-recalibration multiplier never applies.
The one stated defense against regime-driven mis-scoring is inert. **Recommended:** wire a real
regime detector (e.g. SPY drawdown / volatility state) to populate `regime_events`, and actually
call `apply_regime_weights` inside `recency_weighted_brier`; OR remove the dead module and
replace it with a real regime control on the suppression branch.

`[P2] — Beta estimated on LOG returns but applied to SIMPLE returns; market direction is not fully removed — arbiter/data/beta.py:174 vs arbiter/evaluation/outcome_labeler.py:185-190 — `
Beta is the OLS slope of **log** daily returns (`beta.py:174` uses `math.log`), but the labeler
forms `alpha = R_i − beta·R_SPY` with **simple** horizon returns (`outcome_labeler.py:302-304`,
`189`). Over 90–180-day horizons the simple-vs-log gap is large (−15% return → 125 bps gap;
−30% → 567 bps). In a down regime (R_SPY<0) the simple |R_SPY| is smaller than the log value the
beta was fit on, so `beta·R_SPY` **under-subtracts** the market drop and leaves a residual
negative market drift in every long's alpha — a systematic, market-direction-correlated bias
that pushes A1's binary toward −1 in drawdowns. The beta adjustment therefore does **not** fully
neutralize regime direction. **Recommended:** make the return convention consistent — either fit
beta on simple horizon-scaled returns, or compute `alpha` in log space (`log(1+R_i) −
beta·log(1+R_SPY)`).

`[P2] — Imputed beta=1.0 silently biases alpha in drawdowns for thin-history names — arbiter/data/beta.py:70,84 (flagged into arbiter/evaluation/outcome_labeler.py:176-182) — `
When <63 usable return pairs exist, beta is imputed to 1.0 (and warned). For a low-beta or
defensive name in a down market, β=1.0 over-subtracts SPY and can flip alpha negative; for a
high-beta name it under-subtracts. Form-4 insider buys cluster in small/illiquid names with thin
price history → exactly the population most likely to hit imputation, so the bias is not random.
**Recommended:** treat imputed-beta outcomes as lower-weight (or `binary=0`/excluded) in the
Brier so a data-thin name's regime artifact cannot drive suppression; the imputation flag already
exists at `outcome_labeler.py:176` but is only logged, not propagated to scoring.

`[P3] — 30-sample shadow gate avoids single-call noise but not the directional-bias regime trip — arbiter/trust/ledger.py:67,200 — `
`SHADOW_THRESHOLD=30` (plus the 0→composite ramp over `RAMP_OUTCOMES=10`) and the recency-
weighted aggregate (`brier.py:HALF_LIFE_DAYS=182`) do correctly prevent *one* bad call from
suppressing an advisor — suppression keys on the *aggregate* recency-weighted BSS, not a single
row, and `should_update` further requires ≥60 system outcomes and ≥5 new (`ledger.py:70-71`). But
30 samples is enough only against *random* noise; it does **not** protect against the *systematic*
directional bias of Findings P1/P2, where a sustained down/flat regime pushes the entire recent
window's BSS negative in unison. The half-life (182d) is roughly one regime cycle, so a multi-
month drawdown dominates the weighted average and the aggregate trips for the wrong reason.
**Recommended:** treat the 30-sample threshold as adequate for noise but explicitly NOT a regime
control; pair it with the P1/P2 fixes.

`[P3] — Negative-skill mute removes the advisor from trading but not from scoring, so recovery is possible yet slow — arbiter/trust/weight_resolver.py:109-118 + arbiter/trust/ledger.py:344 — `
`cap_reason="negative_skill"` mutes the advisor to weight 0 / shadow (resolver:111-117). Because
A1 always emits opinions and the labeler scores outcomes regardless of weight, suppressed
advisors keep accruing outcomes, so BSS can climb back ≥0 when the regime turns and the mute
lifts on a later `update`. This is benign (no permanent lockout) but means a long-only advisor is
*correctly* benched through a drawdown and *re-enabled* in a recovery — i.e. the system learns
the regime, not the skill. **Recommended:** acceptable as-is once P1/P2 land; document that
"negative_skill" for A1 currently encodes "long in an unfavorable regime."

---

## Answers to the chartered questions

1. **Can negative-skill trip on drawdown/regime vs. true skill?** Yes. A confident long-only
   stance plus a mean-zero (or worse) beta-residual gives BSS<0 even with a real +50–100 bps
   edge (table above). Suppression is governed by stance/confidence vs. realized up-rate, not by
   skill.
2. **Does SPY-beta adjustment remove market direction, making a long-only advisor fairly
   scorable?** Only partially. It removes the *mean* market drift in principle, but (a) the
   log-vs-simple return mismatch (P2) leaves a direction-correlated residual, (b) imputed β=1.0
   biases thin-history names (P2/P3), and (c) even with perfect adjustment the *binary band* over
   mean-zero alpha yields a ~50/50 decisive split that a confident long can't clear (P1). So no —
   not fairly scorable for a confident long-only advisor.
3. **How does the recency-weighted aggregate govern suppression; is 30 samples enough?** The mute
   keys on the aggregate recency-weighted BSS (182-day half-life) over ≥30 non-abstain outcomes,
   gated by ≥60 system / ≥5 new. This is enough against *random* noise but not against the
   *systematic* regime bias, which moves the whole window together (P3).
4. **Do learned weights reflect SKILL vs REGIME/luck?** For A1 today: **regime as much as
   skill.** A skilled long is benched in drawdowns and graduated in rallies.
5. **What's needed?** Real short signals (A2/A3) so stance can be bearish; a *live* regime
   control (the existing `regime.py` is dead — wire it or replace it); a direction-agnostic skill
   gate for the suppression branch; and the return-convention fix in the labeler.

---

## OPPORTUNITIES TO ADD

- **Direction-agnostic skill metric for the mute branch.** Compute a separate
  `sign_accuracy = P(binary sign == stance sign)` or a confidence-normalized BSS, and gate
  `cap_reason="negative_skill"` on *that*, leaving the confidence-weighted BSS for reward sizing
  only. This lets a long-only advisor be benched for *bad picks* but not for *being long in a
  down market*.
- **Add the E4 near-break-even control test** (spec mandates it): a mostly-correct A1 advisor
  with occasional losses must NOT be suppressed; assert the recency-weighted aggregate (not a
  single bad call) governs. Also add a *drawdown* regression: a skilled long in a synthetic down
  regime must not trip `negative_skill` once P1/P2 land.
- **Propagate the imputed-beta flag into scoring** so β=1.0 outcomes are down-weighted or
  excluded from the Brier (the flag exists at `outcome_labeler.py:176` but dies as a log line).
- **Wire or delete `trust/regime.py`.** Either feed it a real SPY-drawdown/vol detector and call
  `apply_regime_weights` in `recency_weighted_brier`, or remove the dead import and parameter to
  stop implying a defense that doesn't run.
- **Fix the log/simple return convention** in `outcome_labeler.py` / `beta.py` so the beta
  adjustment actually removes market direction over multi-month horizons.
- **A `regime_id` stamp on each outcome** would let the ledger compute per-regime skill and avoid
  cross-regime contamination of the recency window — a cheaper interim control than full freeze.
