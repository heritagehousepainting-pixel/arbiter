# Idea-source coverage — activate the whole council

**Date:** 2026-06-24
**Status:** built (plan → build → audit in one pass)

## Problem

Only **two** of the council's idea sources produce ideas regularly:

| Source | Advisor | Status before |
|--------|---------|---------------|
| congress | A1.congress | healthy (~10 filings/7d) |
| mirofish | A2.mirofish | healthy (~750 opinions/7d) |
| **form4** | **A1.insider** | **starved** — only 3 filings/7d |
| **form13d** | **A1.activist** | **dead** — last filing Nov-2024 |
| form13f | A1.fund | OK but quarterly (Q2 not filable until ~Aug) |
| **news** | **A3.news** | **narrow** — only 10 tickers scanned |

Root causes (all the same shape — a too-narrow ticker universe):

1. **`runner.py::_DEFAULT_WATCHLIST` was 10 mega-caps** (`AAPL, MSFT, GOOGL,
   AMZN, NVDA, META, TSLA, BRK.B, JPM, UNH`). This list drives **three**
   consumers: form4 ingest, form13d ingest, and the A3 news pipeline
   (`engine/_engine.py` imports it). Ten names = ten companies' worth of
   insider trades and news.

2. **form13d searches the *subject* company.** `search_sc13_filings(ticker)`
   asks "who filed a 13D **against** `ticker`?" Activists never take a 5%+
   stake in a trillion-dollar mega-cap, so searching only the 10 mega-caps
   returns nothing — hence the channel has been dead since Nov-2024.

3. **form13f** is genuinely fine — it is quarterly by nature. No change.

## Fix

### F1 — Broaden the shared watchlist (fixes insiders + activists-by-subject + news)

`_DEFAULT_WATCHLIST` is now derived from `data.sectors.covered_tickers()` — the
**136-ticker, 11-sector** universe already maintained for the per-sector risk
cap. Benefits:

- form4 now scans 136 issuers for insider trades (13.6× coverage).
- form13d subject-search now has real mid/large-cap targets that activists
  actually file against.
- A3 news now scans 136 tickers.
- **Invariant preserved by construction:** the existing test
  `test_watchlist_is_subset_of_covered_tickers` required the watchlist ⊆ sector
  map. Deriving it *from* the sector map makes the relationship an equality, so
  it can never drift again.

### F2 — Track named activists directly (fixes activists-by-filer)

Subject-search alone still misses activists targeting names outside the
watchlist. So we ALSO follow known activists by their **own filer CIK** — the
same model `FUND_MANAGERS` uses for 13F. New roster
`data/activist_filers.py::ACTIVIST_FILERS` (all CIKs verified live against
EDGAR 2026-06-24):

| Activist | Filer CIK | recent 13D | latest |
|----------|-----------|-----------|--------|
| Carl Icahn | 0000921669 | 463 | 2026-06-09 |
| Starboard Value LP | 0001517137 | 457 | 2026-06-02 |
| Trian Fund Management | 0001345471 | 144 | 2026-06-18 |
| Elliott Investment Management | 0001791786 | 56 | 2026-04-03 |
| JANA Partners Management | 0001998597 | 28 | 2026-06-10 |
| ValueAct Capital | 0001464912 | 3 | 2025-05-06 |

New EDGAR method `search_sc13_by_filer(cik)` mirrors `search_form13f_filings`:
it reads the activist's *own* submissions JSON and returns their recent
13D/13G filings.

**Subject resolution (safety-first, reuses tested infra):** a filer-discovered
13D does not name a ticker up front. The parser already extracts the CUSIP; it
now also surfaces the subject issuer name (`subject_name`, additive). The
ingest resolves the ticker with the **same** `resolve_cusip(cusip,
issuer_name, asset_lookup)` path used by 13F (cache → seed → Alpaca
issuer-name match). Anything that does not resolve with ≥0.9 confidence is
**dropped** — never traded on a guess, consistent with the 13F policy.

`_ingest_sc13_by_filer` is added to the existing `form13d` source, so the
activist channel is now fed by the **union** of subject-search and
filer-search, deduped on `(accession, txn_idx)` by the writer.

## Files

- `arbiter/data/activist_filers.py` — roster (new)
- `arbiter/ingest/edgar/client.py` — `search_sc13_by_filer`, `get_ticker_for_cik`
- `arbiter/ingest/edgar/sc13_parser.py` — surface `subject_name` (additive)
- `arbiter/ingest/runner.py` — watchlist from sectors; `_ingest_sc13_by_filer`
- tests: `tests/data/test_activist_filers.py`,
  `tests/ingest/test_runner_activist.py`, plus edits to `tests/data/test_sectors.py`

## Tradeoffs / follow-ups

- A full cycle now does ~136 EDGAR submission fetches + per-filing docs and ~136
  news gathers. Full cycles run only a few times/day (daemon `_run_full_cycle`),
  so this is within EDGAR's polite-rate budget, but news-gather cost scales with
  the universe — a future triage (only gather news for tickers with a fresh
  catalyst) is the natural next optimization.
- Activist roster is hand-maintained like `FUND_MANAGERS`; extend by adding one
  verified `(name, entity, cik)` row.
