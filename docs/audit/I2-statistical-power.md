# Audit I2 — The ~30-Closed-Trades Premise & Statistical Power

**Auditor lane:** I2 — statistical power / strategy-stats critique (READ-ONLY)
**Date:** 2026-06-19
**Scope:** the "~30 closed trades to answer whether A1 alpha survives the lag" premise; `SHADOW_THRESHOLD=30` graduation; multiple-testing/overfitting; recency-weighted Brier (182-day half-life) on a slow MEDIUM/LONG trade stream; survivorship in idea→trade conversion. The edge itself (A1) is out of scope (lane I1).

**Verdict: FAIL (premise is statistically unsound as a skill/luck discriminator).** Thirty closed trades cannot distinguish skill from luck for any realistic per-trade alpha effect size; the 30-outcome shadow-lift threshold will graduate noise at roughly chance rates; there is no multiple-testing control as advisors/horizons multiply; and the 182-day Brier half-life actively discards history at the very cadence (90–180d holds) where samples accrue slowest. The number "30" appears to be a heuristic borrowed from the "n≥30 ⇒ CLT/normal-approx" rule of thumb, which is a statement about the *sampling distribution of a mean*, **not** about *power to detect a non-zero mean*. Those are different questions, and the system conflates them.

Key code anchors:
- `arbiter/trust/ledger.py:67` — `SHADOW_THRESHOLD = 30`, `RAMP_OUTCOMES = 10`, `PHASE3_ACTIVATION_THRESHOLD = 60`, `MIN_NEW_OUTCOMES = 5`.
- `arbiter/trust/brier.py:36` — `HALF_LIFE_DAYS = 182.0`; `BS_REF = 0.25`; BSS = `1 - BS/0.25`.
- `arbiter/gate/criteria.py:48-49` — paper→live gate: `min_closed_trades=30`, `min_trading_days=60`, `min_sharpe=1.0`.
- `arbiter/engine.py:958-959` — horizons: form4→180d (LONG), congress→90d (MEDIUM).
- `arbiter/evaluation/outcome_labeler.py:14-15` — `alpha_bps` continuous (drives trust) + ±25bps `binary` band → no-call(0).
- `arbiter/types.py:13-19` — MEDIUM=31–120d, LONG=121–365d.
- `ROADMAP.md:22-24` — Phase-2 needs ≥30 closed paper trades; Phase-3 needs ≥60 labeled outcomes.

---

## Findings

### [P0] — 30 trades has near-zero power to detect realistic per-trade alpha — why "30" can't answer the question it's posed to answer
**Why.** The stated premise is to accumulate ~30 closed trades "to answer whether A1 alpha survives the lag." Treat that as a one-sample, one-sided t-test that mean per-trade alpha > 0. At n=30, α=0.05, 80% power, the **minimum detectable effect is Cohen's d ≈ 0.47** (mean/sd of per-trade alpha). Per-trade alpha on 90–180d event-driven equity bets is *extremely* noisy — a realistic cross-trade alpha SD is 800–2500 bps (8–25%). That converts the detectable mean to:
- SD = 800 bps → need **true mean alpha ≥ ~380 bps** (3.8%/trade) to have 80% power.
- SD = 1500 bps → need **~712 bps** (7.1%/trade).
- SD = 2500 bps → need **~1188 bps** (11.9%/trade).

A real smart-money lagged edge, *after* disclosure lag and modeled slippage, is plausibly tens of bps, not hundreds. So 30 trades is under-powered by roughly an order of magnitude: the test will fail to reject the null even if a genuine (but modest) edge exists, i.e. the experiment is built to return "inconclusive" almost regardless of the truth. **The "~30 closed trades will tell us if alpha survives" claim is false as stated.**

The n≥30 folk rule the design seems to lean on is about the *normal approximation of a sample mean's sampling distribution* (CLT), not about *power to detect that the mean ≠ 0*. Conflating the two is the root error.

**Recommendation.** Replace the fixed "30" with a power-driven target. Do a real power calc against an *assumed* edge and an *estimated* per-trade alpha SD (bootstrap the SD from the first ~15–20 closed trades, then re-solve n). Honestly state the MDE the current n can detect in every Phase-2/3 report ("with n=30 we can only detect an edge ≥ X bps at 80% power"). Expect the true required n to be in the **hundreds** for modest edges; if that cadence is infeasible (see P0-cadence finding), reframe the goal from "prove alpha" to "bound the loss / detect catastrophic negative skill," which *is* feasible at small n.

### [P0] — SHADOW_THRESHOLD=30 graduates noise to live weight; the lift criterion is a count, not a significance test
**Why.** `_shadow_ramp_weight` (`ledger.py:187`) lifts shadow purely on **count** (≥30 non-abstain outcomes), then ramps 0→composite over 10 more. There is *no* statistical-significance condition on graduation — an advisor at exactly chance graduates as readily as a skilled one. Quantify with the binary view the system itself computes (±25bps band, `binary` ∈ {−1,0,+1}): at n=30, a one-sided binomial test of directional accuracy vs 50% requires **≥20/30 hits (67%)** to reach p<0.05. The 95% CI on a *measured* 60% hit-rate at n=30 is **[42%, 78%]** — it straddles chance. So a graduated advisor's true skill is statistically indistinguishable from a coin. Because shadow only requires the count, ~50% of truly-null advisors will show a positive-looking BSS by luck and graduate to a non-zero live weight. The `CEILING=0.50` cap limits blast radius but does not fix the false-positive graduation.

Worse, the ±25bps no-call band (`outcome_labeler.py`, `brier.py:104-109`) and abstentions are *excluded* from the count, so "30 non-abstain outcomes" can require far more than 30 closed trades — but the threshold logic doesn't track that, so the effective sample backing a graduation is opaque.

**Recommendation.** Gate graduation on a *significance/CI* condition, not a count: require the BSS (or hit-rate) CI lower bound to exclude the null (e.g., the existing `ci_low` field — currently a crude `composite*0.8` placeholder, `ledger.py:386` — replaced with a real bootstrap/Wilson CI whose lower bound must be > 0). Until that CI excludes chance, keep weight at the thin-sample floor, not a ramped composite. Raise the de-facto graduation n to whatever the power calc demands.

### [P1] — No multiple-testing / overfitting control as advisors × horizons grow
**Why.** Each advisor × horizon bucket (SHORT/MEDIUM/LONG, plus future A2.* MiroFish and any added sources) is an independent hypothesis test at α=0.05. With k advisor-horizon cells, the family-wise probability of ≥1 false "skilled" graduation is `1−0.95^k`: k=5 → 23%, k=10 → 40%, k=20 → 64%. The design explicitly anticipates adding advisors (A2 MiroFish, `MIROFISH_CAP`) and already splits by horizon — so k grows and the system will reliably "discover" a skilled advisor that is pure noise (the winner's-curse / multiple-comparisons trap). There is **no Bonferroni/BH-FDR correction, no shared prior, and no shrinkage** across advisors in `compute_composite_trust`. The geometric-mean composite also has no regularization toward a population mean, so the noisiest small-sample advisor can post the highest composite and win the most weight — the opposite of what shrinkage would do.

**Recommendation.** (1) Apply a multiplicity correction (Benjamini–Hochberg FDR is appropriate for "pick the skilled advisors") across the advisor×horizon family before any graduates. (2) Add hierarchical shrinkage: pull each advisor's BSS toward the cross-advisor mean by an amount ∝ 1/sample (empirical-Bayes / James–Stein), so small-n advisors can't run away on luck. (3) Track the family size k explicitly and surface "expected false graduations = 0.05·k" in reports.

### [P0] — 182-day Brier half-life on a 90–180d hold stream discards history faster than samples accrue
**Why.** MEDIUM ideas (congress, 90d) and LONG ideas (form4, 180d) must be *held to close* before they score. Wall-clock to 30 **closed** MEDIUM trades, assuming steady entry rate r and a 90d (~13wk) hold:
- r=0.5/wk → ~73 weeks (**~1.4 yr**)
- r=1/wk → ~43 weeks (**~0.8 yr**)
- r=2/wk → ~28 weeks (~0.5 yr)

For LONG (180d holds) double the hold lag. Congress/Form-4 disclosure cadence on a single-account paper book is plausibly < 1 qualifying entry/week after dedup and the held-ticker skip (`engine.py:947-956`), so **~1 year to the first 30 closed MEDIUM trades is the realistic case, and Phase-3's 60-outcome activation pushes toward ~1.5–2 yr.**

Against that, a 182-day half-life means by the time the 30th trade closes (~301 days at 1/wk) the **first trade's recency weight has already decayed to 2^(−301/182) ≈ 0.32** — it counts as one-third of a fresh trade. The decay is throwing away ~half the (already tiny) sample before n=30 even lands. Effective sample size `(Σw)²/Σw²` is materially below the nominal 30, which *further* erodes the already-inadequate power in P0 above. A half-life is meant to down-weight *stale* signal when you have *abundant* data; here data is scarce and slow, so aggressive decay is exactly backwards. The half-life (182d) is also barely longer than a single LONG hold (180d), which is incoherent: you're decaying a trade by ~50% over the same span it took to *generate* it.

**Recommendation.** At this cadence, either (a) drop recency weighting until sample is abundant (use a flat window), or (b) lengthen the half-life to ≥3–5× the dominant hold horizon (e.g. ≥540–730 days) so a year of trades stays ~fully weighted. Report effective sample size `(Σw)²/Σw²` alongside raw n everywhere a threshold (30/60) is checked, and gate on *effective* n, not raw count.

### [P1] — Survivorship/selection: ideas→trades funnel biases the outcome sample and its variance
**Why.** Not all ideas become scored trades, and the filtering is non-random in ways that bias the trust estimate:
- `engine.py:947-956`: signals for already-seen or already-**held** tickers are skipped. Skipping held tickers conditions the sample on "ticker not already in book" — a momentum/timing selection that correlates with outcome.
- Dedup (`fusion/dedup.py`) merges overlapping opinions, so the *surviving* opinions are systematically the consensus ones; idiosyncratic calls are under-represented in the scored set.
- The ±25bps no-call band and abstentions are dropped from scoring (`brier.py:104-109`), so only "decisive" outcomes count — truncating the alpha distribution and *under*-stating per-trade variance (which inflates apparent skill / BSS).
- Ideas that never reach CLOSED (ABANDONED, never-filled, stop-outs vs horizon-exits handled differently) leave the scored sample a non-representative subset of all decisions.

Net effect: the 30 scored outcomes are a survivor-selected, variance-truncated sample, so any BSS computed on them is biased and its CI understated — compounding every power problem above.

**Recommendation.** Log the full funnel (signals → ideas → filled → closed → scored) and compute the conversion/attrition at each stage; report scored-n as a fraction of decisions. Score abandoned/stopped ideas as realized outcomes (a stop-out *is* an outcome) rather than dropping them, so the scored distribution matches the traded distribution. Treat no-call/abstain dropping as a known bias and re-include them (at p=0.5 / partial credit) when estimating *variance* even if excluded from the skill mean.

### [P2] — Gate Sharpe≥1.0 over 30 trades / 60 days is itself a low-power, high-variance criterion
**Why.** `criteria.py:49` requires `min_sharpe=1.0` for paper→live, evaluated over `min_closed_trades=30` / `min_trading_days=60`. A *sample* Sharpe over 30 returns has a large standard error (≈ sqrt((1+0.5·SR²)/n) ≈ 0.19 at SR=1, n=30) — and over only 60 calendar days with overlapping 90–180d holds the returns are heavily autocorrelated, violating the i.i.d. assumption behind Sharpe and inflating the estimate. A book can clear Sharpe≥1.0 on 30 trades by luck and then revert. This is the same skill-vs-luck problem wearing a different hat, now wired to a live-money decision.

**Recommendation.** Require the *lower confidence bound* of the deflated/annualization-corrected Sharpe to exceed a threshold (deflated Sharpe ratio, Bailey–López de Prado), not the point estimate; account for return autocorrelation from overlapping holds; and raise the trade count toward the power-driven n from P0.

---

## OPPORTUNITIES TO ADD

- **Power/MDE line in every trust & gate report.** Auto-emit "with current effective-n=X we can detect an edge ≥ Y bps at 80% power; CI on BSS = [lo,hi]" so no one mistakes "n≥30" for "proven."
- **Effective sample size gating.** Replace raw-count thresholds (30/60) with `(Σw)²/Σw²` effective-n under the decay, so the half-life and threshold are consistent.
- **Empirical-Bayes shrinkage of composite trust** toward the cross-advisor mean, with FDR control across the advisor×horizon family — kills winner's-curse graduations.
- **Bootstrap CI for `ci_low`/`ci_high`** (currently the placeholder `composite*0.8/1.2`, `ledger.py:386`) and make graduation conditional on `ci_low > 0`.
- **Sequential / Bayesian testing** (e.g. a Bayes factor or SPRT on cumulative BSS) instead of a fixed-n gate, so the system can graduate *early* when an edge is strong and *never* when it isn't — far more sample-efficient than a hard "30."
- **Half-life sensitivity sweep** reported alongside results (e.g. BSS at 182d / 365d / ∞ half-life) so the recency choice is auditable, not hidden.
- **Reframe Phase-2 goal** from "prove A1 alpha" (infeasible at this n/cadence) to "rule out catastrophic negative skill and bound max drawdown," which *is* achievable at n≈30 — the negative-skill suppression path (`is_negative_skill`, `ledger.py:340`) is the statistically defensible part and should be the headline use of small samples.
