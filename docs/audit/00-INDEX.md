# Arbiter — Full Audit Index (36 lanes)

Date: **2026-06-19**. Mode: **read-only audit, no code changed.** Suite baseline: **1877 passing**.
Per-lane detail in the sibling files (`A1-…md` … `L1-…md`). This index ranks findings across lanes.

## Headline verdict

**Is this the best version of itself? No — but the gap is "wired-but-inert / broken-at-the-production-seam,"
not "bad architecture."** The system is an impressively complete, well-structured scaffold with genuinely
rigorous point-in-time discipline (B2/B3 PASS) and a learning loop that, after #5a, *actually learns*
(a confidently-wrong advisor gets suppressed — verified). BUT a striking number of its safety and signal
features **do not actually do anything at runtime**, and almost none of that is visible because the tests are
green. The recurring pattern: **the sim/fixture path is tested; the production (alpaca_paper / scalar-data /
populated-book) path has real holes.**

The single most important meta-finding: **today, the only things constraining a live order are the 5%-per-name
cap and the gate/quorum. Every portfolio-level risk control (max positions, gross, sector, ADV/liquidity) is
inert.** That must change before real capital.

## Severity tally (distinct issues, deduped)

- **P0 (critical / blocks confident live trading): 11 themes** — see below.
- **P1 (serious): ~28.** P2/P3: many (in the lane files).
- Clean PASS lanes: **B2** (intraday-price isolation), **B3** (learning PIT), **J2** (injection surface).
  Mostly-clean: B4, D1, D4, J1.

---

## P0 — the eleven that matter most

### Risk controls are inert (the biggest theme)
1. **Portfolio risk caps never bind at runtime** [A2, confirmed by G2]. `engine._bound_decide` calls `decide()`
   without the current book, so `max_open_positions(8)`, `max_gross_pct(0.50)`, `max_sector_pct` are evaluated
   against an empty book every order. The `.env` $10k guardrails are decorative. Unit tests pass book-state
   explicitly → the gap is invisible.
2. **ADV / liquidity cap is structurally broken** [B1, C4, confirmed by G2]. ADV is computed from close *price*,
   not dollar *volume* (~$115 vs ~$115M) because the wired price source returns a scalar where `adv.py` expects a
   Bar. The 2%-ADV cap and the A3 volume-anomaly breaker silently never bind. ADV tests use Bar fixtures →
   invisible.
3. **Sector cap degenerates to one book-wide bucket** [A2, L1]. No `sector_by_ticker` mapping exists; every name
   resolves to `"UNKNOWN"`, so "20% per sector" is really "20% of the book in one bucket." Claimed real in
   INTERFACES.

### State / execution integrity
4. **Orphan positions are undetected** [D3]. `reconciler.reconcile()` (LOCAL_ONLY/BROKER_ONLY/QTY_MISMATCH) is
   built, tested, and **never called** by the live engine. The documented "accepted residual" mitigation is not
   in effect — a lost-response broker fill becomes unmanaged capital the exit monitor can never sell.
5. **Crash after a fast-iteration sell resurrects the closed position** [F3]. `run_fast_iteration` sells
   (mutating SimExecutor + ideas/outcomes) but never snapshots `sim_positions`; a crash before the next full
   cycle makes `seed_executor` restore the sold position and roll back cash/realized_pl.
6. **Market calendar is wrong on real days** [F4]. **Juneteenth (2026-06-19) absent → offline calendar says
   "open" today (it's closed).** Veterans Day wrongly listed as a *closure*. Post-2026 floating holidays all
   missed (MLK 2027 = "trading day"). Bites whenever the Alpaca `/v2/clock` is unreachable and the offline
   fallback is used.

### Signal / data
7. **Form-4 insider ingest is broken the moment it's enabled** [C2]. Discovery queries EDGAR with a schema EDGAR
   never emits → 0 filings, reported as silent success. So "set `EDGAR_USER_AGENT` to add the insider brain"
   would add *nothing*. (And ingest isn't called by the cycle at all — manual CLI only.)
8. **Stooq fallback is dead → single-sourced on Alpaca** [C4]. Stooq needs `AAPL.US`; it's handed the bare
   ticker. The "bars not found" from the live run is this. No data redundancy; an Alpaca outage = fail-closed,
   no trades.

### Learning-signal trustworthiness
9. **The learning signal is regime-/leak-contaminated** [E2, E5, E4]. (a) The calibrator fits on a
   *label-derived* feature (`sign(binary)·confidence`) instead of the real stance — an over-confident leak;
   (b) `pool.py` sums calibrated probabilities [0,1] into signed signal-space [-1,1], losing directional sign
   once any model is active; (c) A1 is long-only, so negative-skill suppression can trip on a market *drawdown*
   (and `trust/regime.py`, the stated defense, is **dead code**); (d) a beta fit on log-returns is applied to
   simple returns, leaking market direction into "alpha."

### Premise / stats
10. **The "~30 trades proves it" premise is statistically false** [I2]. n=30 is ~10× under-powered for realistic
    per-trade alpha variance; SHADOW_THRESHOLD=30 graduates an advisor on a *count* with no significance test
    (≈half of null advisors would graduate); the 182-day Brier half-life decays trade #1 to 0.32 weight before
    30 MEDIUM-horizon trades even accrue (~1.4 yr wall-clock). The loop will "learn," but largely noise, for a
    long time.

### Operability
11. **Silent failure is invisible** [K1]. `ALERT_WEBHOOK_URL` is empty (no push ever reaches a human); `/health`
    is a hardcoded `{"status":"ok"}`; and *every* P0 above fails silently — none trips a breaker, raises, or
    shows as a status anomaly. An idle-broken bot looks identical to a healthy one ("orders_submitted=0" looks
    fine).

---

## P1 — serious (by theme)

- **Paper→live boundary** [A3]: `ALPACA_PAPER_BASE_URL` is env/TOML-overridable with **no host validation** — a
  one-line `.env` edit could route "paper" orders to a live endpoint while every guard still labels them paper.
  Add a host assertion. (Structural paper-only is otherwise CONFIRMED.)
- **Breakers mostly unwired** [A4]: 4 of 6 breakers (daily-loss, per-position, mirofish-fail, confidence-shift)
  have zero production callers; the kill-switch is never consulted in the shipped paper posture
  (`live_trading=false`, empty `KILL_SWITCH_URL`).
- **Sizing** [A1]: "quarter-Kelly" is actually linear conviction scaling; its safety is entirely the 5% cap, and
  `|conviction|` is unclamped.
- **Ingest correctness** [C1, C3]: House `filing_ts` uses the PDF notification date (look-ahead risk); Senate
  fabricates tickers from raw cells + defaults unknown txn types to "S"; House amendments are *filtered out*
  (stale originals persist); Senate amendments over-supersede by `(ticker, person_id)` across all history.
- **Fill/partial handling** [D2, D4, F3]: BUY advance still uses the fragile `(ticker,bucket)` join not the
  `orders.idea_id` that migration 023 added; `partial` rows are never re-reconciled and persist requested-not-
  filled qty; an alpaca double-submit window exists (place precedes ledger insert).
- **Trust math** [E1, E3]: stance/confidence unclamped → out-of-range can drive BSS to −8 and permanently mute an
  advisor; the thin-sample floor is dead code; the neutral fallback emits a *real* advisor id (can mask the true
  stance if it ever fires first).
- **PIT half-enforced** [B4, B5]: entry/beta reads aren't bounded by `cutoff_as_of`; the no-look-ahead linter is
  a literal-pattern tripwire (misses `time.time()`, `date.today()`, aliased datetime, helper indirection) and
  there's **no insert-only linter** despite §11.2 being "CI-enforced."
- **Daemon/DB** [F1, F2]: heartbeat isn't rewritten on a *failing* iteration (stale exactly when it matters); no
  explicit `PRAGMA busy_timeout` (relies on CPython's undocumented 5s default).
- **Strategy** [I1]: all 7 live trades were Congress-only megacaps (the weakest signal, most crowded, least
  evidence of post-lag edge); sub-$15 fractional sizes net ≈$0 after costs. The better signal (insider clusters)
  is the one that's switched off/broken.
- **Maintainability** [H1, H2]: `engine.py` is a 1403-line god-object (run_cycle 369 lines, `_advisor_id_for`
  duplicated 4×, the idea join inlined 3×); INTERFACES.md's structural prose is stale (insert-only §11.2 violated
  by 6 paths; `seams.py` undocumented; `output.py` referenced but absent; new Config fields missing from the
  "exact" list).
- **Secrets** [J1]: webhook URLs (with tokens) logged verbatim at 4 sites; frozen `Config.__repr__` would expose
  the Alpaca key+secret in cleartext if ever logged (latent).

---

## What's solid (don't lose this)

- **PIT/no-look-ahead discipline** is genuinely strong: intraday-price isolation (B2) and learning-loop cutoff
  (B3) are airtight; the injected-clock design is the real defense.
- **Idempotency core** (D1) and **the sell-execution path** (D4) are sound.
- **#5a attribution** (E3) and the **negative-skill detection** it unlocked are real (verified end-to-end).
- **Injection surface** (J2) is clean; secrets fundamentals (gitignore, not git-tracked, chmod 600) are fine.
- **SAVEPOINT/atomicity** and **single-writer flock** discipline are correct.
- The docs are mostly *honest* about what's shadow/stub (L1) — the dangerous cases are the few that are claimed
  real but inert (sector cap, ADV cap, breakers).

---

## Recommended priority order (for when you choose to act — no code changed here)

1. **Make risk caps real before any live capital**: pass book-state into `decide()` (A2), fix ADV to dollar-
   volume (B1), add a `sector_by_ticker` map (A2/L1), validate the paper base-url host (A3). *Highest value;
   these are the guardrails on a real account.*
2. **Fix the calendar** (F4) — Juneteenth/Veterans Day/post-2026; it's wrong *today*.
3. **Wire the reconciler + close the crash/partial gaps** (D3, F3, D2).
4. **Wire real alerting + a true `/health` + silent-failure watchdogs** (K1) — so the rest can't fail invisibly.
5. **Repair the learning signal** (E2 stance-fed calibrator, E4 pooling, E5 regime/return-convention) and
   **reframe the stats** (I2: report power/MDE, gate on significance not a count of 30).
6. **Signal upgrades** (I1/C2): fix Form-4 discovery, add 13D/13G, push the universe off crowded megacaps.
7. **Maintainability**: extract from `engine.py` (H1) and reconcile INTERFACES.md (H2) before the next features.

## Note on "best version"

Most of these are exactly the class of bug a **live run catches and offline tests miss** — which is the
project's own stated ethos. The audit found them statically; a few hours on a *real* (paper) run with
instrumentation would surface the rest. The architecture is sound enough that all of the above are fixes, not
rewrites.
