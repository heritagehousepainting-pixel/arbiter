# C4 — Market-Data Source Fallbacks — Audit

**Lane:** C4 (market-data sourcing & fallback behavior)
**Date:** 2026-06-19
**Auditor:** READ-ONLY (no source/test/config modified)
**Scope:** `arbiter/arbiter/data/sources/` (Alpaca primary, Stooq fallback), `build_price_gateway`,
`feed=iex` handling, the live-run "Stooq returned 'bars not found'" issue. Fallback ordering & triggers,
silent-degrade vs surface, whether Stooq actually works, partial-data, retry/timeout, stale-vs-fail-closed,
rate-limit handling, and cross-source data-quality risk. PIT eligibility math is B1's lane — flagged where overlapping.

**Files reviewed:**
- `arbiter/data/sources/_gateway.py` (137 lines) — `_FallbackPriceAdapter`, `build_price_gateway`
- `arbiter/data/sources/alpaca.py` (259 lines) — `AlpacaPriceSource`
- `arbiter/data/sources/stooq.py` (287 lines) — `StooqPriceSource`
- `arbiter/data/sources/__init__.py` (28 lines)
- `arbiter/data/adv.py` — `register_adv_source`, `_ADVSource`, `_extract_dollar_volume` (ADV consumes price_close source)
- `tests/data/test_sources.py` (45 tests, all pass via `.venv/bin/python -m pytest`)
- `INTERFACES.md` §3 (PriceSource protocol), §11 conventions, `arbiter/engine.py` (ticker flow), `arbiter/config.py`

---

## VERDICT

**FAIL (ship-blocking).** The fallback *mechanism* (ordering, fail-closed, exception isolation) is correctly
built and well-tested. But the fallback is **non-functional in production**: Alpaca and the Stooq fallback are
handed the **same bare ticker** (e.g. `"AAPL"`), while Stooq requires an exchange-suffixed symbol (`"AAPL.US"`).
This is the documented root cause of the live-run "Stooq returned 'bars not found'": Stooq is effectively
**dead weight** today — every fallback attempt 404s. Worse, the failure is **silent** (`log.info`, returns
`None`), so an Alpaca outage degrades straight to fail-closed (size 0) with no fallback coverage and no alert.
Separately, ADV via the production source path is structurally broken (sources return a scalar close, not a
`Bar`, so dollar-volume collapses to ~price) — straddles B1 but is rooted in the source-adapter contract.
Tests pass only because every Stooq test hard-codes `.US` symbols, masking the integration gap.

---

## FINDINGS

### P0 — Stooq fallback never receives a valid symbol → fallback is dead weight in production — `arbiter/data/sources/_gateway.py:117-119` + `arbiter/data/sources/stooq.py:188-211`
**Why:** `build_price_gateway` registers ONE `_FallbackPriceAdapter(primary=alpaca, secondary=stooq)` for
`price_open`/`price_close`/`spread`. `PITGateway.get(field, ticker, as_of)` passes the **same ticker string**
to both. Alpaca uses bare US tickers (`"AAPL"`); Stooq's CSV endpoint requires an exchange suffix
(`"AAPL.US"`) — confirmed by every test/docstring in this codebase using `.US` (`stooq.py:144,166`;
`test_sources.py:377,394,404,639,711,731`). There is **no ticker translation layer anywhere** in
`arbiter/` (grep for `.US` / `to_stooq` / `stooq_symbol` finds none). So when Alpaca returns `None`, the
fallback calls Stooq with `"AAPL"`, Stooq returns the `bars not found` / empty CSV, the adapter returns
`None`. This is exactly the reported live symptom. **Net effect: the Stooq fallback has never worked in
production and provides zero resilience** — the system is single-sourced on Alpaca despite advertising a
fallback.
**Recommended action:** Introduce a symbol-mapping seam at the fallback boundary (e.g. an `_alpaca_to_stooq(ticker)`
that appends `.US` for US equities, with overrides for non-US/edge symbols). Map at the point Stooq is
called, not at gateway construction. Add an integration test that drives the *gateway* (not Stooq directly)
with a bare ticker and asserts the Stooq leg receives a suffixed symbol. Until fixed, the "primary=alpaca
fallback=stooq" log line at `_gateway.py:133-136` is misleading.

### P1 — Silent degradation: primary failure logs at WARNING and falls to fail-closed with no surfaced signal — `arbiter/data/sources/_gateway.py:52-81`
**Why:** When Alpaca raises (timeout, 5xx, 429), the adapter swallows it (`except Exception` → `value = None`,
`log.warning`) and tries Stooq. Given the P0 mismatch, Stooq also yields `None`, so the adapter returns
`None`. Downstream (`adv.py:81-87`, INTERFACES §9/§11 conv. 4) treats `None` as fail-closed → **size 0**. The
only trace is a log line; there is **no counter, no alert, no degraded-mode flag** distinguishing "no data
exists for this ticker/date" (legitimate `None`) from "both data sources are down" (operational outage). A
broad Alpaca outage would silently stop the system from sizing any position while looking healthy. This is
the dangerous failure mode: the system cannot tell "fail-closed because no signal" from "fail-closed because
blind."
**Recommended action:** Have `_FallbackPriceAdapter` surface a metric/structured event when the **primary
raised** (distinct from primary returning a legitimate `None`), and when **both legs failed**. Wire a
threshold alert (e.g. N consecutive primary failures within a window → operator notification). Consider a
distinct sentinel/exception for "source unavailable" vs "no data" so the policy layer can choose halt-vs-size-0
deliberately rather than collapsing both to `None`.

### P1 — ADV via production sources is structurally wrong: `get_pit` returns a scalar close, but `adv.py` expects a `Bar` to compute price×volume — `arbiter/data/sources/alpaca.py:249-250` / `stooq.py:278-279` vs `arbiter/data/adv.py:148-167`
**Why:** `register_adv_source` (`_gateway.py:131`) registers `_ADVSource`, which computes ADV by probing
`pit.get("price_close", ticker, day)` day-by-day. `adv.py:_extract_dollar_volume` returns `close*volume`
**only when the value is a `Bar`**; for a numeric scalar it returns `float(value)` treated as *pre-computed
dollar volume*. But the production source adapters (`alpaca.get_pit`/`stooq.get_pit`) return `latest.close`
— a **bare float price**, never a `Bar`. So in production ADV = mean(close) ≈ a few hundred dollars, instead
of mean(close×volume) ≈ millions. `register_adv_source`'s own docstring (`adv.py:213-214`) explicitly states
the `price_close` source "returns Bar objects" — the production sources violate that documented contract.
Liquidity/ADV gating and any ADV-based position cap is therefore meaningless. (PIT/eligibility math is B1's
lane, but the **defect is rooted in the source-adapter return contract**, hence flagged here for coordination
with B1.) Tests pass because ADV tests use `make_adv_fixture_pit` which registers real `Bar` objects.
**Recommended action:** Coordinate with B1. Either (a) make `get_pit("price_close", ...)` return the `Bar`
(so `_extract_dollar_volume` hits the correct branch — but verify no other consumer assumes a scalar), or
(b) give `_ADVSource` direct access to `source.bars(...)` so it computes `close*volume` from full bars
instead of routing through the scalar `price_close` field. Add a production-path ADV test that registers the
real adapters (not the fixture) and asserts dollar-volume scale.

### P2 — No retry / backoff on either source; a single transient failure degrades or fail-closes — `arbiter/data/sources/alpaca.py:158-166`, `arbiter/data/sources/stooq.py:195-207`
**Why:** Both sources make exactly one HTTP attempt. `httpx.RequestError` / `requests.RequestException`
(connection reset, DNS, read timeout) → immediate empty list. No `tenacity`, no backoff, no `sleep`,
`max_attempts` anywhere (grep confirms). A momentary network blip on Alpaca therefore drops straight to the
(currently broken) Stooq leg, then to fail-closed. For daily-bar data that is not latency-sensitive, a single
bounded retry with jitter would absorb most transient failures cheaply.
**Recommended action:** Add a small bounded retry (1–2 attempts, exponential backoff + jitter) around the GET
in each source, scoped to transient/`RequestError` and idempotent reads. Keep it short to respect cycle timing.

### P2 — Rate-limit (HTTP 429) and 5xx re-raise as exceptions, get swallowed by the adapter, and are indistinguishable from "no data" — `arbiter/data/sources/alpaca.py:177` (`raise_for_status`) → `_gateway.py:54-61`
**Why:** Alpaca 429/5xx are NOT in the soft 404/422 set (`alpaca.py:169`), so `raise_for_status()` raises
`HTTPStatusError`, which the adapter catches as a generic `Exception` and converts to `None`. There is no
`Retry-After` honoring, no token-bucket / request pacing, and no distinction between throttling and missing
data. Under burst load (many tickers per cycle), Alpaca's IEX free tier rate limits will appear as silent
fail-closed across many tickers at once.
**Recommended action:** Detect 429 explicitly, log it distinctly (not merged into the generic fallback
warning), honor `Retry-After`, and add basic client-side pacing if cycles fan out across many tickers.
Surface a rate-limit counter to the same alerting added in the P1 silent-degradation finding.

### P2 — Cross-source data-quality drift: Alpaca uses `adjustment=split` (IEX feed); Stooq adjustment policy is unspecified and uncontrolled — `arbiter/data/sources/alpaca.py:106,146` vs `arbiter/data/sources/stooq.py:42,188-193`
**Why:** Alpaca requests IEX bars with `adjustment=split` (split-adjusted, **not** dividend-adjusted) on the
free `iex` feed (`alpaca.py:103-106`). Stooq's CSV download (`d/l/?s=...&i=d`) has its **own, separate
adjustment convention** that the code neither selects nor normalizes; Stooq daily downloads are commonly
already split-and-dividend adjusted. So if/when the fallback is actually fixed (P0), the same ticker's
`price_close`/`spread` can silently come from two sources with **different adjustment bases** depending on
which one answered — a discontinuity in the price series that corrupts spread estimates and any return/beta
math built on top. IEX-only also means thinner coverage and occasional gaps vs SIP, which is precisely when
the (broken) fallback is supposed to kick in.
**Recommended action:** Document and pin the adjustment basis for BOTH sources and make them match (or
explicitly reconcile). When the fallback returns a Stooq value, tag the provenance so downstream can detect
mixed-source series. Add a sanity check (e.g. Stooq-vs-Alpaca close within tolerance on overlapping dates) to
catch adjustment mismatches before they reach sizing.

### P3 — `spread` is a synthetic intraday-range proxy `(high-low)/close`, not a real bid-ask spread, on both sources — `arbiter/data/sources/alpaca.py:253-257`, `arbiter/data/sources/stooq.py:282-285`
**Why:** Both adapters return `(high-low)/close` as "spread". Daily high-low range is typically far larger and
behaves differently than a true quoted bid-ask spread; `model_slippage(price, spread)` (INTERFACES §3) will
systematically over-state slippage. Not a fallback bug per se, but a sourcing-quality risk that compounds with
the cross-source drift in the P2 finding (the proxy differs further once Stooq vs Alpaca OHLC bases diverge).
**Recommended action:** Rename/document this as a range-based proxy, and if a true spread matters for sizing,
source quotes (Alpaca latest-quote endpoint) rather than deriving from daily OHLC. At minimum, ensure both
sources compute the proxy identically (they do today) and note the over-estimate bias to the policy lane.

### P3 — Stooq returns `[]` and `log.info` on 404/empty, never raising — outage looks identical to "ticker has no data" — `arbiter/data/sources/stooq.py:209-218`
**Why:** Stooq maps 404 → `log.info("stooq_bars_not_found")` → `[]`, and empty body → `[]`. Combined with the
adapter swallowing exceptions, there is no path by which a *Stooq* problem (vs a legitimately empty range)
ever surfaces above DEBUG/INFO. The live-run "bars not found" message was an INFO line — easy to miss, and as
P0 shows it actually signaled a systemic symbol-format bug, not a missing ticker. Stooq does at least `raise`
on unexpected 5xx (`raise_for_status` at `stooq.py:214`), which the adapter then swallows.
**Recommended action:** Once P0/P1 alerting exists, escalate repeated `stooq_bars_not_found` for tickers that
Alpaca *does* know about (a strong signal of the symbol-mapping bug) rather than logging at INFO and moving on.

---

## STALE vs FAIL-CLOSED (explicit check)

The system **fails closed, never stale**. Neither source caches; each `get_pit` does a fresh fetch and returns
`None` when no eligible bar exists. The fallback adapter returns `None` when both legs fail (`_gateway.py:81`),
and downstream sizes to 0 (INTERFACES §11 conv. 4). There is **no stale-data return path** — good for PIT
purity, but it means an outage produces silent total fail-closed (see P1) rather than degraded-but-trading.

## PARTIAL-DATA HANDLING (explicit check)

- Alpaca: paginates via `next_page_token` (`alpaca.py:151-189`); per-bar look-ahead guard `timestamp < end`
  (`alpaca.py:184`); malformed bars would raise in `_parse_bar` (no per-row try/except) — a single bad bar
  in a page would propagate as an exception and be swallowed to `None` by the adapter. Lower-severity but worth
  noting: Stooq tolerates bad rows (skips them, `stooq.py:123-125`) while Alpaca does not.
- Stooq: per-row `try/except` skips bad/`"no data"` rows (`stooq.py:91-125`), validates required headers
  (`stooq.py:76-83`). More robust to partial data than Alpaca.

---

## OPPORTUNITIES TO ADD

1. **Symbol-mapping seam (fixes P0) + provenance tag.** A single `to_stooq_symbol()` helper plus a `source`
   tag on returned values would both repair the fallback and enable cross-source drift detection (P2).
2. **Source-health metrics + alerting.** Counters for: primary-raised, both-failed, 429s, Stooq-404-for-known-ticker.
   This is the single highest-leverage add — it converts the current silent fail-closed into an observable signal.
3. **Provider self-test on startup.** `build_price_gateway` could fetch a known liquid ticker (e.g. AAPL) for a
   recent date through BOTH legs and assert non-`None`, catching the P0 symbol bug and credential/feed (403 on
   `sip`) problems before the live loop starts.
4. **Production-path ADV test.** Register the real adapters (not `make_adv_fixture_pit`) and assert ADV is at
   dollar-volume scale — would have caught P1.
5. **Gateway-level integration test with bare tickers.** Every current test injects `.US` into Stooq directly;
   none drives `PITGateway.get` with a bare ticker through the fallback. Add one — it is the test that fails today.
6. **Bounded retry/backoff + 429 `Retry-After`** in both sources (P2 findings), kept short to respect cycle timing.
7. **Reconcile adjustment bases** (split vs split+dividend) across Alpaca and Stooq, or restrict the fallback to
   delisted/no-data cases only and never let it answer for an actively-traded ticker Alpaca already covers.
