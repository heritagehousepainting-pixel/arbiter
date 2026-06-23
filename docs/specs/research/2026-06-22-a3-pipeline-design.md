# A3 News/Smart-Money Advisor — Ingest-to-Opinion Pipeline Design

> **Status:** Design spec — no code.
> **Date:** 2026-06-22
> **Lane:** Pipeline design (ingest → `NewsItem` → ticker → stance/confidence → `Opinion`)
> **Scope:** Source-agnostic. The sources themselves are picked by the data-sources
> research lane. This spec assumes whatever free adapters the data-sources lane
> selects will map their payloads into the `NewsItem` interface defined here.

---

## 0. Context and placement in the codebase

### What already exists

The arbiter codebase already contains a dormant **Lane 8 tips layer** at
`arbiter/arbiter/tips/`:

- `tips/source.py` — `UnverifiedTip(ticker, claim, account, ts, url, source_id)` +
  `TipSource` ABC
- `tips/account_scorer.py` — rule-based `AccountScorer` (credibility [0, 1])
- `tips/diversity.py` — `DiversityGate` (≥ 2 independent `source_id` before
  corroboration)
- `db/migrations/020_tips.sql` — `unverified_tips` + `account_scores` tables

A3 is **not a separate architecture**. It is the activation of Lane 8: a thin
adapter layer (`arbiter/arbiter/adapters/a3/`) that transforms the already-built
`TipSource`/`UnverifiedTip`/`DiversityGate` chain into a validated `Opinion`
using the canonical contract at `arbiter/arbiter/contract/opinion.py`.

The A3 advisor ID is `"A3.news"`.

---

## 1. Module breakdown

```
arbiter/arbiter/
├── tips/                           # ALREADY EXISTS — Lane 8
│   ├── source.py                   # UnverifiedTip + TipSource ABC
│   ├── account_scorer.py           # AccountScorer
│   └── diversity.py                # DiversityGate (≥2 independent sources)
│
└── adapters/
    └── a3/                         # NEW — thin adapter, mirrors mirofish/ pattern
        ├── __init__.py
        ├── news_item.py            # NewsItem dataclass (source-agnostic interface)
        ├── ticker_resolver.py      # Headline/cashtag/entity → watchlist ticker(s)
        ├── stance.py               # Sentiment → stance_score + confidence
        ├── corroborate.py          # Dedup + diversity-gate + source-credibility fold
        └── adapter.py              # run(ticker, as_of, ...) → list[Opinion]
```

No `mirofish`-style external service is needed. A3 is **in-process**, built on
top of the existing tips layer. The separation into these five modules mirrors
how the EDGAR pipeline separates `client.py`, `parser.py`, `normalize.py`, and
the signals layer.

---

## 2. The `NewsItem` interface

`arbiter/arbiter/adapters/a3/news_item.py`

`NewsItem` is the source-agnostic lingua franca that every free-source adapter
must produce. Adapters are concrete `TipSource` implementations; they receive raw
API/RSS payloads and must emit exactly one `UnverifiedTip` per item, plus one
`NewsItem` (or they may internally produce `NewsItem` first and derive the
`UnverifiedTip` from it — either direction is fine as long as both types flow
through the pipeline).

### Fields

| Field | Type | Notes |
|---|---|---|
| `title` | `str` | Headline or post title. May be empty for body-only sources. |
| `body` | `str` | Full text or summary. May be truncated to 2 000 chars by the adapter. |
| `url` | `str` | Canonical URL (used as part of `UnverifiedTip.fingerprint`). |
| `published_at` | `datetime` | Tz-aware UTC timestamp from the source. **Never wall-clock.** |
| `source_id` | `str` | Platform adapter ID matching `TipSource.source_id` (e.g. `"rss.seekingalpha"`, `"reddit.wsb"`). |
| `account` | `str` | Author handle or channel identifier. Used by `AccountScorer`. |
| `raw` | `str` | JSON-serialised original payload, stored for audit and backtest replay. |

### Constraints

- `published_at` must carry `tzinfo` (tz-aware UTC). Any adapter that receives a
  naive timestamp must attach `timezone.utc` explicitly before constructing a
  `NewsItem`. A naive `published_at` is a contract violation rejected at
  validation time.
- `url` must be non-empty. For sources without stable URLs (e.g. some RSS feeds
  that redirect), the adapter must construct a deterministic synthetic URL from
  (`source_id`, `account`, `published_at.isoformat()`).
- `title` and `body` may not both be empty. If the source supplies only a
  title, `body` defaults to `""`. If only body text is available, `title`
  defaults to `""`.

### Fingerprinting

`NewsItem.fingerprint()` returns `SHA-256(f"{source_id}|{account}|{url}")`.
This mirrors `UnverifiedTip.fingerprint()` exactly and is the `source_fingerprint`
that flows into the `Opinion` contract. Using the URL (not the body) as the
fingerprint key means minor edits and syndicated rewrites of the same article
dedup correctly.

---

## 3. Ticker resolution

`arbiter/arbiter/adapters/a3/ticker_resolver.py`

### Problem

A news item may reference a company by:
- `$AAPL` cashtag (most reliable)
- Company name: "Apple announced…"
- Ambiguous partial name: "Apple" could mean AAPL or APLE (Apple Hospitality)
- Tickers not on the watchlist (off-watchlist items must be discarded)

### Algorithm (three-pass, in order)

**Pass 1 — Cashtag extraction (high confidence)**

Scan `title + " " + body` for `$[A-Z]{1,5}` tokens. Normalise to uppercase.
Intersect with the watchlist set loaded at startup. Cashtag hits are accepted
directly; ambiguity is rare because cashtags are explicit.

**Pass 2 — Company-name lookup (medium confidence)**

If Pass 1 yields nothing: use a static lookup table (JSONL file bundled with the
adapter, e.g. `a3/data/company_aliases.jsonl`) mapping canonical company names
and known aliases to tickers. Apply case-insensitive substring search against
`title`. For example: `"apple" → "AAPL"`, `"alphabet" → "GOOGL"`, `"meta platforms" → "META"`.

The lookup table is **a frozen snapshot**. It must not be fetched at runtime
(no network calls inside the resolver). The data-sources lane populates this
file as a build artifact. The resolver filters the result against the live
watchlist to prevent stale entries from producing off-watchlist tickers.

**Pass 3 — Ambiguity handling and watchlist filter**

After Passes 1 and 2:
- If zero watchlist tickers are found → **abstain** (return `[]`, log at DEBUG).
  This is the correct outcome for general market-news items that happen to
  mention no watchlist company.
- If exactly one watchlist ticker is found → accept it.
- If more than one watchlist ticker is found → keep all of them (the adapter
  emits one `Opinion` per resolved ticker, each independently validated by the
  diversity gate). Cap at 5 tickers per item to guard against run-on lists.

The resolver is pure (no network, no clock). It takes `(title, body, watchlist: frozenset[str])` → `list[str]`.

---

## 4. Stance extraction

`arbiter/arbiter/adapters/a3/stance.py`

### Recommendation: option (a) — lightweight keyword-polarity sentiment, not Claude

**Rationale:** A2 (MiroFish) already calls Claude (`claude-sonnet-4-6`) with an
`emit_opinions` tool and costs real tokens per invocation. A3 is designed to run
across many items per cycle (potentially dozens of news items across the
watchlist). Calling Claude per item would multiply the token cost by an order of
magnitude with no structural benefit, because news items lack the quantitative
evidence that makes A2's LLM call worthwhile (A2 supplies price action, RSI,
P/E, etc. in its `EvidencePack`; a news headline gives the LLM nothing it
couldn't already derive from the text itself).

The correct approach for A3 is a **deterministic, auditable, free sentiment
library** — specifically, a rule-based polarity scorer. Recommended:
`VADER` (Valence Aware Dictionary and sEntiment Reasoner, pip: `vaderSentiment`,
no network, ~2 MB, MIT-ish license) or a simple custom keyword dictionary.

Both are:
- Instant (microseconds per item)
- Reproducible across backtest replays (same input → same score)
- Free from API costs and rate limits
- Transparent for audit (no black-box LLM call)

A future Wave can optionally add a Claude call as a **parallel opinion**
(`A3.news.llm`) with a hard token budget per cycle, evaluated separately by the
trust ledger, but that is out of scope for the initial build.

### `StanceResult` shape

```
@dataclass(frozen=True)
class StanceResult:
    stance_score: float      # [-1.0, 1.0]; positive = bullish, negative = bearish
    raw_polarity: float      # raw scorer output before scaling (for audit)
    method: str              # "vader" or "keyword" or future tag
```

### Scoring formula

VADER returns a `compound` score in `[-1.0, 1.0]`. This maps directly to
`stance_score` with one adjustment: clamp absolute values below `0.05` to `0.0`
(the "neutral zone" — VADER compound scores near zero are noise).

```
stance_score = 0.0                   if |compound| < 0.05
             = compound               otherwise
```

The resulting `stance_score` is the value placed directly into `Opinion.stance_score`.
No further scaling. This matches the INTERFACES.md §2 contract: `stance_score ∈ [-1.0, 1.0]`,
positive = long, negative = short.

### What makes a news item "about" a smart-money / insider / political trade vs generic news

A3 is a **news advisor**, not an insider-detection advisor (A1 owns that signal).
A3 does not attempt to distinguish earnings news from an acquisition rumour from
a regulatory headline — it treats all item types equally and lets the stance
score speak for itself. Confidence (section 5) is the right lever to express
that a CNBC M&A rumour warrants lower confidence than a corroborated Bloomberg
earnings beat.

If a specific sub-category of news warrants a separate advisor ID (e.g.
`A3.news.rumour` for acquisition rumours that have a distinct horizon profile),
that is a future Wave concern, not a pipeline-design concern.

---

## 5. Confidence derivation

`arbiter/arbiter/adapters/a3/corroborate.py`

`confidence ∈ [0.0, 1.0]` per INTERFACES.md §2. A3 uses `ConfidenceSource.MODELED`.

### Component weights

```
confidence = clamp(
    source_tier_score × 0.40
  + corroboration_score × 0.40
  + recency_score × 0.20,
  0.05, 0.85
)
```

The cap at `0.85` prevents A3 from ever claiming very-high confidence —
news sentiment is inherently noisy and its track record is not yet established.
The floor at `0.05` ensures valid, non-zero confidence for any surviving item
(abstain is represented by not emitting an Opinion at all, per INTERFACES.md §11).

#### Source tier score (40%)

The `source_id` is assigned to one of three tiers by a frozen config table
(also a JSONL file in `a3/data/source_tiers.jsonl`):

| Tier | Score | Example `source_id` values |
|---|---|---|
| `high` | 0.80 | `"rss.bloomberg"`, `"rss.wsj"`, `"rss.reuters"` |
| `medium` | 0.50 | `"rss.seekingalpha"`, `"rss.motleyfool"`, `"reddit.wsb"` |
| `low` | 0.25 | unknown / unrecognised sources (default) |

The source-tiers table is populated by the data-sources research lane. Unknown
`source_id` values fall back to `"low"`.

#### Corroboration score (40%)

This reuses the existing `DiversityGate` logic from `tips/diversity.py`.
After all tip adapters have run for the current cycle:

- 0 independent sources (single item, gate not yet evaluated) → `0.20`
- 1 independent source (gate passed, threshold was 1) → `0.40` (note: the gate
  requires ≥ 2 for producing an Opinion at all; this branch should be unreachable
  in practice — see section 6)
- 2 independent sources → `0.65`
- 3+ independent sources → `0.90`

If the diversity gate has not yet been passed (fewer than 2 independent sources),
A3 **abstains** — no confidence score is computed and no Opinion is emitted. The
corroboration score only matters when the gate has already passed.

#### Recency score (20%)

Based on `(as_of - published_at).total_seconds()`:

| Age | Score |
|---|---|
| ≤ 4 hours | 1.00 |
| 4–24 hours | 0.70 |
| 24–72 hours | 0.40 |
| > 72 hours | 0.10 |

`as_of` is always the **caller-supplied information timestamp**, never
`datetime.now()` (INTERFACES.md §11.1). In live mode, the engine passes the
current cycle's `clock.now()` as `as_of`. In backtest mode, the backtest harness
passes the replay date. This is the same pattern A2 uses.

---

## 6. `horizon_days` and `HorizonBucket`

News items are a **short-horizon signal**. A regulatory filing, earnings beat, or
M&A rumour is priced into the market within days. The appropriate horizon is
**7 calendar days**, which maps to `HorizonBucket.SHORT` (1–30 days per
`arbiter/arbiter/types.py`).

Justification:
- A1.insider uses form4 signals with a longer lag (~30–60 days) because insider
  information is slow-moving and structural.
- A2.mirofish emits both `SHORT` (14 days) and `MEDIUM` (60 days) because it
  has quantitative technical + fundamental evidence to anchor a medium-term view.
- A3 news signals are rapidly consumed by the market. A headline that hasn't
  moved the stock within a week is likely noise. Using `horizon_days = 7` is
  conservative and consistent with the empirical half-life of news sentiment.

`horizon_days = 7` is a **module constant**, not configurable per item. If the
data-sources lane discovers a category of news (e.g. FDA calendar events) that
warrants a different horizon, that is a new sub-advisor (`A3.news.fda`, horizon
30 days), not a modification to the base A3 pipeline.

---

## 7. Dedup and de-noising

### `source_fingerprint` dedup

`source_fingerprint = SHA-256(f"{source_id}|{account}|{url}")` — the same value
as `NewsItem.fingerprint()`. This is stored in `unverified_tips.fingerprint`
(migration 020) and in `opinions.source_fingerprint`. The `opinion_store.py`
idempotency guard (`SELECT ... WHERE source_fingerprint = ? AND as_of = ?`)
ensures that re-running the same cycle never double-inserts.

### PR-wire / syndication spam filter

Before the diversity gate runs, the adapter filters items by:

1. **Domain block list** (frozen config): PRNewswire, BusinessWire, GlobeNewswire,
   EIN Presswire, and other wire distribution services are assigned
   `source_tier = "blocked"`. Items with a blocked `source_id` are dropped
   before ticker resolution (not even stored in `unverified_tips`).

2. **Title dedup within a cycle window**: Two items with identical `title`
   (after stripping punctuation and lowercasing) within the same `ticker +
   24-hour window` are treated as syndicated duplicates. Only the earliest
   `published_at` is kept; the later item is dropped with no DB write.

3. **Body hash dedup**: If `SHA-256(body[:500])` matches an already-seen item
   for the same ticker in the same cycle window, the later item is dropped.

### Rate-limit-aware polling

The `TipSource.fetch(ticker, as_of)` interface already enforces that adapters
return `[]` on failure. The adapter runner in `adapters/a3/adapter.py` calls
sources in sequence (not parallel), with a configurable inter-source sleep if
needed. Adapters are responsible for their own rate-limit handling internally
(typically by caching the last `N` results and only making a network call after
a minimum polling interval). The A3 adapter itself does not retry — a failed
source for one cycle simply contributes one fewer voice to the diversity gate
for that cycle. If A3 consistently gets zero results for a ticker, the absence
of an Opinion (abstain) is the correct and safe outcome.

---

## 8. Point-in-time (PIT) safety and no-lookahead

This is the hardest correctness requirement.

### The rule

`Opinion.as_of` must be the **real information timestamp** of the underlying
item, not wall-clock time. Specifically:

```
opinion.as_of = item.published_at   (NewsItem.published_at, from the source)
```

NOT `datetime.now()`. NOT the cycle start time. The source's own `published_at`
is the moment when the information became available to the market. Using it
directly means that a backtest replaying 2025-11-01 data will only see opinions
whose `as_of <= 2025-11-01` — exactly the PIT discipline enforced by
`opinion_store.py`'s `query_opinions(..., as_of=..., strict_lt=True)`.

### How `check_no_lookahead.sh` will pass

The no-lookahead checker (`arbiter/scripts/check_no_lookahead.sh`) bans:
- `datetime.now()` outside `clock.py`
- `datetime.utcnow()` outside `clock.py`
- `time.time()` outside `clock.py`
- `date.today()` outside `clock.py`
- `pd.Timestamp.now()` outside `clock.py`

A3 pipeline modules (`news_item.py`, `ticker_resolver.py`, `stance.py`,
`corroborate.py`, `adapter.py`) must contain **zero** of these calls. They are
all passed `as_of: datetime` as a parameter from the caller (the engine or
backtest harness).

The one tricky case is the recency score in section 5: `(as_of - published_at)`.
Both `as_of` and `published_at` are injected timestamps. No clock read occurs.
The subtraction is pure arithmetic on two `datetime` values — this is correct
and passes the linter.

### Backtest guard

When `is_backtest=True` (following A2's pattern in `adapter.py`), the adapter
must **not** fetch live news. All items must come from a pre-fetched, PIT-sorted
dump stored in the DB (`unverified_tips` table, ordered by `ts`). The adapter
filters `WHERE ts <= as_of.isoformat()` against the stored tips — never fetching
from the network — exactly as the `PITGateway` pattern used by A2's evidence
fetcher.

In live mode, the `TipSource` adapters do make network calls. The `as_of`
parameter serves as the **upper bound**: adapters must drop any item whose
`published_at > as_of` (this mirrors `TipSource.fetch`'s docstring: "The adapter
MUST NOT return tips with `ts > as_of`"). This prevents a slow API response that
returns a future-timestamped item from leaking into a current opinion.

---

## 9. Complete data flow

```
[TipSource adapters]
  ↓
  fetch(ticker, as_of)  →  list[UnverifiedTip]

[news_item.py — NewsItem construction]
  ↓
  Each UnverifiedTip → NewsItem
  Validate: published_at tz-aware, url non-empty, title/body non-both-empty

[ticker_resolver.py — Ticker resolution]
  ↓
  NewsItem.(title + body) → list[str] (watchlist tickers, ≤ 5)
  Off-watchlist items → dropped (abstain)

[PR-wire / syndication filter — corroborate.py]
  ↓
  Blocked source_id → drop
  Title dedup within 24h window → drop duplicate
  Body hash dedup → drop duplicate

[unverified_tips persistence — 020_tips.sql]
  ↓
  Surviving items → INSERT OR IGNORE into unverified_tips (fingerprint UNIQUE)
  Stored as audit trail regardless of whether they later corroborate

[diversity gate — tips/diversity.py]
  ↓
  DiversityGate.evaluate(ticker, tips)
  < 2 independent source_ids → abstain (return [])

[stance.py — VADER sentiment]
  ↓
  For each corroborated ticker:
    StanceResult = vader_compound_score(title + " " + body)
    stance_score = 0.0 if |compound| < 0.05 else compound

[corroborate.py — Confidence assembly]
  ↓
  source_tier_score = tier_table[source_id]
  corroboration_score = f(n_independent_sources)
  recency_score = f((as_of - published_at).total_seconds())
  confidence = clamp(0.4 × tier + 0.4 × corr + 0.2 × recency, 0.05, 0.85)

[adapter.py — Opinion construction]
  ↓
  Opinion(
    advisor_id         = "A3.news",
    ticker             = resolved ticker,
    stance_score       = stance_result.stance_score,
    confidence         = confidence,
    confidence_source  = ConfidenceSource.MODELED,
    horizon_days       = 7,                          # SHORT bucket
    as_of              = published_at,               # PIT: source's publish time
    rationale          = f"[{source_id}] {title[:200]} | "
                         f"VADER compound={raw_polarity:.3f} | "
                         f"sources={n_independent_sources}",
    source_fingerprint = SHA-256(source_id|account|url),
    run_group_id       = ulid(),                     # fresh per cycle run
  )
  validate_opinion(opinion)    # raises → logged, Opinion dropped
  → list[Opinion]  (one per resolved ticker × corroborated window)
```

---

## 10. Exact `Opinion` field mapping

| `Opinion` field | A3 source | Notes |
|---|---|---|
| `advisor_id` | `"A3.news"` | Registered with `default_registry.register("A3.news")` at startup. No hard weight cap specified (system default applies); shadow until trust earned. |
| `ticker` | `ticker_resolver.py` output | One Opinion per resolved watchlist ticker. |
| `stance_score` | VADER compound, clamped | `[-1.0, 1.0]`. `0.0` when |compound| < 0.05. |
| `confidence` | Weighted formula (section 5) | `[0.05, 0.85]`. `ConfidenceSource.MODELED`. |
| `confidence_source` | `ConfidenceSource.MODELED` | Modeled because derived from a scoring formula. |
| `horizon_days` | `7` (constant) | Maps to `HorizonBucket.SHORT`. |
| `as_of` | `NewsItem.published_at` | Tz-aware UTC from the source. **Never `datetime.now()`**. |
| `rationale` | Constructed string | Includes `source_id`, truncated title, VADER compound, n-sources. ≤ 500 chars. |
| `source_fingerprint` | `SHA-256(source_id\|account\|url)` | Same as `UnverifiedTip.fingerprint()`. Non-empty guaranteed by URL validation. |
| `run_group_id` | Fresh ULID per cycle run | All opinions from the same A3 cycle invocation share a `run_group_id`, matching A2's pattern. |

---

## 11. Failure modes and abstain behaviour

A3 must **never raise** out of the `run()` boundary (mirroring A2's fail-closed
outer `try/except` in `adapter.py`). The following failure modes are all handled
by returning `[]` (abstain):

| Failure | Handling |
|---|---|
| All `TipSource` adapters return `[]` (source down / rate-limited) | No tips → diversity gate fails → abstain cleanly |
| `published_at` is naive (adapter bug) | `NewsItem` validation rejects → item dropped → logged at WARNING |
| Ticker resolver finds no watchlist match | `[]` returned by resolver → item dropped → logged at DEBUG |
| VADER scorer raises (corrupt text) | Caught inside `stance.py`, returns `StanceResult(stance_score=0.0, ...)` → Opinion not emitted (0.0 stance = abstain per INTERFACES.md §11) |
| `validate_opinion` raises on constructed Opinion | Caught in `adapter.py`, logged at WARNING, Opinion dropped from results |
| `unverified_tips` INSERT fails (DB error) | Non-fatal: logged at WARNING, pipeline continues without persisting that tip. Opinion still potentially emitted if other tips corroborate. |
| Diversity gate yields `corroborated=False` | `adapter.py` returns `[]` for that ticker — clean abstain |
| Entire `run()` raises unexpectedly | Outer `try/except Exception` in `adapter.py` logs WARNING, returns `[]` |

The engine (`arbiter/arbiter/engine/advisors.py`) already handles `None`/`[]`
from A1 and A2 correctly. A3 follows the same contract: `run()` returns
`list[Opinion]` — empty list = abstain.

---

## 12. Registration

`adapter.py` registers A3 at module import time:

```python
from arbiter.contract.opinion import default_registry
default_registry.register("A3.news")
```

No `hard_weight_cap` is set at registration (unlike A2 which sets `0.35`).
A3 starts as shadow: the trust ledger assigns weight 0 until forward-test data
accumulates. The shadow flag lives in the `advisor_weights` table (migration 011),
not in the Opinion contract.

---

## 13. Conventions match checklist

This design was validated against the existing codebase conventions:

- [x] `from __future__ import annotations` — all new modules
- [x] `@dataclass(frozen=True)` for `NewsItem` and `StanceResult` (mirrors `UnverifiedTip`, `Opinion`)
- [x] `as_of` always injected, never `datetime.now()` — passes `check_no_lookahead.sh`
- [x] `stance_score = None` = abstain (Opinion not emitted at all)
- [x] `source_fingerprint` non-empty (enforced by URL validation + `validate_opinion`)
- [x] Insert-only: `unverified_tips` uses UNIQUE fingerprint index + INSERT OR IGNORE
- [x] `horizon_days = 7` → `bucket_for_days(7) == HorizonBucket.SHORT` (valid per `types.py`)
- [x] `validate_opinion(op)` called before any Opinion is returned
- [x] `run()` boundary is fail-closed (never raises, returns `[]` on any error)
- [x] AGPL isolation: no `import mirofish` (A3 is pure in-process)
- [x] `ConfidenceSource.MODELED` (correct enum value from `arbiter.types`)
- [x] `run_group_id` is a fresh ULID per cycle (not a fixed string)
- [x] structlog used for logging (not stdlib `logging`) — matches `adapter.py` pattern
- [x] Backtest guard: `is_backtest=True` blocks live fetches, reads from `unverified_tips` table only
