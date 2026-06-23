# Simfin Fundamentals — PIT Feasibility for MiroFish A2

**Date:** 2026-06-21
**Author:** research wave (read-only)
**For:** `mirofish/evidence/fundamentals.py` + `mirofish/clients/simfin.py` (design spec §3.2, §9.1)
**Question:** Is Simfin's FREE tier viable for point-in-time-correct (PIT) fundamentals, and exactly how do we use it?

---

## VERDICT

**(c) — Simfin's free tier is INADEQUATE as the primary source. Recommend SEC `companyfacts` XBRL JSON (data.sec.gov) as the fundamentals source instead.**

Simfin's free tier *does* expose a real publish date (so PIT is technically possible), but two free-tier limitations kill it for our use case:
1. **The DERIVED ratios dataset (P/E, P/S, margins, etc.) is paywalled** — BASIC plan ($35/mo) and up only. On free we'd have to compute everything from raw line items anyway.
2. **Free data is only 5 years deep and the bulk download is delayed**, and the free API is a 2 calls/sec, credit-metered, bulk-oriented mechanism — a poor fit for a per-ticker, on-demand `/analyze` call.

Since the arbiter **already has EDGAR access and an `EDGAR_USER_AGENT`** (per project memory; Form-4 work needs it too), SEC `companyfacts` gives us the *same* fundamentals **with a true filing date (`filed`)**, **no API key**, **no paywall**, **full history**, and **10 req/s** — strictly better for PIT and for our access pattern. Keep Simfin only as an optional secondary if the user later supplies a `SIMFIN_API_KEY` on a paid plan.

---

## 1. Auth / signup (Simfin)
- Free API key still exists in 2026: register at simfin.com / app.simfin.com, then the key is shown under the data/API access page. Env var per spec: `SIMFIN_API_KEY`.
- Two API generations exist: legacy **v1** (`https://simfin.com/api/v1/...`, SimFin-ID based) and current **v3** (`https://simfin.com/api/v3/companies/...`). The Python `simfin` package wraps the **bulk download**, not per-ticker REST.

## 2. Coverage (free tier)
- ~5,000 US stocks; 80+ indicators.
- **History: only 5 years on free** (paid: 10y START / 15y BASIC / 20y+ PRO).
- Update latency: new statements added ~24–48h after a QA pass; **free bulk downloads are additionally delayed** vs paid.

## 3. Fields
Raw statement line items ARE present in the free bulk fundamentals datasets (income / balance / cashflow, in annual / quarterly / TTM variants). From the `simfin` package income dataset the columns include: `Ticker`, `Report Date`, `Fiscal Year`, `Fiscal Period`, **`Publish Date`**, `Revenue`, `Cost of Revenue`, `Gross Profit`, `Operating Income`, etc. So we *can* derive:
- **Revenue, revenue-growth YoY** — directly (`Revenue`, two periods).
- **Gross margin** — `(Revenue − Cost of Revenue) / Revenue` or `Gross Profit / Revenue`.
- **Operating margin** — `Operating Income / Revenue`.
- **P/E, P/S** — NOT free. The precomputed **DERIVED** dataset (ratios, EV, FCF, margins) is **BASIC+ only**. On free we'd compute P/E and P/S ourselves as `price × shares / earnings` and `price × shares / revenue`, which needs shares-outstanding + a price series (we already have Alpaca prices in the service).

Per-ticker vs bulk: the free path is fundamentally a **bulk dataset download cached to disk** (the `simfin` package's model), not a clean single-ticker REST call. v3 REST (`companies/statements?ticker=...`) is the per-ticker path but is credit/plan gated.

## 4. PIT — the critical question
- **Simfin DOES expose a publish date.** The field is literally **`Publish Date`** in the package datasets (and `publishDate` in v3 JSON), and it is **distinct from `Report Date`** (which in Simfin = fiscal-period end). So `Publish Date <= as_of` is a valid PIT filter — Simfin is *not* one of the fiscal-period-end-only sources. This part is fine.
- Caveat: Simfin restates aggregated data after its QA pass, and `Publish Date` reflects when Simfin published, which can trail the actual SEC filing by the 24–48h (or longer, on free) ingestion lag — slightly conservative, not leaky.

## 5. Rate limits / quotas (free)
- **2 calls/sec.** Credit-metered ("500 credits" for high-speed access on free). **Filings: only 8/day** on free. 429 / rate-limit HTTP responses on overage → slow down + backoff. This bulk-oriented, low-QPS, credit-budget model is awkward for an on-demand per-ticker service.

## 6. Access mechanism
- Official `simfin` PyPI package = bulk download to local Pandas, designed for nightly/offline use, not low-latency per-ticker lookups.
- Raw v3 REST = per-ticker but plan-gated and key-bound.
- **Neither is a great fit** for "given one ticker + as_of, on demand, PIT." For our pattern, SEC companyfacts (below) is better.

---

## RECOMMENDED FALLBACK / PRIMARY: SEC `companyfacts` (data.sec.gov)

We already have EDGAR access + `EDGAR_USER_AGENT`. This is the better primary source:

- **Endpoint (per-ticker, on demand):**
  `GET https://data.sec.gov/api/xbrl/companyfacts/CIK{10-digit-zero-padded-CIK}.json`
  (resolve ticker→CIK once via `https://www.sec.gov/files/company_tickers.json`, cache it).
- **No API key.** Requires a descriptive `User-Agent` header (else 403) — we already set `EDGAR_USER_AGENT`. **10 req/s.**
- **PIT field = `filed`** (true SEC submission date) on every fact, alongside `end` (period end), `fy`, `fp`, `form`, `accn`, `frame`. Filter `filed <= as_of` → exact PIT, no leak. This is the gold-standard PIT signal (better than Simfin's `Publish Date`, which lags the filing).
- **Fields / XBRL tags we need (US-GAAP):**
  - Revenue: `RevenueFromContractWithCustomerExcludingAssessedTax` (fallback `Revenues` / `SalesRevenueNet`).
  - Gross margin: `GrossProfit` ÷ Revenue (or Revenue − `CostOfRevenue`/`CostOfGoodsAndServicesSold`).
  - Operating margin: `OperatingIncomeLoss` ÷ Revenue.
  - EPS / earnings: `NetIncomeLoss`; shares via `dei:EntityCommonStockSharesOutstanding` or `WeightedAverageNumberOfDilutedSharesOutstanding`.
  - P/E, P/S: compute with the Alpaca price the service already fetches × shares ÷ (earnings | revenue).
  - Revenue-growth YoY: pick the latest two same-`fp` facts with `filed <= as_of`.
- **History:** full (back many years), no 5-year cap.
- **Cost:** $0, no signup, no quota worries at our volume.

Implementation note: companyfacts JSON is large per company; cache per-CIK with a daily TTL (matches the service's once-per-ticker-per-day cache). The PIT filter is purely "drop any fact with `filed > as_of`, then take the most recent remaining per concept."

---

## Recommendation (planner-actionable)

1. **Make SEC `companyfacts` the fundamentals source for `mirofish/evidence/fundamentals.py`.** Build the client as `mirofish/clients/sec_facts.py` (ticker→CIK cache + per-CIK companyfacts fetch, `User-Agent` from `EDGAR_USER_AGENT`, `filed <= as_of` PIT filter). This removes the `SIMFIN_API_KEY` dependency from the critical path and gives strictly-better PIT.
2. **Keep `SIMFIN_API_KEY` optional/secondary.** If the user later supplies a paid (BASIC+) Simfin key, a `mirofish/clients/simfin.py` can supply the precomputed DERIVED ratios as a cross-check. On the free tier Simfin adds little over SEC and is worse for our access pattern.
3. **Setup the user must provide:** `EDGAR_USER_AGENT` (a descriptive UA string with contact, e.g. `"mirofish research heritagehousepainting@gmail.com"`) — *already on the project's needed-setup list for Form-4*. `SIMFIN_API_KEY` becomes **optional**, not required; if absent, fundamentals still work via SEC.
4. **Degradation unchanged:** if SEC lookup fails / ticker has no CIK or no XBRL facts ≤ as_of → `compute_fundamentals` returns `None` → service emits technical-only opinion (spec §2.6, §3.2). No change to the contract.
5. The spec's §9.1 "≥45-day reporting-lag fallback" is **not needed** with SEC's `filed` date — PIT is exact, not heuristic.

---

## Sources
- Simfin GitHub / package: https://github.com/SimFin/simfin , https://pypi.org/project/simfin/ , https://simfin.readthedocs.io/
- Simfin tutorial (columns incl. `Publish Date`): https://github.com/simfin/simfin-tutorials/blob/master/01_Basics.ipynb
- Simfin pricing / free-tier limits (2 calls/sec, 5y, derived = BASIC+, 8 filings/day): https://www.simfin.com/en/prices/
- Simfin v3 / bulk technical updates: https://www.simfin.com/en/technical-updates-to-api-v3-and-bulk-download/
- Simfin v1 REST tutorial: https://simfin-official.medium.com/simfin-api-tutorial-6626c6c1dbeb
- SEC EDGAR APIs (companyfacts, `filed` field, UA requirement, 10 req/s): https://www.sec.gov/search-filings/edgar-application-programming-interfaces , https://tldrfiling.com/blog/sec-edgar-api-guide , https://tldrfiling.com/blog/sec-edgar-xbrl-api-python-tutorial
