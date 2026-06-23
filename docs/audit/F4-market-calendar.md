# F4 — Market-Calendar Correctness Audit

**Lane:** F4 — market-calendar correctness (READ-ONLY)
**Auditor:** automated audit pass
**Date:** 2026-06-19
**Scope:** `arbiter/arbiter/runtime/market_calendar.py`, `arbiter/arbiter/data/replay_clock.py`
**Verdict:** ⚠️ **FAIL** — two real holiday-list errors produce wrong-but-confident "market open" answers from the OFFLINE calendar. One of them (Juneteenth) is live *today* (2026-06-19 is Juneteenth, a Friday — NYSE closed, offline calendar says trading day). DST, early-close, cache-invalidation, and is_open boundary logic are all **correct**.

---

## Findings

### P0 — Juneteenth missing from holiday list → offline calendar trades on a closed exchange — `data/replay_clock.py:53-126` (`_KNOWN_CLOSURES`)
**Why:** NYSE has observed Juneteenth (June 19, or the observed weekday) as a full closure since 2022. It appears nowhere in `_KNOWN_CLOSURES` and is not a `_FIXED_HOLIDAYS` entry. Verified: `_is_trading_day(date(2026,6,19)) == True`, `date(2023,6,19)`, `date(2024,6,19)`, `date(2025,6,19)` all return `True`. 2022 only "passed" by coincidence (2022-06-19 was a Sunday). **This is live right now:** today is 2026-06-19, a Friday — NYSE is closed for Juneteenth, but `OfflineMarketCalendar.session()` reports `is_open` on the regular schedule and computes `next_open`/`next_close` as if it were a normal session. Any scheduling/stop/horizon decision taken on the offline fallback today is wrong.
**Recommended action:** Add Juneteenth observed closures (2022-06-20, 2023-06-19, 2024-06-19, 2025-06-19, 2026-06-19, and forward) to `_KNOWN_CLOSURES`. Add a regression test asserting `_is_trading_day` is `False` for each.

### P1 — Veterans Day wrongly marked as a market closure → offline calendar reports CLOSED on an OPEN session — `data/replay_clock.py:46` (`_FIXED_HOLIDAYS` entry `(11, 11)`)
**Why:** The NYSE is **open** on Veterans Day (it is a federal holiday but not an exchange holiday). `_FIXED_HOLIDAYS` includes `(11, 11)` "for safety," so `_is_trading_day(date(2025,11,11)) == False`. The offline calendar therefore reports the market closed on a normal trading day — the inverse error of the Juneteenth bug. Effect: the daemon would skip a full open session (no entries/reconcile/stop checks) every Nov 11 that falls on a weekday whenever it is on the offline fallback. The "for safety" comment is backwards: including a non-holiday is *unsafe*.
**Recommended action:** Remove `(11, 11)` from `_FIXED_HOLIDAYS`. Add a test asserting `_is_trading_day(date(2025,11,11)) is True`.

### P1 — Post-2026 cliff returns wrong answers *confidently*; the C3 WARNING is the only guard and it does not change the result — `runtime/market_calendar.py:75-86` / `data/replay_clock.py:148-173`
**Why:** Beyond `CURATED_HOLIDAY_MAX_YEAR` (2026), `_is_trading_day` only recognizes the three `_FIXED_HOLIDAYS` (Jan 1 literal, Jul 4 literal, Dec 25 literal). **Every floating holiday is missed:** verified `_is_trading_day(date(2027,1,18))` (MLK 2027) returns `True`. Also missed post-2026: Presidents' Day, Good Friday, Memorial Day, Juneteenth, Labor Day, Thanksgiving, all observed weekend-shifts, and all early closes. `OfflineMarketCalendar._maybe_warn_stale` logs a WARNING when `now.year > 2026`, but the WARNING is *advisory only* — `session()` still returns a fully-populated, wrong `MarketSession` with `is_open`/`next_open`/`next_close` computed on the bad calendar. A log line does not stop a trade. The live `AlpacaMarketCalendar` is authoritative and masks this, but the offline path is the fallback on any `/v2/clock` error, so a January-2027 API outage = trading on MLK Day.
**Recommended action:** Either (a) extend the curated lists yearly and bump `CURATED_HOLIDAY_MAX_YEAR`, and/or (b) make the offline calendar fail safe past the curated range (e.g. treat the open/close decision conservatively, or compute floating holidays algorithmically — `nth-weekday` for MLK/Presidents'/Memorial/Labor/Thanksgiving, Good Friday from Easter, plus observed-shift rules). At minimum, escalate the WARNING to also be surfaced where the trade decision is consumed, not just logged.

### P2 — Observed-date weekend shifts not handled for fixed holidays beyond the curated list — `data/replay_clock.py:42-47, 166-167`
**Why:** `_FIXED_HOLIDAYS` matches the literal `(month, day)` only. When a fixed holiday lands on a weekend the NYSE observes it on the adjacent weekday (Jul 4 Sat → Fri close; Jan 1 Sun → Jan 2 close; Dec 25 Sat → Dec 24/26). Inside the curated range these observed dates are hand-listed in `_KNOWN_CLOSURES`, so it works; *outside* it (post-2026) the observed weekday is treated as a normal trading day. Lower severity than P1 only because it is a subset of the same post-2026 cliff. Within the curated window the literal-weekend match is harmless (weekend is already non-trading).
**Recommended action:** Fold into the P1 fix — add observed-shift logic, or keep curating `_KNOWN_CLOSURES` and document the dependency.

### P2 — Offline early-close map is incomplete and silently degrades half-days to full sessions — `data/replay_clock.py:132-141` (`_EARLY_CLOSE`)
**Why:** `_close_time_for` returns the 16:00 regular close for any date not in `_EARLY_CLOSE`. The map omits the "day before Independence Day" early close for years where it differs from the listed pattern and has **no entries at all past 2026** (only 2024-2025 day-before-July-4, plus Thanksgiving-Friday and Christmas-Eve through 2026). On a missing half-day the offline calendar reports `next_close` three hours late (16:00 vs 13:00 ET). For the daemon this means stop/horizon/reconcile work scheduled into a window when the exchange is already closed and the cached Alpaca session would have refetched. Authoritative `/v2/clock` masks this; it bites only on the offline fallback. Note 2026 correctly has *no* July early-close because July 4 is a Saturday (full close Fri Jul 3) — that omission is correct, not a bug.
**Recommended action:** Extend `_EARLY_CLOSE` yearly alongside the holiday list, or derive half-days from the same conservative post-curated-range policy as P1.

### P3 — `MarketSession` from a fallback can be cached implicitly via the offline path's freshness, but Alpaca fallback results are never cached (correct) — `runtime/market_calendar.py:187-203`
**Why:** Confirmed *not* a bug, documented for completeness. On a `/v2/clock` fetch or parse error, `session()` returns `self._offline.session(now)` **without** writing `self._cached`. So a transient API error does not poison the cache with an offline answer, and the next call retries the live API. This is the correct design. Cache invalidation verified: refetches at/after `cached.next_close`, and refetches every call when `next_close is None`.
**Recommended action:** None. Keep as-is.

---

## Verified CORRECT (no finding)

- **DST handling.** `datetime.combine(d, time(9,30), tzinfo=ZoneInfo("America/New_York"))` resolves the per-date offset correctly: EDT (−04:00) in summer, EST (−05:00) in winter (verified 2025-07-15 → 13:30Z, 2025-01-15 → 14:30Z). Modern `zoneinfo` does *not* exhibit the old `pytz`/fixed-offset `combine` bug. No DST off-by-one.
- **is_open boundary semantics.** `open_dt <= now_et < close_dt` — open inclusive, close exclusive (verified: 09:30 → open, 16:00 → not open). Correct for a session that ends *at* the close bell.
- **Early-close is_open** (for dates *in* the map). 2025-11-28 12:59 ET → open, 13:01 ET → closed, `next_close` 18:00Z (13:00 ET). Correct.
- **next_open / next_close edge cases.** Pre-open returns today's open; in-session returns today's close; post-close/weekend/holiday skip forward up to 370 days via `_is_trading_day`. Logic is correct *given* an accurate `_is_trading_day` (which the P0/P1/P2 findings above undermine).
- **Cache invalidation (C1).** Verified refetch exactly at `next_close`, reuse before it, and per-call refetch when `next_close is None`. O(once per session boundary).
- **Offline fallback on API error.** Does not crash the loop, does not poison the cache (see P3).
- **`_parse_clock_dt`** handles `Z` suffix, naive datetimes (assumes UTC), and bad input (returns `None`).

---

## OPPORTUNITIES TO ADD

1. **Negative/regression tests for the holiday list** — assert NYSE-open days that look like holidays (Veterans Day, Columbus Day) are trading days, AND that real closures (Juneteenth, every floating holiday per year) are not. The current list has both a false-positive (Veterans Day) and a false-negative (Juneteenth); a table-driven test against a known-good year would have caught both.
2. **Replace the hand-curated list with `exchange_calendars`/`pandas_market_calendars`** (the code's own TODO at `replay_clock.py:40, 50`). It removes the yearly-refresh burden, the 2026 cliff, the observed-shift gap, and the early-close gap in one move. Gate behind the offline path only so the live Alpaca source stays authoritative.
3. **Make staleness fail safe, not just loud.** Have `OfflineMarketCalendar.session()` past `CURATED_HOLIDAY_MAX_YEAR` (or on a missing year) return a conservative session (e.g. `is_open=False` unless it can positively confirm an open day) so a post-curated-range API outage cannot cause a trade on a closed exchange. A WARNING that does not alter the returned `MarketSession` is not a safety control.
4. **Cross-check offline vs live in non-prod.** When `AlpacaMarketCalendar` succeeds, periodically compare its `is_open`/`next_close` against `OfflineMarketCalendar.session(now)` and log divergences — an automatic detector for exactly the list-drift bugs found here.
5. **Add a unit test for the live→offline fallback cache discipline** (P3): assert that an exception from `http_get` leaves `self._cached is None` and the next call re-invokes `http_get`.
