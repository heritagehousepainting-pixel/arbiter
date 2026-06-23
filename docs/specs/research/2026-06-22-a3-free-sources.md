# A3 Advisor: Free Data Source Research

**Date:** 2026-06-22
**Lane:** Data-source research only — no pipeline design, no engine wiring.
**Constraint:** Genuinely free, ToS-compliant, reliable for ~12 tickers polled a few times/day.

---

## 1. Existing Ingest Style (Reference)

The arbiter EDGAR client (`arbiter/arbiter/ingest/edgar/client.py`) establishes the patterns all A3 sources must fit:

- Pure HTTP via `httpx`, no browser automation, no scraping behind login walls.
- User-Agent declared per SEC requirements (`Config.edgar_user_agent`).
- Rate-limit guard: 0.11 s minimum between requests (≈ 9 req/s).
- Exponential back-off on 429/5xx (base 2 s, 3 retries).
- Idempotent: ticker → CIK map cached; filing lists paginated to a fixed `count`.
- All identifiers sanitized before interpolation into URLs (SSRF guards).
- Tests mock the client; no real HTTP in unit tests.

Any A3 ingest module must follow the same pattern: one thin HTTP client class, rate-limit guard, back-off, injectable mock in tests.

---

## 2. Candidate Evaluation

### 2.1 EDGAR 8-K Material Event Filings (RECOMMENDED PRIMARY)

| Attribute | Detail |
|---|---|
| What it provides | Real-time disclosure of material corporate events: M&A announcements, earnings surprises, CEO changes, major contracts, FDA decisions, restatements, default notices. These are the single most legally-reliable "news" signals available for US equities. |
| US equity coverage | All SEC-registered US public companies — complete universe. |
| Auth | None. Requires `User-Agent: Name email@domain` header (identical to existing arbiter EDGAR client). |
| Rate limits | 10 req/s hard cap across all EDGAR endpoints; 429 causes 10-minute IP block. With 12 tickers and polling every 30 min, the load is trivial (~24 requests per poll cycle). |
| Latency | RSS/Atom feed updated every 10 minutes Mon–Fri, 6 AM–10 PM ET. |
| Output format | Two access paths, both free: |
| | **Path A — Atom feed per company:** `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={CIK}&type=8-K&owner=exclude&output=atom&count=40` — returns structured Atom XML with accession number, filing date, form type, company name. |
| | **Path B — EFTS JSON search:** `https://efts.sec.gov/LATEST/search-index?q="{TICKER}"&forms=8-K&dateRange=custom&startdt={DATE}&enddt={DATE}` — full-text search returning JSON with ticker, entity name, accession, form type, file date. |
| ToS | SEC public data; programmatic access explicitly permitted. The `robots.txt` and developer docs encourage machine consumption. No commercial restriction. |
| Reuse of existing code | **Very high.** The existing `EdgarClient._get()`, `_rate_limit()`, `_sanitize_cik()`, and `_parse_submissions_json()` are directly reusable. The 8-K atom feed and EFTS paths are new methods bolted onto the existing client — this is essentially free engineering. |
| Caveats | 8-K items 1.01–8.01 vary in trading relevance. Item 2.02 (earnings results) and 8.01 (other events) are most signal-rich. The body of the 8-K is free-text — extracting a structured signal requires either keyword matching or a Claude call (fits the A2 MiroFish pattern). No sentiment score is provided — only the existence and text of the event. |
| Recommendation | **BUILD THIS FIRST.** Zero marginal cost, zero new auth, reuses existing plumbing. |

---

### 2.2 Finnhub Company News (RECOMMENDED SECONDARY)

| Attribute | Detail |
|---|---|
| What it provides | Per-ticker company news headlines + summaries from aggregated news sources. Includes a sentiment score per article (`sentiment.bearishPercent`, `sentiment.bullishPercent`). Covers earnings, analyst upgrades/downgrades, product news, regulatory filings. |
| US equity coverage | All major US equities. |
| Auth | Free API key required. Register at finnhub.io (no payment info required for free tier). |
| Rate limits | **60 API calls per minute** on the free tier — more generous than every competitor. For 12 tickers polled 4x/day with a 1-week lookback window, total call budget is ~48 calls/poll, well within 60/min. |
| Latency | Real-time / near-real-time news aggregation. |
| Output format | JSON. `GET https://finnhub.io/api/v1/company-news?symbol={TICKER}&from={YYYY-MM-DD}&to={YYYY-MM-DD}&token={API_KEY}` |
| ToS | Free tier is for **personal or non-commercial use**. Finnhub distinguishes "non-professional personal use" from commercial/professional use, which requires written approval at sales@finnhub.io. Running arbiter as a personal paper-trader (non-commercial) is consistent with the non-professional category. **Caveat:** if arbiter is ever commercialized or deployed for clients, a paid plan ($11.99–$99.99/mo) is required. The free tier is ToS-fragile for commercial deployment but fine for a personal paper-trading system. |
| Caveats | Sentiment scores are included but are aggregated across articles, not per-topic. The free tier excludes some endpoints (detailed financials, international markets). News history depth on free tier is unspecified — empirically reported as ~1 year. Non-professional use ToS means this cannot be the primary source in a commercial product. |
| Recommendation | **USE AS SECONDARY: sentiment enrichment for news events found elsewhere.** The built-in bullish/bearish score saves a Claude call. Register one free key. |

---

### 2.3 GDELT DOC 2.0 API

| Attribute | Detail |
|---|---|
| What it provides | Global news media monitoring across 65+ languages, updated every 15 minutes. Queries English keywords over the last 3 months of English-language news coverage. Returns article URLs, tone scores, location mentions. |
| US equity coverage | Indirect — queries by keyword/company name or ticker symbol string. Not ticker-symbol-native; must query "Apple Inc" or "AAPL stock" as free text. |
| Auth | No key, no registration. Open API. |
| Rate limits | Quota-based; exact numeric limits not published. Documented as "rate-limited" with an unspecified quota. Project experienced a major infrastructure outage in June 2025. |
| Latency | 15-minute update cycle. |
| Output format | JSON, CSV, RSS. Endpoint: `https://api.gdeltproject.org/api/v2/doc/doc?query={QUERY}&mode=artlist&format=json&maxrecords=10` |
| ToS | No explicit commercial/non-commercial restriction documented. Project is open-data by stated ethos. |
| Caveats | (1) Rate limits are opaque — no numeric SLA. (2) Keyword matching only, not symbol-native; "MSFT" in a news article is not reliably the ticker. (3) Infrastructure reliability is questionable (June 2025 outage). (4) Tone score is document-level sentiment on a -100 to +100 scale calibrated for geopolitical events, not financial markets — not directly tradeable. (5) 3-month rolling window only; no deep history. |
| Recommendation | **Deprioritize.** Too noisy, too many caveats, opaque rate limits. Use only if primary/secondary fail. |

---

### 2.4 Alpha Vantage NEWS_SENTIMENT

| Attribute | Detail |
|---|---|
| What it provides | Per-ticker news articles with AI-generated sentiment scores (relevance, sentiment label, sentiment score) via the `NEWS_SENTIMENT` function. Part of "Alpha Intelligence" product. |
| US equity coverage | Good — major US equities. |
| Auth | Free API key required (no credit card). |
| Rate limits | **25 requests per day** on the free tier. NEWS_SENTIMENT is classified as a "premium endpoint" within the free tier's 25-req/day allotment. With 12 tickers each needing at least 1 call, 12 daily calls exhausts half the daily budget with no headroom for retries. |
| Latency | Near-real-time news aggregation. |
| Output format | JSON. |
| ToS | Free for personal use. |
| Verdict | **DO NOT USE.** 25 req/day is non-workable for 12 tickers polled multiple times daily. Any meaningful polling pattern hits the wall immediately. Paid plans start at $49.99/month. |

---

### 2.5 Marketaux

| Attribute | Detail |
|---|---|
| What it provides | Financial news with entity extraction (stock/crypto/forex symbols identified in articles) and per-article sentiment score (-1 to +1). Symbol-native filtering. |
| US equity coverage | US equities covered. Free tier filtered by symbol. |
| Auth | Free API key required (no credit card, instant). |
| Rate limits | **100 requests per day** on the free tier. For 12 tickers, one poll cycle = 12 requests minimum. That allows ~8 poll cycles/day — workable for a few-times-per-day schedule. |
| Latency | Near-real-time. |
| Output format | JSON. `GET https://api.marketaux.com/v1/news/all?symbols={TICKER}&api_token={KEY}` |
| ToS | Free tier with no payment details required. Terms not explicitly restricting commercial use in the free tier (as of research date), but standard "developer use" framing applies. |
| Caveats | (1) 100 req/day is tight but workable for 12 tickers at 4x/day polling (48 req/day). One retry request blows the budget. (2) Free tier article volume and source breadth unspecified. (3) Smaller provider — less proven reliability than Finnhub. |
| Recommendation | **VIABLE ALTERNATIVE** to Finnhub if Finnhub ToS becomes an issue. Not worth stacking both. |

---

### 2.6 Tiingo News API

| Attribute | Detail |
|---|---|
| What it provides | Financial news aggregation with ticker tagging. |
| Auth | Free API key required. |
| Rate limits | Not applicable — see verdict. |
| ToS | — |
| Verdict | **DO NOT USE.** The Tiingo News API is not available on the free plan. It is a paid add-on. The free tier covers EOD price data only. Tiingo's marketing implies a generous free tier but the news product is behind a paywall. |

---

### 2.7 NewsAPI.org

| Attribute | Detail |
|---|---|
| What it provides | General news headlines and articles. No financial-specific ticker filtering. |
| Auth | Free API key. |
| Rate limits | 100 req/day free tier. |
| ToS | **Critical disqualifier:** the free tier is **development/localhost only**. Production deployment (i.e., any automated process on a server or cron) is explicitly prohibited on the free tier. Paid plans start at $449/month. |
| Verdict | **DO NOT USE.** Production use on the free tier violates ToS. No ticker-native filtering. Not financial-specific. |

---

### 2.8 NewsData.io

| Attribute | Detail |
|---|---|
| What it provides | General + financial news API. 97,000+ sources, 206 countries. |
| Auth | Free API key (no credit card). |
| Rate limits | **200 req/day** free tier (each request returns up to 10 articles). |
| Latency | **12-hour delay** on free tier. |
| ToS | Free tier explicitly permits commercial use — a notable distinction from competitors. |
| Caveats | The 12-hour delay is a hard disqualifier for anything resembling a trading signal. By the time arbiter sees news, the price has moved. No ticker-native filtering. |
| Verdict | **DO NOT USE for trading signals.** The 12-hour delay makes this unusable as a market signal. |

---

### 2.9 Yahoo Finance RSS

| Attribute | Detail |
|---|---|
| What it provides | Per-ticker or per-category news via informal RSS feeds. URL pattern: `https://finance.yahoo.com/rss/headline?s={TICKER}` |
| Auth | None formally required. No API key. |
| Rate limits | No official documented limits. Aggressive polling (< 5 min intervals) risks IP block. |
| ToS | **Flagged as problematic.** Yahoo discontinued its official API in 2017. Community libraries (yfinance, etc.) use unofficial/undocumented endpoints that Yahoo has intermittently blocked. Yahoo's ToS prohibits automated scraping of their service. The RSS feed itself is a gray area — it is publicly accessible but not an officially sanctioned developer interface. Yahoo has historically disrupted community tools (e.g., yfinance blocks in 2024–2025). |
| Caveats | Unreliable — Yahoo can change or block the feed endpoint without notice. No ToS guarantee. Not appropriate as a primary source for a system that must be reliable. |
| Recommendation | **AVOID as a primary source.** Acceptable as an opportunistic fallback with error-handling that degrades gracefully. Not worth engineering around. |

---

### 2.10 Google News RSS

| Attribute | Detail |
|---|---|
| What it provides | News articles matching a keyword query via RSS. URL pattern: `https://news.google.com/rss/search?q={QUERY}+stock&hl=en-US&gl=US&ceid=US:en` |
| Auth | None. |
| Rate limits | No official limits. Safe polling interval is 15–30 minutes per query to avoid IP rate-limiting. |
| ToS | Feed metadata includes a copyright notice restricting use to "personal, non-commercial" reading. Google's ToS prohibits systematic automated access. Scraping Google News is a ToS violation for commercial purposes. |
| Caveats | ToS forbids programmatic commercial use. Google has no official developer API for News (they shuttered the News API in 2013). The RSS feed is a user-facing feature, not a developer interface. Structural changes can break parsers without warning. |
| Recommendation | **AVOID.** ToS violation risk. Not a stable developer interface. |

---

### 2.11 SeekingAlpha / MarketWatch RSS

| Attribute | Detail |
|---|---|
| What it provides | SeekingAlpha: analyst commentary, earnings reactions. MarketWatch: financial news. Both expose some RSS feeds. |
| ToS | **SeekingAlpha:** ToS explicitly prohibits robots, spiders, crawlers, or any automated process. Even their RSS feeds carry a "personal, non-commercial use only" restriction. Programmatic use in a trading system is a clear ToS violation. **MarketWatch:** Owned by News Corp. Similar restrictions on automated scraping. |
| Recommendation | **DO NOT USE.** Both are explicitly ToS-prohibited for automated programmatic access. |

---

### 2.12 Reddit API (r/wallstreetbets, r/stocks)

| Attribute | Detail |
|---|---|
| What it provides | Retail sentiment, meme stock momentum, WSB "DD" posts, ticker mention counts. |
| Auth | OAuth2 client credentials (free), but as of November 2025, **pre-approval is required** for all applications including personal projects. Approval process takes 2–4 weeks and requires detailed documentation. |
| Rate limits | 100 queries/minute per OAuth client ID for approved non-commercial apps. |
| ToS | Non-commercial, pre-approved use is the only free path. Reddit's "Responsible Builder Policy" (2025) requires explicit written approval from Reddit before accessing any data — bots and automated tools included. |
| Caveats | (1) The 2–4 week approval process creates a hard dependency before development can begin. (2) Reddit has aggressively cracked down post-2023; the policy environment is actively hostile. (3) Retail chatter on WSB has a poor signal-to-noise ratio for systematic trading. (4) Third-party Reddit sentiment aggregators (e.g., Tradestie) exist but are themselves unverified free sources. |
| Recommendation | **LOW PRIORITY.** The pre-approval gate and ToS instability make this unreliable infrastructure. The signal quality for a 12-ticker systematic strategy is also weak. Only worth pursuing if the user is willing to apply for Reddit API access for the specific purpose. |

---

### 2.13 StockTwits API

| Attribute | Detail |
|---|---|
| What it provides | Per-cashtag message streams with bullish/bearish sentiment labels (user-tagged). Social trading chatter. |
| Auth | API key required — **currently not accepting new registrations.** The developer page states "we unfortunately won't be accepting new registrations until we have finished our review." No timeline provided. |
| Rate limits | Unknown (registration blocked). |
| ToS | Automated use conditionally allowed when adding genuine value (not spam). |
| Recommendation | **BLOCKED.** Cannot register for access. Do not build dependency on this source. Revisit in 6+ months. |

---

### 2.14 X / Twitter / Nitter (FLAGGED — RECOMMEND AGAINST)

| Attribute | Detail |
|---|---|
| Status | **EXPLICITLY REJECTED.** X's ToS (updated 2023) prohibits unauthorized data crawling and scraping. The API costs $200/month minimum (the user's stated hard limit is $0). Nitter instances have been systematically shut down since early 2024 as X blocked the guest account tokens they depend on. All remaining Nitter instances are intermittent or dead. X has pursued legal action against scrapers. |
| Risk | IP block, account suspension, potential legal exposure. |
| Recommendation | **DO NOT BUILD.** This was explicitly flagged by the user. Any X/Nitter/logged-in scraping path is ToS violation + technical instability + legal risk. |

---

## 3. Rate-Limit Math

Scenario: 12 tickers, polled 4 times per day (every 6 hours during market hours).

| Source | Calls per poll | Calls per day | Daily budget | Headroom |
|---|---|---|---|---|
| EDGAR 8-K Atom feed | 12 (one per ticker, via existing CIK map) | 48 | Unlimited (10 req/s cap) | Ample |
| Finnhub company-news | 12 | 48 | 60/min (no daily cap documented) | Ample |
| Marketaux | 12 | 48 | 100/day | 52 remaining |
| Alpha Vantage NEWS_SENTIMENT | 12 | 48 | 25/day | **FAILS on first poll cycle** |
| NewsAPI.org | 12 | 48 | 100/day but **localhost only** | **ToS violation** |
| NewsData.io | 12 | 48 | 200/day | OK — but 12-hr delay kills signal |

**EDGAR + Finnhub** together require: 96 total daily calls, both well within limits.

---

## 4. Required Free API Key Registrations

| Service | URL | Approval time | Credit card? |
|---|---|---|---|
| Finnhub | https://finnhub.io/register | Instant (email only) | No |
| Marketaux (fallback) | https://www.marketaux.com/ | Instant | No |

EDGAR requires no API key — only the existing `EDGAR_USER_AGENT` env var already set up in the arbiter config.

---

## 5. Recommended Free Stack

### Primary: EDGAR 8-K Atom Feed

**Why:** Zero new dependencies. Reuses existing `EdgarClient` infrastructure (CIK lookup, rate limiting, back-off, User-Agent, sanitizers). The `search_8k_filings()` method is a 30-line addition to the existing client. 8-K filings are the most reliable, legally-grounded, latency-appropriate news source for US equities — they are the events that *cause* news, not reactions to it. Coverage is 100% of the ticker universe. Free forever, no rate-limit risk, ToS unambiguous.

**What it does not provide:** Sentiment scores. 8-K text must be parsed or passed to Claude to extract signal direction. This is a natural fit for the A2 MiroFish pattern (Claude tool use).

**Polling pattern:** Per-ticker Atom feed every 30 minutes during market hours (9:30 AM–4:00 PM ET). Flag any 8-K filed since the last check. Extract Item number (1.01 M&A, 2.02 earnings, 5.02 officer change, etc.) as the first-pass signal type. Pass full 8-K text to Claude for sentiment + magnitude.

### Secondary: Finnhub Company News

**Why:** 60 req/min free tier is the most generous in the market. Per-ticker JSON endpoint is symbol-native (no ambiguous keyword matching). Includes built-in bullish/bearish sentiment scores that can supplement or replace a Claude call for routine news. Free for non-commercial personal use, which covers a personal paper-trading system. Provides breadth coverage (analyst upgrades, earnings previews, product announcements) that EDGAR 8-K misses (EDGAR only captures SEC-required disclosures).

**ToS note:** Mark clearly in the codebase that the free tier is non-commercial only. If arbiter is ever deployed as a commercial product, Finnhub requires a paid license.

**Register:** One API key at finnhub.io (instant, no credit card).

**Polling pattern:** Per-ticker, 4x/day, 7-day lookback window. Filter to articles with `sentiment.bullishPercent > 0.6` or `< 0.4` as candidate signals.

### Optional Chatter Layer: Reddit (Conditional)

**Not recommended for v1.** The November 2025 pre-approval requirement introduces a hard 2–4 week gate before development and adds ongoing ToS instability risk. The signal quality from WSB/r/stocks for a 12-ticker systematic strategy is weak compared to EDGAR + Finnhub.

**If pursued later:** Apply for Reddit API access via the Responsible Builder Policy for a non-commercial personal research use case. Use PRAW (Python Reddit API Wrapper). Focus on r/stocks rather than r/wallstreetbets for higher signal-to-noise. Track ticker mention velocity and net sentiment direction, not individual posts.

---

## 6. Definitively Ruled Out

| Source | Reason |
|---|---|
| X / Twitter / Nitter | ToS violation + API costs $200/mo + Nitter dead |
| SeekingAlpha RSS | ToS explicitly prohibits automated access |
| Google News RSS | ToS personal/non-commercial only; no official developer API |
| Yahoo Finance RSS | Unofficial endpoint, historically blocked, ToS gray area |
| MarketWatch RSS | Automated scraping prohibited by ToS |
| NewsAPI.org | Free tier is localhost-only; production use violates ToS |
| Tiingo News API | Not on free tier; paid add-on only |
| Alpha Vantage NEWS_SENTIMENT | 25 req/day hard cap fails for 12-ticker polling |
| StockTwits | New registrations blocked; no timeline to reopen |
| NewsData.io | 12-hour delay disqualifies as a trading signal |

---

## 7. Implementation Fit with Existing Architecture

The EDGAR 8-K path requires:
1. A new `search_8k_filings(ticker, since_date)` method on `EdgarClient` (uses existing `_get()`, `_rate_limit()`, Atom-format URL).
2. A new `parse_8k_items()` helper (extracts Item numbers and headline text from Atom XML).
3. Optional: `get_8k_body(accession, cik)` reusing existing `get_form4_xml()` pattern.

The Finnhub path requires:
1. A new `FinnhubClient` class following the same `httpx` + rate-limit + back-off pattern.
2. Config field: `finnhub_api_key` (maps to `FINNHUB_API_KEY` env var).
3. One endpoint: `GET /api/v1/company-news?symbol={ticker}&from={date}&to={date}&token={key}`.

Both integrate naturally into the existing `arbiter/arbiter/ingest/` package alongside the `edgar/` module.

---

*Research conducted 2026-06-22. API terms and limits verified via live web search; treat rate-limit numbers as accurate as of this date but subject to change at provider discretion.*
