# A3 — Free News / Smart-Money Advisor (consolidated, audited spec)

**Date:** 2026-06-22  **Status:** audited from 3 planning lanes; awaiting sign-off → build.
Sources: `docs/specs/research/2026-06-22-a3-{free-sources,pipeline-design,wiring-design}.md`.

A3 is the third advisor family — public news + corporate events — that pushes against A1 (SEC
insiders / Congress / activists) and A2 (MiroFish/Claude). **100% free data**, **shadow-first**
(scored but not sizing until it earns trust), and **inert until configured** (one free API key).

## 0. Audit verdicts (what changed vs the lane drafts)
- **Reuse the existing `arbiter/arbiter/tips/` Lane 8** (`UnverifiedTip`+`fingerprint`, `TipSource`
  ABC, `DiversityGate`, `AccountScorer`, `unverified_tips` table) — VERIFIED to exist. A3 is its
  activation, not new architecture.
- **Stance-extraction conflict RESOLVED.** The pipeline lane said "VADER not Claude"; the research
  lane said "8-K has no sentiment → Claude." Resolution: **Finnhub provides per-article sentiment
  for free → use it as the primary directional signal; VADER (free, deterministic) is the fallback
  for items lacking a score; Claude is NOT used in v1** (A2 already owns the paid LLM work; keeps A3
  free + backtest-reproducible). 8-K filings are an **event/corroboration** source, not a sentiment
  source.
- **Horizon orphan-risk CONFIRMED** (`_engine.py:500`). A3 must be added to that branch with a
  SHORT-bucket horizon or its opinions orphan from their ideas and attribution silently breaks.
- **Verified free stack:** EDGAR 8-K (no key, reuses `EdgarClient`) + **Finnhub** company-news
  (free key, 60 req/min, includes sentiment). Everything else (X, Alpha Vantage, NewsAPI, Yahoo/
  Google/SeekingAlpha RSS, StockTwits, Reddit-for-v1, Tiingo, NewsData) ruled out for ToS/limit/
  delay reasons — see research doc §6.

## 1. Data sources (free)
- **EDGAR 8-K material events** — PRIMARY EVENT source. No key (uses existing `EDGAR_USER_AGENT`).
  New `EdgarClient.search_8k_filings(ticker, since)` + `parse_8k_items()` (Item 1.01 M&A, 2.02
  earnings, 5.02 officer change, 8.01 other). Reuses `_get`/`_rate_limit`/`_sanitize_cik`.
- **Finnhub company-news** — PRIMARY DIRECTIONAL source. Free key `FINNHUB_API_KEY`. New
  `FinnhubClient` (`httpx` + rate-limit + backoff, mirrors `EdgarClient`). `GET
  /api/v1/company-news?symbol=&from=&to=&token=`. Per-article publisher + sentiment.
  - **ToS note in code:** free tier is non-commercial/personal only (fine for a personal paper
    trader; a commercial deploy needs a paid Finnhub license).

## 2. Pipeline (new `arbiter/arbiter/adapters/a3/`, ~5 thin modules)
`NewsItem` (frozen): `title, body, url, published_at (tz-aware UTC, from the SOURCE), source_id,
publisher, raw`. → maps to `UnverifiedTip` (Lane 8) with `fingerprint = sha256(source_id|publisher|url)`.

Flow per cycle (market-wide sweep, returns **at most one** `Opinion`, like A1):
1. **Fetch** from each configured source over the watchlist (rate-limited, backoff, fail-closed → []).
2. **Ticker resolution:** cashtag → company-name alias → watchlist filter; off-watchlist abstains;
   cap 5 tickers/item.
3. **Stance:** Finnhub `bullishPercent − bearishPercent` → `stance_score` (±0.05 neutral dead-zone);
   VADER fallback on title when no score; 8-K item-type → coarse direction for corroboration only.
4. **Diversity gate (Lane 8):** emit ONLY when ≥2 **independent publishers/sources** corroborate the
   same ticker+direction (kills single-headline / PR-wire noise). An 8-K event co-occurring with a
   Finnhub item counts as strong corroboration.
5. **Confidence:** `0.4·source_tier + 0.4·corroboration + 0.2·recency`, clamp `[0.05, 0.85]`.
6. **Opinion:** `advisor_id="A3.news"`, `horizon_days=7` (**SHORT bucket**), `as_of =
   item.published_at` (NO wall-clock → passes `check_no_lookahead.sh`; backtest reads
   `unverified_tips WHERE ts<=as_of`, no network), `source_fingerprint`, `confidence_source=MODELED`.
   Pick the single best-corroborated lead this cycle.
7. **Fail-closed:** any error / no items / gate fail → `[]` (abstain), never crashes the cycle (mirrors A2).

## 3. Engine wiring (3 files change; learning loop unchanged)
- `engine/advisors.py`: new `_build_a3_news_fn(config)` → returns `None` when `FINNHUB_API_KEY`
  unset (inert, EDGAR-only can't satisfy the 2-source gate) — the EDGAR/MiroFish inert pattern.
- `engine/_engine.py`: (a) add A3 to `advisor_map` only when the builder is non-None; (b) **add the
  A3 source tag to the horizon branch at line ~500** → `horizon = 7` (SHORT) for A3 signals so the
  `(ticker, HorizonBucket)` link in `_persist_cycle_opinions` matches and attribution is NOT orphaned.
- `cockpit/api/graph.py`: drop `future=True` on the `A3.news` node so it un-dims; `state.py`
  `_advisor_intensities` already lights it from `trust_weights` once it has a row.
- **Shadow-first:** no `trust_weights` row → `EQUAL_FLOOR=0.25` probationary (in fusion, below the
  0.50 graduated ceiling). For STRICT shadow (scored, not sizing) seed one `trust_weights` row
  `shadow=True` — one line, no new machinery. Graduates automatically via the existing
  `TrustLedger`/significance gate once it has enough real outcomes.
- **Attribution/calibration:** generic — `resolve_advisor_outcomes` + `MultiAdvisorCalibrator`
  handle `A3.news` with no changes. (Verify a `stance_base`/advisor-registry entry isn't required;
  add if so.)

## 4. Config / activation (user action = ONE free key)
- `FINNHUB_API_KEY` (register free at finnhub.io, instant, no card). Until set, A3 is **fully
  inert** — not in `advisor_map`, emits nothing, cannot touch the live account. EDGAR needs no new
  key (`EDGAR_USER_AGENT` already set).
- Optional `A3_ENABLED` master flag (default off) for an explicit kill.

## 5. Testing & safety
- All offline (mock `httpx`/clients; fixtures for 8-K Atom + Finnhub JSON). NO network in tests.
  No-lookahead + insert-only linters stay clean. Full suite stays green.
- **Live-safe:** shadow-first + inert-until-keyed means building/merging A3 cannot affect the live
  T/UBER/AMZN positions. When the user adds the key, A3 starts emitting in shadow, the learning loop
  scores it, and it only ever sizes trades after it graduates on real measured skill.

## 6. Build order (thin tested slices)
1. `FinnhubClient` + fixtures + tests.  2. `EdgarClient.search_8k_filings`/`parse_8k_items` + tests.
3. `adapters/a3/` (NewsItem→UnverifiedTip→Opinion via Lane 8 diversity/scoring) + tests.
4. Engine wiring (`_build_a3_news_fn`, advisor_map, **horizon tag**) + tests. 5. Cockpit un-dim.
6. Full suite + both linters green; A3 verified inert without the key, and emitting (shadow) with a
   stubbed key in tests.
