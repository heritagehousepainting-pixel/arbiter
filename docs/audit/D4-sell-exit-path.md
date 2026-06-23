# D4 Audit — Sell / Exit Execution Path

**Auditor lane:** D4 — sell/exit EXECUTION (sizing, pricing, submission, confirmation of a SELL)
**Date:** 2026-06-19
**Scope:** `execution/submit.py`, `data/slippage.py`, `execution/exit_monitor.py` (close-out + stranded-retry), `shared/sim_executor.py::_sell`. Exit *trigger* logic (E-lane / `evaluate_triggers`) is OUT of scope except as it feeds the sell.
**Mode:** READ-ONLY. No source/test/config modified.

---

## VERDICT

**PASS (with one P2 and minor hardening items).** The core execution invariants the lane was asked to verify all hold:

- A SELL liquidates the **exact held share count** — `presized_shares=int(position.shares)` is passed straight through; `submit.py` SKIPS the A0 notional→shares divide when `presized_shares is not None` (submit.py:252-253). The held qty is never re-divided by the limit price.
- Sell-side slippage **biases the limit DOWN** correctly: `model_slippage(price, spread, side=SELL) = price·(1−5bps) − 0.5·spread` (slippage.py:65-66), and `submit.py` calls it with `order.side` so an exit SELL always gets the DOWN bias (submit.py:238).
- `is_exit=True` **bypasses the broker-position dedup** (holding ≠ duplicate) but **still enforces the local-ledger check**, so a repeated identical SELL (same dedup_hash) stays blocked (idempotency.py:135-150).
- **Selling something not held is impossible**: `_sell` rejects when `pos is None or pos.shares <= 0` (sim_executor.py:192-194), and the monitor only fires for positions with `shares > 0` tied to an owning BUY row (exit_monitor.py:578-587).
- **Over-sell is impossible** in sim: `_sell` caps at held qty via `min(intent.qty, pos.shares)` (sim_executor.py:196).
- The close-out **labels with the REAL fill price**: `result.avg_fill_price` is carried on `SubmitResult` and used as `exit_price` (exit_monitor.py:698-702), falling back to the slippage-adjusted SELL limit only if the broker reports no price.
- **Partial residual** is handled with a **fresh nonce** that yields a DISTINCT dedup_hash, so the residual re-sell is not blocked by the persisted `partial` row (exit_monitor.py:644-651, build_sell_order nonce → advisor_signature → dedup_hash). Verified green by `test_partial_sell_reconcile_keeps_monitored_then_resells_residual`.
- The **stranded-retry** sweep correctly recovers a SELL that filled but failed to label on a transient `LookupError`, using strict-subset selection so a partial per-advisor fan-out is not stranded (exit_monitor.py:397-505).

Relevant tests pass: `test_submit.py` + `test_exit_monitor_engine.py` = 25 passed.

No P0 / P1 over-sell, double-sell, or un-sellable-position defect was found. Findings below are correctness-hardening.

---

## FINDINGS

### P2 — Partial SELL ledger row records REQUESTED qty, not FILLED qty — execution/submit.py:339 — why — On an Alpaca `partial` fill, `_insert_order_row(..., qty=share_qty)` persists the full requested `presized_shares` (e.g. 10), not `report.filled_qty` (e.g. 5). The ledger therefore overstates the shares sold on that row. The residual re-sell still works (it re-reads the *broker* position, not the ledger qty), so this does not cause an over-sell or un-sellable position — but any P&L/reconciliation/exposure logic that trusts the ledger `qty` of a `partial` SELL row will be wrong by the unfilled remainder, and a later analytics pass summing SELL `qty` would double-count the residual (counted once in the partial row at full qty, again in the residual row). — **Recommended action:** for a `partial` status, persist `qty=report.filled_qty` (fall back to `share_qty` only when the broker omits `filled_qty`); keep `share_qty` for `filled`/`pending`.

### P3 — `int(position.shares)` truncates a sub-1-share fractional residual to an un-sellable 0 — execution/exit_monitor.py:683 — why — `presized_shares=int(position.shares)` floors. The system's own entries are always whole shares (`math.floor` in submit.py:255), so this is not reachable from normal flow. But a broker-side fractional fill or an externally-introduced fractional lot (e.g. 0.6 shares) passes the `position.shares <= 0` guard (0.6 > 0) yet `int(0.6) = 0` → `submit_order` returns a `ZERO_SHARE_SKIP`, the SELL never places, the idea never closes, and the position is stranded forever (the positions loop re-skips it every cycle, never erroring). — **Recommended action:** if Alpaca fractional trading is ever enabled, round the held qty up to a sellable unit or special-case `0 < shares < 1` to sell the whole fractional position; otherwise add an explicit assert/log that a fractional held position is un-sellable so it surfaces instead of silently stranding.

### P3 — Stranded-retry re-labels at the PIT close, not the original SELL fill price — execution/exit_monitor.py:489 — why — `_retry_stranded_closeouts` calls `close_idea_on_sell_fill(exit_price=None, exit_as_of=now)`, so a retry that succeeds a *later* cycle labels the outcome at the PIT close for `now`, NOT the price at which the SELL actually filled. The docstring at exit_monitor.py:468-471 says the persisted SELL `limit_price` (the real economic exit) should be reused, but the code passes `None` and falls through to the PIT-close fallback in `close_idea_on_sell_fill` (exit_monitor.py:297-308). The labeled exit price can drift from the true fill, biasing attribution for any idea that hit the LookupError path. — **Recommended action:** read the SELL row's recorded fill price (the ledger `limit_price` / a persisted `avg_fill_price` column) and pass it as `exit_price` so the retry reproduces the original economic exit, matching the in-cycle path and the docstring's stated intent.

### P3 — Sell-side slippage can produce a non-positive limit at tiny prices / wide spreads — data/slippage.py:66 — why — `price·(1−5bps) − 0.5·spread` is not floored at 0. For a low-priced name with a wide quoted spread (`spread ≥ 2·price·(1−5bps)`), the SELL limit goes ≤ 0. In sim, `_sell` then rejects on `fill_price <= 0` (sim_executor.py:190-191) → the protective exit silently does not fill and the idea stays MONITORED; on Alpaca a ≤0 limit is rejected. A stop-loss that fails to execute is the dangerous direction. Unlikely with normal equity spreads, but unguarded. — **Recommended action:** floor the sell limit at a small positive tick (e.g. `max(adjusted, 0.01)`) or have the monitor detect a non-positive computed limit and fall back to a marketable price / alert.

---

## OPPORTUNITIES TO ADD

- **Assert held-qty == presized at submit:** `build_sell_order` sets `order.qty = float(position.shares)` (informational) and the monitor separately passes `presized_shares=int(position.shares)`. A defensive assert that these agree (mod the int truncation) would catch a future caller that diverges them.
- **Test: repeated identical SELL across cycles is blocked.** `test_is_exit_not_blocked_by_position_presence` proves holding ≠ duplicate, but I did not find a direct test asserting that a *second* identical exit SELL (same dedup_hash, same cycle/next cycle, no partial) returns `duplicate=True` via the local-ledger path. The behavior is correct in code (idempotency.py:135-145); add the test to lock it.
- **Test: partial ledger qty.** Once the P2 fix lands, add a test asserting the persisted `partial` SELL row's `qty` equals `report.filled_qty`, not the requested presized count.
- **Surface stranded-forever positions.** A position that is un-sellable (P3 fractional, or P3/P4 non-positive limit) currently strands silently. A once-per-N-cycles metric/alert for "held position with no successful exit after K trigger fires" would make these observable rather than invisible.
- **Carry `filled_qty` on `SubmitResult`.** The result exposes `avg_fill_price` but not `filled_qty`; the close-out and reconcile would benefit from knowing the actually-filled share count without re-reading the broker.
