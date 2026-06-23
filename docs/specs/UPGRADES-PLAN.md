# Arbiter — Upgrade Build Plan (from the 36-lane audit)

Date: 2026-06-19. Source: `docs/audit/` (esp. `00-INDEX.md`). Baseline suite **1877 green**, `.venv/bin/python`.
Discipline: plan → audit-plan → build in waves with **disjoint file ownership**, TDD, suite stays green, NO
network/real-sleep in tests, no-look-ahead clean. Repo is NOT git-tracked → no worktrees → parallel build agents
share one tree, so **strictly disjoint ownership + each agent runs only its OWN targeted tests** (the full suite
is run by the orchestrator after each wave).

## Hard sequencing constraint
These findings all edit `engine.py`: risk-cap binding, reconciler wiring, crash-snapshot, breaker wiring,
health/watchdog. They CANNOT be built by parallel agents. Resolution: parallel agents build **self-contained NEW
modules** (a risk-book accumulator, a sector map, a reconcile/orphan helper, a health monitor); then **ONE
engine-integration owner** wires them into `engine.py` in a single pass. Engine refactor (H1) is LAST.

## Scope tags
- **NOW** — build this effort (clear, in-repo, high value).
- **NEEDS-USER** — buildable but inert/undelivered without a user action (flagged, built anyway where useful).
- **DEFER** — risky or research-grade; do after the NOW set or with explicit sign-off.

---

## WAVE A — parallel, disjoint (no engine.py edits). ~13 work packages.

**A-CAL — Market calendar holidays** [F4, P0]. Owner files: `arbiter/data/replay_clock.py`,
`arbiter/runtime/market_calendar.py` (offline only). Add Juneteenth (2021+), remove Veterans Day (NYSE open),
add MLK/Presidents/Good Friday/Memorial/Labor/Thanksgiving floating-holiday GENERATION (not a literal list) so it
doesn't cliff after 2026; keep early-close map. Tests: 2027 holidays correct, Juneteenth closed, Veterans open.
**NOW.**

**A-ADV — ADV = dollar-volume, not price** [B1, C4, P0]. Owner: `arbiter/data/adv.py`, the price-source
Bar-vs-scalar seam in `arbiter/data/sources/` (and how `adv.py` gets volume). Make ADV compute close×volume from
a Bar (or a dedicated volume field) so the 2%-ADV cap and volume-anomaly breaker actually bind. Tests must use
the PRODUCTION source shape, not Bar fixtures (this is what hid the bug). **NOW.**

**A-STOOQ — Stooq symbol mapping** [C4, P0]. Owner: `arbiter/data/sources/` (Stooq adapter). Map bare ticker →
`TICKER.US`; restore real fallback redundancy. Surface "both sources down" distinctly from "no data". **NOW.**

**A-SECTOR — sector_by_ticker map** [A2, L1, P0-enabler]. Owner: NEW `arbiter/data/sectors.py` (a static
ticker→sector map for the watchlist + an "UNKNOWN" default + a clear extension path). Pure module; consumed by
the engine-integration owner in Wave B to make the sector cap real. **NOW.**

**A-RISKBOOK — book-state accumulator** [A2, P0-enabler]. Owner: NEW `arbiter/policy/book.py` — a pure helper
that, given current positions + a running set of this-cycle decisions, returns current open-count / gross /
per-sector exposure to feed `decide()`. Pure, unit-testable; the engine owner threads it in Wave B. Coordinate
the `decide()` signature it targets with `policy/decision.py` (read-only here; the param already exists). **NOW.**

**A-PAPERURL — paper base-url host validation** [A3, P1]. Owner: `arbiter/config.py`. Validate the resolved
`alpaca_paper_base_url` host == `paper-api.alpaca.markets` (fail-closed); assert `not live_trading` until a real
live path exists. **NOW.**

**A-DBTIMEOUT — SQLite busy_timeout + WAL discipline** [F2, P1]. Owner: `arbiter/db/connection.py`. Set an
explicit `PRAGMA busy_timeout`; document/encourage checkpoint. Small, disjoint. **NOW.**

**A-SECRETS — stop leaking webhook URLs + redact Config repr** [J1, P1]. Owner: `arbiter/safety/alerting.py`,
`arbiter/safety/kill_switch.py`, `arbiter/config.py` (a redacting `__repr__`/`__str__` that masks api_key/secret/
webhook). Don't log full tokened URLs. **NOW.**

**A-LEARN — learning-signal correctness** [E1, E2, E5, P1/serious]. Owner: `arbiter/calibration/calibrator.py` +
`calibration/multi_advisor.py`, `arbiter/fusion/pool.py`, `arbiter/trust/brier.py`, `arbiter/trust/ledger.py`
(clamp only), `arbiter/data/beta.py`, `arbiter/evaluation/outcome_labeler.py`. Fixes: (1) calibrator fits on the
REAL `stance_score`, not the label-derived `sign(binary)·confidence`; (2) `pool.py` must not collapse calibrated
[0,1] prob into signed [-1,1] losing direction — keep direction explicit; (3) clamp stance∈[-1,1] & confidence∈
[0,1] before Brier; (4) make beta's return convention consistent (fit and apply on the SAME return type). This WP
is internally coupled but disjoint from engine.py. Large — give it room. **NOW.** (NOTE: changes learned-weight
behavior — intended.)

**A-INGEST — congress ingest correctness** [C1, C3, P1]. Owner: `arbiter/ingest/congress/`. House `filing_ts`
uses the Clerk receipt date (not the PDF notification date) where that's the true public-availability date;
validate Senate tickers (kill the dead `_VALID_TICKER_RE`, drop non-tickers); don't default unknown txn types to
"S" (mark ambiguous); fix amendment over-supersede (scope to the same filing, not all `(ticker,person_id)`
history); decide House-amendment handling (at least stop silently dropping corrections). **NOW.**

**A-SUBMIT — submit/idempotency hardening** [D1, P1]. Owner: `arbiter/execution/submit.py`,
`arbiter/execution/idempotency.py`. Make "rejected order is NOT persisted" unconditional (not gated on a
breaker); single-source the `dedup_hash` (remove the decision.py duplication); harden `entry_date` typing in the
hash. **NOW.**

**A-LINT — PIT/insert-only lint hardening** [B4, B5, P1]. Owner: `scripts/` (NEW `check_insert_only.sh`,
broaden `check_no_lookahead.sh` to catch `time.time()`/`date.today()`/aliased datetime/`pd.Timestamp.now`), and
bound entry/beta reads by `cutoff_as_of` in `arbiter/evaluation/outcome_labeler.py`. Coordinate the labeler edit
with A-LEARN (both touch outcome_labeler) — assign outcome_labeler ENTIRELY to A-LEARN and have A-LINT own only
the scripts. **NOW.**

**A-DOCS — INTERFACES/contract reconciliation** [H2, L1, P1/P2]. Owner: `arbiter/INTERFACES.md` (+ a short
`docs/` note). Document `contract/seams.py`; reconcile the §11.2 insert-only rule with the 6 real carve-outs;
remove the dead `fusion/output.py` reference; add the new Config fields to the "exact" list; fix stale
LIVE_TRADING docstrings (the docstrings live in code — list them for the engine owner, don't edit engine here).
Docs only. **NOW.**

---

## WAVE B — engine integration (single owner of `engine.py`, sequential).

**B-ENGINE — wire the Wave-A modules + inline engine fixes.** Owner: `arbiter/engine.py` (+ `position_store.py`,
`reconciler.py` call sites). Does, in one coherent pass:
- **Risk caps bind** [A2, P0]: thread A-RISKBOOK book-state + A-SECTOR map into every `decide()` call, accumulate
  across ideas in a cycle so gross/sector/open-count actually constrain; remove the dead $100k phantom equity
  fallback.
- **Reconciler wired** [D3, P0]: call `reconciler.reconcile()` each cycle (alpaca_paper) to surface
  LOCAL_ONLY/BROKER_ONLY/QTY_MISMATCH; route orphans to an alert/log.
- **Fill fixes** [D2, P1]: use `orders.idea_id` (mig 023) for the BUY advance instead of the `(ticker,bucket)`
  join; persist partial-fill `filled_qty` and re-reconcile partials.
- **Crash snapshot** [F3, P0]: snapshot `sim_positions` after a fast-iteration sell; add reconcile-on-start.
- **Breakers wired** [A4, P1]: actually call daily-loss / per-position checks each cycle; consult the kill switch
  in paper posture (or document why not).
- Fix the stale LIVE_TRADING docstrings A-DOCS flags.
Depends on: A-RISKBOOK, A-SECTOR (built first). Built AFTER Wave A lands green.

**B-HEALTH — observability & watchdogs** [K1, P0]. Owner: NEW `arbiter/runtime/health.py` + `arbiter/web/server.py`
(real `/health`) + the daemon heartbeat read. A self-contained HealthMonitor: live `/health` (checks heartbeat
age, last-cycle, last-fill, paused, data-source up, fallback rate), and watchdog conditions (ingest wrote 0 rows,
N cycles with all-orders-skipped, no fills in N days, high fallback rate, still-paused reminder) that emit via the
existing alerting. Mostly a new module + server edit; minimal engine touch (emit a few signals) — coordinate the
engine touch with B-ENGINE (do B-HEALTH's engine hooks inside the B-ENGINE pass to keep one engine owner).
**NOW** (delivery NEEDS-USER: alerting only pushes once `ALERT_WEBHOOK_URL` is set — build the plumbing + the
audit-log path regardless).

**B-STATS — significance-gated graduation + power reporting** [I2, P0-stats]. Owner: `arbiter/trust/ledger.py`
(graduation logic) — coordinate with A-LEARN which also edits ledger (clamp only): split ownership — A-LEARN owns
the brier/clamp; B-STATS owns the `should_update`/graduation/shadow-lift logic. Add: graduate on a significance/
effective-n test, not a bare count of 30; surface power/MDE + bootstrap CI in the leaderboard/reports. Build after
A-LEARN. **NOW (measurable parts); DEFER the deep shrinkage/FDR research.**

---

## DEFER / NEEDS-USER (flagged, not in the first build)

- **Form-4 discovery rewrite** [C2, P0-when-enabled] — buildable (fix the EDGAR query/schema) but inert until the
  user sets `EDGAR_USER_AGENT`. Build it, mark NEEDS-USER. Owner: `arbiter/ingest/` form4/edgar. **Build in a
  later wave** (disjoint from Wave A; could be added to Wave A if capacity).
- **MiroFish A2 (#5b)** — BLOCKED on user endpoint. Not in this effort.
- **engine.py refactor** [H1] — DEFER until the functional fixes settle (it conflicts with B-ENGINE).
- **Strategy/universe changes** [I1] (13D/13G feed, mid-cap universe, true Kelly [A1]) — research/judgment;
  surface as proposals, don't auto-build.
- **Egress redirect/zip-bomb hardening** [J2, P2], **A1 true-Kelly** [A1, P1] — nice-to-have, later wave.

## Definition of done (per WP)
Its targeted tests pass; after each wave the orchestrator runs the FULL suite (must stay green, ≥1877 + new) and
`check_no_lookahead.sh` + the new `check_insert_only.sh`. Behavior-changing WPs (A-LEARN, B-ENGINE risk caps,
B-STATS) note the behavior change explicitly. No item ships on `sim`; nothing flips `EXECUTOR_BACKEND`.

---

## POST-PLAN-AUDIT AMENDMENTS (binding — supersede conflicting text above)

**Ownership corrections (3 collisions found):**
- **MERGE A-ADV + A-STOOQ -> `W-DATA`** (owns `data/sources/alpaca.py`, `stooq.py`, `_gateway.py`, `data/adv.py`,
  AND `defenses/volume_anomaly.py`). **ADV fix:** do NOT flip `price_close` to a Bar (4 scalar callers). Add a NEW
  internal Bar/volume accessor (e.g. `adv.py` calls `source.bars()` directly); leave scalar `price_close` intact.
  BEHAVIOR-CHANGING (ADV cap binds).
- **CONFIG single owner `W-CONFIG`**: paper-url host validation + `not live_trading` assert + redacting
  `__repr__` (merges A-PAPERURL + A-SECRETS).
- **`W-SUBMIT` also owns `policy/decision.py`** (dedup single-source) + partial-SELL `filled_qty` fix +
  rejected-not-persisted-unconditional + entry_date typing. A-RISKBOOK stays READ-ONLY on decision.py (book
  params already exist, no signature change).
- **`ledger.py` -> B-STATS only.** A-LEARN's clamp is in `brier.py` (+ `seams.py __post_init__`).

**Frozen contract decisions:**
- **Pool sign-space [E2/E4]:** ALL calibrator branches return P(positive-alpha) in [0,1]; the pool maps to signed
  via `2p-1` before weighting. Standardize the inconsistent identity/unknown branches in `multi_advisor.py`. The
  cross-ticker bucket-pooling restructure [E4 2nd P1] is a bigger change -> DEFER (flag only).
- **Calibrator fit [E2]:** fit on real `stance_score`, EXCLUDE legacy rows where `stance_score==0.0`.
- **Risk-cap accumulator [B-ENGINE]:** mutable running gross/sector/count in the closure, seeded from broker
  positions as USD market value (qty is notional dollars, not shares), updated only AFTER a successful submit.
- **Beta [E5]:** fit & apply the SAME return convention; expect beta/labeler test churn.
- **Calendar [F4]:** arithmetic floating holidays (Good Friday via Easter computus, no lib) + weekend-observed
  shift; Juneteenth observed 2022+; remove Veterans Day.

**ADDED work packages:**
- **W-BACKFILL — historical outcome-backfill harness** [L1 #1, highest-value ADD]. NEW module + CLI: replay
  already-ingested historical disclosures through the PIT gateway against REAL historical bars + `outcome_labeler`
  to mint `ResolvedOutcome` rows for past closed ideas, so the ledger/calibrator have DATA (vs cold-starting ~a
  year). MUST be PIT-clean (label at the historical horizon, never future-of-replay-date). Disjoint from engine.
- **W-LEARN also owns** `evaluation/attribution.py` (E3 reserved-proxy-id fallback) + the pool double-count [E4].
- **W-TESTHARDEN — untested money-path coverage** [G1 P0s]: NEW tests only — `loop_runner.main` flock, Alpaca
  cancel/get_order failure-mapping, submit IntegrityError race, engine partial-SELL guard. After B-ENGINE.
- **B-STATS also:** real bootstrap CI (replace `composite*0.8` placeholder), effective-n gating, net-dollar-
  expectancy + realized-lag metrics [I1].
- **13D/13G advisor** [I1 #2]: promote to a later buildable wave (NEEDS-USER on `EDGAR_USER_AGENT`).

**Also behavior-changing:** W-DATA, W-INGEST (add to the flag list).

**Build order:** Wave-A1 (cleanest, fully disjoint — START NOW): W-CAL, W-SECTOR, W-RISKBOOK, W-CONFIG,
W-DBTIMEOUT, W-LINT. Wave-A2: W-DATA, W-INGEST, W-SUBMIT, W-DOCS, W-LEARN (front-load, largest), W-BACKFILL.
Wave B: B-ENGINE (single engine owner) -> B-HEALTH -> B-STATS -> W-TESTHARDEN. Audit after each wave. Engine
refactor (H1), MiroFish, deep stats research = LAST/separate.
