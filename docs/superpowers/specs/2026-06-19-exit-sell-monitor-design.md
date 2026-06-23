# Exit / Sell Monitor — Design Spec (sub-project #2)

> Status: 2026-06-19. DESIGN ONLY — no implementation code is written by this spec author.
> Build agent: implement strictly from this document, plan→build→audit with disjoint file
> ownership, tests OFFLINE (fake PIT + fake executor/FakeAlpaca, no network), then verify on a
> live `arbiter run`. Always use `.venv/bin/python` from `/Users/jonathanmorris/poly_bot/arbiter`.
>
> Scope: an autonomous **exit/sell monitor** that, each cycle, inspects every open position and
> fires a SELL order when one of three exit triggers is met — **stop-loss**, **horizon-expiry**,
> **conviction-reversal** — then drives the idea → outcome lifecycle (MONITORED → OUTCOME_READY →
> CLOSED) with the correct `label_kind` and the REAL exit price/date. Builds on sub-project #1
> (real Alpaca paper execution, A0 notional→shares) and Phase-2 persistence. This is autonomous
> selling on a real (paper) account: rigor over cleverness.

---

## 0A. POST-AUDIT BINDING AMENDMENTS (these SUPERSEDE any conflicting text below)

The plan audit returned **GO-WITH-AMENDMENTS**. These are binding; do not relitigate.

**B0 — [P0] NO supersede-based exit persistence. Recompute exits IN-MEMORY each cycle.** The `orders`
table has NO `supersedes_id`/`is_superseded` columns (verified: `supersede_row("orders", …)` raises
`OperationalError`), and `dedup_hash` is a FULL `UNIQUE` constraint (not a partial index). So the
spec's "recompute and persist via `supersede_row`" path is impossible. Instead: the monitor recomputes
the stop level **live, in memory, each cycle** from the broker/position `avg_price` (the true cost
basis) + the bucket stop-fraction (`exits.py::_STOP_LOSS_BY_BUCKET`), and horizon-expiry from the
order row's `entry_date + horizon_days`. Do NOT persist a corrected order row at all. The recompute is
deterministic and idempotent (inputs avg_price + entry_date are stable), so "never ratchet looser" is
automatic. The stored `exits_json.stop_loss` (phantom-$100) is IGNORED by the monitor. (Optional, not
required: a one-line fix to `_bound_decide` to pass the real entry price for FUTURE stored values — but
the monitor must not depend on it.)

**B1 — [P1] SELL slippage biases the limit DOWN.** `model_slippage(price, spread)` is BUY-biased
(`price*(1+0.0005) + 0.5*spread` — always raises price). For a SELL that overstates proceeds in sim and
leaves a non-marketable limit unfilled on Alpaca. The exit path MUST apply sell-side slippage:
`limit = price*(1-0.0005) - 0.5*spread` (add a `side`-aware branch to `model_slippage` or a dedicated
sell helper). Must land before any real sell.

**B2 — [P1] Outcome-sweep guard against double-labeling.** `run_outcome_sweep` MUST skip a MONITORED
idea that has a non-superseded **SELL** order row in the ledger. Otherwise a pending-SELL idea whose
horizon date has passed gets labeled `normal` by the horizon sweep in the same cycle while the sell is
in flight — a double-process + wrong label. Add the guard (a small query in the sweep). Binding.

**B3 — submit_order gains `presized_shares: int | None = None` and `is_exit: bool = False`.** When
`presized_shares` is set: skip the A0 notional→shares divide and use that share count directly; persist
the order ledger row with `qty = presized_shares` (the actual shares sold — NOT `order.qty`); apply
B1 sell-side slippage. When `is_exit=True`: route idempotency to a **local-ledger-only** check (the
broker-position check in `ensure_not_duplicate` would otherwise block every sell, since holding the
position is the precondition). A repeated identical SELL (same dedup_hash, which includes `side`) stays
blocked → idempotent across cycles. v1 = FULL exit (sell entire held qty).

**B4 — Reconcile + close-out branches on the LOCAL order row's `side`.** Extend
`_reconcile_pending_orders` (and the close-out path) to SELECT and branch on the order row's `side`
(NOT `report.side` — `AlpacaAdapter.get_order` hardcodes BUY): a filled **BUY** advances its idea →
MONITORED (unchanged); a filled **SELL** transitions the idea OUTCOME_READY → CLOSED and labels the
outcome via the labeler/store using the REAL exit price/date with the correct `label_kind`
(stop-loss → `early_exit`, conviction → `reversal`, horizon → `normal`). Guard a `None`
`filled_avg_price` (mid-partial): do NOT label with a null price — fall back to the PIT close for that
date with a logged note, or leave the order `pending` for next-cycle reconciliation if no price exists.
Handle a `partial` sell with a fresh dedup nonce for the residual (a persisted `partial` row would
otherwise block the re-sell — `_check_local_ledger` has no status filter).

**B5 — RECOMMENDED (do if low-cost): add `orders.idea_id` via additive `ALTER TABLE ADD COLUMN`
(migration 023), populated at BUY submit time**, and link order→idea by `idea_id` on the sell/close-out
path instead of the (ticker,bucket) join. The join is "adequate for v1" only under the one-live-bucket-
per-held-ticker invariant; `idea_id` removes that fragility on the path that fires real sells. If
threading `idea_id` into the BUY submit path proves invasive, the (ticker,bucket) join is an acceptable
v1 fallback — document whichever is chosen.

**Unchanged & confirmed by audit:** paused/kill-switched engine does NOT sell (even protective stops) —
documented paper-only residual; stale-state selling is mitigated (iterate `get_positions()` only, sim
`_sell` caps at held qty); FSM path MONITORED→OUTCOME_READY→CLOSED is legal for early exits; daily
cadence means a stop is checked at most once/day vs `price_close` (intraday gaps can blow through it) —
intraday loop is out-of-scope #3.

**Build structure:** the exit monitor is a NEW cohesive unit but it edits the shared `submit.py` /
`engine.py` / `outcome_runner.py`; build it with ONE focused build agent (TDD), NOT fanned across
agents onto those shared files. Audit follows as a separate lane.

---

## 0. BINDING CONSTRAINTS (carried from #1 / Phase-2 — do not relitigate)

- **A0 (notional→shares).** `submit_order` divides `order.qty` (a USD notional) by `limit_price`
  to get whole shares. A SELL exit sizes in **SHARES already** (the held position qty); it MUST
  NOT be re-divided. This spec adds a dedicated, presized sell path (§3).
- **Paper-only floor (#1 §2).** The system is structurally paper-only; `AlpacaAdapter` only ever
  hits `paper-api.alpaca.markets`. This spec adds NO live endpoint and MUST NOT weaken that.
- **Fill-confirmation invariant (#1 §4.4).** An idea advances on a *confirmed fill* only. The same
  rule applies to a SELL closing the lifecycle: a position is only treated as CLOSED on a confirmed
  SELL fill (sim is synchronous; alpaca_paper may be pending → reconcile next cycle).
- **Insert-only + the ideas carve-out (Phase-2 §1).** Fact tables are append-only. The ONLY
  in-place UPDATEs permitted are `supersede_row` and `idea_store.update_idea_state`. The monitor
  uses `update_idea_state` for lifecycle transitions and `outcome_store.store_outcome` (insert) for
  outcomes. **Exit-level correction (§1) is done by inserting a new orders row via `supersede_row`,
  never by mutating the original `exits_json` in place.**
- **Tests stay OFFLINE and the suite stays green** (~1743+). Live `arbiter run` is the human
  verification step, never pytest.

---

## 1. Goal

Today the engine submits the entry BUY and stores `exits_json` on the order row, but **nothing ever
acts on those exits** (confirmed: #1 §4.7 OUT-of-scope, and there is no SELL path anywhere in
`run_cycle`). Positions are only ever closed when the Phase-2 outcome sweep labels them at horizon
expiry as `label_kind="normal"` — and even that does not place a SELL; it just labels and CLOSEs the
idea while the broker position stays open forever.

The exit/sell monitor must, once per cycle, for every open position:

1. Read the position's exit levels (stop-loss price, horizon-expiry date, conviction-reversal
   threshold), **trusting them only if they are valid** (see §1-finding below — they currently are
   NOT, because of the phantom $100 entry price).
2. Check each trigger PIT-correctly (no look-ahead).
3. On any trigger firing, submit a **full-exit SELL** for the held share quantity, idempotently,
   through both executor backends.
4. On a confirmed SELL fill, drive the idea MONITORED → OUTCOME_READY → CLOSED and write the
   outcome with the correct `label_kind` and the REAL exit price/date.
5. Reconcile cleanly with the Phase-2 horizon sweep so the two paths never double-label or conflict.

---

## 2. Current state (VERIFIED against source)

### 2.1 The exit-level validity finding (the headline issue) — **STORED EXITS ARE INVALID**

`arbiter/policy/decision.py::decide` has signature `… entry_price: float = 100.0`. The engine's
`_bound_decide` (engine.py:538–550) calls `_decide(...)` and **does NOT pass `entry_price`**, so
every decision uses the **default $100**. `compute_exits` (exits.py:95–98) then computes:

```
stop_loss = entry_price * (1 - stop_frac)   # BUY
```

So for every order placed to date, `stop_loss` ≈ `$100 * (1 - stop_frac)` (e.g. MEDIUM bucket →
`$95.00`), **completely decoupled from the real fill price**. `horizon_expiry` (`entry_date +
horizon_days`) is fine; `conviction_reversal` (`0.0`) is fine. But the **stop-loss price level in
every persisted `exits_json` is garbage** unless a stock happened to trade near $100.

Note the entry BUY itself fills at the *real* slippage-adjusted `price_open` (engine `_bound_submit`
sources `price_open` from PIT and passes it as `raw_price` to `submit_order`, which converts to a
limit price and shares — submit.py:214–246). So the **fill is real but the stored stop is phantom**.
This is exactly the #1-audit claim, now confirmed in source.

**Decision (chosen approach): recompute exits from the REAL fill price at monitor time and persist a
corrected order row. Do NOT block on a #1 fix to `decide`.**

Rationale and the §9 "never revised upward" reconciliation:

- The monitor already needs the position's `avg_price` (cost basis) to size the SELL — it gets this
  free from `executor.get_positions()[ticker].avg_price` (both `SimExecutor` and `AlpacaAdapter`
  populate `PositionSnapshot.avg_price`; the Alpaca adapter reads `avg_entry_price`). This is the
  authoritative realized entry price, strictly better than any decision-time estimate.
- INTERFACES §9 forbids *loosening a real stop* ("exits never revised upward"). Correcting a
  **phantom $100-derived stop** to the stop implied by the **actual fill** is not a loosening of a
  real stop — there was never a real stop. We make this explicit and one-time:
  - The corrected stop is computed **once**, deterministically, from `avg_price` and the order's
    horizon bucket, using the exact same `compute_exits(bucket, side=BUY, entry_price=avg_price,
    entry_date=<original entry_date from the order row>)`. `horizon_expiry` is recomputed from the
    **original** `entry_date` (not "now") so the clock is not reset.
  - It is persisted via `supersede_row(conn, "orders", old_order_id, new_row)` (insert-only
    compliant), with the new row carrying `supersedes_id = old_order_id`, the corrected `exits_json`,
    and a marker (`status` unchanged; add an audit line `order.exits_corrected`).
  - A "corrected" exits row is corrected **at most once** — guard by skipping rows that already have
    a non-default stop consistent with `avg_price` (or simpler: skip rows that already have a
    `supersedes_id`, i.e. are themselves corrections). The monitor never ratchets a stop looser on a
    subsequent cycle.
- **Belt-and-suspenders: ALSO fix the root cause in #1's `decide` path** so future orders store a
  correct stop natively. This is a one-line wiring fix in `engine._bound_decide` to pass
  `entry_price=raw_price` — BUT the entry price is fetched inside `_bound_submit`, not
  `_bound_decide`, and the decision happens before submission. See §1-fix below for the clean
  approach. Even with this fix, the monitor MUST still recompute from `avg_price` for legacy rows and
  because the decision-time `price_open` can differ from the realized `avg_fill_price` (slippage,
  partial-day moves). **avg_price is the source of truth.**

> **Net:** the monitor does NOT trust the stored `stop_loss`. On first sight of a position it derives
> the stop from the broker's `avg_price`, persists the correction (supersede), audits it, and from
> then on uses the corrected stored value. `horizon_expiry` and `conviction_reversal` from the stored
> exits ARE trustworthy and are kept (horizon recomputed from the original entry_date during the same
> correction for internal consistency).

### 2.2 SELL sizing / A0 (VERIFIED)

- `submit_order` (submit.py:223–246): `notional = float(order.qty); shares = math.floor(notional /
  limit_price)`. For a BUY this is correct. For a SELL we have a **share count** (held qty), not a
  notional — re-dividing by price would sell `shares/price` shares (e.g. 10 shares ÷ $50 = 0 →
  zero-share skip, or a tiny wrong qty). **The SELL must bypass the divide.**
- `SimExecutor._sell` (sim_executor.py:188–221) already exists, is correct, and:
  - fills at `intent.limit_price`,
  - sells `min(intent.qty, pos.shares)` (so an over-ask can't go short),
  - computes realized P&L, updates cash, and DELETES the position when shares hit 0.
  - rejects if there is no position (`no position in {ticker}`).
- `AlpacaAdapter.place` already supports `side="sell"` (it lowercases `intent.side.value`) and sends
  a `limit`/`day` order with `client_order_id = intent.order_id`. No adapter change needed for sells.

### 2.3 Idempotency / dedup_hash (VERIFIED)

- `dedup_hash = sha256(ticker | side | horizon_bucket | entry_date | advisor_signature)`
  (idempotency.py:30). **`side` is part of the hash**, so a SELL hash differs from the BUY hash —
  good, a SELL is NOT blocked by the BUY's row.
- `ensure_not_duplicate` ALSO calls `_check_broker(executor, ticker)` which returns True if the
  ticker is in `get_positions()` (idempotency.py:73–98). **This is fatal for a sell**: when we go to
  SELL a held name, the broker *does* have a position, so `ensure_not_duplicate` would raise
  `DuplicateOrderError` and the sell would be skipped. **The sell path MUST NOT use the
  position-presence broker check** (it is a buy-side guard). See §3.

### 2.4 Lifecycle / outcome (VERIFIED)

- FSM (lifecycle.py): `MONITORED → OUTCOME_READY → CLOSED` only. No direct MONITORED → CLOSED. The
  exit path must go through OUTCOME_READY.
- `outcome_labeler.label(...)` accepts `exit_price` and `exit_as_of` overrides and a `label_kind`
  (labeler.py:56–68, 157–160). When `exit_price` is supplied it is used directly (no PIT close
  read) — perfect for an early exit at a known SELL fill price. `label_kind ∈ {normal, early_exit,
  reversal, corporate_event, partial}`.
- `outcome_store.store_outcome(outcome, conn, *, as_of, audit_path)` inserts + audits (insert-only).
- The Phase-2 `outcome_runner.run_outcome_sweep` (outcome_runner.py) ONLY processes ideas in
  `MONITORED`, sweeps to `OUTCOME_READY` **by horizon date** (`sweep_outcomes`), labels with a
  hard-coded `label_kind="normal"`, stores, and CLOSEs. It places **no SELL**. It runs every cycle
  from `engine.run_cycle` (engine.py:673–683).
- **orders table has NO `idea_id` column** (001_core.sql:86–98). Linkage today is the (ticker,
  horizon_bucket) join used in `_reconcile_pending_orders` (engine.py:282–296):
  `ideas.dedupe_key_ticker == orders.ticker AND ideas.dedupe_key_bucket == orders.horizon_bucket`.

### 2.5 Where the cycle runs (VERIFIED — engine.run_cycle order today)

1. auto-pause short-circuit (paused / kill-switch / breaker) → return early.
2. `_reconcile_pending_orders(now)` (alpaca_paper only) — promote pending BUYs → filled, idea →
   MONITORED.
3. `account = executor.get_account()`; fail-closed if adapter equity ≤ 0 (A2).
4. load `active_ideas`; `held_tickers = executor.get_positions().keys()`.
5. detect signals → build ideas (skipping held tickers) → gather opinions → fuse → decide → submit
   (BUYs), via `orchestrator.cycle.run_cycle`.
6. snapshot SimExecutor positions (sim only).
7. `outcome_runner.run_outcome_sweep(...)` — horizon-based labeling.

The exit monitor must slot in **before** new entries (step 5) and the horizon sweep (step 7). See §5.

---

## 3. Design decisions

### Decision 1 — Exit-level validity: recompute from `avg_price`, persist via supersede

See §2.1 for the finding and rationale. Concrete mechanics:

- New module `arbiter/execution/exit_monitor.py` owns a helper
  `ensure_corrected_exits(conn, *, order_row, avg_price, clock, audit_path) -> dict`:
  - If `order_row` already has `supersedes_id` set (it is itself a correction) OR a per-row "exits
    corrected" marker is present, return the parsed stored exits unchanged (idempotent; never
    ratchet).
  - Else recompute `exits = compute_exits(bucket=HorizonBucket(order_row["horizon_bucket"]),
    side=OrderSide.BUY, entry_price=avg_price, entry_date=date.fromisoformat(order_row["entry_date"]))`.
  - Persist the correction with `supersede_row(conn, "orders", order_row["order_id"], new_row)` where
    `new_row` is the same order with `exits_json = corrected`, `supersedes_id = old order_id`, and
    its own fresh ULID `order_id`. (NOTE: the new row's `dedup_hash` must stay UNIQUE — reuse the
    original dedup_hash is impossible because of the UNIQUE constraint; append a `:exitfix` suffix
    before hashing, OR — simpler and preferred — keep the SAME dedup_hash by having `supersede_row`
    flip the old row's `is_superseded=1` first within the same transaction so the UNIQUE index only
    ever sees one live row. **Verify `supersede_row` semantics** — INTERFACES §10 says it sets
    `is_superseded` on the old row; if the UNIQUE index does not exclude superseded rows, give the
    correction a distinct dedup_hash `sha256(orig || "|exitfix")`. Build agent must check the schema
    and pick whichever keeps the insert legal.)
  - Audit `order.exits_corrected` `{order_id, supersedes_id, old_stop, new_stop, avg_price}`.
- **Also fix the root cause for future orders (§1-fix).** The cleanest fix: have the policy size in
  notional (unchanged) but pass the real entry reference into `decide`. Since `_bound_decide` runs
  before `_bound_submit` fetches `price_open`, hoist the `price_open` read: fetch it once per idea in
  `_bound_decide` via `self.pit.get("price_open", idea.ticker, now)` and pass `entry_price=raw_price`
  into `_decide(...)`; cache it so `_bound_submit` reuses the same value (avoids a double PIT read and
  keeps the decision stop and the fill consistent). If `price_open is None`, skip the idea (same
  fail-closed behavior `_bound_submit` already has). This makes newly-stored stops sane; the monitor's
  `avg_price` recompute is still authoritative (slippage/partial-day) and covers legacy rows.

### Decision 2 — What the monitor checks per position, each cycle

For each ticker in `executor.get_positions()` (the live source of truth in both modes):

1. **Resolve the owning order + idea.** Find the latest non-superseded BUY order row for the ticker
   that is `filled`/`partial` (this carries `exits_json`, `entry_date`, `horizon_bucket`). Then the
   matching MONITORED idea via the (ticker, horizon_bucket) join (same join as
   `_reconcile_pending_orders`). If no MONITORED idea exists (e.g. a manually-held or orphan
   position), log and **skip the lifecycle** but still allow a protective stop SELL (see §6). If
   multiple orders/ideas match (concurrent buckets on one ticker — INTERFACES allows this), process
   each (ticker, bucket) pair independently; the position qty is the **whole** broker position, so for
   v1 with at-most-one bucket per held ticker this is unambiguous. **v1 assumption: one live bucket
   per held ticker** (the engine's entry path skips held tickers, so a second bucket can't be opened
   while the first is held). Document and assert this; if violated, skip and audit
   `exit_monitor.ambiguous_position` rather than over-sell.

2. **Correct exits** via Decision 1 (`ensure_corrected_exits`) using `position.avg_price`.

3. **Check triggers** (in priority order; first match wins, full exit):
   - **stop-loss** (direction-aware). Read current price PIT: `px = pit.get("price_close",
     ticker, now)` (use `price_close` as the most recent realized mark; fall back to `price_open` if
     close is None). If `px is None` → cannot evaluate stop this cycle → log
     `exit_monitor.no_price` and skip (do NOT fire on missing data — fail closed against spurious
     sells). For a long (BUY) position: fire if `px <= exits["stop_loss"]`. (The held side is always
     BUY in v1 — we only ever go long; a short would invert to `px >= stop`.)
   - **horizon-expiry**: fire if `now.date() >= exits["horizon_expiry"]` (the stored/recomputed
     expiry date). This is the deterministic, data-free trigger.
   - **conviction-reversal**: fire if the **current fused conviction** for this ticker's bucket has
     flipped against the position past the threshold. For a long: fire if `conviction <=
     -(threshold)` i.e. `conviction < 0` when threshold is `0.0` (the default reversal threshold).
     See Decision 2b for the data path.

4. If any trigger fires → construct and submit a full-exit SELL (Decision 3), then on confirmed fill
   drive the lifecycle + outcome (Decision 4) with the `label_kind` mapped from the trigger:
   - stop-loss fired → `early_exit`
   - horizon-expiry fired → `normal` (this is the natural full-horizon close — but see §4 on
     reconciling with the Phase-2 sweep; the monitor takes ownership of horizon closes that have a
     live position, and the sweep handles only position-less horizon closes)
   - conviction-reversal fired → `reversal`

   Priority when multiple fire same cycle: **stop-loss > reversal > horizon** (a stop blowing through
   is the most urgent and the most negative; record the most specific cause). Document this ordering.

#### Decision 2b — the conviction-reversal data path (the hardest part)

Opinions are **not** persisted to a queryable per-ticker store inside the cycle: the engine gathers
them in-memory each cycle via `run_named_advisors_parallel(self.advisor_map, …)` (engine.py:497) and
fuses on the fly (`_bound_fuse`). The advisor functions (`_build_a1_*_fn`) call `detect_signals` +
`score_signal` + `emit_opinion` against the live DB for the current `as_of`.

**Chosen path: re-use the SAME in-cycle fusion the entry path already computes — do not invent a
second opinion-gathering pass.** Concretely:

- The monitor runs **after** opinions are gathered and **with access to the same
  `raw_opinions`/fusion** the entry path builds. Implementation: in `engine.run_cycle`, after
  `raw_opinions`/`valid_opinions` are computed (engine.py:497–499) and the `_bound_fuse` closure
  exists, build a small per-(ticker,bucket) conviction lookup for **held** tickers:
  - For each held ticker + its owning bucket, gather the subset of `valid_opinions` for that bucket
    (the fusion is per-bucket, ticker-agnostic in the current MVP fusion — **verify**: `_fuse` fuses
    a list of opinions into a per-bucket FusionOutput; opinions are per-ticker via their `ticker`
    field but the MVP equal-weight fusion may pool across tickers in a bucket). The build agent MUST
    confirm whether `FusionOutput.conviction` is ticker-specific. Two cases:
    - If fusion is **per-ticker** (opinions filtered to the ticker before fusing): conviction for the
      held ticker = `_bound_fuse([ops for that ticker+bucket], bucket).conviction`.
    - If the MVP fusion is **bucket-pooled** (not ticker-specific): then "conviction for ticker X"
      isn't well defined from pooled fusion. In that case, derive the per-ticker stance directly from
      the **current opinions for that ticker**: `signed_stance = mean(op.stance_score for op in
      valid_opinions if op.ticker == ticker and op.horizon_bucket == bucket)`; reversal fires when
      `signed_stance` flips sign past threshold relative to the original long. This is the robust,
      ticker-specific signal and avoids mis-reading a pooled bucket conviction.
  - **Recommended v1: use the per-ticker current opinion stance** (the second case) regardless,
    because it is unambiguously ticker-specific and look-ahead-safe (opinions are emitted for
    `as_of = now` from PIT-gated detection). If there are **no current opinions for the held ticker
    this cycle** (the common case — a name we bought weeks ago has no fresh filing), then there is **no
    reversal signal** and the conviction-reversal trigger simply **does not fire** (absence of a
    contrary opinion is not a reversal). Document this explicitly: conviction-reversal only fires when
    there is a *fresh, opposite-signed* opinion on the held name this cycle. This is the correct,
    conservative semantics for a daily monitor.
- No new DB reads, no second advisor pass, no look-ahead: the monitor consumes the opinions already
  gathered for the current `as_of`.

This makes the conviction-reversal path: **(held position is long) AND (a fresh opinion exists for
this ticker+bucket this cycle) AND (its signed stance ≤ −threshold) → reversal exit.**

### Decision 3 — SELL order construction & sizing (the A0 interaction)

**Full exit for v1** (no scale-out). The SELL qty is the **held share count** from the position
snapshot, NOT a notional.

The A0 divide in `submit_order` is the problem. **Chosen approach: a presized share-mode flag on
`submit_order`**, NOT a separate parallel function (keeps idempotency, audit, breaker, and ledger
logic in one place).

- Add a keyword `presized_shares: float | None = None` to `submit_order`. When provided:
  - **Skip the A0 notional→shares divide entirely**; `share_qty = presized_shares` directly.
  - Still compute `limit_price` from `raw_price` + slippage (so the SELL is a marketable-ish limit,
    consistent with buys; for a SELL the slippage model should bias the limit *down* slightly to
    improve fill probability — verify `model_slippage` direction; if it only models a spread cost,
    apply it as `raw_price - slippage_cost` for sells. The build agent confirms the sign).
  - `shares <= 0` still returns `ZERO_SHARE_SKIP` (defensive; a 0-share position shouldn't reach
    here).
- Add a parallel keyword path so the SELL **does not run the position-presence broker dedup check**
  (§2.3) — that check is buy-only. Cleanest: pass `is_exit: bool = False` to
  `ensure_not_duplicate` (or a new `ensure_not_duplicate_sell`) that checks **only the local ledger
  for an existing live SELL with the same dedup_hash**, NOT broker positions. Rationale:
  - A SELL's dedup_hash already differs from the BUY's (side is in the hash), so the local-ledger
    check makes a **second SELL attempt idempotent** (the first SELL row blocks the duplicate) — this
    is exactly what we want for "a second exit attempt must be idempotent".
  - The broker-position check must be **inverted/omitted** for sells: holding the position is the
    *precondition* for selling, not a duplicate signal.
- The SELL `PaperOrder` is constructed by the monitor with: `side=OrderSide.SELL`, `qty=share_qty`
  (shares — but since we pass `presized_shares`, the qty field is informational), the same
  `horizon_bucket`, `entry_date` (original), `advisor_signature` (carry from the BUY order row so the
  dedup hash is stable/reproducible), and `exits=<the corrected exits>` (stored for the record).
- **Both modes:** `SimExecutor._sell` fills synchronously at limit_price and returns `filled` with
  realized P&L; `AlpacaAdapter.place` sends the sell limit `day` order and returns `filled` or
  `pending`. The monitor treats `pending` SELLs exactly like pending BUYs — the position is NOT yet
  closed; the **next cycle's `_reconcile_pending_orders` must be extended to handle SELL fills too**
  (today it only advances ideas → MONITORED; extend it to recognize a filled SELL and run the
  close-out lifecycle — see Decision 4 / §4b).

> **A0 corruption avoided:** the SELL never touches the notional→shares divide because
> `presized_shares` short-circuits it. The held qty flows straight to `OrderIntent.qty`.

### Decision 4 — Idea → outcome lifecycle on exit

On a **confirmed SELL fill** (sim: immediate; alpaca_paper: this cycle if filled, else next-cycle
reconcile):

1. **Link position → order → idea.** Use the (ticker, horizon_bucket) join (same as
   `_reconcile_pending_orders`). **Assess of robustness:** the join is adequate for v1 because the
   entry path enforces *at most one live bucket per held ticker* (held tickers are skipped for new
   ideas), so (ticker, bucket) uniquely identifies the live idea. **However it is fragile** if that
   invariant ever breaks (concurrent buckets, re-entry after a close in the same cycle). **Recommended
   hardening (do it): add an `idea_id` column to the `orders` table** via a new migration
   (`023_orders_idea_id.sql`, `ALTER TABLE orders ADD COLUMN idea_id TEXT`) and populate it on entry
   (the engine's `_bound_decide`/submit path knows the idea). New orders carry `idea_id`; legacy rows
   stay NULL and fall back to the (ticker,bucket) join. This makes the link exact and future-proofs
   the learning loop. If the migration is deemed out of scope for v1, the (ticker,bucket) join is the
   documented fallback — but flag it as an open risk. **This spec recommends adding the column.**
2. **Transition the idea** MONITORED → OUTCOME_READY → CLOSED via `idea_store.update_idea_state`
   (two calls, mirroring `outcome_runner`). The in-memory FSM check in `update_idea_state` is
   log-only, but the path is legal.
3. **Label the outcome** via `outcome_labeler.label(idea, pit=pit, cutoff_as_of=now,
   advisor_id=advisor_id_for(idea), advisor_confidence=…, exit_price=<real SELL avg_fill_price>,
   exit_as_of=now, label_kind=<mapped kind>)`. Passing `exit_price` and `exit_as_of` makes the
   labeler use the REAL fill (no PIT close read, no look-ahead). `label_kind` from Decision 2:
   `early_exit` (stop), `reversal` (conviction flip), `normal` (horizon with a live position). Use the
   same `advisor_id_for` horizon heuristic the engine already passes to the sweep (engine.py:670–671).
4. **Store** via `outcome_store.store_outcome(outcome, conn, as_of=now, audit_path=…)`.

**Reconciling with the Phase-2 outcome sweep (no double-label / no conflict):**

- Today the sweep advances **MONITORED** ideas to OUTCOME_READY purely by horizon date and labels
  `normal`, **without selling**. If both the monitor and the sweep run in the same cycle, they could
  both try to process the same MONITORED idea.
- **Chosen reconciliation:** the **exit monitor runs FIRST** (before the horizon sweep) and **only
  the monitor closes ideas that still have a live position**. By the time the monitor finishes:
  - Ideas it closed are now `CLOSED` (or `OUTCOME_READY` if a pending SELL hasn't filled). The sweep
    loads `{MONITORED}` only (outcome_runner.py:87), so it will **not** see a CLOSED idea — no
    double-label.
  - Ideas whose SELL is `pending` are left `MONITORED` until the SELL fills (the monitor must NOT
    advance them to OUTCOME_READY before the fill, exactly like buys). The sweep would see them as
    MONITORED — so the sweep MUST be guarded to **skip ideas that have a live (pending) exit SELL**
    (check: a SELL order row for (ticker,bucket) with status `pending`). Add this guard to
    `outcome_runner` OR, cleaner, have the monitor mark the idea's exit-in-flight and have the sweep
    skip it. **Recommended: the sweep skips any MONITORED idea that has a non-superseded SELL order
    row** (pending or filled) for its (ticker,bucket) — those are owned by the exit monitor's
    reconcile path, not the horizon sweep. This is a small, surgical query addition to
    `run_outcome_sweep`.
- **The horizon sweep retains responsibility only for position-less horizon closes** — i.e. ideas
  that reached horizon but whose position was already gone (e.g. closed externally, or legacy ideas
  with no reconstructable position). For those there is nothing to sell and the `normal` PIT-close
  label is correct. With the broker as source of truth, the common case (a still-held name hitting
  horizon) is now handled by the monitor with a real SELL + real exit price, which is strictly better
  than the sweep's PIT-close estimate.

> **Net:** monitor first (sells + labels live positions), sweep second (labels only the leftover
> position-less horizon cases, and skips any idea with a SELL order row). No idea is labeled twice;
> the FSM (`MONITORED` load filter + CLOSED terminal) structurally prevents re-processing.

### Decision 4b — Partial fills / SELL rejections on the real broker

- **SELL rejected** (`status == "rejected"`): `submit_order` already trips `broker_non_200` and
  raises `BrokerError` when a breaker is passed (submit.py:278–300). For a SELL we still want the
  breaker + alert (a broker refusing our protective sell is a critical condition). The idea stays
  MONITORED, position stays open, audit `exit_monitor.sell_rejected`, engine auto-pauses (consistent
  with buys). Do NOT transition the idea. Next cycle retries (the SELL is idempotent via local
  ledger; but a rejected order is NOT persisted, per submit.py — so the retry is clean).
- **SELL partial** (`0 < filled_qty < qty`): persist `status="partial"`, the position is now
  partially reduced. v1 = full-exit intent, but a partial leaves residual shares. **Handling:** do
  NOT immediately label/close (the position is not fully closed). Leave the idea MONITORED; the
  leftover `day` order expires at close. **Next cycle** the monitor sees the (smaller) remaining
  position and re-fires the same trigger → submits a SELL for the new remaining qty (idempotency:
  the partial's dedup_hash matches the prior SELL → the local-ledger check would block it. So the
  re-sell must use a **fresh dedup_hash** — include a monotonically increasing exit-attempt nonce or
  the current `entry_date`/cycle date in the SELL hash so each cycle's residual sell is a distinct
  order). Document: residual shares are swept on subsequent cycles; we never short. Audit
  `exit_monitor.sell_partial {filled_qty, remaining}`. Only when `get_positions()` shows the ticker
  **gone** (fully closed) do we run the close-out lifecycle (label with the **volume-weighted average
  exit price** across the partial fills if easily available, else the last fill price;
  `label_kind="partial"` if any fill was partial, else the trigger-mapped kind). For v1 simplicity:
  label with the last SELL fill's avg price and `label_kind="partial"` when partials occurred.
- **Pending SELL across cycle boundary** (alpaca_paper): handled by extending
  `_reconcile_pending_orders` to detect a filled SELL and invoke the close-out lifecycle (Decision 4)
  — the same way it currently promotes pending BUYs. The reconciler must distinguish BUY vs SELL rows
  (read `side` in the `SELECT`) and route SELL fills to the close-out, BUY fills to MONITORED.

### Decision 5 — Where it runs in the cycle & ordering

New engine method `_run_exit_monitor(now)` invoked inside `run_cycle`, in this exact sequence:

1. auto-pause / kill-switch / breaker gates (unchanged) → may return early.
2. `_reconcile_pending_orders(now)` — **extended** to handle pending SELL fills → close-out (Dec 4b)
   in addition to pending BUY fills → MONITORED. (Runs for alpaca_paper; harmless for sim where there
   are no pendings.)
3. `account = get_account()` + A2 fail-closed.
4. **`_run_exit_monitor(now)`** — the new step. Inspect open positions, correct exits, check
   triggers, submit SELLs, drive lifecycle on confirmed fills. **Runs in BOTH modes.** Must run after
   opinions are gathered IF conviction-reversal needs them — so the cleanest placement is: gather
   opinions early (the engine already does at engine.py:497, before entries), then run the exit
   monitor with the gathered opinions, then run entries. Restructure so opinion-gathering happens once
   and feeds both the exit monitor (reversal check) and the entry path. (If reordering opinion
   gathering is too invasive, an acceptable v1 fallback: run stop-loss + horizon triggers in the exit
   monitor BEFORE opinions, and evaluate conviction-reversal in a second small pass after opinions —
   but prefer the single-gather restructure.)
5. entries (new BUYs) via `orchestrator.cycle.run_cycle` — unchanged. (Selling derisks before we add
   new exposure; also frees buying power for the same cycle's entries.)
6. SimExecutor snapshot (sim only) — now also captures the post-SELL positions/cash.
7. `outcome_runner.run_outcome_sweep(...)` — **guarded** to skip MONITORED ideas that have a SELL
   order row (Decision 4), so it only mops up position-less horizon closes.

**Ordering rationale:** reconcile (settle prior async fills) → sell (derisk, free buying power, and
close ideas) → buy (new exposure) → snapshot → horizon-sweep-mop-up. Selling before buying is the
safe order; the sweep last so it never races the monitor.

**Cadence limitation (call it out):** the monitor runs **once per cycle (daily)**. A stop-loss is
therefore checked **at most once per day** against `price_close`. **Gaps can blow through the stop**:
an intraday crash well below the stop is only detected at the next daily cycle and sold at the *then*
price, which may be far below the stop level. True intraday monitoring (a market-hours runtime loop
polling fills/prices) is **out-of-scope #3**. The realized exit P&L can be materially worse than the
stop implies. Document prominently.

### Decision 6 — Safety for autonomous selling on $10k

- **Selling something we don't hold (stale state):** the monitor sizes from
  `executor.get_positions()` — the live broker/sim truth — and `SimExecutor._sell` /
  `_check_broker` (for buys) already reject/avoid non-held names. For the SELL path we additionally
  guard: only iterate tickers actually present in `get_positions()`, and `presized_shares =
  position.shares` (capped by the executor's own `min(qty, pos.shares)`). We never construct a SELL
  for a ticker not in the live positions map. If `get_positions()` raises (broker flaky), **fail
  closed: skip the exit monitor this cycle** (do not guess) and audit — a missed protective sell for
  one cycle is safer than a wrong sell; the breaker/kill-switch remains the hard stop.
- **Double-selling:** prevented by (a) the local-ledger SELL dedup check (a live SELL row for the
  same dedup_hash blocks a duplicate within/across cycles until the position is gone), and (b) the
  position becoming empty after a full fill (next cycle's `get_positions()` no longer lists it, so no
  re-sell is even attempted). The partial-fill residual sweep uses a fresh per-cycle dedup nonce
  (Dec 4b) so it is intentional, not a double-sell.
- **Breaker / kill-switch / auto-pause interaction (decide and justify):**
  - **A paused or kill-switched or breaker-tripped engine returns early BEFORE the exit monitor**
    (the gates are step 1, the monitor is step 4). **Decision: a paused engine does NOT sell either —
    including protective exits — in v1.** Justification: the kill switch and circuit breakers are
    *infrastructure-level* safety latches that mean "something is wrong, stop ALL autonomous broker
    activity." Allowing autonomous SELLs while paused would mean the bot keeps trading on a real
    account during exactly the conditions we declared unsafe (e.g. a broker-non-200 storm, a data
    anomaly, a confidence-distribution shift). The safest invariant is **paused = no autonomous orders
    of any kind**. Protective liquidation under a fault is a **human** action (the operator resumes or
    manually flattens). This matches INTERFACES §8: the kill switch "blocks NEW orders; does NOT
    auto-close (v1)" — a SELL is a new order, so it is blocked, and there is explicitly no auto-close
    on halt. We honor that.
  - **Document the residual risk:** while paused, stops are not enforced and a position can keep
    falling. This is an accepted v1 tradeoff and an argument for the operator to wire `ALERT_WEBHOOK_URL`
    (so they learn of the pause) and `KILL_SWITCH_URL` (so they can stop the bot but must then manage
    positions manually). Calling this out is required.
- **Order-of-magnitude safety:** SELLs reduce exposure and free buying power; they don't risk
  over-leverage. The `$10k` guardrails (#1 §4.6) are about *entries*; sells are inherently derisking.
  No new gross/position caps needed for sells.

### Decision 7 — Scope boundary

**IN:** monitor open positions each cycle; the three exit triggers (stop-loss, horizon-expiry,
conviction-reversal); full-exit SELL construction/sizing that bypasses the A0 divide; SELL
idempotency; pending/partial/rejected SELL handling incl. next-cycle reconcile; idea →
OUTCOME_READY → CLOSED lifecycle with correct `label_kind` and REAL exit price/date; reconciliation
with the Phase-2 horizon sweep; both `sim` and `alpaca_paper`; the one-time exit-level correction
from `avg_price`; (recommended) the `orders.idea_id` column.

**OUT:** intraday runtime loop / intraday stop polling (#3 — the once-a-day cadence + gap risk is the
key limitation); the learning/trust-calibration loop consuming these outcomes (#4); MiroFish (#5);
scale-out / partial-exit *strategies* (v1 is full exit; residual-share *sweeping* after a broker
partial fill IS in scope as correctness, but deliberate scaling out is not); real-money
(`live_trading=true`) path.

---

## 4. Files / functions to change (build-agent map)

- **NEW `arbiter/execution/exit_monitor.py`** (owns the trigger logic + sell construction):
  - `ensure_corrected_exits(conn, *, order_row, avg_price, clock, audit_path) -> dict` (Dec 1).
  - `evaluate_triggers(*, position, exits, current_price, current_stance, now) -> ExitDecision | None`
    — pure function returning the fired trigger + `label_kind` (Dec 2), no I/O (easily unit-tested).
  - `build_sell_order(*, position, owning_order_row, exits, now) -> PaperOrder` (Dec 3).
  - `run_exit_monitor(conn, executor, pit, clock, *, opinions_by_ticker_bucket, breaker, audit_path,
    advisor_id_for, idea_link_fn) -> list[str]` — orchestrates per-position: correct exits, read
    price PIT, compute stance, evaluate, submit via `submit_order(..., presized_shares=..., is_exit=True)`,
    and on confirmed fill run the close-out lifecycle. Returns closed idea_ids.
- **`arbiter/execution/submit.py`**: add `presized_shares: float | None = None` (skip A0 divide) and
  `is_exit: bool = False` (route idempotency to the SELL-safe check). Sell-direction slippage sign.
- **`arbiter/execution/idempotency.py`**: add a sell-safe duplicate check (local-ledger only, no
  broker-position check) — `ensure_not_duplicate(..., is_exit=False)` or a sibling function. Optional
  per-cycle nonce in the SELL dedup_hash for partial-residual sweeps (Dec 4b).
- **`arbiter/engine.py`**:
  - `run_cycle`: insert `_run_exit_monitor(now)` at step 4 (after reconcile + account/A2, before
    entries); restructure so opinions are gathered once and the per-(ticker,bucket) stance lookup for
    held tickers is built and passed in (Dec 2b, Dec 5).
  - `_reconcile_pending_orders`: extend to route filled **SELL** rows to the close-out lifecycle (Dec
    4b), distinguishing `side`.
  - `_bound_decide`: pass `entry_price=raw_price` (hoist the `price_open` read; cache for
    `_bound_submit`) — the §1 root-cause fix.
  - close-out helper `_close_idea_on_sell_fill(order_row, sell_report, now)` shared by the monitor and
    the reconciler: (ticker,bucket)→idea, MONITORED→OUTCOME_READY→CLOSED, label with real exit price,
    store outcome.
- **`arbiter/orchestrator/outcome_runner.py`**: guard `run_outcome_sweep` to skip MONITORED ideas
  that have a non-superseded SELL order row for their (ticker,bucket) (Dec 4).
- **NEW migration `arbiter/db/migrations/023_orders_idea_id.sql`** (recommended): `ALTER TABLE orders
  ADD COLUMN idea_id TEXT;` + populate on entry; monitor/reconciler prefer `idea_id`, fall back to the
  (ticker,bucket) join for legacy NULL rows.
- **INTERFACES.md**: §9 note that exits are acted on by the monitor; document the one-time
  `avg_price` exit correction as a deliberate, audited carve-out from "never revised upward" (it
  corrects a phantom value, does not loosen a real stop). Flag as a cross-lane amendment (do not
  silently diverge). If `orders.idea_id` is added, note it in §10.
- **Tests** under `tests/execution/` (exit_monitor, submit presized/is_exit), `tests/orchestrator/`
  (sweep guard), `tests/` engine (cycle ordering, reconcile SELL).

---

## 5. Test strategy (OFFLINE — fake PIT + fake executor/FakeAlpaca, no network)

Hard rule (INTERFACES §11.7, Phase-2 §4): pytest never hits the network. Use `FixtureSource` +
`PITGateway` for prices/stance inputs, `SimExecutor` and the #1 `FakeAlpaca` (injected
http_post/get/delete) for the broker.

- **Exit-level correction (Dec 1):** seed an order row with a phantom `stop_loss=95.0` and a position
  with `avg_price=300.0`; assert `ensure_corrected_exits` supersedes the row, the new stop ≈
  `300*(1-stop_frac)`, an `order.exits_corrected` audit fires, and a **second** call is a no-op
  (never ratchets; never loosens).
- **`evaluate_triggers` (pure):** table-driven — long position, current price below corrected stop →
  `early_exit`; `now.date() >= horizon_expiry` → `normal`; fresh opposite stance past threshold →
  `reversal`; missing price → no stop fire; priority stop>reversal>horizon when multiple fire.
- **SELL sizing / A0 (Dec 3):** a position of 10 shares at $300; assert the SELL goes out with
  `qty=10` (NOT `10/limit`), `presized_shares` bypasses the divide, `SimExecutor` realizes the P&L,
  the position is removed, cash increases. Assert a SELL is NOT blocked by the buy-side
  position-presence check, and a **second** identical SELL the same cycle is blocked by the local
  ledger (idempotent).
- **Lifecycle + outcome (Dec 4):** MONITORED idea + held position + fired trigger → idea ends CLOSED,
  exactly one outcome row with the correct `label_kind` and `exit_price == SELL avg_fill_price`. The
  Phase-2 sweep run afterward stores **no second outcome** for that idea (CLOSED is filtered out; and
  the SELL-row guard skips it while pending).
- **Reconcile pending SELL (alpaca_paper, Dec 4b):** FakeAlpaca returns the SELL `pending` this
  cycle (idea stays MONITORED, no outcome), then `filled` next cycle → `_reconcile_pending_orders`
  closes the idea and stores the outcome once.
- **Partial SELL (Dec 4b):** FakeAlpaca fills 4 of 10; assert idea stays MONITORED, residual 6
  shares, next cycle re-sells 6 with a fresh dedup_hash, then closes with `label_kind="partial"`.
- **Rejected SELL:** FakeAlpaca rejects → breaker trips, `BrokerError`, engine auto-pauses, idea
  stays MONITORED, no outcome, no position change.
- **Safety / pause:** a paused or kill-switched or breaker-tripped engine runs the monitor **not at
  all** (early return) — assert no SELL is attempted while paused. Stale state: `get_positions()`
  raising → monitor skips the cycle, no SELL, audit.
- **Sim regression:** all existing ~1743 tests stay green; the monitor is a no-op when there are no
  open positions or no fired triggers.
- **No-lookahead lint clean** (`scripts/check_no_lookahead.sh`); all timestamps from the injected
  clock, all prices via PIT.

Live verification (human, not pytest): an `arbiter run` where a held name's `price_close` is below
its corrected stop should produce a real paper SELL and a `early_exit` outcome; `arbiter status`
should show the position gone and `open_positions` decremented.

---

## 6. Out-of-scope (restate)

- #3 intraday runtime loop / intraday stop polling — **the once-daily cadence means stops can gap
  through; realized exit can be far below the stop.** The single most important limitation.
- #4 learning/trust-calibration loop consuming these outcomes (outcomes are written, fed nowhere yet).
- #5 MiroFish.
- Scale-out / deliberate partial-exit strategies (v1 = full exit; broker-partial residual sweeping is
  in scope only as correctness).
- Real-money (`live_trading=true`) path.

---

## 7. Open risks

1. **Daily cadence / stop gap risk** (above). Accepted for v1; #3 closes it.
2. **Paused engine does not enforce stops** (Dec 6). Deliberate (matches INTERFACES §8 "no
   auto-close on halt"); residual downside while paused is a human-managed risk. Operator should wire
   alert + kill-switch URLs.
3. **(ticker,bucket) idea linkage fragility.** Mitigated by the recommended `orders.idea_id` column;
   if the column is dropped from v1, the join relies on the "one live bucket per held ticker"
   invariant — flag loudly.
4. **Conviction-reversal only fires on a fresh opposite opinion** (Dec 2b). A held name with no new
   filing never triggers reversal — by design (absence ≠ reversal), but it means reversal is rare in
   practice and stop/horizon do most of the work. Confirm this matches intent.
5. **`supersede_row` + UNIQUE(dedup_hash)** interaction for the exit-correction row (Dec 1): the build
   agent MUST verify whether superseded rows are excluded from the UNIQUE constraint; if not, the
   correction needs a distinct dedup_hash. A wrong choice here throws `IntegrityError` on every
   correction.
6. **`model_slippage` sign for sells.** If the slippage model only adds a cost (raises the price), a
   SELL limit set *above* the market would not fill. The build agent must confirm the sell-side limit
   is set to improve fill probability (limit at/below mark), or the SELL sits unfilled and only
   reconciles as a stuck `pending` until the day order expires.
7. **VWAP exit price for partials.** v1 uses the last fill / `label_kind="partial"`; a precise
   volume-weighted exit price across fills is deferred. Minor accuracy loss in `alpha_bps`.
8. **Could NOT fully determine** whether the MVP `_fuse` produces a **ticker-specific** conviction or
   a **bucket-pooled** one — Decision 2b handles both by preferring the unambiguous per-ticker current
   stance, but the build agent should confirm `fusion/engine.py::fuse` semantics before wiring the
   reversal check, and adjust if fusion is already per-ticker.
```
