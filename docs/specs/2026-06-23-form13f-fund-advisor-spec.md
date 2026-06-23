# Spec — Form 13F Fund-Manager Advisor (`A1.fund`)

- **Date:** 2026-06-23
- **Status:** Approved design, ready for implementation plan
- **Owner:** arbiter
- **Related:** `A1.activist` (13D/13G) is the structural template; `A3.news` is the
  precedent for probationary advisors that expand the traded universe.
- **Out of scope (separate future spec):** the per-person *news scrub* (X/Reddit/Google
  intent signals). This spec is **only** the 13F feed.

---

## 1. Purpose & goal

Expand **who** arbiter follows from disclosed insiders/activists/politicians to famous
**fund managers** (Cathie Wood / ARK, Michael Burry / Scion, Leopold Aschenbrenner /
Situational Awareness, Buffett, Ackman, Tepper, Einhorn, Druckenmiller, Klarman,
Coleman, Loeb). The mechanism is SEC **Form 13F-HR** — the quarterly institutional
holdings disclosure — which is free from EDGAR and slots into the existing
disclosure→signal→advisor pipeline.

Primary objective is **diversity of tracked smart money** and surfacing *what these
managers are moving into*, governed by the learning loop. 13F alpha is inherently weak
(see §2), so this is a probationary, learning-suppressed advisor — not a high-conviction
source.

## 2. The hard truth about 13F (drives every decision below)

A 13F-HR is a **snapshot of long US-equity/options holdings as of quarter-end, disclosed
up to 45 days later.** Consequences baked into the design:

1. **It is stale by construction** (45–135 days old when public) → the advisor's
   conviction is **hard-capped at 0.7**, never max; horizon is **LONG (180d)**.
2. **The signal is the *delta*, not the snapshot** — we diff consecutive quarters and act
   on *changes* (new/exit/add/trim), never on static inertia holdings.
3. **PIT/no-lookahead:** the signal's `as_of` is the **filing date** (when it became
   public), **never** the quarter-end (when the position existed). Under `BacktestClock`
   the ingest returns nothing, like every other live source.
4. **Longs only:** 13F discloses long positions and options; pure shorts are invisible.
   Even Burry's filing shows only his longs. We act only on outright **share** holdings
   (puts/calls are stored but not traded).

## 3. Decisions locked in brainstorming

| # | Decision | Choice |
|---|----------|--------|
| Q1 | Manager roster | Curated 11-manager starter set (§5), incl. Aschenbrenner; trivially extensible |
| Q2 | Signal interpretation | **(A) Delta-based** (quarter-over-quarter changes), not static snapshot |
| Q3 | Universe | **Expand** — 13F may surface/trade off-watchlist tickers, gated by existing liquidity/ADV/sector/position caps |
| §2-1 | First filing for a manager | **(b) Top-5 conviction snapshot** (emit "new position" for the 5 most-concentrated holdings), pure-delta thereafter |
| §2-2 | Noise floors | Skip holdings `< $10M` **or** `< 0.5%` of the manager's book; ignore share changes within `±25%` |
| §3 | Stance/conviction | Direction from delta; magnitude from event-cleanliness × concentration; conviction capped 0.7; exits/trims drive **A1 shorts** |
| §4 | Cadence | Folded into the existing daily `arbiter ingest` runner (no separate cron) |
| §4 | Weighting | Probationary **EQUAL_FLOOR**, learning-governed (like A3/A1 bootstrap) |

## 4. Architecture & data flow

```
SEC EDGAR 13F-HR XML  (free, existing EdgarClient, per tracked manager CIK)
        │  daily poll via arbiter ingest --sources form13f
        ▼
ingest/edgar/form13f_parser.py     parse information-table XML
        │                          (cusip, issuer, value_usd, shares, put_call)
        ▼
ingest/edgar/form13f_normalize.py
        │  1. CUSIP→ticker resolve (drop unresolved/foreign/options/bonds)  §6
        │  2. write raw snapshot   → form13f_holdings  (migration 027)
        │  3. DELTA vs manager's prior quarter (or top-5 on first filing)   §2
        │       new / +≥25% add  → txn_type "P" (bullish)
        │       exit / −≥25% trim → txn_type "S" (bearish)
        ▼
filings table  (source="form13f", as_of = FILING date)   ← PIT-safe, reuses existing schema
        ▼
detect_signals → run_cycle spawns 1 idea per (ticker,"form13f"), horizon 180d LONG
        ▼
engine/advisors.py::_build_a1_fund_fn → Opinion (stance/conviction §7)
        ▼
council fuse → sizing/gates/risk caps → paper execution → outcome → learning loop
```

**Why this is mostly free plumbing:** the engine spawns ideas from the **`filings`
table**, not the watchlist (Congress/Form-4 already trade off-watchlist names). Writing
each delta as a `form13f` filing row means idea-spawning, universe expansion, opinion
attribution, learning, and the cockpit graph all work with minimal new code. `source`
is free TEXT in `filings` (no enum migration needed — same as when `form13d` was added).

## 5. Manager roster (`data/fund_managers.py`)

Static seed: `[(canonical_name, fund_name, cik)]`. On ingest each is upserted into
`people` with `source="form13f"` → auto-renders as a gold figure node in the cockpit.
Starter set (CIKs to be resolved during implementation via EDGAR
`company_tickers`/full-text CIK lookup and **verified** before commit):

Cathie Wood / ARK Investment Management · Michael Burry / Scion Asset Management ·
Warren Buffett / Berkshire Hathaway · Bill Ackman / Pershing Square Capital ·
David Tepper / Appaloosa Management · David Einhorn / Greenlight Capital ·
Stanley Druckenmiller / Duquesne Family Office · Seth Klarman / Baupost Group ·
Chase Coleman / Tiger Global Management · Daniel Loeb / Third Point ·
**Leopold Aschenbrenner / Situational Awareness LP**.

Notes:
- Aschenbrenner's fund is new (2024). If it has not filed a 13F yet, he renders as a
  tracked figure with "building…" and the feed auto-picks up his first filing when it
  exists — zero code change.
- Berkshire already appears via `form13d`; the `form13f` path is a *separate* advisor
  channel and may co-exist (different `source`, different advisor attribution).
- Extending the roster later = one line in the seed.

## 6. CUSIP→ticker resolution (the one hard part; safety-first)

13F reports **CUSIP + issuer name**, not ticker, and there is no clean free CUSIP→ticker
map (CUSIP is licensed). Bias: **safety (never trade the wrong ticker) over coverage.**

- A cached `cusip_map` table (migration 028): `cusip → ticker, issuer_name, source,
  confidence, resolved_at`.
- Resolution order:
  1. Exact hit in `cusip_map` (cache).
  2. Hand-seed for obvious megacaps (small static dict shipped with the resolver).
  3. **Exact issuer-name match** against Alpaca's tradeable **US-equity** asset list
     (`/v2/assets`, `class=us_equity, status=active, tradable=true`).
- **Drop** (log, never guess) anything not resolved with high confidence: foreign
  issuers, OTC, options/puts/calls, bonds, ambiguous names. We knowingly trade only the
  cleanly-resolvable subset — an accepted scope cut, not a bug.
- Every confident resolution is cached so the map grows over time.
- **Sector:** new tickers get a best-effort GICS-ish sector from the SEC submissions
  **SIC code** (already fetched in the Form-4 path); anything still unknown falls into a
  conservative `UNKNOWN` bucket that the existing sector cap still bounds. (Verify the
  off-watchlist sector path during implementation — Congress/Form-4 already exercise it.)

## 7. Stance / conviction model (`_build_a1_fund_fn`)

Mirrors `_build_a1_activist_fn` (filters `signals` to `source=="form13f"`), emitting one
`Opinion` per delta. All constants are **config-tunable defaults**, not magic numbers.

- **Direction:** new/add → bullish (positive stance); exit/trim → bearish (negative
  stance). This gives the **A1 family its first disclosure-driven short signals.**
- **`stance_score`** = sign × event-strength: clean new-position / full-exit = **±0.6**;
  add/trim = **±0.3–0.5** scaled by `min(|Δshares%| , cap)`.
- **`confidence`** = concentration-driven (position value ÷ manager's total book):
  ≥10% of book → ~0.8; <1% → ~0.3.
- **Conviction hard-capped at 0.7** (13F is stale; must not out-shout a fresh A2/A3 or a
  Form-4).
- **Horizon 180d (LONG)** — matches the spawned idea's `(ticker, LONG)` typed key so the
  opinion never orphans (the A3 attribution-bug class).
- **Weight:** probationary **EQUAL_FLOOR**; learning loop down-weights fast if it loses.

## 8. Config keys (`config.py`, env-overridable)

| key | default | meaning |
|-----|---------|---------|
| `FORM13F_MANAGER_CIKS` | (seed) | optional override/extension of the roster |
| `FORM13F_MIN_POSITION_USD` | `10_000_000` | drop holdings below this $ value |
| `FORM13F_MIN_BOOK_FRACTION` | `0.005` | drop holdings below this fraction of the book |
| `FORM13F_MIN_DELTA_FRACTION` | `0.25` | ignore share changes within ±this |
| `FORM13F_FIRST_FILING_TOP_K` | `5` | top-K conviction snapshot on first filing |
| `FORM13F_MAX_CONVICTION` | `0.7` | conviction cap |

INERT without a roster/`EDGAR_USER_AGENT`; `[]` under `BacktestClock`.

## 9. Cockpit

- Managers appear as figures automatically (graph reads `people`).
- Add an **`A1.fund`** advisor node to the council cluster (same one-line un-dim
  treatment used for `A3.news`).
- Figure inspection panel: surface each manager's recent 13F deltas (ticker, P/S,
  Δshares, % of book, filing date).
- Read-only invariant unchanged.

## 10. Testing (offline, hermetic) & guardrails

- **Parser:** real 13F-HR information-table XML fixture → expected rows; malformed/empty
  XML never raises (matches the SSRF/hostile-input hardening of the other EDGAR parsers).
- **Delta engine:** new / exit / +add / −trim / flat-nibble / below-floor cases;
  first-filing top-5; both noise floors.
- **CUSIP resolver:** cache hit, seed hit, exact-name hit, **drop** on
  foreign/ambiguous/option.
- **PIT:** `as_of` == filing date (not report_date); `BacktestClock` → `[]` (no
  lookahead — must pass `scripts/check_no_lookahead.sh`).
- **Stance:** sign/magnitude/conviction-cap per §7.
- **Engine wiring:** the `A1.fund` opinion **links to its spawned idea**
  (`op.idea_id == idea.idea_id`), no orphan (explicit regression, the A3 bug class).
- **Idempotency:** re-ingesting the same filing writes no duplicate holdings/signals
  (`UNIQUE(person_id, accession, cusip, put_call)`); must pass
  `scripts/check_insert_only.sh`.
- **Guardrails:** bounded by existing `MAX_OPEN_POSITIONS`/`MAX_GROSS_PCT`/sector cap/ADV
  liquidity; probationary weight; longs-and-shorts both managed (short support already
  shipped); no lookahead; never trades an unresolved/low-confidence CUSIP.
- **Suite:** full `pytest tests/ -q` + both linters stay green; cockpit web/api tests
  unaffected.

## 11. Migrations

- `027_form13f_holdings.sql` — raw snapshots, `UNIQUE(person_id, accession, cusip, put_call)`.
- `028_cusip_map.sql` — cached CUSIP→ticker resolutions.
- No change to `filings`/`opinions`/`outcomes` (reuse free-TEXT `source`).

## 12. Risks & explicit non-goals

- **Weak signal** — 13F lag means modest/no alpha; acceptable because the goal is
  diversity + the learning loop suppresses losers. Not a high-conviction source.
- **Coverage gap** — unresolved CUSIPs are dropped; we trade only the clean subset.
- **Non-goal:** real-time intent (that's the future news-scrub spec).
- **Non-goal:** options/short replication from 13F (not disclosed).
- **Non-goal:** re-firing static holdings (only deltas fire).
