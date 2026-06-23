# Feasibility Memo — "Signal Figures" Advisor (Angle A)

> **Status:** Research / feasibility only. No code. No source changes.
> **Date:** 2026-06-19  **Author:** research-analyst pass
> **Verdict (TL;DR):** **Marginal**, leaning to a *narrow* Promising slice. The
> general idea ("follow uncanny callers, reverse-engineer their picks into a
> tradeable signal") is mostly survivorship-driven narrative and is **likely
> noise** as stated. A *disciplined, falsifiable subset* — treat a small,
> pre-registered watchlist of figures as low-trust **A3 tip sources** that emit
> shadow Opinions and earn weight only through the existing forward-test /
> trust loop — is feasible and cheap to pilot, **because arbiter already has
> the scaffolding for it** (`arbiter/tips/`: `UnverifiedTip`, `TipSource`,
> `AccountScorer`, diversity gate, shadow A3 advisor, `unverified_tips` table).

---

## 0. Where this lands in arbiter

This is **not** a new architecture. The concept maps almost exactly onto the
already-built-but-dormant Lane-8 tips layer:

- `arbiter/tips/source.py` — `UnverifiedTip(ticker, claim, account, ts, url, source_id)`
  and the `TipSource` ABC. A "signal figure" is just an `account`; a news/X/blog
  scraper for that figure is a `TipSource` adapter (`source_id` e.g. `"fintwit.aschenbrenner"`).
- `arbiter/tips/account_scorer.py` — `AccountScorer` already produces a [0,1]
  credibility score from account metadata; this is the natural home for a
  *figure* credibility prior.
- `arbiter/tips/diversity.py` — enforces ≥2 *independent* corroborating sources
  before a tip can influence anything.
- `db/migrations/020_tips.sql` — `unverified_tips` + `account_scores` tables
  exist; tips are **recorded for audit/forward-test replay but NEVER contribute
  to live fusion** in the current phase (A3 is shadow/dormant by design).

So the honest framing for the user: **we are not inventing a "thought-leader
advisor"; we are proposing which figures to wire as A3 tip sources, and proving
they have edge before they ever touch a live order.** The contract discipline
(Opinion = stance/confidence/horizon/`as_of`; abstain = `None`; point-in-time,
no look-ahead; outcomes labeled with SPY-beta alpha; trust earned, capped, and
revocable) does all the heavy lifting against the traps below.

---

## 1. Candidate figures & a forward-testable selection rule

### The trap up front
The user's own example — citing SBF as a "good caller" — *is the disease, not a
data point.* SBF looked uncanny right up until he was a fraud; the only reason
he's memorable is that we're picking from the set of people we already have
strong priors about. Any watchlist built from "people who were obviously right"
is **survivorship + hindsight bias by construction** and will backtest
beautifully and trade terribly. Aschenbrenner is a milder version of the same:
his *Situational Awareness* essay reads as prophetic in mid-2026 partly because
AI/compute names ran — but the essay was a macro thesis, not a dated, sized,
tradeable pick, and we only celebrate it because the trade worked.

### The rule (objective, forward-testable, no hindsight)
Do **not** select figures by remembered accuracy. Select by *ex-ante eligibility
criteria* that could have been applied before knowing the outcome, then let the
forward test decide who survives:

1. **Eligibility filter (mechanical, point-in-time):** a figure qualifies for the
   watchlist only if, *as of the enrollment date*, they have:
   - a public, timestamped, archivable channel (X handle, Substack, fund letters,
     or 13F filer ID) — so picks can be captured at publish time, not reconstructed;
   - a history of **specific, falsifiable, ticker-level or clearly-mappable calls**
     (not just "AI is big") — measured by counting how many of their last N public
     statements parse to a concrete (ticker, direction) under the extractor in §3;
   - **no role conflict** that makes the statement a book-talk (flag if they
     disclose a position they're promoting — that's still recordable, but tagged).
2. **Pre-registration:** enroll a fixed cohort with a frozen `as_of` enrollment
   timestamp written to the audit log. From that moment, *every* parsed call is
   recorded — winners and losers — so the denominator is honest.
3. **Promotion is earned, not assumed:** all enrolled figures start as **shadow**
   advisors (zero live weight, `AdvisorWeight.shadow=True` already in the contract).
   They earn weight only through `evaluation/outcome_labeler.py` → trust ledger,
   exactly like every other advisor. Negative-skill → weight 0.0 (the ledger
   already does this; MiroFish-style hard caps apply).

This converts "follow smart people" (untestable) into "record a pre-registered
cohort and let SPY-beta alpha sort them" (testable). It directly kills
survivorship because **losers stay in the denominator** and the trust loop
demotes them automatically.

### A concrete starter cohort (illustrative, not endorsements)
Pick 3–5 spanning *different evidence types* so we test the sourcing pipeline,
not one person: one fintwit macro caller with dated specific calls; one
13F/13D-disclosing fund manager (Burry-type, structured filings); one
sector-specialist essayist (the Aschenbrenner archetype — AI/compute); one
"public process" investor who narrates entries (Twitter threads with tickers).
The names matter far less than the **diversity of source types** for the v1 test.

---

## 2. Data sourcing & timeliness (the lag problem dominates)

The core question is **not** "can we get the data" — it's **"how late are we vs.
when they actually acted?"** Arbiter already bleeds on the ~22d congress
disclosure lag (and the 45-day 13F lag is worse). Same disease here, by source:

| Source | Availability | Cost | Latency vs. their *action* | Legal/ToS | Extraction |
|---|---|---|---|---|---|
| **X / Twitter** | API v2 paid tiers; scraping is fragile/ToS-hostile | $100–$5k+/mo for usable tiers | **Real-time-ish** — but a *post* ≠ an *entry*; they may have positioned days/weeks earlier | ToS restricts scraping; API ToS allows but rate-limited; reposting content has IP limits | Unstructured NLP, very noisy (sarcasm, threads, memes) |
| **News APIs** (e.g. benzinga/newsapi-class) | Good | $0–$1k+/mo | Hours–days after the statement; news *about* a figure lags the figure | Generally licensable | Semi-structured; needs entity+ticker linking |
| **Substack / blogs** | RSS, public | Free–cheap | Same-day to days; essays are slow by nature | Public; respect robots/excerpting | Long-form unstructured; thesis-heavy, pick-light |
| **Podcast transcripts** | Whisper/3rd-party | Compute or per-min API | **Days** (record→publish→transcribe) | Transcribing public audio mostly OK; redistribution isn't | Very noisy ASR; conversational hedging |
| **13F (13F-HR)** | EDGAR, free, structured | Free | **Quarter-end + 45-day lag → up to ~135 days stale**; longs only, no shorts/options clearly | Public | **Structured** (best extraction) but **uselessly stale for timing** |
| **13D/13G** | EDGAR, free | Free | **Days** (13D due 10 days after crossing 5%) — much fresher than 13F | Public | Structured, activist-only, big-cap-rare |
| **Fund letters** | Public/leaked, irregular | Free–scrape | Weeks–months | Often copyrighted | Unstructured, retrospective |

**Takeaways:**
- **13F is a trap for this use case** — quarterly + 45-day lag means by the time
  you see Burry bought X, the move (and often the *exit*) has happened. Good for
  *studying* a thesis, near-useless as a fresh signal. Treat it as a credibility
  *prior* on a figure, **not** a live trigger.
- **X is the only genuinely fresh channel**, and even there the post lags the
  position. The realistic edge is *narrow*: a dated, specific, ticker-level public
  call where (a) the figure plausibly hadn't fully positioned and (b) the call is
  contrarian/non-consensus enough not to be priced in within minutes.
- **13D/13G is the underrated source**: structured, free, ~10-day lag, and it
  marks a *committed, sized, disclosed* action by a real money manager. It's the
  cleanest "signal figure" feed that isn't already stale — but it's rare and
  large-cap-light.

---

## 3. "What made them pick it" — what we can actually extract

The prompt asks to capture three things; they are **not** equally capturable:

- **The pick** (ticker + direction): *capturable.* An LLM can reliably extract
  `(ticker, stance ∈ [-1,1])` from a specific statement, with confidence reflecting
  parse certainty. This maps directly to `Opinion.ticker` + `Opinion.stance_score`.
- **The timing/horizon:** *partially capturable.* Sometimes stated ("over the next
  year"); usually inferred. Map to `Opinion.horizon_days` with a conservative
  default and a low `confidence` when implied. **Critical caveat:** the post
  timestamp is the `as_of`, but their *action* timestamp is unknown and earlier —
  so any horizon should be measured from a deliberately **pessimistic** entry
  (next-session open, per arbiter's existing entry convention), never from when
  they actually bought.
- **The thesis / "what made them pick it":** **largely NOT capturable as signal —
  it's mostly hindsight narrative.** This is the heart of the user's idea and the
  part I'd push back on hardest. We can extract the *stated rationale string* (and
  should, into `Opinion.rationale` for audit), but "reverse-engineering what about
  their reasoning predicted the move" is a post-hoc story we tell about the calls
  that happened to work. The reasoning is not independently verifiable ex-ante; the
  same reasoning template is applied to their losers too. **We should record the
  rationale for transparency, but the trust loop must score the *pick*, never the
  eloquence of the thesis.**

### Proposed extraction → Opinion (no look-ahead)
An LLM extractor turns one public statement into a candidate `Opinion`:
```
advisor_id        = "A3.figure.<slug>"          # e.g. A3.figure.aschenbrenner
ticker            = <resolved exchange ticker>   # NER + ticker linking; abstain if ambiguous
stance_score      = <signed, [-1,1]>             # direction + strength of language
confidence        = <[0,1]>                       # PARSE confidence, not calibrated prob
confidence_source = ConfidenceSource.SELF_REPORTED
horizon_days      = <stated or conservative default>
as_of             = <publish timestamp of the statement>   # NEVER now()
rationale         = <verbatim/condensed stated reason>      # audit only, not scored
source_fingerprint= <hash of the post/filing>               # correlation detection
```
Per the contract, **abstain (`None`) is the default** — emit an Opinion only on a
clean, specific, single-ticker, directional parse.

### Failure modes (each → abstain, not a guess)
- **Sarcasm / irony** ("oh sure, $X to the moon 🙄") — sentiment flips; LLM must
  flag uncertainty → abstain.
- **Hedging** ("I might trim, could add, watching X") — no clear stance → abstain.
- **Vague macro** ("compute is the new oil") — no tradeable ticker → abstain.
- **Multi-ticker baskets / "the AI trade"** — fan-out risk; either resolve to
  named tickers or abstain.
- **Book-talking / promotion** — record but tag; the diversity gate's
  independence requirement and the account scorer should discount self-interested
  hype.
- **Quotes/RTs of others** — attribution error; the statement isn't theirs.
- **Stale resurfacing** — an old call re-shared; `as_of` must be the *original*
  publish time or it injects look-ahead.

---

## 4. The honest traps (why I rate this Marginal, not Promising)

1. **Survivorship / cherry-picking** — the dominant risk. Mitigated *only* by §1's
   pre-registered cohort + honest denominator. If we ever hand-pick "the calls that
   worked," the whole thing is theater.
2. **Hindsight narrative** — "what made them pick it" is the story, not the signal.
   Handled by scoring the pick, not the thesis (§3).
3. **Base rates** — even genuinely skilled public figures' *public* picks beat the
   market only modestly and inconsistently; the literature on tracking 13F/fund
   manager disclosures, "expert" stock picks, and fintwit calls is broadly
   underwhelming after costs and lag. The honest prior is **near-zero alpha** until
   forward-tested. This is why the cohort starts **shadow** with floor weight.
4. **Macro-thesis vs. tradeable entry** — Aschenbrenner archetype: a *correct
   directional macro view* over years is not a sized, dated, risk-managed entry.
   The gap between "AI/compute goes up" and "buy NVDA here, this size, this stop"
   is where most of the imagined edge evaporates. Arbiter's sizing/horizon/exit
   machinery partly absorbs this, but a 2-year thesis is a poor fit for SHORT/MEDIUM
   buckets and will look like noise at those horizons.
5. **Front-running / already-priced-in** — *the killer for X/news.* By the time a
   followed figure's call is public and parseable, the most-watched names have often
   already moved (the figure themselves, plus everyone else following them, plus
   algos). The residual edge concentrates in: (a) less-followed figures, (b)
   less-liquid names not instantly arbitraged, (c) 13D-type disclosures of
   *committed* positions. For mega-cap AI names the public-call edge is likely gone
   on arrival.

**What would have to be true for this to work:** the figure's public statement
must lead, not lag, a move of meaningful size **after** a pessimistic entry, in a
name liquid enough to trade but not so over-watched it's instantly priced —
*and* that has to be true in a pre-registered forward sample, not a cherry-picked
backtest. That's a narrow target, which is exactly why the verdict is Marginal.

---

## 5. Concrete proposal & verdict

### Minimal v1 (pilot, fully shadow — zero live capital risk)
1. **Sources (2):** (a) **13D/13G EDGAR feed** — structured, free, ~10-day lag,
   marks committed sized actions; the cleanest non-stale signal-figure source, and
   it reuses the existing EDGAR ingest plumbing. (b) **One X/fintwit `TipSource`
   adapter** for the pre-registered cohort — real-time but noisy; this is where we
   test the LLM extractor (§3) and the front-running hypothesis (§4.5).
2. **Figures (3–5):** the pre-registered, source-diverse cohort from §1, enrolled
   with a frozen `as_of` and written to audit.
3. **Emit Opinions** via the existing `UnverifiedTip → AccountScorer → diversity
   gate → A3 advisor` path. Each clean parse becomes a candidate `Opinion`
   (`advisor_id = "A3.figure.<slug>"`, `confidence_source=SELF_REPORTED`), abstaining
   by default. **All Opinions are SHADOW** (`AdvisorWeight.shadow=True`, zero live
   weight) — recorded, never sized.
4. **Forward-test without look-ahead:** `as_of` = original publish/filing time; entry
   = next-session open net modeled slippage (existing convention); outcomes labeled
   by `evaluation/outcome_labeler.py` (SPY-beta alpha). Because everything is
   point-in-time and shadow, this is a *clean forward test*, not a backtest — the
   only honest way to evaluate it.
5. **Fit to the trust loop:** after enough closed shadow outcomes (mirror arbiter's
   ~30-closed-trade bar), feed the labeled alphas to the trust ledger. A figure
   earns live weight **only** if its forward alpha is positive and survives the
   ledger's CI/floor/cap logic; negative-skill figures auto-floor to 0.0. The
   diversity gate prevents one loud account from dominating; correlation deflation
   (already in the design) handles figures who all parrot the same call.

### Verdict: **Marginal** (with a Promising *narrow* slice)
- As the user framed it ("uncanny callers, reverse-engineer the thesis") it is
  **likely noise** — survivorship + hindsight + priced-in + macro≠entry stack
  against it, and the SBF example is a live demonstration of the failure mode.
- But the *cost to find out is low* and the *downside is bounded to zero* because
  the tips/A3 scaffolding already exists and the shadow/forward-test/trust
  machinery makes a wrong cohort self-correcting. The 13D/13G feed specifically is
  a genuinely fresh, structured, free signal that's worth building regardless of
  the X experiment.
- **Recommendation:** build the **13D/13G shadow source first** (cleanest data,
  reuses EDGAR plumbing, no LLM-extraction risk), add the X/figure adapter as a
  second shadow experiment, pre-register the cohort, and let the existing trust
  loop render the verdict on real forward data. Do **not** give any figure live
  weight on faith, and do **not** score the thesis — score the pick.

---

### Appendix — key arbiter touchpoints (for whoever implements)
- `arbiter/tips/source.py` — `UnverifiedTip`, `TipSource` ABC (figure adapter goes here).
- `arbiter/tips/account_scorer.py` — figure credibility prior.
- `arbiter/tips/diversity.py` — ≥2 independent-source corroboration gate.
- `arbiter/db/migrations/020_tips.sql` — `unverified_tips`, `account_scores` (already present).
- `arbiter/contract/opinion.py` — `Opinion` contract (`SELF_REPORTED` confidence source).
- `arbiter/trust/ledger.py` — shadow weight, floor/cap/negative-skill demotion.
- `arbiter/evaluation/outcome_labeler.py` — SPY-beta alpha labeling (the actual judge).
- `arbiter/ingest/edgar/` — reuse for the proposed 13D/13G feed.
