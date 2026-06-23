# D1 Audit — Order submission & dedup_hash idempotency

- **Auditor lane:** D1 (READ-ONLY) — `execution/submit.py` (submit_order, SubmitResult, order-row persistence, rejected→BrokerError path) and `execution/idempotency.py` (dedup_hash, ensure_not_duplicate, is_exit bypass, client_order_id).
- **Date:** 2026-06-19
- **Spec oriented via:** `docs/superpowers/specs/2026-06-19-real-alpaca-paper-execution-design.md` §0 (A0/A3), §4.5 (dedup ledger, client_order_id), §4.4 (rejected-fills).
- **Out of scope (other lanes):** fill-confirmation/reconcile (D2), orphans (D3), sells (D4).

## VERDICT: PASS (with hardening) — core idempotency is sound; one P1 latent persistence gap and several P2/P3 robustness items.

The dedup_hash composition is correct and excludes `qty` (re-sizing cannot dodge dedup). The local-ledger check, the `UNIQUE(dedup_hash)` backstop with `IntegrityError` handling, the broker-position fail-closed check, the `is_exit` local-only bypass, and `client_order_id = intent.order_id` for broker-side retry idempotency are all present and behave as the spec requires. No live double-submit or trivial dedup bypass was found. The one substantive defect is a rejected-order persistence gap that only fires when a caller passes `breaker=None` (not the live engine path today).

---

## FINDINGS

### P1 — A rejected order IS persisted when `breaker is None` — submit.py:309 / submit.py:337-339 — the rejected→BrokerError→no-persist guarantee is conditional on a breaker being supplied — recommend action below.

The spec (§0 A0 "do NOT persist an order row" for skips; §4.4 "Rejected fills … raises `BrokerError`"; A3 "we do NOT pre-persist a row … complicates the rejected-order path") requires a rejected broker order to NEVER land in the local ledger. The rejection handling is guarded by `if report.status == "rejected" and breaker is not None:` (submit.py:309). When `report.status == "rejected"` but `breaker is None`, that whole block (which raises `BrokerError` before persistence) is skipped, and control falls through to step 4 where `status = report.status` (= `"rejected"`) is written into the `orders` table via `_insert_order_row` (submit.py:337-339). This persists a `status="rejected"` row that holds the `dedup_hash` UNIQUE slot — which then *blocks all future retries of that logical order* via the local-ledger check (`_check_local_ledger`), i.e. a transient rejection becomes a permanent dedup poison.

Mitigating fact: the live engine caller always passes `breaker=self.breaker` (a non-None `CircuitBreaker`, engine.py:217/1078), and `SimExecutor` never returns `"rejected"`, so the bug is **not reachable on today's live path**. It is reachable via the exit-monitor caller's `breaker: CircuitBreaker | None = None` default (exit_monitor.py:521) if ever invoked without a breaker, and via any future/test caller. This is a latent correctness landmine, not a live P0.

Recommended action: make the rejected-order non-persistence unconditional. Detect `report.status == "rejected"` first and `raise BrokerError` (and skip persistence) regardless of `breaker`; only the *breaker-tripping* side-effect (`check_broker_non_200`) should be gated on `breaker is not None`. I.e. split "raise + don't persist" (always) from "trip breaker" (when breaker present).

### P2 — `_check_broker` uses `get_positions()` only; an accepted-but-unfilled (open) BUY order is invisible to the broker-side dedup check — idempotency.py:73-98 — a same-cycle/cross-cycle resubmit before a fill is caught only by the local ledger, not the broker — recommend documenting / open-order check.

`_check_broker` calls `executor.get_positions()` and treats `ticker in positions` as the duplicate signal. A limit order that Alpaca has accepted but NOT yet filled creates NO position, so the broker-side check returns `False` for it. The docstring already acknowledges this ("Open-order polling is broker-specific; the base executor ABC does not expose `get_open_orders()`"). In practice the local-ledger row (persisted with `status="pending"`) is the backstop that prevents a duplicate, and `client_order_id` is the broker-side backstop for the single retry — so this is acceptable per the spec's layered design (§4.5). But the broker arm of `ensure_not_duplicate` is weaker than the prose "checks … the broker via `get_positions()`" implies for the pending window. Recommended action: leave as-is for D1 scope but note that the true cross-process double-submit guard during the pending window is `UNIQUE(dedup_hash)` + `client_order_id`, not the position check; consider an `AlpacaAdapter`-only open-orders check if cross-process concurrency is ever introduced.

### P2 — `dedup_hash` is duplicated in two places that can silently drift — idempotency.py:30-46 vs policy/decision.py:59-66 — two independent SHA-256 compositions; a change to one and not the other would split the hash space — recommend single source of truth.

`idempotency.dedup_hash(order)` builds `ticker|side.value|horizon_bucket.value|str(entry_date)|advisor_signature`; `policy/decision._dedup_hash(...)` builds `ticker|side.value|horizon.value|entry_date.isoformat()|advisor_signature`. Today these are equal (for a `date`, `str(d) == d.isoformat()`, and the field order matches), and `decision` stamps the value onto `PaperOrder.dedup_hash` while `submit_order` *recomputes* via `idempotency.dedup_hash` (it does NOT trust `order.dedup_hash`). Because submit recomputes, the persisted hash is always the idempotency-module hash — so a drift in `decision._dedup_hash` would not break submit dedup directly, but it WOULD make `PaperOrder.dedup_hash` (carried on the object, surfaced in audit/UI and used by exit_monitor's owning-order lookups) disagree with the ledger. Recommended action: have `decision._dedup_hash` call `idempotency.dedup_hash` (or share one helper) so the two can never diverge; add a test asserting `idempotency.dedup_hash(order) == order.dedup_hash` for a freshly-decided order.

### P3 — `str(order.entry_date)` is type-fragile in the hash — idempotency.py:43 — if `entry_date` is ever a `datetime` or a string instead of a `date`, the hash silently changes and dedup breaks open — recommend explicit `.isoformat()` / type assertion.

`dedup_hash` does `str(order.entry_date)`. `PaperOrder.entry_date` is typed `date` (seams.py:263) and for a `date` `str()` is the ISO `YYYY-MM-DD`, matching `decision`'s `.isoformat()`. But `str()` of a `datetime` is `'YYYY-MM-DD HH:MM:SS'` and `str()` of an already-stringified date is a no-op — both would produce a DIFFERENT hash than `decision._dedup_hash`, splitting the dedup space and allowing a duplicate to slip the local-ledger check (the persisted row from `decision`-time would not be found). This is a fail-OPEN failure mode (the dangerous direction). Recommended action: coerce explicitly with `order.entry_date.isoformat()` (mirroring decision.py) or assert `isinstance(order.entry_date, date) and not isinstance(order.entry_date, datetime)`.

### P3 — Hash-collision surface is acceptable but `advisor_signature` `exit:{nonce}` suffix is the only entropy distinguishing residual sweeps — idempotency.py:39-46 / exit_monitor.py:159-170 — collision risk is cryptographically negligible; flagged only for completeness — no action required.

The five hashed fields (ticker, side, horizon, entry_date, advisor_signature) joined by `|` with a justified "no field contains `|`" assumption. SHA-256 makes accidental collision negligible. The one place the separator assumption matters: exit_monitor appends `|exit:{nonce}` INTO `advisor_signature` (exit_monitor.py:170) to make partial-residual sweeps hash-distinct. This means `advisor_signature` CAN contain `|`, mildly violating the "none of the fields can contain `|`" comment in `dedup_hash` — but since it only ever *adds* entropy at the tail (and the BUY-side signatures never contain `|`), it cannot cause a BUY/SELL or two-different-orders collision. No correctness issue. Recommended action: update the `dedup_hash` docstring's "safe because none of the fields can contain `|`" claim to acknowledge `advisor_signature` may carry an `exit:` suffix.

---

## CROSS-CHECKS CONFIRMED (no finding)

- **qty is NOT in the hash** (idempotency.py:39-46): re-sizing the same logical order produces the SAME dedup_hash → cannot dodge dedup. CONFIRMED — the headline anti-bypass property holds.
- **UNIQUE(dedup_hash) backstop** (migrations/001_core.sql:88 `dedup_hash TEXT NOT NULL UNIQUE`) + **IntegrityError → DUPLICATE_SKIP** (submit.py:340-353): race between check and insert is caught and returns `duplicate=True`. CONFIRMED, matches §4.5.
- **Broker check fails CLOSED** (idempotency.py:85-98): an exception from `get_positions()` is caught and re-raised as `DuplicateOrderError` (block the submit), NOT swallowed. CONFIRMED, matches §4.5 ("a flaky broker must not cause a double-buy").
- **is_exit bypass** (idempotency.py:147-150): EXIT SELLs skip ONLY the broker position-presence check (holding the position is the precondition) but STILL run the local-ledger check, so a repeated identical SELL (same dedup_hash) stays blocked. CONFIRMED, matches B3 intent.
- **client_order_id = intent.order_id** (alpaca_adapter.py:172) and the retry reuses the SAME `body` (alpaca_adapter.py:123-178 `_post_with_retry` POSTs the identical body on both attempts) → the single lost-response retry is idempotent at Alpaca (duplicate client_order_id rejected). CONFIRMED, matches §4.5(c) and A3.
- **Rejected order NOT persisted on the live path** (submit.py:309-331): with a breaker present, a `"rejected"` report trips `broker_non_200` and `raise BrokerError` BEFORE `_insert_order_row`, so no row is written. CONFIRMED for the live engine caller (see P1 for the `breaker=None` gap).
- **ZERO_SHARE_SKIP does not persist** (submit.py:256-275): `shares <= 0` returns the sentinel with no `_insert_order_row`. CONFIRMED, matches A0.
- **submit recomputes the hash** (submit.py:198 `dh = dedup_hash(order)`) rather than trusting `order.dedup_hash`, so the persisted/checked hash is internally consistent within the execution module.

---

## OPPORTUNITIES TO ADD

1. **Make rejected-non-persistence unconditional (resolves P1).** Restructure submit.py:309 so that `report.status == "rejected"` always raises `BrokerError` and never persists; gate ONLY the `check_broker_non_200` breaker side-effect on `breaker is not None`.
2. **Single dedup_hash helper (resolves P2-drift + P3-fragility).** Collapse `decision._dedup_hash` and `idempotency.dedup_hash` to one function using `entry_date.isoformat()`, and add a regression test `idempotency.dedup_hash(order) == order.dedup_hash`.
3. **Property test for the anti-bypass invariant.** Assert that two `PaperOrder`s differing ONLY in `qty` hash identically, and that orders differing in ANY of the five keyed fields hash differently — locks in the "re-sizing can't dodge dedup" guarantee.
4. **Persist a small negative-cache / audit-only record for rejections** (without occupying the `dedup_hash` UNIQUE slot) so a transient rejection is observable in the ledger/audit without poisoning future retries — currently a rejected order leaves only an audit-log entry (or, on the buggy path, a poisoning row).
5. **`AlpacaAdapter`-level open-orders dedup check** (`GET /v2/orders?status=open`) to cover the accepted-but-unfilled window (P2) if cross-process concurrency is ever added; today the local ledger + client_order_id cover it within one process.
6. **Assert `entry_date` type at PaperOrder construction** (e.g. in `decide`) so a stray `datetime`/`str` can never reach the hash and silently fail dedup OPEN (P3).
