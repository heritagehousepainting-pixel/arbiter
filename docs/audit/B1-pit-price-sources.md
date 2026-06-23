# Audit — Lane B1: PIT gateway & historical price sources

- Auditor lane: B1 (PIT gateway & price sources)
- Date: 2026-06-19
- Scope: `arbiter/data/pit.py`, `arbiter/data/sources/alpaca.py`, `arbiter/data/sources/stooq.py`, plus the adjacent wiring (`data/sources/_gateway.py`, `data/adv.py`) and the two production consumers of the price path (`engine.py`, `execution/exit_monitor.py`) needed to judge the historical-price-path correctness.
- Mode: READ-ONLY. No source/test/config modified. Only this file written.
- Verdict: **FAIL — 1×P0, 2×P1.** The PIT eligibility *plumbing* (look-ahead guard, fail-closed-on-missing, tz handling, fallback) is largely sound, but two correctness defects make the historical price path produce wrong numbers in production: (1) the ADV liquidity cap is silently wrong by ~6 orders of magnitude because of a Bar-vs-scalar contract mismatch, and (2) daily OHLC fields (`price_close`/high/low) are treated as "known" at the bar's open-aligned midnight timestamp, leaking same-day close/high/low into reads taken before the session ended.

---

## Findings

### [P0] — Production ADV computed from close PRICE, not dollar VOLUME (Bar-vs-scalar contract mismatch) — arbiter/data/adv.py:120 + arbiter/data/sources/_gateway.py:123 + arbiter/data/sources/alpaca.py:249

**Why.** `adv.py::_fetch_dollar_volumes` calls `pit.get("price_close", ticker, day)` and assumes the value is either a `Bar` (→ `close * volume`) or a *pre-computed dollar-volume scalar* (test fixtures). But in production, `build_price_gateway` (`_gateway.py:123`) registers `price_close` to the `_FallbackPriceAdapter`, whose `get_pit` delegates to `AlpacaPriceSource.get_pit` / `StooqPriceSource.get_pit` — and those return a **bare scalar close price** (`alpaca.py:249-250` returns `latest.close`; same in `stooq.py:278-279`). They never return a `Bar`. So `_extract_dollar_volume` hits the scalar branch and treats the *close price* (~$115) as if it were the day's *dollar volume*. ADV ends up ~= mean close price instead of mean(price×volume), i.e. wrong by roughly 6–8 orders of magnitude.

Reproduced (no network) with a scalar-close source mimicking the Alpaca adapter:
```
adv via scalar source = 115.5   # should be ~1.1e8 (price ~115 × volume 1e6)
```
Because `adv_20d` feeds the Lane-12 ADV position-size cap, an ADV that is ~$115 instead of ~$115M means the liquidity cap effectively *never binds* — the safety limit that prevents oversizing illiquid names is silently disabled in production. Note: every unit test passes because tests use `make_adv_fixture_pit` (registers real `Bar` objects) — the tests never exercise the production scalar adapter, so the gap is invisible to CI.

Second, related defect in the same path: the carry-forward dedup guard (`adv.py:123-134`) only runs for `Bar` instances. With the production scalar source, `FixtureSource`/adapter "carry-forward most-recent" semantics are *not* deduped, so the most-recent close is also re-counted across multiple probe days — a second source of corruption on top of the units error.

**Recommended action.** Make the `price_close` contract unambiguous on the production path. Cleanest: give `AlpacaPriceSource`/`StooqPriceSource` a way to expose the full `Bar` for `price_close` (e.g. an `adv`-dedicated source or a `bars`-based `_ADVSource` that reads OHLCV directly rather than going through the scalar `price_close` field). Alternatively register a distinct internal field (e.g. `_close_bar`) that returns `Bar` and have `_ADVSource` read that. Until fixed, the ADV cap should be treated as non-functional. Add a production-path ADV test that registers the real Alpaca/Stooq adapter (with a stubbed `bars()`) so the scalar/Bar mismatch is caught by CI.

---

### [P1] — Daily OHLC close/high/low leak as "known" at the bar's open-aligned midnight timestamp (same-day look-ahead) — arbiter/data/sources/alpaca.py:243-256 (and stooq.py:272-285)

**Why.** Alpaca daily bars are timestamped at the session open (`T00:00:00Z`). The PIT eligibility guard is `bar.timestamp <= as_of` (`alpaca.py:243`, `stooq.py:272`). That guard is correct for `price_open` (the open *is* known at the open). But the same eligible `Bar` is also used to answer `price_close`, `spread` (high−low), via `latest.close` / `latest.high`/`latest.low`. Those values are **not known until the session closes** at end of day T, yet a read with `as_of = T00:00:00Z` (or any intraday `as_of` on day T) returns them.

Reproduced (no network):
```
as_of=2026-01-15T00:00:00Z  open=10.0  close=10.5   # close is end-of-day-15, not yet known at midnight
as_of=2026-01-15T14:30:00Z  open=10.0  close=10.5   # still leaks the not-yet-realized close mid-session
```
This is a genuine point-in-time leak. It directly affects the two production consumers in scope-adjacent code: `exit_monitor.py:616` reads `price_close` (then `price_open`) at `now` for the stop-check fallback, and `engine.py:1063` reads `spread` at `now`. In a backtest where the clock's `now` lands on day T before T's close, both read day T's realized close/high/low — future information. (`price_open` alone is safe; the leak is specifically the close/high/low-derived fields sharing the open-timestamped bar.)

Note this is the precise "off-by-one allowing same-day-close as an open" risk the lane was asked to verify: the guard does not distinguish "open is known at the open timestamp" from "close/high/low are only known at the close." The `as_of + 1 day` window-end comment (`alpaca.py:230-236`) defends only the *open* eligibility and explicitly intends midnight-aligned daily bars to be included — which is exactly what makes the close leak.

**Recommended action.** Treat OHLC fields with distinct eligibility. For `price_close`/`spread`/high/low derived from a daily bar timestamped at the open, the bar should only be eligible when `as_of >= bar.timestamp + session_length` (or, conservatively, `as_of` is on a strictly later calendar day, i.e. `bar.date() < as_of.date()`), while `price_open` may use `bar.timestamp <= as_of`. Equivalent: store the close under a close-aligned timestamp (T close, ~T20:00–21:00Z) inside the source so the existing `<= as_of` guard does the right thing for all fields. This also makes the `spread` derivation honest.

---

### [P1] — `spread` proxied from (high−low)/close is a daily-range proxy, not a bid/ask spread, and shares the same-day leak — arbiter/data/sources/alpaca.py:253-256 (stooq.py:282-285)

**Why.** `get_pit("spread", ...)` returns `(latest.high - latest.low) / latest.close`. That is the **intraday range**, which is typically many multiples of the true bid/ask spread used by the slippage model (`model_slippage` = 5bps + 0.5×spread, INTERFACES.md §10b.3). Feeding a daily range where a half-spread is expected systematically over-penalizes fills (slippage inflated by the full day's high-low swing). It also inherits the P1 look-ahead above (high/low not known until session close). The engine's default when `spread` is `None` is `0.01` (`engine.py:1062`) — a flat 1.0, not 1%, magnitude that is itself arbitrary; combined with the range-proxy this means slippage is either ~0.01 (huge if interpreted as price units) or a full-range proxy, never an actual spread.

**Recommended action.** Document explicitly that `spread` is a daily-range *proxy*, and either (a) scale it down to approximate a half-spread, or (b) source a real quote-based spread for the live/`alpaca_paper` path and reserve the range proxy for backtest only. At minimum, gate it behind the same close-eligibility fix as the P1 above so it stops leaking. Confirm the `0.01` default's intended units in `engine.py:1062`.

---

### [P2] — Alpaca pagination mutates a shared `params` dict; `page_token` is never cleared between... (latent) — arbiter/data/sources/alpaca.py:141-189

**Why.** `params["page_token"] = next_page_token` is set inside the loop on the shared `params` dict. Within a single `bars()` call this is fine (the token is overwritten each iteration and the loop ends when the token is falsy). It is not a live bug today because `params` is rebuilt per call. Flagging as low-risk: if this method is ever refactored to reuse `params` across calls, a stale `page_token` would silently corrupt the next fetch. Cheap to harden.

**Recommended action.** Pop/clear `page_token` when `next_page_token` is falsy, or build the per-page params dict fresh each iteration.

---

### [P2] — Fallback adapter swallows ALL exceptions including transient/auth/5xx, masking outages as "no data" — arbiter/data/sources/_gateway.py:52-81

**Why.** `_FallbackPriceAdapter.get_pit` catches `except Exception` on both primary and secondary, downgrading any failure (auth 401, rate-limit 429, 5xx, network) to `None`. The individual sources already fail-soft on 404/422 and re-raise on unexpected status (`alpaca.py:177` `raise_for_status`); the adapter then catches that re-raise and turns a *systemic outage* into the same signal as *legitimately no data for this ticker*. Downstream this fails-closed (sizes to 0), which is safe for capital, but it means a full-blown data-provider outage is indistinguishable from "this ticker has no history" — no alert, no breaker. For a system whose core promise is PIT correctness, silent total-blackout is a monitoring blind spot.

**Recommended action.** Narrow the catch (or at least distinguish): let auth/5xx/429 surface a counter/log at WARNING+ that an operator/breaker can watch, while still falling back. Keep fail-closed for sizing, but don't make a provider outage invisible.

---

### [P3] — `Bar.timestamp` docstring ambiguity ("close or open, depending on convention") — arbiter/data/pit.py:42-43

**Why.** The `Bar.timestamp` field doc says "Bar close (or open, depending on convention)." Given the P1 finding, this ambiguity is the root cause: nothing pins down what a daily bar's timestamp means, so the open-aligned Alpaca convention silently makes close reads leak. Pin the convention down in the dataclass.

**Recommended action.** State the convention explicitly (daily bars are open-aligned `T00:00:00Z`) and cross-reference the close-eligibility rule.

---

### [P3] — `_parse_bar` requires keys o/h/l/c/v with `float(raw[...])`; a malformed/partial bar raises KeyError/ValueError inside the page loop — arbiter/data/sources/alpaca.py:55-78

**Why.** Stooq's parser skips bad rows defensively (`stooq.py:123` catches `ValueError, KeyError`), but Alpaca's `_parse_bar` does not — a single malformed bar in a page aborts the whole `bars()` call with an unhandled exception that propagates up through `get_pit` and is then swallowed by the fallback adapter as `None`. Inconsistent robustness between the two sources, and the failure mode is opaque.

**Recommended action.** Mirror Stooq's per-row skip-and-log in `_parse_bar`/the Alpaca loop so one bad bar doesn't blank out an otherwise-good fetch.

---

## OPPORTUNITIES TO ADD

- **Production-path PIT tests.** Every existing ADV/price test uses `FixtureSource`/`make_adv_fixture_pit`, which is why the P0 scalar/Bar mismatch slipped through. Add tests that register the *real* `AlpacaPriceSource`/`StooqPriceSource` (or `_FallbackPriceAdapter`) with a stubbed `bars()` and assert ADV/close/open/spread values — this is the missing coverage that would have caught both P0 and the P1 leak.
- **A single explicit close-eligibility helper.** Both sources duplicate the identical `get_pit` window/eligibility block (`alpaca.py:222-259` ≈ `stooq.py:251-287`). Extract one shared helper that encodes the open-vs-close eligibility rule once, so the P1 fix lands in exactly one place and the two sources can't drift.
- **Assert tz-awareness at the boundary.** `_to_utc` silently *assumes* naive datetimes are UTC (`alpaca.py:48`, `stooq.py:55`). A naive local-time `as_of` would be mislabeled UTC and shift the eligibility window by hours. Consider rejecting naive datetimes (or logging) at the PIT boundary rather than coercing.
- **Distinguish "no data" from "outage."** Tie the P2 outage-masking fix to a health counter/breaker so the daemon can pause rather than silently size everything to 0 during a provider blackout.
- **Stooq weekend/holiday `d2` rounding.** `d2 = end_utc` formatted as `YYYYMMDD` (`stooq.py:191`); since `window_end = as_of + 1 day` in the `get_pit` adapter, confirm Stooq's inclusive `d2` semantics don't pull a bar dated on the `as_of+1` day that the in-parser `bar_date >= end_utc` guard then has to drop — works today but is a second guard doing the first guard's job.
