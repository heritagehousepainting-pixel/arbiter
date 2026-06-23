# I1 — Alpha Viability vs Disclosure Lag & Capacity

**Auditor lane:** I1 (alpha viability / strategy-quant critique — NOT statistical power, NOT code bugs)
**Date:** 2026-06-19
**Scope:** Does the arbiter A1 strategy (Form-4 insiders + Congress, long-only, MEDIUM/LONG horizon)
actually have tradeable edge *after* the observed ~22-day disclosure lag, net of costs and at this
account's capacity? Read-only. No source/test/config modified.

---

## VERDICT (read this first)

**Marginal-to-negative as currently wired; the system is honest about it but is pointed at the
weakest of its own signals.** The architecture is genuinely good — point-in-time discipline, SPY-beta
alpha labeling, shadow-then-earn trust gating, fail-closed sizing. Those make this a *safe place to
find out* whether edge exists. But on the merits of the strategy itself:

1. **The signal that is actually live is the weak one.** Form-4 insider ingest is **disabled** (empty
   `EDGAR_USER_AGENT` → skipped every run, per handoff). So every real order so far was driven by
   **Congress**, which the project's *own* design doc and research memos rate as the lowest-alpha,
   most-lagged, most-crowded signal. The 7 live paper BUYs were **AAPL, MSFT, AMD, FN, UBER, ETN, T**
   (extracted from `data/audit.jsonl`) — exactly the liquid mega-caps where post-lag edge is most
   likely already arbitraged away.
2. **The literature on the live signal (Congress) is weak-to-null after lag and crowding.** The
   stronger published edge belongs to *opportunistic insider cluster buys* — which are exactly the
   thing the system can't currently trade because Form-4 is off.
3. **Capacity is a non-problem here, but for a telling reason:** the names are so liquid that the
   2%-ADV cap never binds at a $10k account — which is another way of saying the strategy is fishing in
   the most efficient pond. The capacity headroom is real; the *edge* in those names post-lag is the
   doubt.
4. **Long-only + cold-start halving + quarter-Kelly produces near-homeopathic position sizes** (sample
   orders were fractional shares, sub-$15 notional). That is correct risk discipline for an unproven
   edge, but it also means even a *real* small edge nets ~nothing after the modeled 5bps + 0.5×spread.

**Can it make money?** Plausibly yes, but only under a narrow set of conditions that the *current*
live configuration does not satisfy: (a) trade the insider-cluster signal, not Congress; (b) bias
toward less-followed, less-liquid mid-caps where the lag hasn't been arbitraged; (c) accept that the
expected edge is *small lift, not a moonshot*, and that net-of-cost it may round to zero. As wired
today (Congress-only, mega-caps, long-only), the honest base-rate prior is **near-zero alpha after
the 22-day lag.** The shadow/trust machinery exists precisely so this gets *measured* rather than
*believed* — and that is the single most valuable property of the whole system.

---

## FINDINGS

### [P1] — Live signal is Congress-only; the strong signal (insider clusters) is switched off
**Why it matters:** The entire edge thesis rests on *insiders* (opportunistic, economically-motivated,
genuinely leading) far more than on *Congress* (thin, ~22-day-lagged, heavily crowded, weak in the
literature). The project's own design doc §1 says the insider edge is "event-specific" and real, while
"the ~45-day [Congress] disclosure lag erases most alpha — demoted to a slow sector/theme signal."
Yet `EDGAR_USER_AGENT` is empty, so `detect_signals` sees no Form-4 rows and the only thing driving
live orders is `congress_sector`. The system is currently running the experiment it least believes in.
The cold-start prior in `signals/scoring.py` even encodes this self-awareness: cluster_buy = 0.62,
single_insider = 0.58, **congress = 0.55** (barely above coin-flip). It is trading its 0.55 signal and
benching its 0.62 one.
**Recommendation:** Set `EDGAR_USER_AGENT`, backfill historical Form-4, and make the cluster-buy signal
the *primary* live driver. Treat Congress as a secondary/sector tilt, not the lead. Until then, label
every paper result as "Congress-only — does not test the core thesis."

### [P1] — Published evidence does NOT support Congress-following alpha after disclosure lag
**Why it matters:** The honest answer to "is there published evidence that disclosure-following beats
the market after the lag, net of costs?" is: **for Congress, broadly no.** The post-STOCK-Act
literature and practitioner track records show that (a) the headline "Congress beats the market"
results are pre-lag and survivorship-flattered, (b) the median ~22-day disclosure lag (observed here,
handoff §gaps) destroys most of the timing edge, and (c) a wave of copy-trading ETFs/bots (NANC, KRUZ,
Autopilot, Quiver, Unusual Whales) now front-runs the public filings within hours — the residual edge
for a *lagged* follower is close to zero. For *insiders*, the evidence is better but specific:
open-market **cluster** buys with no 10b5-1 plan show a small, documented post-filing drift; routine
single sells and planned trades show none. The system correctly filters 10b5-1 and requires `txn_type='P'`
— so its insider logic is aimed at the *right* sub-signal — but again, that signal is currently off.
**Recommendation:** Do not assume Congress alpha; require the shadow forward-test to *prove* positive
SPY-beta alpha before Congress ever earns live weight. Pre-register the hypothesis (per the
winner-archaeology memo's discipline) so a null result is a documented win, not a silent loss.

### [P1] — Long-only + MEDIUM/LONG horizon makes the strategy a leveraged-disguised beta bet
**Why it matters:** A1 only fires on purchases (`txn_type='P'`) and conviction maps to BUY; there are
no short entries from the disclosure signal. Over a MEDIUM (31–120d) / LONG (121–365d) horizon in a
bull tape, a long-only book of liquid large-caps will *look* profitable on raw return simply because it
is long equities — that is market beta, not alpha. The system's saving grace is that it scores
**SPY-beta-adjusted alpha**, not raw return (`outcome_labeler.py`: `alpha = R_i − beta_i·R_SPY`), so
the trust loop will not be fooled by beta. But the *strategy* still has an implied directional bet:
"disclosed buys outperform their own beta over months." If insiders/Congress buy *high-beta* names
near tops, the beta adjustment can turn a positive raw return into *negative* alpha — i.e. the strategy
can lose on the metric even while the account is up. This is the correct, honest framing, but it means
long-only here is structurally fighting an uphill alpha battle: it must beat beta with a lagged,
public, crowded signal.
**Recommendation:** Keep the SPY-beta metric (it is the right judge — see next finding) but explicitly
acknowledge in the strategy doc that long-only disclosure-following must generate *cross-sectional*
alpha (right names vs wrong names), since it gives up the short side entirely. Consider a long/short or
long-vs-sector-ETF construction in a later phase to isolate the actual stock-selection edge from beta.

### [P2] — SPY-beta alpha is the RIGHT success metric, but it doesn't match how the bot sizes/holds
**Why it matters:** SPY-beta-adjusted alpha is the correct success measure (it strips the beta freebie,
it's continuous, it drives trust) — good. But there are two mismatches between the *metric* and the
*mechanics*:
(a) **Sizing is quarter-Kelly × cold-start-halving × caps**, producing sub-$15 fractional-share orders
on a $10k account (observed in `audit.jsonl`). The alpha metric is scale-free (bps), so a 200bps
"win" on a $12 position is $0.24 — economically meaningless after the modeled 5bps + 0.5×spread
slippage and any commission. The metric says "edge"; the P&L says "noise." For paper-sim that's fine;
the danger is reading a positive *alpha* as a positive *strategy* when net-dollar expectancy is ~0.
(b) **Horizon mismatch:** Congress is bucketed MEDIUM by design but its observed lag is ~22d (SHORT-ish,
handoff §gaps). The outcome is labeled at `idea.as_of + horizon_days` from the *filing* date, but the
*information* is already ~22d stale at entry. So the metric measures alpha over a window that starts
well after the insider/member acted — correctly pessimistic, but it means the bot is structurally
measuring the *tail* of a move, not the move.
**Recommendation:** Add a **net-dollar expectancy after costs** companion metric alongside alpha_bps,
and a **per-bucket realized-lag** report, so a "positive alpha / zero-dollar / fully-lagged" result is
visible as the negative result it is.

### [P2] — Crowding: the live names are the most-followed disclosure targets in existence
**Why it matters:** The 7 live BUYs (AAPL, MSFT, NVDA-adjacent, AMD, UBER, ETN, FN, T) are precisely
the names that every congress-tracker ETF, every fintwit account, and every retail copy-trading app
(Autopilot ~$750M AUM, named in the design doc as the existing competitor) already buys within hours of
the same public filing. A lagged follower entering ~22 days later, in the most-watched megacaps, is the
textbook definition of being last to a crowded trade. The design doc's own correlation-deflation /
lone-bull-tax machinery is built for *internal* advisor crowding; it does nothing about *external*
market crowding on the underlying signal, which is the real threat to this edge.
**Recommendation:** Bias the universe toward **less-covered names** (smaller mid-caps, non-headline
tickers) where the public-filing edge plausibly survives the lag, and *down-weight* any name that is a
known congress-ETF top holding. The differentiator the design doc claims ("proof-of-edge before
copying") only matters if it leads to *different* trades than the crowd, not the same megacaps later.

### [P3] — Capacity/liquidity is NOT a binding constraint here — and that is itself a yellow flag
**Why it matters:** With a $10k account and a 2%-of-20d-ADV cap (`adv_cap_pct = 0.02`), the cap can
only bind on names with ADV below ~$0.5M/day. The live names trade $1B–$50B/day, so the ADV cap
*never bites* and slippage is minimal — capacity is a solved problem. But the reason it's solved is
that the strategy lives entirely in ultra-liquid, ultra-efficient mega-caps, which is exactly where
post-lag disclosure edge is *least* likely to exist (the winner-archaeology memo §7 makes the inverse
point: the real edge lives in thin names where capacity *would* bind). So arbiter has the *opposite*
problem to the classic microcap-alpha trap: not "great edge, no capacity," but "ample capacity, dubious
edge." Borrow/halt risk is also moot because it's long-only large-cap. The capacity machinery is
correct and well-built; it's just not where the risk is.
**Recommendation:** None on the capacity code (it's right). But treat "the ADV cap never binds" as a
*diagnostic that the universe is too efficient*, and use it to justify pushing toward less-liquid names
where the cap *and* the edge would both become real — then the capacity curve (notional vs alpha) becomes
the decisive test, per the winner-archaeology memo Step 7.

---

## OPPORTUNITIES TO ADD (what would most improve expected edge)

1. **Turn on the strong signal.** Set `EDGAR_USER_AGENT`, backfill Form-4, and make
   **opportunistic insider cluster buys** (≥2 distinct non-10b5-1 buyers) the primary live driver.
   This is the single highest-expected-value change — it swaps the 0.55 signal for the 0.62 one and
   aligns the live system with the only sub-signal that has real published support.
2. **Add the 13D/13G EDGAR feed** (signal-figures memo §5): structured, free, ~10-day lag (vs 22–45d),
   marks *committed, sized* manager positions, reuses existing EDGAR plumbing. Freshest clean signal
   available; far better lag profile than Congress or 13F.
3. **Universe selection toward less-crowded names.** Down-weight congress-ETF top holdings and megacaps;
   prioritize mid-caps where the public-filing edge can survive the lag. This is where the ADV cap and
   the alpha both become non-trivial.
4. **Companion economic metrics.** Report **net-dollar expectancy after costs** and **per-bucket
   realized-lag** beside alpha_bps so "positive-alpha / zero-dollar / fully-lagged" results are
   unmistakable. Prevents mistaking a scale-free metric win for a tradeable strategy.
5. **Cross-sectional / market-relative construction.** Because the book is long-only, isolate
   stock-selection alpha by benchmarking each name against its *sector ETF* (not just SPY-beta),
   or pair longs against a sector short in a later phase — this directly tests whether the signal
   picks the *right* disclosed names, which is the only thing a lagged long-only follower can win on.
6. **Pre-register before any live weight.** Adopt the winner-archaeology memo's discipline: written
   hypothesis, time-split, label-shuffle null, minimum lift threshold — so a null Congress result is a
   *documented* finding that retires the signal, not a quietly-tolerated drag.

---

## What the system gets RIGHT (so the critique is fair)

- **SPY-beta alpha labeling** is the correct judge and immune to the long-only beta illusion.
- **Point-in-time `get(field, ticker, as_of)` + no-look-ahead lint** make the forward test honest.
- **Shadow → earn-weight trust gating** means no signal trades on faith; a bad signal auto-floors to 0.
- **10b5-1 exclusion + `txn_type='P'` filter** aim the insider logic at the *one* sub-signal with edge.
- **Fail-closed sizing** (quarter-Kelly, cold-start halving, ADV cap last) keeps an unproven edge from
  doing damage while it's being measured.

The verdict is not "the architecture is wrong" — it's "the architecture is a good instrument currently
pointed at the weakest signal, in the most-crowded names, at a size where even real edge nets ~zero.
Re-aim it (insiders + less-crowded names + economic metrics) and let the trust loop render the verdict
on real forward data."
