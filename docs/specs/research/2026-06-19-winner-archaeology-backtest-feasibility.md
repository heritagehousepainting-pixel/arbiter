# Feasibility Memo — "Winner Archaeology / Early-Edge Indicator" (Angle B)

> **Author:** Quant research analyst (skeptical review)
> **Date:** 2026-06-19
> **Status:** Research memo only. No code. No source changes.
> **Scope:** Evaluate whether arbiter can find a *common, observable-at-the-time precursor* to enormous
> stock run-ups (+300% / +2,000% / +10,000%) by looking backward from winners, and backtest it as an
> early-entry indicator under arbiter's point-in-time (PIT), no-look-ahead discipline.

---

## 0. One-paragraph verdict (read this first)

**Feasibility: LIKELY-OVERFIT-TRAP as stated; MARGINAL if reframed.** The idea as literally posed —
"study the rockets, find what they had in common, then buy the next one" — is the canonical
survivorship-bias error and will reliably produce a beautiful, useless signal. It can be salvaged into
a *marginal* but honest research program only by inverting the question: instead of "what do winners
share," ask "**conditional on observing feature X at time t, what is the forward return distribution
across the FULL universe (winners and the thousands of identical-looking names that died)?**" That is a
base-rate / precision question, and arbiter's PIT framework is actually well-suited to answer it. The
realistic prize is a *small lift over base rate* on a screen (e.g. moving the right-tail hit rate from
~1% to ~2–4% with controlled drawdown), **not** a 10,000% picker. The biggest reason it still may not
work: the largest movers are illiquid microcaps where capacity, slippage, and borrow/halt frictions eat
the entire paper edge, and the precursor signals that actually matter (float squeezes, news catalysts,
dilution events) are mostly data arbiter does not yet have PIT.

---

## 1. Survivorship bias — the central flaw, stated plainly

The proposal selects the sample **on the outcome**. You take the set of stocks that already went up
2,000% and inspect their pasts. Any feature you find ("they were small," "they had a volume spike,"
"they broke a 200-day base," "insiders bought") will appear *common among winners* — but that tells you
**nothing** about edge, because the same feature is also common among the tens of thousands of stocks
that had the identical look-back and then went sideways, got delisted, or went to zero.

The quantity you care about is **not** `P(feature | winner)` (what backward-looking inspection gives
you). It is `P(big winner | feature)` — the **precision** of the signal, and more usefully the entire
**forward return distribution conditional on the feature**. These differ by Bayes' rule by a factor of
the base rate, and the base rate of 2,000% moves is on the order of `1e-3` to `1e-4` per name-year.
Selecting on winners discards the denominator entirely, so it cannot estimate precision even in
principle.

Two compounding traps inside the same mistake:

- **Sample-on-the-dependent-variable.** Classic econometric error. Coefficients are biased toward
  whatever co-moves with the selection. There is no statistical fix other than *not doing it* — i.e.,
  restoring the full universe.
- **Dead-name survivorship.** If you build the winner universe (or even the "universe of small caps") from
  *today's* listings, you have already silently dropped every company that delisted, went bankrupt, or
  was acquired at $0.30. The losers that shared the exact early features are precisely the names that no
  longer exist in a current ticker list. Studying only survivors makes any precursor look far more
  predictive than it is.

**Design around it (the non-negotiable):** the unit of analysis must be **every (ticker, date) cell in a
point-in-time universe**, not a curated winner list. Compute the candidate feature at each cell, then
measure forward outcomes across *all* cells. Winners are then just the right tail of the outcome
distribution, never the sampling frame. Arbiter's PIT gateway (`arbiter/data/pit.py`,
`PITGateway.get(field, ticker, as_of)` returning `None` when unknown-as-of) and the
`scripts/check_no_lookahead.sh` lint are exactly the right primitives to enforce this — but only if the
universe membership itself is point-in-time (see §2).

---

## 2. Defining the winner universe & the "1,000 feet" entry point

### 2a. Point-in-time universe (kills dead-name survivorship)

- **Required:** a survivorship-bias-free constituent set — for each `as_of`, the list of names that were
  *tradeable that day*, including names that later delisted/bankrupted/merged. Today's Stooq/Alpaca feeds
  give prices for names that still exist; they do **not** reliably give you the full historical roster of
  the dead. **This is a known data gap.** Honest options:
  1. Acquire a PIT universe with delisting flags (e.g., a CRSP-style or commercial corporate-actions
     dataset). Aspirational — arbiter does not have it now.
  2. Approximate: take the broadest historical ticker set Stooq/Alpaca *do* expose, and explicitly
     **bound the claim** — "this study under-samples delisted names, so measured precision is an
     **upper bound** (optimistic)." Document the bias direction; do not pretend it's gone.
- Never define the universe by "tickers that exist in 2026." That single shortcut invalidates the whole
  study.

### 2b. Defining "winner" without look-ahead in the label

A winner is just a label on a forward window, computed *after* the entry timestamp. There is no
look-ahead in the *label* (it's the realized future of the entry cell); the look-ahead risk lives in the
*features* and the *universe*. Define labels as forward total return over fixed horizons measured from
the PIT entry:

- `ret_6m`, `ret_1y`, `ret_2y` from the entry open, split-/dividend-adjusted.
- Tiered right-tail labels: `≥ +300%`, `≥ +2,000%`, `≥ +10,000%` over the matching horizon.
- **Crucially:** include the *downside* tail too (`≤ −80%`, delisted) — because a signal that finds
  rockets but equally finds craters has no edge. The realistic outcome of any high-octane microcap
  screen is a **barbell**: more right-tail AND more left-tail. You must measure both.

### 2c. The "rocket at 1,000 feet" entry point — objective, not eyeballed

Eyeballing the chart bottom is itself look-ahead (you can only pick the bottom in hindsight). The entry
must be a **rule that fires on information available at `t`**, then we look forward. Candidate objective
"1,000 feet" triggers (any of these defines an entry event; test them as separate hypotheses):

- **First N% breakout from a base:** price closes above the high of a trailing K-day consolidation whose
  range was < X% (a tight base resolving upward). Fires once per base.
- **First volume surge:** 20-day-relative-volume crosses a percentile threshold (e.g. RVOL > 3) for the
  first time in M months.
- **First new multi-month high** after a long dormancy.
- **Nth-percentile precursor crossing:** the candidate feature (e.g. insider-cluster score) crosses a
  pre-set percentile of its own history.

The point: the entry timestamp is **machine-defined and reproducible**, fires at the *start* of a
potential move (low feet), and is computable in arbiter as an `as_of` event. We then measure the forward
return distribution from `entry_open + 1 day` (matching arbiter's existing entry convention:
filing-date+1 OPEN, net modeled slippage — see INTERFACES §6 `outcome_labeler`). This slots directly
into the `ResolvedOutcome` / SPY-beta alpha labeler that already exists.

---

## 3. Candidate early signals & data availability (specific to arbiter NOW)

Rating each on **(a) predictive plausibility** and **(b) PIT availability in arbiter today.**

| Signal | Plausibility | Arbiter PIT data? | Verdict |
|---|---|---|---|
| **Price/volume technicals** (tight base + breakout, RVOL surge, multi-month-high, ATR expansion, momentum) | Medium — these are *coincident-to-slightly-leading*; they describe the launch, not the cause. Real but weak and crowded. | **YES** — `PITGateway` fields `price_open/price_close/adv_20d`, beta via `beta_252d`. | **Realistic NOW.** The only fully-available family. Build v1 here. |
| **Insider cluster buying** (Form 4) | Medium-High — multiple insiders buying open-market is one of the few genuinely *leading*, economically-motivated signals; documented small edge in literature. | **PARTIAL** — Form 4 ingest exists but `EDGAR_USER_AGENT` is EMPTY in `.env`, so Form 4 is *skipped every run* (handoff §"Known gaps"). Backfill depth unknown. | **Realistic NOW if backfilled** — highest-value among available. Needs the UA set + a historical Form-4 pull. |
| **Congressional trading** | Low-Medium — political-information edge is real but tiny, lagged (~22d median disclosure lag per handoff), and *thin* (few names/quarter). Far too sparse to populate a right-tail study. | **YES** — congress ingest live (~313 House + ~196 Senate). | **Weak as a rocket precursor** (coverage too thin) — keep as a *secondary* feature, not the screen. |
| **Short interest / days-to-cover** (squeeze fuel) | High for the *2,000%+* tail specifically — many extreme moves are short squeezes. | **NO** — not ingested; bi-monthly FINRA data, PIT-able but new. | **Aspirational, high-value.** Single best *new* dataset to add for this question. |
| **Float / share structure / dilution** (low float + offering risk) | High — tiny float is the mechanical enabler of explosive moves; dilution is the killer of them. | **NO** — not ingested. | **Aspirational.** Hard to get PIT cheaply. |
| **Fundamentals / revenue acceleration** | Medium — works for *durable* multibaggers (the +300% over 2y kind), not the +10,000% pump kind. | **NO** PIT (point-in-time fundamentals with as-reported dates are notoriously look-ahead-prone). | **Aspirational + dangerous** (restatement/look-ahead landmines). Defer. |
| **News / social inflection** (catalyst, retail attention) | High for the explosive tail — but extremely noisy and the hardest to get clean PIT. | **NO** (handoff: A3 news/X is stub). | **Aspirational.** Phase-3+ territory. |
| **Options flow** (unusual call activity) | Medium-High but microcaps often have no options. | **NO.** | **Aspirational.** |

**Takeaway:** of the seven families, only **technicals (now)** and **insider-cluster Form-4 (now, once
backfilled)** are honestly available PIT today. Congress is available but too sparse to drive a
right-tail screen. Everything genuinely associated with the *extreme* tail (short interest, float,
catalysts) is **new data arbiter must acquire** — which is the central practical constraint on
ambition.

---

## 4. The needle-in-haystack / multiple-testing problem

Extreme winners are rare (`~1e-3`–`1e-4` base rate for the giant tail), so:

- **Class imbalance is brutal.** A screen with 99% "accuracy" can be achieved by always predicting "not a
  rocket." Accuracy is meaningless here. Evaluate on **precision, recall, and lift over base rate** in
  the right tail, plus full forward-return distribution moments. (See §5 metrics.)
- **Multiple-comparisons / garden-of-forking-paths.** With dozens of features × thresholds × horizons ×
  entry rules, *something* will look significant by chance. This is the single fastest way to fool
  yourself.

**Guardrails (pre-register before touching data):**

1. **Pre-registered hypothesis.** Write down, *before* running, the exact feature, entry rule, horizon,
   universe, and metric, plus the minimum lift that would count as success. Commit it to the repo. No
   post-hoc threshold shopping.
2. **Train / validation / out-of-sample split by TIME, not random.** E.g. fit on 2005–2015, validate
   2016–2019, lock and test once on 2020–2025. Random splits leak regime information.
3. **Walk-forward / expanding-window** evaluation to expose regime dependence (2020–2021 microcap mania
   will flatter any momentum signal; a signal that only worked then is not a signal).
4. **Multiple-testing correction.** If testing K features, apply Benjamini–Hochberg FDR or a
   Bonferroni-style penalty; better, cap K *a priori* (test 3–5 hypotheses, not 50).
5. **Minimum sample sizes.** Require a floor of right-tail *positive events* (not just cells) — e.g.
   ≥ 100 distinct winner events spread across ≥ 8 calendar years and ≥ 3 sectors before any claim. With
   <30 winners the CI on precision is uselessly wide.
6. **Deflated / null-model benchmarking.** Compare every result against (a) the unconditional base rate,
   and (b) a **label-shuffle / random-entry null** run through the identical pipeline. The signal must
   beat the shuffled-label distribution, not just zero.
7. **Realistic expectation, stated up front.** Target = a *modest, stable lift in right-tail precision
   with bounded left-tail damage and positive net-of-cost expectancy*. If the honest answer is "2× the
   base rate with 2× the crater rate and it nets to zero after slippage," that is a **negative result**,
   and reporting it is the win.

---

## 5. Concrete, rigorous backtest design (step-by-step, PIT-clean)

A future arbiter advisor (call it `A4.archaeology`, shadow-only) could implement this. It must obey
INTERFACES §3 (all reads via `PITGateway`, no `datetime.now()` outside `clock.py`) and pass
`scripts/check_no_lookahead.sh`.

**Step 1 — PIT universe construction.**
Build, per `as_of`, the set of tradeable names *as known on that date*, including later-delisted ones if
the data allows (§2a). If using only currently-available feeds, log the survivorship caveat as a
first-class output field. Liquidity-tag each cell (ADV, price level) for the capacity analysis in Step 7.

**Step 2 — Entry-event generation (the "1,000 feet" rule).**
For each (ticker, date), evaluate the objective entry triggers from §2c using only `≤ as_of` data via
`PITGateway`. Each fired trigger is a candidate **entry event** with timestamp `t`. Dedupe so one base
produces one event (avoid counting the same launch many times → inflated n).

**Step 3 — Feature computation at the entry PIT.**
At each entry event, compute the candidate feature vector strictly from data with `filing_ts`/publish
ts/`as_of ≤ t`:
- technicals (base tightness, RVOL, distance from highs, beta_252d as of `t−1`),
- insider-cluster score from Form-4 (#distinct insiders buying, $ size, cluster window) — **once
  EDGAR_USER_AGENT is set and Form 4 is backfilled**,
- congress flag (secondary).
Reuse arbiter's exact entry convention: features as-of `t`, execution at `t+1 OPEN`, net
`model_slippage(price, spread)` (INTERFACES §3, §6).

**Step 4 — Labeling (forward outcome across the FULL set).**
For every entry event compute forward `ret_6m/1y/2y`, the tiered right-tail flags, the left-tail/delist
flag, and the **SPY-beta-adjusted alpha** via the existing `outcome_labeler` (`alpha_i = R_i − beta_i ·
R_SPY`). The label set spans *all* entry events, winners and losers alike — this is the survivorship fix
made operational.

**Step 5 — The screen / classifier.**
Start with a **transparent univariate / few-rule screen**, not a black-box model (interpretability is
mandatory in a multiple-testing regime and matches arbiter's `Opinion.rationale`/`stance_score`
contract). E.g. "insider-cluster ≥ 3 buyers AND first 30% breakout from a ≤ 6-month tight base." If and
only if a simple screen shows lift, consider a regularized logistic / gradient model with the same
time-split discipline. Never let model complexity exceed the right-tail event count can support.

**Step 6 — Evaluation metrics (precision & lift, never accuracy).**
- **Base rate** of each right-tail label in the unconditional universe.
- **Precision @ signal** = `P(right-tail | signal)`, with bootstrap CIs; **lift** = precision / base rate.
- **Recall / coverage** (what fraction of all rockets the signal would have caught — usually tiny; be
  honest).
- **Full conditional forward-return distribution** (mean, median, P5/P95, hit rate, crater rate) —
  signal vs. universe vs. label-shuffle null.
- **Expectancy net of cost** = signal-conditioned mean alpha after slippage and an explicit borrow/halt
  haircut.
- **By-year and by-liquidity-bucket** breakdowns (regime + capacity).

**Step 7 — Capacity / liquidity reality check (often the killer).**
The biggest movers concentrate in sub-$300M, sub-$1M-ADV microcaps. Re-run every metric **after**
applying arbiter's real constraints: the 2%-of-20d-ADV liquidity cap and `model_slippage` (INTERFACES
§9). Expect the edge to *shrink dramatically or vanish* once you can only buy a few thousand dollars of a
name without moving it. A signal that's profitable at $1k notional and dead at $50k notional is not
investable at arbiter's account size. Report the **dollar-capacity curve**, not a single number.

**Step 8 — Feed-back into arbiter (only if Steps 6–7 survive).**
Wrap the surviving screen as a **shadow advisor** emitting an `Opinion` (`advisor_id="A4.archaeology"`,
`confidence_source=EMPIRICAL`, honest `confidence`, `source_fingerprint` = hash of the entry-event basis).
It runs `shadow=True` through the trust ledger (zero live weight) until it earns weight on *real
out-of-sample* outcomes via the existing `outcome_labeler` → trust pipeline. This is exactly the
onboarding path the architecture already defines (INTERFACES §5 `shadow` flag). No special-casing.

---

## 6. Verdict & the single best v1 experiment

**Rating: LIKELY-OVERFIT-TRAP if run as "study the winners"; MARGINAL (worth one disciplined shot) if
run as a full-universe, base-rate, pre-registered study.** The reframe is the entire ballgame.

**Kill survivorship bias in one sentence:** Never sample on winners — define a point-in-time universe of
*all* tradeable names (delisted ones included as far as data allows), fire an objective entry rule at
every (ticker, date), and measure the *forward* return distribution conditional on the candidate feature
across the whole set, so you estimate `P(winner | feature)` (precision/lift over base rate), not the
useless `P(feature | winner)`.

**Single most defensible v1 experiment on arbiter's CURRENT data:**

> **Insider-cluster + breakout screen, evaluated as right-tail lift over base rate, fully PIT.**
> 1. First, **set `EDGAR_USER_AGENT` and backfill historical Form 4** (cheap, already-built ingest; this
>    is the gating prerequisite — without it, the only available family is plain technicals, which are
>    crowded and weak).
> 2. Pre-register ONE hypothesis: "An open-market insider cluster (≥ N distinct insiders buying within a
>    K-day window) *co-occurring with* a first breakout from a tight ≤ 6-month base predicts elevated
>    right-tail forward alpha (1y) versus the universe base rate, with positive net-of-slippage
>    expectancy at arbiter-feasible position sizes."
> 3. Run it through Steps 1–7 on the broadest PIT universe available (log the survivorship caveat),
>    time-split with a single locked out-of-sample test, benchmarked against the unconditional base rate
>    and a label-shuffle null, and capacity-tested with the real ADV cap + `model_slippage`.
> 4. Success criterion (pre-set): right-tail precision lift ≥ 1.5× **and** positive net-of-cost
>    expectancy that survives the capacity curve down to a few-thousand-dollar position. Anything less is
>    a documented negative result.
>
> Why this one: it uses *only* data arbiter can plausibly have PIT now (prices + Form 4), pairs the one
> genuinely *leading*, economically-motivated signal (insiders putting cash in) with an objective launch
> trigger, fits the existing `Opinion`/`outcome_labeler`/shadow-advisor plumbing with zero new
> contracts, and — most importantly — is structured as a falsifiable base-rate test, so even a null
> result is informative and publishable into the trust ledger.

**Biggest reason it may still not work:** the extreme winners live in illiquid microcaps where (a) the
truly causal precursors (float squeezes, dilution, news catalysts, short interest) are data arbiter does
**not** have, and (b) even a real statistical edge gets erased by slippage, borrow cost, halts, and the
2%-of-ADV capacity cap — so a paper edge that looks great at $1k notional is likely uninvestable at
arbiter's account size. Plan to report the capacity curve and treat a "true-but-uncapturable" finding as
the most probable honest outcome.

---

### Appendix — arbiter primitives this study reuses (no new contracts needed)
- `PITGateway.get(field, ticker, as_of)` + `scripts/check_no_lookahead.sh` — enforce no look-ahead (§3).
- `beta_252d` + `outcome_labeler` `ResolvedOutcome.alpha_bps` — SPY-beta forward labeling (§6 INTERFACES).
- Entry convention: `t+1 OPEN`, net `model_slippage` — matches Form-4 outcome labeling already in place.
- ADV cap (2% of 20d ADV) + sizing caps (INTERFACES §9) — the capacity reality check (Step 7).
- `Opinion` + `AdvisorWeight.shadow` + trust ledger — the onboarding path for `A4.archaeology` (Step 8).
- **Prerequisite gap:** `EDGAR_USER_AGENT` empty → Form-4 skipped; must be set + backfilled before v1.
- **Universe gap:** no PIT survivorship-free constituent/delisting set today → claims are an optimistic
  upper bound until acquired.
