# B2 Audit — Intraday Current-Price Isolation from PIT / Backtest

- **Lane:** B2 — live current-price provider (#3) and its isolation from the PIT / backtest path
- **Auditor mode:** READ-ONLY (no source/test/config modified)
- **Date:** 2026-06-19
- **Scope:** `arbiter/data/current_price.py`, the `build_engine` injection gate (engine.py),
  `arbiter/evaluation/backtest/runner.py` (Null assert), `arbiter/execution/exit_monitor.py`
  (live-price stop check + daily-PIT fallback). Orientation: spec §0 amendment **C0**.
- **Central question:** *Can a live "now" price ever reach a backtest or the PIT historical path?*

## VERDICT: PASS — isolation is airtight (no leak path found)

The PIT-purity boundary holds along every path I could trace:

1. **Off the PIT surface (structural).** `current_price` is NOT in `pit._SUPPORTED_FIELDS`
   (pit.py:179-188 — only `price_open, price_close, adv_20d, beta_252d, spread, filing, news,
   trust`). There is **no** `register_source("current_price", …)` anywhere (grep of all
   non-test source: the only `register_source` calls are for the daily/PIT fields). So
   `PITGateway.get(field, ticker, as_of)` can never resolve the live price. Verified by test
   `test_current_price_not_a_pit_field` (tests/data/test_current_price.py:92).

2. **No `as_of`, no clock, no latest-style read.** The Protocol accessor is
   `current_price(ticker) -> float | None` / `current_prices(tickers)` — no `as_of` parameter
   (current_price.py:47-49), so it is structurally unusable as a historical read. The module
   contains no `datetime.now()`, no `.now()`, no `get_latest()` (grep clean). The repo
   no-look-ahead AST lint (`scripts/check_no_lookahead.sh`) passes. Verified by
   `test_no_lookahead_strings_in_module` (test_current_price.py:95).

3. **Clock-type gate (C0) is the single chokepoint and is correct.**
   `AlpacaCurrentPriceSource(config)` is constructed in exactly ONE place — engine.py:1369 —
   behind `if config.executor_backend == "alpaca_paper" and not _is_backtest`, where
   `_is_backtest = isinstance(clock, BacktestClock)` (engine.py:1367-1373). Every other branch
   (sim, OR any `BacktestClock`, even with `executor_backend=alpaca_paper`) yields
   `NullCurrentPriceProvider`. No other constructor of `AlpacaCurrentPriceSource` exists in the
   tree; the daemon and CLI never build a provider themselves — they consume
   `engine.current_price_provider`.

4. **Backtest runner belt-and-suspenders assert.** runner.py:472 asserts
   `isinstance(engine.current_price_provider, NullCurrentPriceProvider)` immediately after
   `build_engine`, with a clear C0 message. A regression that broke the gate would fail the
   backtest loudly rather than silently leak.

5. **Single consumer, fail-closed fallback.** `_run_exit_monitor` (engine.py:511, called by
   both `run_cycle` at 926 and `run_fast_iteration` at 682) forwards
   `self.current_price_provider`. In `run_exit_monitor` (exit_monitor.py:557-617) the live read
   is per-ticker; `current_price is None` falls back to `pit.get("price_close"/"price_open")`
   — i.e. backtests/sim (Null provider → empty `live_prices`) keep the exact pre-#3 daily-PIT
   behavior. `evaluate_triggers` is unchanged and fails closed on `None`.

C0's mandated test cases all exist and pass (12 + 7 tests green):
`test_backtest_clock_with_alpaca_paper_yields_null`, `test_sim_yields_null_provider`,
`test_live_clock_alpaca_paper_yields_live_source`, `test_none_live_price_falls_back_to_pit`,
`test_live_price_fires_stop_when_pit_close_is_above` (tests/execution/test_fast_iteration.py).

---

## FINDINGS

### P3 — Live price flows into a persisted SELL/outcome price, but only on the synchronous-fill path — exit_monitor.py:668,698-717 — clarify
`sell_raw_price = current_price …` (line 668), and on a synchronous fill `exit_price` (line
698) defaults to `sell_raw_price` when the broker reports no fill price, then is written via
`close_idea_on_sell_fill` into the outcome row. So a live "now" price CAN become a persisted
exit/outcome value. **This is not a PIT/backtest leak** and is by design: (a) it is a
forward-looking *realized* exit, never a historical PIT read and never registered with
`PITGateway`; (b) the synchronous-fill branch (`result.filled`) is the sim/immediate-adapter
path — in real `alpaca_paper` operation fills are asynchronous and `result.avg_fill_price` (the
real broker fill) wins, so the live tick is only the no-broker-price fallback; (c) in backtest
the provider is Null so `current_price` here is always the daily PIT value anyway. **Why P3:**
no correctness or purity defect; flagging only because "live price → persisted outcome value"
looks adjacent to the boundary and deserves an explicit note so a future reader doesn't mistake
it for a leak. **Recommended action:** none required; optionally add a one-line comment at
exit_monitor.py:668 noting the live price here feeds only a realized SELL/outcome and never the
PIT/backtest entry path.

### P3 — `ALPACA_DATA_FEED` read via `os.getenv` rather than `Config` — current_price.py:98 — minor
`self._feed = os.getenv("ALPACA_DATA_FEED", "iex")` bypasses the `Config` object the rest of
the constructor uses. No isolation impact (it only affects which live feed is queried, and only
when the live source is already gated on). **Recommended action:** for consistency/testability,
consider sourcing the feed from `Config` (it already centralizes Alpaca settings). Out of B2's
purity scope; cosmetic.

### (No P0/P1/P2 findings.)
The clock-type gate, the absence of `current_price` from `_SUPPORTED_FIELDS`, the no-`as_of`
signature, the lint cleanliness, the single construction site, and the runner assert
collectively make a live price reaching a backtest or the PIT historical path unreachable. I
actively searched for: a second provider constructor, a daemon/CLI bypass, a `register_source`
for current_price, an `as_of`-bearing overload, and a write-back from the SELL price into PIT —
none exist.

---

## OPPORTUNITIES TO ADD

1. **Defense-in-depth assert in `run_fast_iteration` / daemon start.** The runner asserts Null
   for backtests; consider a symmetric assert at the live-daemon entry that the provider is
   *not* Null when `executor_backend==alpaca_paper` AND clock is live, catching a
   silently-degraded live deployment (currently only logged at engine.py:1380).

2. **Lint rule for `current_price` as a PIT field.** Extend `check_no_lookahead.sh` (or add a
   tiny guard test) to fail if `"current_price"` ever appears in `_SUPPORTED_FIELDS` or in a
   `register_source(...)` call — turns the current behavioral test into a structural CI gate
   against a future refactor re-introducing the field.

3. **Guard against re-adding `as_of` to the provider.** Open risk #1 in the spec calls this out.
   A small AST/grep test asserting `current_price.py` defines no method with an `as_of`
   parameter would make the "structurally impossible to misuse as PIT" claim enforced, not just
   documented.

4. **Comment the C1 batch fallback edge.** In `current_prices`, a quote with only one of
   bid/ask present is silently dropped (line 150 requires both). That correctly fails closed,
   but a one-line note would prevent a future "fix" that mids against a missing side.
