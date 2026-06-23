# Short-position support — build plan (2026-06-22)

Live state confirmed: `EXECUTOR_BACKEND=alpaca_paper`, daemon `com.arbiter.daemon` running.
Two live shorts (T −3, UBER −1) opened via SELL; their **opening SELL orders are `filled`
but their ideas are stuck at `FINAL_DECIDED`** (never advanced to MONITORED) because the
engine reconcile treats every filled SELL as an exit close-out.

## Root insight
The handoff framed exit-order detection by **side** (SELL = exit). That breaks for shorts,
where the **opening** order is a SELL and the **exit (cover)** is a BUY. The robust
discriminator is: **exit orders carry `exit_label_kind` in `exits_json`; opening orders do
not** (the entry path's `compute_exits` never sets it; the exit monitor always stamps it).
Reframe close-out-vs-advance on that, not on side.

## Changes (all keep long behavior identical)

1. **`execution/exit_monitor.py`**
   - `recompute_stop(avg, bucket, *, is_short=False)` → short stop = `avg*(1+frac)`.
   - `evaluate_triggers(..., is_short=False)`: short stop fires `price >= stop`; short
     reversal fires on a **bullish** fresh opinion (`stance >= +thresh and stance > 0`);
     horizon unchanged.
   - `build_sell_order` → `build_exit_order`: side = SELL for long-exit, **BUY-to-cover**
     for short; qty = `abs(shares)`.
   - `_latest_buy_order_for` → `_latest_opening_order_for(conn, ticker, *, is_short)`:
     opening side = SELL if short else BUY, and **skip exit orders** (those with
     `exit_label_kind`) so a cover BUY / exit SELL is never mistaken for the opener.
   - positions loop: manage any non-flat position (`shares != 0`), `is_short = shares < 0`,
     `presized_shares = abs(int(shares))`, `compute_exits(side=SELL if short else BUY)`.
   - `_retry_stranded_closeouts`: select filled **exit** orders (both sides, filtered by
     `exit_label_kind`) joined to MONITORED ideas — excludes short opening SELLs, includes
     short cover BUYs.
   - new `is_exit_order(row)` helper (shared with reconcile).

2. **`engine/reconcile.py` `reconcile_pending_orders`**: branch on `is_exit_order(row)`
   not side. Exit order (long SELL / short cover BUY) → `close_out_filled_sell`; opening
   order (long BUY / **short SELL**) on full fill → `advance_buy_idea` (→ MONITORED). This
   is what lets a freshly-shorted idea reach MONITORED going forward.

3. **`engine/safety_ops.py`**
   - `position_market_value` → `abs(shares) * price` (shorts count toward gross/limits).
   - `check_portfolio_breakers` per-position P&L made sign-aware so a **losing short**
     (price up) can trip the per-position intraday breaker (long unchanged).

4. **`execution/reconciler.py` `_local_positions`**: filter `abs(v) > _QTY_EPSILON` (was
   `v > _QTY_EPSILON`, which silently dropped shorts → false BROKER_ONLY divergence). The
   handoff said this was "already correct"; it is **not** — a test proves it.

## Out of scope (deliberate)
- **Not** extending `SimExecutor` to hold shorts (it's long-only by construction; backtest
  P&L/margin semantics are a separate, larger change). Short tests use a purpose-built fake
  broker; the live path is the real Alpaca paper adapter, which already supports shorts.

## One-time DB heal (deploy step, with backup)
The two existing shorts' ideas (`01KVR483FDBH4YV49QZ3HG80AA` T, `01KVR483FDBH4YV49QZ3HG80AF`
UBER) are stuck at FINAL_DECIDED. Their opening SELLs already `filled`, so the reconcile fix
won't reprocess them. Advance both → MONITORED (legal FSM transition, same as a long after
its BUY fills) so the patched exit monitor manages them. Backup the DB first.

## Tests (offline) + deploy
Per-fix offline tests; full suite green (~2360) + both linters; mirofish suite green.
Redeploy via `launchctl kickstart -k gui/$(id -u)/com.arbiter.daemon`; verify it manages
T/UBER.
