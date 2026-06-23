# P2 Refinements — Design Spec (NON-engine.py lane)

**Date:** 2026-06-19
**Author:** planning agent (P2-refinements lane)
**Constraint:** This lane MUST NOT edit `arbiter/engine.py`. Anything needing an
engine edit is listed under *FOR WAVE 2* and is NOT designed here.
**Status:** design only — no code written by this agent.

---

## 0. Provenance / honesty note (read first)

My instructions and auto-memory referenced two source documents:

- `docs/audit/00-INDEX.md` (36-lane audit synthesis)
- `docs/specs/UPGRADES-PLAN.md` (built-vs-deferred plan, the canonical P2 list)

**Neither file exists in this checkout.** There is no `docs/` tree at all prior to
this spec (`find . -name 00-INDEX.md` and `find . -name UPGRADES-PLAN.md` both
return nothing; the only root docs are `ROADMAP.md`, `INTERFACES.md`,
`SETUP_NEEDED.md`). The canonical, numbered P2 list therefore could not be read
verbatim.

To avoid inventing a list, I reconstructed the deferred-P2 inventory from **primary
sources that ARE present**:

1. In-code references to audit lanes — source comments still cite
   `docs/audit/A2-risk-caps-gate.md`, `docs/audit/L1-roadmap-honesty.md`,
   `docs/audit/I2-statistical-power.md`.
2. The two explicitly-named P2s in my task brief: **wider sector map** and
   **notional-vs-realized fold**.
3. In-code "stopgap / eventual-upgrade / follow-up" markers
   (`arbiter/data/sectors.py` docstring; `SETUP_NEEDED.md` rows 2/3/5).

Where a "P2" cannot be pinned to a concrete, present artifact I mark it
**`[VAGUE → DEFER]`** and recommend re-deriving it once `docs/specs/UPGRADES-PLAN.md`
is restored, rather than guessing. Quality over quantity.

---

## 1. Deferred-P2 inventory

Each item tagged `[DOABLE-NOW]` (no engine.py edit), `[NEEDS-ENGINE → Wave 2]`,
`[NEEDS-USER]`, or `[VAGUE → DEFER]`.

| # | P2 item | Source | Tag | Owning file(s) |
|---|---------|--------|-----|----------------|
| P2-1 | **Widen the sector map** — `arbiter/data/sectors.py` covers ~62 tickers; the audit flagged sector cap collapsing to an `UNKNOWN` bucket for any traded/watchlisted ticker not in the table. | brief + `sectors.py` docstring (A2 lane) | **`[DOABLE-NOW]`** | `arbiter/data/sectors.py`, `tests/data/test_sectors.py` |
| P2-2 | **Watchlist↔sector-map sync invariant** — the table must be a superset of the ingest watchlist (`runner.py::_DEFAULT_WATCHLIST`) so no watchlisted name is ever `UNKNOWN`. Currently enforced only by one ad-hoc test; no machine-checked invariant. | `sectors.py` docstring ("keep in sync with watchlist"), `runner.py:41` | **`[DOABLE-NOW]`** | `tests/data/test_sectors.py` (+ optionally `arbiter/data/sectors.py` helper) |
| P2-3 | **Notional-vs-realized fold** — `engine.py:1393` folds the *requested* `order.qty` (notional dollars) into the `RiskBook` on every confirmed submit, even on a **partial fill** where the realized notional is smaller. Book over-counts headroom consumed. | brief + `engine.py:1386-1393`, `submit.py` SubmitResult | **`[NEEDS-ENGINE → Wave 2]`** | `arbiter/engine.py` + `arbiter/execution/submit.py` (see §4) |
| P2-4 | **Provider-backed sector source** — replace the hand-maintained table with a GICS/SIC provider feed (SEC company-facts SIC, or vendor). | `sectors.py` docstring "Eventual upgrade" | **`[NEEDS-USER]`** (data-source decision) / partially `[VAGUE → DEFER]` | n/a — needs a provider decision |
| P2-5 | **Sector-map breadth as a recurring chore** — keep the table current as the universe drifts. Ongoing maintenance, not a discrete change. | memory ("sector-map breadth") | **`[VAGUE → DEFER]`** (subsumed by P2-1) | n/a |
| P2-6 | Residual minor P2 refinements named only in `UPGRADES-PLAN.md` (e.g. I2 statistical-power refinements, L1 roadmap-honesty wording). | `backfill.py:6`, `L1` ref | **`[VAGUE → DEFER]`** | n/a — re-derive when plan restored |

**Net: two `[DOABLE-NOW]` items (P2-1, P2-2). Everything else is engine-bound,
user-bound, or too vague to implement safely without the restored plan.**

---

## 2. Why P2-3 (notional-vs-realized fold) is NOT doable in this lane

The fold lives at **`arbiter/engine.py:1393`**:

```python
if sub_result.order_id is not None:
    _book[0] = _book[0].add(order.ticker, float(order.qty))
```

`order.qty` is the *requested* notional USD. On a `partial` fill the realized
notional is `avg_fill_price × filled_qty`, which is smaller. Folding the full
requested notional makes the book *over*-count gross/sector exposure — a
**conservative** error (it tightens caps, never loosens them), which is why it was
correctly deprioritized to P2 rather than P0.

Fixing it correctly requires BOTH:

1. **`arbiter/execution/submit.py`** — `SubmitResult` currently exposes
   `avg_fill_price` but **not** `filled_qty`. To fold realized notional you must
   surface the filled quantity (or a precomputed `filled_notional`) on
   `SubmitResult` (the broker `ExecutionReport.filled_qty` is already available at
   `submit.py:350`, used for the `partial` ledger qty).
2. **`arbiter/engine.py:1393`** — change the fold to use realized notional on
   `partial`, requested notional otherwise.

Step 2 is an `engine.py` edit, which this lane is forbidden to make. Therefore the
whole item is handed to Wave 2 (see §4). I am NOT designing the engine change here
beyond identifying the seam; designing it would tempt a same-PR engine edit.

---

## 3. Design for the DOABLE-NOW P2s

### P2-1 — Widen the sector map (`arbiter/data/sectors.py`)

**Goal:** ensure no ticker that the system actually touches falls to `UNKNOWN`
unnecessarily, and that every value stays a valid GICS top-level label. Keep the
existing pure/deterministic API (`sector_for`, `sector_map`, `GICS_SECTORS`,
`UNKNOWN`) byte-for-byte unchanged — this is an **additive table edit only**.

**Source of truth for classifications:** GICS top-level sector per issuer. Because
P2-4 (provider feed) is deferred, classifications are sourced manually from the
issuer's primary business per the public GICS taxonomy. Each added row gets a short
trailing comment naming the company so a reviewer can verify without a lookup
(matching the existing `# Fabrinet` / `# Eaton Corp` style). Do **not** invent
tickers — only add names that are (a) on a current/plausible watchlist, (b) common
large-caps likely to trade, or (c) S&P sector leaders that round out thin buckets.

**Tickers to add — by GICS bucket** (none currently in the table; verified absent
via the existing `_SECTOR_BY_TICKER` dump). This deliberately fills the three thin
buckets first (Materials, Real Estate, Utilities — currently EMPTY, so any such
ticker collapses to `UNKNOWN`) and rounds out the rest:

- **Materials** *(currently 0 entries — highest priority; UNKNOWN-collapse risk)*:
  `LIN` (Linde), `SHW` (Sherwin-Williams), `APD` (Air Products), `FCX`
  (Freeport-McMoRan), `NEM` (Newmont), `ECL` (Ecolab), `DOW` (Dow Inc).
- **Real Estate** *(currently 0 entries)*: `PLD` (Prologis), `AMT` (American
  Tower), `EQIX` (Equinix), `SPG` (Simon Property), `O` (Realty Income), `CCI`
  (Crown Castle).
- **Utilities** *(currently 0 entries)*: `NEE` (NextEra), `DUK` (Duke), `SO`
  (Southern Co), `D` (Dominion), `AEP` (American Electric Power), `EXC` (Exelon).
- **Information Technology** (round-out): `ACN` (Accenture), `IBM`, `NOW`
  (ServiceNow), `INTU` (Intuit), `LRCX` (Lam Research), `KLAC` (KLA), `ADI`
  (Analog Devices), `PANW` (Palo Alto Networks), `SNPS` (Synopsys), `CDNS`
  (Cadence).
- **Communication Services** (round-out): `CMCSA` (Comcast), `CHTR` (Charter).
- **Consumer Discretionary** (round-out): `LOW` (Lowe's), `BKNG` (Booking),
  `TJX`, `LULU`, `GM`, `F` (Ford), `ABNB`.
- **Industrials** (round-out): `UNP` (Union Pacific), `LMT` (Lockheed), `NOC`
  (Northrop), `GD` (General Dynamics), `MMM` (3M), `EMR` (Emerson), `CSX`, `FDX`.
- **Financials** (round-out): `C` (Citigroup), `SCHW` (Schwab), `BLK`
  (BlackRock), `SPGI` (S&P Global), `CB` (Chubb), `PGR` (Progressive), `PYPL`
  (PayPal — GICS classifies PayPal under Financials).
- **Health Care** (round-out): `ABT` (Abbott), `DHR` (Danaher), `BMY`
  (Bristol-Myers), `AMGN`, `GILD`, `CVS`, `ISRG` (Intuitive Surgical), `MDT`
  (Medtronic), `VRTX`.
- **Energy** (round-out): `SLB` (Schlumberger), `EOG`, `MPC` (Marathon
  Petroleum), `PSX` (Phillips 66), `WMB` (Williams), `OXY` (Occidental).
- **Consumer Staples** (round-out): `MO` (Altria), `PM` (Philip Morris), `MDLZ`
  (Mondelez), `CL` (Colgate), `TGT` (Target), `KMB` (Kimberly-Clark).

**GICS-bucket-consistency guardrails (the UNKNOWN-bucket fix):**

- Every new value MUST be one of the existing 11 `GICS_SECTORS` strings —
  verbatim, case-sensitive ("Health Care" not "Healthcare", "Information
  Technology" not "Tech"). A test (§4) asserts every table value ∈ `GICS_SECTORS`,
  so a typo'd label fails CI rather than silently becoming a phantom 12th bucket
  that would *split* the sector cap.
- Classify by GICS, not by colloquial sector. Notable traps to get right:
  `AMZN`/`TSLA` = Consumer Discretionary (already correct), `GOOGL`/`META`/`NFLX` =
  Communication Services (already correct), `V`/`MA`/`PYPL` = Financials,
  `AMT`/`PLD`/`EQIX` = Real Estate (NOT Utilities/IT), `LIN`/`SHW` = Materials.
- No change to defaulting behavior: a genuinely-unknown ticker still returns
  `UNKNOWN`. The fix is *coverage*, not *abolishing* the fallback — `UNKNOWN` must
  remain a valid, conservative fail-soft bucket for anything off the table.

**Explicitly OUT of scope for this edit:** changing `sector_for`/`sector_map`
signatures, adding I/O, or wiring a provider (that is P2-4, deferred). Engine
already calls `sector_for` (engine.py:739/742) — widening the *table* needs **no**
engine edit because the function contract is unchanged.

### P2-2 — Watchlist↔sector-map sync invariant (`tests/data/test_sectors.py`)

**Problem:** the docstring instructs maintainers to keep the table a superset of
`runner.py::_DEFAULT_WATCHLIST`, but only a hard-coded copy of the 10-name
watchlist is checked. If someone edits the watchlist in `runner.py`, the test does
NOT catch a newly-uncovered name.

**Fix (test-only, no engine):** replace the hard-coded list in
`test_default_watchlist_fully_mapped` with an **import of the real watchlist** and
assert full coverage:

```python
from arbiter.ingest.runner import _DEFAULT_WATCHLIST

def test_default_watchlist_fully_mapped():
    mapped = sector_map(_DEFAULT_WATCHLIST)
    unknown = [t for t, s in mapped.items() if s == UNKNOWN]
    assert not unknown, f"watchlist tickers missing from sector map: {unknown}"
```

This turns the prose invariant into a machine-checked one with zero production-code
change. (Optional, still non-engine: expose a tiny `covered_tickers()` accessor in
`sectors.py` returning `frozenset(_SECTOR_BY_TICKER)` so the test can assert the
superset relation without reaching into a private name — recommended but not
required; the import-based test above is sufficient.)

---

## 4. Test plan (OFFLINE only)

All tests are pure/deterministic — no network, no clock, no DB. They extend the
existing `tests/data/test_sectors.py`.

**File: `tests/data/test_sectors.py`** (extend; do not rewrite existing cases)

| Test | Asserts |
|------|---------|
| `test_every_table_value_is_valid_gics` *(new)* | Iterate **all** values of `_SECTOR_BY_TICKER` (import the private dict) and assert each ∈ `GICS_SECTORS`. Catches a mistyped/phantom sector label on any added row — the core UNKNOWN-split guard. |
| `test_three_thin_buckets_now_populated` *(new)* | Assert ≥1 ticker resolves to each of `"Materials"`, `"Real Estate"`, `"Utilities"` (e.g. `sector_for("LIN")=="Materials"`, `sector_for("PLD")=="Real Estate"`, `sector_for("NEE")=="Utilities"`). Proves the previously-empty buckets exist so those sectors no longer collapse to UNKNOWN. |
| `test_known_tickers_map_to_expected_sector` *(extend params)* | Add representative new rows incl. the GICS traps: `("PYPL","Financials")`, `("AMT","Real Estate")`, `("LIN","Materials")`, `("NEE","Utilities")`, `("CMCSA","Communication Services")`, `("LOW","Consumer Discretionary")`, `("ABT","Health Care")`, `("SLB","Energy")`, `("MO","Consumer Staples")`, `("UNP","Industrials")`, `("ACN","Information Technology")`. |
| `test_default_watchlist_fully_mapped` *(rewrite to import)* | Import `_DEFAULT_WATCHLIST` from `arbiter.ingest.runner`; assert no watchlist ticker maps to `UNKNOWN`. Locks the sync invariant (P2-2). |
| `test_no_duplicate_or_empty_keys` *(new)* | Assert every key in `_SECTOR_BY_TICKER` is non-empty and already upper-cased (`k == k.strip().upper()`), so `sector_for` (which upper-cases input) can never miss a row. |
| `test_all_added_tickers_resolve_non_unknown` *(new, lightweight)* | For a curated list of the names this spec adds, assert `sector_for(t) != UNKNOWN`. Guards against a row being lost in a future merge. |

Existing cases (`test_unknown_ticker_is_unknown`,
`test_lookup_is_case_and_whitespace_insensitive`, `test_sector_map_round_trips`,
`test_sector_map_empty_input`, `test_sector_map_preserves_input_key_casing`) stay
unchanged — they pin the fail-soft + purity contract that the table widening must
not break.

**Run:** `pytest tests/data/test_sectors.py -q` (and the policy book/decision suites
as a regression sanity check, since they consume `sector_for` —
`pytest tests/policy/test_book.py tests/policy/test_decision.py -q`). No new test
file is needed.

---

## 5. Ownership map

**This lane will touch (NON-engine.py only):**

- `arbiter/data/sectors.py` — additive rows in `_SECTOR_BY_TICKER`; optional
  `covered_tickers()` accessor. No API/signature change.
- `tests/data/test_sectors.py` — new + extended offline cases (§4).

**Explicitly NOT touched by this lane:** `arbiter/engine.py` (forbidden),
`arbiter/policy/book.py`, `arbiter/policy/decision.py`, `arbiter/ingest/runner.py`
production code (P2-2 only *imports* its watchlist from tests).

**FOR WAVE 2 (engine owner) — do NOT design here:**

- **P2-3 notional-vs-realized fold.** Two-file change:
  `arbiter/execution/submit.py` (add `filled_qty`/`filled_notional` to
  `SubmitResult`, populated from the broker `ExecutionReport.filled_qty` already in
  scope at `submit.py:350`) **and** `arbiter/engine.py:1393` (fold realized
  notional on `status == "partial"`, requested notional otherwise). Conservative
  bug — over-counts headroom, never loosens a cap — hence P2, not urgent.

**NEEDS-USER / DEFER:**

- **P2-4 provider-backed sector feed** — needs a data-source decision (SEC
  company-facts SIC vs vendor). Re-derive scope when chosen.
- **P2-5 / P2-6 vague residuals** — re-derive from `docs/specs/UPGRADES-PLAN.md`
  once that file is restored to the repo; do not guess them into existence.

---

## 6. Recommendation

Ship **P2-1 + P2-2 together** as one small, fully-offline, additive PR touching
exactly two files (`arbiter/data/sectors.py`, `tests/data/test_sectors.py`). It
removes the most concrete UNKNOWN-bucket risk (three empty GICS buckets) and
converts the watchlist-sync prose into a CI-enforced invariant, with zero
production-logic or engine change. Defer P2-3 to the engine owner and P2-4/5/6 until
the canonical upgrade plan is restored.
