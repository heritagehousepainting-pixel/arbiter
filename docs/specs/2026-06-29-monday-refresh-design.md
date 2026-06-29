# Monday Refresh — Design Spec

**Date:** 2026-06-29
**Status:** Approved (design); pending spec review → implementation plan
**Owner:** arbiter

## 1. Summary

A pre-market intelligence pass that runs **08:00 every Monday (America/New_York)**,
independent of the market-hours daemon. It performs three scans, pushes a digest to
the user's phone, and feeds findings into the trading engine through existing
trust-gated seams:

1. **Market-wide (macro) news** — what moved or will move the broad market this week.
2. **Open-position news** — news affecting each currently-held ticker.
3. **Data-source staleness** — which arbiter data sources (fund managers, activist
   filers, watchlist, sectors) need refreshing.

Findings are delivered to the user (phone + saved report) **and** fed to the engine:
position news already flows via the existing `A3.news` advisor; macro news flows via a
**new probationary `A4.macro` advisor**; staleness triggers re-ingest of the affected
source.

## 2. Decisions (locked)

| # | Decision | Choice |
|---|----------|--------|
| 1 | What the scan does with findings | **Briefing + feed engine** (digest to phone, news → engine, staleness → re-ingest) |
| 2 | Macro → trades | **New `A4.macro` advisor**, registered probationary, governed by the trust/learning loop exactly like `A3.news` |
| 3 | News gathering | **Finnhub (per-trade) + Claude web-search (macro & staleness reasoning)** |
| 4 | Staleness detection | **Both**: deterministic health checks + Claude news-driven flags |
| 5 | Scheduling | **New launchd plist** `com.arbiter.monday.plist`, 08:00 Monday, one-shot |
| 6 | Anthropic dependency | **Reuse existing** `ANTHROPIC_API_KEY` (already in `.env`); reuse MiroFish's `AnthropicLLM`/`FakeLLM` pattern |

## 3. Context / existing building blocks

- **`A3.news` already feeds the engine every cycle** (`arbiter/engine/_engine.py:501`,
  `_gather_a3_opinions`): per-ticker Finnhub company-news + sentiment → probationary,
  `EQUAL_FLOOR`, learning-governed opinions sized SMALL, fail-closed. So per-trade news
  → trades is **already done**; the Monday scan surfaces/refreshes it, it does not
  re-implement it.
- **`A3` has no macro channel.** Macro market news is genuinely new signal → the new
  `A4.macro` advisor.
- **Finnhub client** (`arbiter/ingest/finnhub/client.py`): `get_company_news`,
  `get_news_sentiment`. Already wired, deterministic, rate-limited.
- **Alerting** (`arbiter/safety/alerting.py`): `Alerting.alert(tier, msg, ctx, as_of=)`
  writes audit + posts the ntfy webhook **only when `tier == "critical"`**. The digest
  therefore needs a dedicated push path (see §5.4), not `alert()`.
- **Anthropic**: `ANTHROPIC_API_KEY` is already in `arbiter/.env`; the `anthropic`
  SDK (0.111.0) is installed in the shared venv; MiroFish already calls Claude via
  `AnthropicLLM` (`mirofish/llm.py`) with a `FakeLLM` twin for hermetic tests.
  Arbiter's `config.py` does **not** yet read the key — we add a field.
- **Data sources that can go stale**: `arbiter/data/fund_managers.py` (CIKs),
  `arbiter/data/activist_filers.py`, the shared watchlist, `arbiter/data/sectors.py`.
- **Daemon** is launchd KeepAlive and long-sleeps while the market is closed; 08:00 is
  pre-market, so a separate one-shot process is the cleanest trigger. DB writes are
  covered by the existing WAL + `busy_timeout`.

## 4. Architecture

New package `arbiter/refresh/` with one orchestrator and four small, independently
testable units. New CLI command `arbiter monday-refresh`.

```
arbiter/refresh/
  __init__.py
  orchestrator.py     # run_monday_refresh(engine, as_of) -> RefreshReport
  position_news.py    # PositionNewsScan: Finnhub per open position
  macro_scan.py       # MacroScan: Claude web_search → market news + staleness flags
  source_health.py    # SourceHealthScan: deterministic staleness checks
  digest.py           # build_digest(report) -> markdown; push_digest(...)
  llm.py              # LLM protocol + AnthropicLLM + FakeLLM (mirrors mirofish/llm.py)
  types.py            # RefreshReport, MacroFinding, PositionFinding, StaleSource, Severity
```

### 4.1 Orchestrator flow (`run_monday_refresh`)

```
as_of = engine.clock.now()
positions = engine.open_positions()                 # tickers held

pos      = position_news.scan(positions, as_of)     # fail-closed, Finnhub
macro    = macro_scan.scan(positions, as_of, llm)   # fail-closed, Claude web_search
health   = source_health.scan(engine, as_of)        # fail-closed, deterministic
health   = health.merge(macro.stale_flags)          # union heuristic + news-driven

# Feed the engine (each guarded, never aborts the refresh):
feed_macro_advisor(engine, macro.findings, as_of)   # → A4.macro opinions (probationary)
for src in health.confirmed_stale:
    run_ingest(engine.config, sources=[src.source], ...)   # refresh that source only

report = RefreshReport(as_of, pos, macro, health, fed=..., reingested=...)
digest = digest.build_digest(report)                # markdown
save(digest, data/monday-refresh-YYYY-MM-DD.md)
digest.push_digest(engine.config, report)           # ntfy to phone
return report
```

Every scan is independent and fail-closed: one failing does not abort the others, and
**nothing here can abort or block the trading daemon** (separate process; engine-feed
calls are wrapped and swallow exceptions with a logged warning).

### 4.2 `position_news.py`

For each open-position ticker: `get_company_news(ticker, from=as_of-7d, to=as_of)` +
`get_news_sentiment(ticker)`. Produce a `PositionFinding` per ticker (headlines,
sentiment score, a severity heuristic from sentiment magnitude + recency). Finnhub
absent/empty/error → section marked "unavailable", refresh continues. **No new
engine wiring** — `A3.news` already consumes this channel on Monday's first cycle; this
unit surfaces it for the digest and confirms freshness.

### 4.3 `macro_scan.py`

One `LLM.analyze(prompt, tools)` call (`AnthropicLLM` → Claude). Returns structured
findings the unit parses defensively:

- **Market findings**: list of `{summary, severity, affected_tickers[], sources[]}` —
  what moved / will move the broad market this week and which held tickers it could hit.
- **Staleness flags**: list of `{source, reason, sources[]}` — event-driven rot the
  deterministic checks can't see ("Icahn wound down his fund" → flag `activist_filers`).

LLM config (grounded against the claude-api skill):
- Model: **`claude-opus-4-8`** (intelligence-sensitive weekly synthesis; one call/week,
  cost negligible). Configurable via `REFRESH_MODEL` env, default `claude-opus-4-8`.
- Tool: `{"type": "web_search_20260209", "name": "web_search"}` (opus-4-8 supports the
  dynamic-filtering variant; do **not** also declare `code_execution`).
- `thinking: {"type": "adaptive"}` (no `budget_tokens` — removed on opus-4-8).
- `max_tokens` ~8000; handle `stop_reason == "pause_turn"` by re-sending the
  assistant turn until `end_turn` (server-tool sampling loop), with a
  `max_continuations` cap (e.g. 5).
- Output: instruct the model to emit a single fenced JSON block; parse it defensively
  (mirrors MiroFish's parse/abstain rules). No `output_config.format` (avoid combining
  structured-output constraints with server tools).
- No key / SDK error / unparseable → degrade gracefully: macro section "skipped
  (no key)" or "unavailable", `stale_flags = []`. Refresh and digest still complete.

`FakeLLM` returns a canned structured response so the whole unit is hermetically
testable with no network (mirrors `mirofish/llm.py`).

### 4.4 `source_health.py` (deterministic)

Checks, each producing a `StaleSource{source, reason, confirmed: bool}`:

- **Fund-manager CIKs** (`fund_managers.py`): each CIK still returns a recent 13F via
  EDGAR submissions JSON; flag if newest 13F older than threshold.
- **Watchlist tickers**: each still trades (Alpaca asset lookup / last bar present).
- **Per-source last-ingest age**: query the DB for newest row per source vs. threshold.
- **Endpoint reachability**: EDGAR / Finnhub / Alpaca reachable (cheap HEAD/ping).

`confirmed_stale` = deterministic confirmations ∪ Claude flags that match a known
source. Network errors degrade to "unknown", never to a false "stale".

### 4.5 `digest.py`

`build_digest(report)` → markdown report (MARKETS / OPEN TRADES / DATA SOURCES /
actions taken). Saved to `data/monday-refresh-YYYY-MM-DD.md`.

`push_digest(config, report)` posts the digest to the user's phone via ntfy. Since
`Alerting.alert()` only POSTs the webhook on `tier=="critical"`, add a new public
`Alerting.notify(title, body, *, as_of)` method that always posts the webhook (info
tier) and writes an audit row — reusing the existing `_post_webhook` internally. The
digest always sends, even when a sub-scan failed (the failure is reported in its
section).

> NOTE: auto-pause on a HIGH-severity hit was explicitly **out of scope** (the user
> chose "feed engine", not "auto-pause"). Severity is reported but does not pause the
> engine.

### 4.6 `A4.macro` advisor (the one place touching trades)

Mirrors `A3.news` exactly so it inherits all safety machinery:

- Registered probationary at import (like `arbiter/adapters/a3/pipeline.py`,
  `ADVISOR_ID = "A4.macro"`), `EQUAL_FLOOR` trust, sized SMALL.
- `feed_macro_advisor` converts each market finding with `affected_tickers` into an
  `A4.macro` opinion (direction from severity/sentiment, confidence bounded low),
  persisted so it fuses and is governed by the trust/learning loop — **it cannot
  dominate until it earns trust**, and the calibrator/graduation gates apply unchanged.
- Config knobs mirror A3 (`A4_MIN_*`, `A4_WEIGHT_*`, `A4_ADVISOR_ID`) with conservative
  defaults. Inert when no Anthropic key (no findings → no opinions).

## 5. Scheduling

New `deploy/com.arbiter.monday.plist` (mirrors `deploy/com.arbiter.daily.plist`):

- `ProgramArguments`: `<venv>/bin/python -m arbiter.cli monday-refresh`
- `StartCalendarInterval`: `Weekday=1, Hour=8, Minute=0`
- `RunAtLoad=false`, `KeepAlive=false` (one-shot)
- `WorkingDirectory`, `StandardOutPath`/`StandardErrorPath` under `data/` (must exist)

Installed via the existing `scripts/schedule.sh` pattern. 08:00 is pre-market while the
daemon long-sleeps; brief DB-write contention is covered by WAL + `busy_timeout`.

## 6. Config additions

- `anthropic_api_key: str = ""` in `config.py` (reads `ANTHROPIC_API_KEY`; secret-
  redacted in repr, added to `_SECRET_FIELDS`).
- `refresh_model: str = "claude-opus-4-8"` (reads `REFRESH_MODEL`).
- `A4_*` advisor knobs mirroring the existing `A3_*` set.

## 7. Failure model

| Failure | Behavior |
|---------|----------|
| Finnhub down | Position section "unavailable"; refresh continues |
| No Anthropic key / Claude error | Macro section "skipped/unavailable"; `stale_flags=[]`; digest still sends |
| EDGAR/Alpaca unreachable | Affected health checks → "unknown" (never false "stale") |
| Engine-feed raises | Caught + logged; digest still built and pushed |
| Re-ingest raises | Caught + logged per source; others still run |
| ntfy push fails | Logged; the saved `data/monday-refresh-*.md` is the durable record |

Nothing in the Monday refresh can abort, pause, or block the trading daemon.

## 8. Testing

- Hermetic: `FakeLLM` + a fake Finnhub client + an in-memory DB. No network in tests
  (consistent with the conftest hermeticity guard).
- Unit tests per scan (success, empty, error paths), the orchestrator (one scan failing
  doesn't abort the rest), digest rendering (snapshot), `A4.macro` opinion shaping +
  probationary registration, plist validity, and the new config fields/redaction.

## 9. Out of scope (YAGNI)

- Auto-pause on HIGH severity (explicitly declined).
- Any macro path that bypasses the trust/learning loop.
- A new web dashboard/cockpit panel for the digest (the saved markdown + phone push are
  the deliverable; cockpit integration can be a later, separate spec).
