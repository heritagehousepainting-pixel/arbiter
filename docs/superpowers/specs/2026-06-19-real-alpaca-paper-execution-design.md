# Real Alpaca PAPER Execution — Design Spec (sub-project #1)

> Status: 2026-06-19. DESIGN ONLY — no implementation code is written by this spec author.
> Build agent: implement strictly from this document, plan→build→audit with disjoint file ownership,
> tests OFFLINE, verify on a live `arbiter run` afterward. Always use `.venv/bin/python` from
> `/Users/jonathanmorris/poly_bot/arbiter`.
>
> Scope: replace the in-memory `SimExecutor` with the **real Alpaca PAPER broker** on a ~$10,000
> paper account, for the order-SUBMISSION path only. Exit/sell monitor and a market-hours runtime
> loop are explicit out-of-scope follow-ons (#2, #3).

---

## 0. BINDING AMENDMENTS (post-audit — these SUPERSEDE any conflicting text below)

The plan audit returned **NO-GO until these land**. They are binding; do not relitigate.

**A0 — P0 notional→shares conversion (the headline bug).** `PaperOrder.qty` / `OrderIntent.qty` is
currently a **dollar notional**, not a share count (`compute_size` returns quarter-Kelly USD; `decide`
assigns it to `qty`; `SimExecutor._buy` does `cost = fill_price * qty`, which hid the bug). Fix at a
SINGLE seam in `submit_order`, AFTER `limit_price` is computed and BEFORE building the `OrderIntent`:
- `shares = math.floor(notional / limit_price)` where `notional = order.qty`.
- If `shares <= 0`: do NOT place, do NOT persist an order row, return a new sentinel `ZERO_SHARE_SKIP`
  (engine maps it to `False` → idea is not advanced). Log + audit `order.zero_share_skip`.
- Build `OrderIntent(..., qty=float(shares))` and persist the order row with `qty = shares` (so the
  ledger, reconciliation, and the broker all agree in SHARES). Leave `compute_size`/`decide` UNCHANGED
  (they keep producing notional); the conversion lives only in `submit_order`.
- This also CORRECTS `SimExecutor` (cost becomes `price * shares`). Existing `submit_order` tests that
  passed `qty` as a share count must be updated to pass a realistic notional (or assert the new
  share-count behavior) — fix them in the same work package; the ~1743 suite must end green.

**A1 — `get_order(order_id)` is REQUIRED, not optional** (§4.4 step 4). Add `AlpacaAdapter.get_order`
hitting `GET /v2/orders/{order_id}`; `_reconcile_pending_orders` MUST prefer it over position-presence
(position presence cannot advance a *specific* pending idea or distinguish a new fill from a
pre-existing position). Keep it on the adapter only (no `Executor` ABC change).

**A2 — Fail CLOSED on a broker account-read failure.** In `alpaca_paper` mode, if
`executor.get_account()` returns `equity` that is `None`/`<= 0` (the adapter returns zeros on a
`/v2/account` exception), the engine MUST NOT fall back to the `100_000.0` phantom equity
(engine.py:394). Instead log a critical condition and run NO sizing/orders this cycle (skip the cycle
body, return a zero-order `CycleResult`). The $100k fallback is sim-only convenience and is unsafe on a
real (paper) account.

**A3 — Lost-response orphan: accepted residual.** A POST that succeeds at the broker but whose HTTP
response is lost raises `BrokerError` → the order is not persisted locally (orphan broker position). We
do NOT pre-persist a row (it complicates the rejected-order path). Instead: `client_order_id` makes the
single retry idempotent, and the per-cycle reconciler audits the orphan as `BROKER_ONLY` for human
review. Document this as a known, paper-only residual; the reconciler does not auto-adopt an
order-less position.

**A4 — Off-hours = no-fill, logged.** If a day limit order is placed while the market is closed it
expires unfilled; the idea simply never reaches MONITORED and reconciles as no-fill. Add an explicit
log/warning when `run_cycle` submits while the market is (heuristically) closed. No new scheduler here
(that is out-of-scope #3).

**Build structure:** this change set is tightly interconnected across `submit.py` ↔ `engine.py` ↔
`alpaca_adapter.py` (the fill-confirmation invariant spans all three), so it is built by ONE focused
build agent (TDD), NOT fanned across agents onto shared files. Audit follows as a separate lane.

---

## 1. Goal

Make `arbiter run` / `run_cycle` place its BUY orders on the **real Alpaca paper brokerage**
(`https://paper-api.alpaca.markets`) against a ~$10k paper account, instead of filling them in the
process-local `SimExecutor`. Positions, cash, and equity for status/sizing/dedupe must then come from
the broker. The change must:

- keep the system **structurally paper-only** (no live-money endpoint introduced);
- not silently break the kill-switch fail-closed gate (engine must not halt every cycle);
- correctly advance an idea to `MONITORED` only on a **real fill**, not on an accepted-but-unfilled
  market order;
- preserve idempotency (no double-submits on retry) across the real broker;
- tighten guardrails appropriately for a small $10k account.

---

## 2. Foundational safety property (paper-only) — CONFIRMED

`AlpacaAdapter._base()` returns `self.config.alpaca_paper_base_url.rstrip("/")` and **nothing else**.
There is no `live_base_url` field on `Config` (verified: `config.py` has only `alpaca_paper_base_url`
and `alpaca_data_base_url`). A repo-wide grep for a live endpoint
(`api.alpaca.markets` excluding `paper-api` and `data.alpaca`) returns **NONE**. Therefore:

> **The system is structurally paper-only: every order `AlpacaAdapter` can place goes to the paper
> endpoint. There is no code path to a live-money trading endpoint.** This property is the safety
> floor for the entire sub-project and MUST NOT be weakened. The build agent must NOT add any
> live base URL, and the audit lane must grep to confirm none was added.

`alpaca_data_base_url` (`data.alpaca.markets`) is market DATA, not trading — unaffected.

---

## 3. Current state (verified)

- `build_executor(config)` (alpaca_adapter.py:298) returns `AlpacaAdapter` **only** when
  `config.live_trading AND config.alpaca_api_key AND config.alpaca_secret_key`; otherwise `SimExecutor`.
- `.env` today: `LIVE_TRADING` is present but resolves to **False** (verified via `load_config()`);
  both Alpaca keys are **SET**; `KILL_SWITCH_URL` and `ALERT_WEBHOOK_URL` are **EMPTY**;
  `EDGAR_USER_AGENT` is **EMPTY** (Form 4 ingest skipped — congress-only, unchanged here).
- The live paper account is currently ~$100k and ACTIVE (per handoff). This sub-project wants ~$10k.
- `engine.build_engine` (engine.py:606–640):
  - asserts keys present if `live_trading`;
  - calls `build_executor(config)`;
  - **if `not config.live_trading`: asserts the executor is a `SimExecutor`** and then
    `position_store.seed_executor(conn, executor)` (seeds in-memory broker from the durable snapshot).
- `engine.run_cycle`:
  - kill-switch gate (engine.py:261): `if (config.live_trading or config.kill_switch_url) and
    kill_switch.is_halted(...)` → auto-pause. `KillSwitch.is_halted` **fail-closes to True when
    `kill_switch_url` is empty** (kill_switch.py:91). So with `live_trading=true` and an empty URL,
    **every cycle halts**. This is the gate that must be handled.
  - `account = self.executor.get_account()` — used for sizing (`portfolio_equity`) and the gate.
  - `held_tickers = set(self.executor.get_positions().keys())` — the held-ticker double-buy guard.
  - end of cycle (engine.py:499): `if isinstance(self.executor, SimExecutor):
    position_store.snapshot_executor(...)` — snapshot is SimExecutor-only.
- `engine.status` (engine.py:539): `open_positions` from `position_store.open_position_count(conn)`
  (durable SimExecutor snapshot), `is_sim` from `isinstance(executor, SimExecutor)`,
  `account_equity`/`account_cash` from `executor.get_account()`.
- `orchestrator/cycle.run_cycle` (cycle.py:306–313): advances idea `FINAL_DECIDED → EXECUTED →
  MONITORED` whenever the injected `submit` callable returns **truthy**. The engine's `_bound_submit`
  returns `sub_result != "DUPLICATE_SKIP"` (engine.py:446) — i.e. truthy for **any** non-duplicate,
  including an Alpaca `pending`/unfilled market order. **This is the central correctness gap for the
  real broker** (see §6.3).
- `submit.submit_order`: idempotency check (local ledger + broker `get_positions`), slippage-adjusts
  `raw_price` into a `limit_price`, places via `executor.place`, persists the order row with
  `status = report.status` (so `pending` is recordable), trips `broker_non_200` + raises `BrokerError`
  if `report.status == "rejected"` and a breaker is present.
- `AlpacaAdapter.place`: sends `type="limit"` when `intent.limit_price is not None` (it always is, via
  slippage), else market. Returns `status="filled"` if `filled_qty>0` else `"pending"`; on
  `BrokerError` returns `status="rejected"`.
- `AlpacaAdapter.get_account`: `realized_pl` hardcoded `0.0` (no /v2/account field — adapter.py:286);
  `daily_pl = equity - last_equity`; `open_positions` from `position_count` or `len(get_positions())`.
- `reconciler.reconcile(conn, executor, as_of)` exists and compares local filled-orders ledger vs
  broker `get_positions()` — **built but NOT wired into the engine**. This sub-project wires it.
- `paper_only` on every `ExecutionReport` is computed as `not self.config.live_trading`. Under the
  naive "set `live_trading=true`" approach this would flip to **False on the paper broker** — a
  misleading flag (the orders ARE paper). This is a concrete argument for the separate-flag design.

---

## 4. Design decisions

### 4.1 Executor selection — introduce `executor_backend = sim | alpaca_paper` (DO NOT overload `live_trading`)

**Chosen approach.** Add a new explicit config field `executor_backend: str` with allowed values
`"sim"` (default) and `"alpaca_paper"`. Selection becomes:

- `executor_backend == "alpaca_paper"` AND both Alpaca keys present → `AlpacaAdapter`.
- otherwise → `SimExecutor` (fail-closed default, unchanged).

Keep `live_trading` exactly as is and **leave it `false`**. `live_trading=true` is reserved for a
future real-money path that does not exist yet; nothing in this sub-project sets it.

**Rationale.** `live_trading` is overloaded with side-effects that are wrong for paper-broker mode:

1. `build_engine` asserts `isinstance(executor, SimExecutor)` whenever `not live_trading`
   (engine.py:634) — setting `live_trading=true` to get the adapter would be the only way past that
   assert, but it also...
2. flips the kill-switch gate into "always consult, fail-closed" (engine.py:261) → halts every cycle
   with an empty URL;
3. flips `paper_only` to `False` on every `ExecutionReport` (adapter.py:187 etc.) — labelling genuine
   paper fills as non-paper, corrupting audit truth;
4. changes the web/CLI banner to "LIVE" (cli.py:48, server.py).

A dedicated `executor_backend` flag selects the broker **without** dragging those four behaviors
along, and keeps the door to a future real-money mode (which legitimately should gate on
`live_trading`) clean and separate. Net: `live_trading` continues to mean "real money" and stays
`false`; `executor_backend` means "which broker object", and `alpaca_paper` is still 100% paper
because of §2.

**Exact changes for selection:**

- `arbiter/config.py`:
  - Add `executor_backend: str` to the `Config` dataclass (place it next to `live_trading` under Core).
  - Add `"executor_backend"` to `_KNOWN_KEYS["core"]`.
  - In `load_config`, resolve: `executor_backend = _env_str("EXECUTOR_BACKEND",
    str(core.get("executor_backend", "sim")))`. Validate value ∈ {`"sim"`,`"alpaca_paper"`}; raise
    `ConfigError` otherwise (fail-closed on typos).
  - Update the module docstring field list and INTERFACES §10b.5 field list (cross-lane note — see
    §9 Open risks; this is a frozen-interface change and must be flagged, not silently diverged).
- `config/arbiter.toml`: add `executor_backend = "sim"` under `[core]` (documented default).
- `arbiter/execution/alpaca_adapter.py::build_executor`: change selection predicate to
  `if config.executor_backend == "alpaca_paper" and config.alpaca_api_key and config.alpaca_secret_key:`
  return `AlpacaAdapter`; else `SimExecutor`. Keep `**adapter_kwargs` passthrough (tests inject
  `http_post`/`http_get`/`http_delete`).
- `paper_only` truth fix: in `AlpacaAdapter`, since the adapter is structurally paper-only, set
  `paper_only=True` unconditionally on every `ExecutionReport`/`AccountSnapshot` it returns (replace
  the five `paper_only=not self.config.live_trading` occurrences with `paper_only=True`). This keeps
  audit truth correct under the new flag and is still correct even if `live_trading` is later added,
  because the adapter only ever talks to the paper endpoint.

INTERFACES.md §10b.5 must gain `executor_backend`; INTERFACES §9 default note ("Default
LIVE_TRADING=false -> SimExecutor") should be amended to "Default `executor_backend=sim` →
SimExecutor; `executor_backend=alpaca_paper` (+ keys) → AlpacaAdapter (paper endpoint only)."

### 4.2 Kill-switch handling so the engine doesn't halt every cycle

**Problem.** The gate at engine.py:261 must keep its fail-closed property in a future real-money mode,
but must not brick the paper-broker mode where `KILL_SWITCH_URL` is empty (empty URL ⇒
`is_halted → True` ⇒ auto-pause every cycle).

**Chosen approach (do BOTH a and b):**

- **(a) Gate condition.** Replace the gate predicate so the kill switch is consulted when **a real
  kill-switch URL is configured OR a future real-money mode is on**, NOT merely because we switched
  brokers:
  `if (self.config.live_trading or self.config.kill_switch_url) and self.kill_switch.is_halted(...)`
  — this line ALREADY reads correctly for the new design (it keys off `kill_switch_url`, not
  `executor_backend`). So **no change is required to the gate predicate**: with
  `executor_backend=alpaca_paper`, `live_trading=false`, and an empty `KILL_SWITCH_URL`, the gate is
  skipped and the engine does NOT halt. Confirm this with a test. The build agent MUST NOT add
  `executor_backend == "alpaca_paper"` into this predicate — doing so would re-introduce the
  halt-every-cycle bug.
- **(b) Recommended USER decision.** Because we are about to let the bot hit a real (paper) brokerage
  unattended, recommend the user stand up a trivial kill-switch endpoint and set `KILL_SWITCH_URL`.
  This is OPTIONAL for paper but is the honest safety posture. The endpoint contract is already
  defined (kill_switch.py): `GET <url> → 200 {"halted": false}`. A static file/JSON on any always-on
  host (or a tiny serverless function) satisfies it. If the user sets it, the engine fail-closes
  correctly (unreachable ⇒ halt) — which is desirable for unattended trading. This is a USER SETUP
  item (§8), not code.

**Net:** code path requires no gate change; the safety upgrade is a user-set URL. Document both.

### 4.3 Position & account source of truth (per mode)

With `AlpacaAdapter`, `get_positions()` and `get_account()` hit the broker; the SimExecutor-only
`sim_positions`/`sim_account` snapshot is meaningless. Resolve as follows.

**Principle:** the executor object is the source of truth for *current* positions/cash/equity in BOTH
modes. The Phase-2 durable snapshot is only needed because `SimExecutor` is in-memory and would
otherwise lose state across processes. The real broker is *itself* durable, so we do NOT snapshot it
into `sim_positions`.

| Concern | sim mode | alpaca_paper mode |
|---|---|---|
| `held_tickers` double-buy guard (engine.py:301) | `executor.get_positions()` (in-mem, seeded from snapshot) | `executor.get_positions()` (broker) — **already correct, no change** |
| sizing `portfolio_equity` (engine.py:394) | `account.equity` from SimExecutor | `account.equity` from broker `/v2/account` — **already correct** |
| gate `account` (engine.py:288) | SimExecutor account | broker account — **already correct** |
| idempotency broker check (idempotency.py:73) | SimExecutor positions | broker positions — **already correct** |
| `seed_executor` at build (engine.py:639) | YES (restore in-mem broker) | **SKIP** (broker is already durable) |
| `snapshot_executor` end of cycle (engine.py:499) | YES | **SKIP** (already gated by `isinstance(SimExecutor)`) |
| `status.open_positions` (engine.py:546) | `position_store.open_position_count(conn)` | **broker** `len(executor.get_positions())` |
| `status.is_sim` | True | False |
| reconciliation | n/a | run `reconciler.reconcile` once per cycle (NEW wiring) |

Because almost every read already goes through `self.executor`, most of the runtime path is correct
for free. The two real edits are at `build_engine` (skip seed) and `status` (broker counts), plus the
snapshot is already correctly gated. Exact changes:

- `engine.build_engine` (the `if not config.live_trading:` block, engine.py:633–640): change the
  branch to key on the **executor type**, not `live_trading`. New logic:
  - `if isinstance(executor, SimExecutor): position_store.seed_executor(conn, executor)`.
  - else (`AlpacaAdapter`): do nothing (no seed). Drop the old `assert isinstance(... SimExecutor)`
    (it was the paper-only guard; the §2 structural guarantee + the `executor_backend` validation now
    cover safety). Optionally add `assert isinstance(executor, (SimExecutor, AlpacaAdapter))` to keep
    a defensive type check.
- `engine.run_cycle` end-of-cycle snapshot (engine.py:499): unchanged — it is already
  `if isinstance(self.executor, SimExecutor):`, so it correctly no-ops for the adapter.
- `engine.status` (engine.py:539): compute `open_positions` per mode:
  `if isinstance(self.executor, SimExecutor): open_positions = position_store.open_position_count(conn)
  else: open_positions = len(self.executor.get_positions())`. Keep `account_equity`/`account_cash`
  from `self.executor.get_account()` (already mode-correct). `is_sim` already reflects the type.

**P&L gap (adapter.py:286).** `get_account().realized_pl` is hardcoded `0.0` for the adapter
(/v2/account has no realized_pl). `status` must therefore NOT present broker `realized_pl` as truth in
alpaca_paper mode. Decision: `status` exposes `account_equity` and `account_cash` (both real from
/v2/account) and OMITS or labels `realized_pl` as unavailable for the adapter. `daily_pl`
(`equity - last_equity`) IS real and may be surfaced. Document this clearly; do not "fix" realized_pl
in this sub-project (it needs a separate P&L source — out of scope, note in §9).

### 4.4 Fill reconciliation & async fills

Market/limit orders on a real broker may not fill instantly (and the paper book mirrors live market
hours). Two problems: (1) an order may come back `pending`; (2) the cycle currently treats any
non-duplicate submit as success and advances the idea to `MONITORED`.

**Chosen approach:**

1. **Order type.** Keep the existing limit-order behavior: `submit_order` always passes a
   slippage-adjusted `limit_price`, so `AlpacaAdapter.place` sends a `type="limit"`, `tif="day"`
   order. This is deterministic and avoids unbounded market slippage on a small account. (A future
   change could use marketable limits / `tif=gtc`; not now.)

2. **Advance to MONITORED only on a confirmed fill.** The fill semantics must be made explicit. The
   build agent implements a **fill-confirmation step** keyed off `ExecutionReport.status`:
   - `submit_order` already returns a sentinel string or order_id. Extend `_bound_submit` (engine.py)
     to distinguish three outcomes from the `ExecutionReport`/return:
     - `DUPLICATE_SKIP` → return `False` (do not advance; unchanged).
     - report `status == "filled"` (filled_qty>0) → return `True` (advance to MONITORED). Correct.
     - report `status == "pending"` (accepted, not yet filled) → **return `False`** so the cycle does
       NOT advance the idea to MONITORED this cycle. The order row is persisted with `status="pending"`
       (submit.py already records `report.status`), the idea stays at `FINAL_DECIDED`/`EXECUTED`-pending,
       and the NEXT cycle reconciles (see step 3) and advances it.
   - To do this, `_bound_submit` needs the report status, not just a bool. **Design choice:** change
     `submit_order` to return the `ExecutionReport` (or a small result struct
     `SubmitResult(order_id|None, status, duplicate: bool)`) instead of the bare `str`/sentinel, and
     update `_bound_submit` to interpret it. This is a contained change to `submit.py` +
     `engine._bound_submit`; `orchestrator/cycle.py`'s `submit` callable signature stays
     `(order)->bool` (the engine adapts the richer result down to a bool). Existing `submit_order`
     callers in tests must be updated (flag as part of the work package).
   - **Important:** for `SimExecutor`, `place` always returns `filled` (synchronous), so this logic is
     a no-op for sim mode — sim behavior is preserved. Confirm with existing tests.

3. **Reconciliation each cycle (wire `reconciler.py`).** At the START of `run_cycle` in alpaca_paper
   mode (after the kill-switch/breaker gates, before building new ideas), call
   `reconciler.reconcile(self.conn, self.executor, as_of=now, audit_path=config.audit_path)`. Its job
   here: detect `pending`→`filled` transitions and divergences. Concretely:
   - It already audits LOCAL_ONLY / BROKER_ONLY / QTY_MISMATCH. Extend the engine wiring so that for a
     ticker that the local ledger has as `pending` and the broker now shows as a position
     (BROKER_ONLY w.r.t. the reconciler's filled-only local query, OR a status check on the order),
     the engine **promotes the order row `pending → filled`** and advances the corresponding idea
     `→ MONITORED` via `idea_store.update_idea_state`. (The reconciler stays diagnostic/audit-only;
     the engine owns the state mutation, consistent with the reconciler's "engine decides what to do"
     docstring.)
   - Simplest robust implementation: add a small engine helper `_reconcile_pending_orders(now)` that
     (a) selects local orders with `status='pending'`, (b) checks the broker via
     `executor.get_positions()` (and/or a new `get_order(order_id)` — see below), (c) on confirmed
     fill updates the order row to `filled` and transitions the idea to MONITORED, (d) audits. Run it
     each cycle in alpaca_paper mode only.
   - **Partial fills.** If broker reports `0 < filled_qty < qty`: persist `status="partial"` and the
     `filled_qty`. Treat the idea as MONITORED on the filled portion (a real position exists). Do NOT
     re-submit the remainder automatically (avoids runaway orders on a small account); audit the
     shortfall for human review. Leftover `day` orders expire at market close, so no dangling order
     persists overnight.
   - **Rejected fills.** `status=="rejected"` already trips `broker_non_200` and raises `BrokerError`
     in `submit_order` (submit.py:199) → engine fires critical alert + auto-pauses. Idea is NOT
     advanced (submit returns via the raise path). Unchanged; confirm.

4. **Optional broker order lookup.** To reconcile by order id rather than only by position presence,
   the build MAY add `AlpacaAdapter.get_order(order_id) -> ExecutionReport` hitting
   `GET /v2/orders/{order_id}` (status/filled_qty/filled_avg_price). This is the cleaner reconciliation
   primitive (a ticker can have an order without yet being a position). Recommended but optional;
   keep it on the adapter only (do not add to the `Executor` ABC unless sim gets a stub) to avoid a
   cross-lane ABC change. If added, `_reconcile_pending_orders` prefers `get_order` over the
   position-presence heuristic.

**Note on `daily_pl`/`realized_pl` in `get_account`** (adapter.py:282–287): `realized_pl=0.0` is a
known gap; `status` must not treat it as real (see §4.3). `daily_pl = equity - last_equity` is real and
fine. Call out in §9.

### 4.5 Idempotency across the real broker — CONFIRMED HOLDS

The dedup ledger is broker-agnostic and continues to hold:

- `dedup_hash = sha256(ticker|side|horizon|entry_date|advisor_signature)` (idempotency.py:30) is
  computed from the `PaperOrder`, independent of executor.
- `ensure_not_duplicate` checks (1) the local `orders` table for the hash AND (2) the broker via
  `executor.get_positions()`; on broker exception it **fails closed** (treats as potential duplicate,
  blocks the submit) — correct for the real broker (a flaky broker must not cause a double-buy).
- The DB `UNIQUE(dedup_hash)` constraint is the backstop; `submit_order` catches `IntegrityError` on a
  race and returns `DUPLICATE_SKIP`.
- Retry path: `AlpacaAdapter._post_with_retry` does exactly **1 retry** then raises `BrokerError`. A
  retried POST could in theory create two broker orders if the first actually succeeded but the
  response was lost. Mitigations: (a) the pre-submit local-ledger + broker-position check; (b) the
  `pending`-aware reconciliation in §4.4 which will surface a duplicate broker position as a
  divergence for human review; (c) **recommended hardening:** send a client order id to Alpaca so the
  broker itself dedupes the retry. **Design choice:** set
  `body["client_order_id"] = intent.order_id` in `AlpacaAdapter.place`. Alpaca rejects a duplicate
  `client_order_id`, making the retry idempotent at the broker. This is a small, high-value addition —
  include it. (`order_id` is the ULID, globally unique per logical order.)

With (c) added, the design guarantees no double-submit on the single retry: the second POST either
hits the already-recorded broker order id (rejected as duplicate client_order_id) or the local
ledger/position check blocks the next cycle.

### 4.6 Safety / guardrails for unattended paper trading on $10k

Existing caps (config + policy, INTERFACES §9), all still applied via `decide`:

- `max_position_pct = 0.05` (5%/name), `max_sector_pct = 0.20`, `max_gross_pct = 0.80`,
  `max_open_positions = 20`, `adv_cap_pct = 0.02` (2% of 20d ADV, last transform).
- Quarter-Kelly sizing; quorum (2+ live advisors → full, 1 → 0.25, 0 → HALT).
- Latching circuit breakers (daily loss ≥2%, per-position −5% intraday, broker non-200, etc.) +
  auto-pause on critical alert. Kill switch (broker-side) blocks new orders.

**Tightened for a $10k account (recommended config overrides, NOT new code paths):**

- `max_open_positions`: lower from 20 → **8–10**. On $10k, 20 names at 5% each = $500 notional/name —
  fractional/odd-lot churn and per-order friction dominate. Fewer, more meaningful positions.
- `max_position_pct`: keep **0.05** (5% = $500) or modestly raise to ~0.08 so positions clear a
  sensible minimum notional; recommend leaving at 0.05 to stay conservative. (Decision left to user;
  document the tradeoff.)
- `max_gross_pct`: lower from 0.80 → **0.50** for an unattended paper account (cap total exposure).
- `adv_cap_pct`: keep 0.02 (irrelevant at $10k scale but harmless).
- Whole-share rounding: a $10k account at 5% ($500) buying a $300 stock = 1 share. The sizing path
  must floor to whole shares and **skip orders that round to 0 shares** (verify `decide`/sizing does
  this; if not, it is a small fix — flag in the work package). Alpaca paper supports fractional shares
  for market orders but NOT for limit orders — and we send **limit** orders — so **whole-share
  rounding is mandatory**. This is a correctness requirement, not just a guardrail.

All of the above are config values (`ARBITER_MAX_OPEN_POSITIONS`, `ARBITER_MAX_GROSS_PCT`, etc.) and
go in `.env` / `arbiter.toml` — **no new live path, no new code** beyond the whole-share-rounding
verification. Keep `live_trading=false`.

### 4.7 Scope boundary

IN scope (this sub-project #1): selecting and wiring the real Alpaca paper broker for the **order
submission path**, fill reconciliation for BUY orders, position/account source-of-truth in
alpaca_paper mode, idempotency hardening (`client_order_id`), kill-switch gate handling, and $10k
guardrail config.

OUT of scope — explicit follow-ons:

- **#2 Exit/sell monitor.** Generating and submitting SELL orders at stop-loss / horizon-expiry /
  conviction-reversal (the `PaperOrder.exits` dict) is NOT wired here. Today the engine only submits
  the entry BUY; exits are stored but never acted on. This sub-project does not add the sell path.
- **#3 Market-hours runtime loop.** A long-running scheduler that respects market hours, polls fills
  intraday, and re-runs reconciliation between cycles is out of scope. We rely on the existing daily
  launchd schedule + next-cycle reconciliation for `pending` fills.
- Real-money (`live_trading=true`) path: explicitly NOT built; reserved.
- Broker realized-P&L source: not built (status omits/labels it).

---

## 5. Test strategy (OFFLINE — broker network stays mocked)

Hard rule (PHASE2 plan #4, INTERFACES §11.7): **pytest never hits the network.** All Alpaca calls go
through `AlpacaAdapter.http_post/http_get/http_delete`, which are injectable dataclass fields.

- **Fake AlpacaAdapter via injected HTTP callables.** Construct `AlpacaAdapter(config=cfg,
  http_post=fake_post, http_get=fake_get, http_delete=fake_delete)` where the fakes return canned
  Alpaca JSON. Provide a small reusable `FakeAlpaca` test helper that maintains an in-memory
  positions/account/orders dict and responds to:
  - `POST /v2/orders` → echo an order with controllable `filled_qty`/`filled_avg_price`/`status`
    (filled, pending, partial); record `client_order_id`; reject a duplicate `client_order_id`.
  - `GET /v2/positions` → current positions; `GET /v2/account` → cash/equity/last_equity/position_count;
    `GET /v2/orders/{id}` (if `get_order` added) → order status; `DELETE /v2/orders/{id}` → cancel.
- **build_executor selection tests:** `executor_backend="sim"` → SimExecutor; `="alpaca_paper"` +
  keys → AlpacaAdapter; `="alpaca_paper"` + missing key → SimExecutor (fail-closed); invalid value →
  `ConfigError`.
- **Engine in alpaca_paper mode:** inject the adapter (via `build_engine` with a config that sets
  `executor_backend=alpaca_paper` and a stubbed adapter, OR add an injection seam — prefer building the
  engine and monkeypatching `build_executor` to return a `FakeAlpaca`-backed adapter). Assert:
  - kill-switch gate is SKIPPED when `kill_switch_url` empty and `live_trading=false` → cycle runs (no
    auto-pause);
  - `seed_executor`/`snapshot_executor` are NOT called in adapter mode (no `sim_positions` writes);
  - `held_tickers`, sizing equity, and idempotency read from the fake broker;
  - a `filled` report advances the idea to MONITORED; a `pending` report does NOT (idea stays
    pre-MONITORED, order row `status='pending'`);
  - next-cycle reconciliation promotes `pending → filled` and advances the idea to MONITORED;
  - a duplicate `client_order_id` retry does not create a second logical order;
  - a `rejected` report trips the breaker, raises `BrokerError`, fires the critical alert, pauses.
- **Sim-mode regression:** all existing ~1743 tests stay green; sim `place` returns `filled`
  synchronously so the new fill-confirmation branch is a no-op for sim.
- **status tests:** sim mode → `open_positions` from durable count; adapter mode → from
  `len(get_positions())`; `realized_pl` not presented as truth in adapter mode.
- **Paper-only grep test (audit lane):** assert no live base URL string exists in the package.

A live `arbiter run` against the real $10k paper account is the human verification step (NOT pytest),
run twice to confirm no double-buy and that a pending fill reconciles on the second run.

---

## 6. USER SETUP checklist (only the user can do these)

1. **Create/reset a $10k Alpaca paper account.** In the Alpaca dashboard
   (`app.alpaca.markets/paper/dashboard/overview`): either reset the existing paper account to a
   **$10,000** starting balance, or create a fresh paper account funded at $10,000. Balances are set
   in the dashboard — **our code cannot set them.**
2. **Confirm the API keys in `.env` belong to THAT $10k account.** Regenerate the paper key/secret on
   that account if needed and update `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` in `arbiter/.env`. (If a
   different account than the current ~$100k one is used, the keys MUST be the new account's.)
   Verify by hitting `/v2/account` and checking equity ≈ $10,000 before the first real run.
3. **Set `EXECUTOR_BACKEND=alpaca_paper`** in `arbiter/.env` (leave `LIVE_TRADING=false`). This is the
   single switch that turns on the real paper broker.
4. **Kill-switch decision (recommended).** Decide whether to stand up a kill-switch endpoint. For
   unattended trading, recommend YES: host a tiny always-on endpoint returning `{"halted": false}` and
   set `KILL_SWITCH_URL` in `.env`. Then the engine fail-closes (unreachable ⇒ halt) — the safe
   posture. If left empty, the engine simply skips the gate (acceptable for paper, but no remote
   stop). Flag the tradeoff to the user.
5. **Tighten guardrails for $10k (config, user-set):** set in `.env` (or `arbiter.toml`):
   `ARBITER_MAX_OPEN_POSITIONS=8`, `ARBITER_MAX_GROSS_PCT=0.50`, keep `ARBITER_MAX_POSITION_PCT=0.05`.
   Adjust per §4.6.
6. **(Optional) set `ALERT_WEBHOOK_URL`** so critical auto-pause alerts actually reach the user during
   unattended runs.
7. **Market hours.** Run during/near US market hours so limit orders can fill same-session; otherwise
   day orders expire unfilled and reconcile as no-fill (idea won't reach MONITORED). (Full intraday
   handling is out-of-scope #3.)

---

## 7. Files / functions to change (build-agent map)

- `arbiter/config.py` — add `executor_backend` field + known-key + env override + value validation;
  update docstring.
- `config/arbiter.toml` — add `executor_backend = "sim"` under `[core]`.
- `arbiter/execution/alpaca_adapter.py` —
  - `build_executor`: select on `executor_backend == "alpaca_paper"` (+ keys);
  - `place`: add `body["client_order_id"] = intent.order_id`; set `paper_only=True` (5 sites);
  - optional new `get_order(order_id)`.
- `arbiter/execution/submit.py` — `submit_order` returns a richer result (ExecutionReport or
  SubmitResult) instead of bare str sentinel; update internal flow + all callers/tests.
- `arbiter/engine.py` —
  - `build_engine`: seed only when `isinstance(executor, SimExecutor)`; drop the `not live_trading`
    SimExecutor assert (or replace with `(SimExecutor, AlpacaAdapter)` defensive check); do NOT add
    `executor_backend` into the kill-switch predicate;
  - `run_cycle`: `_bound_submit` interprets `filled` vs `pending` (only `filled` → `True`); add
    `_reconcile_pending_orders(now)` and call it each cycle in adapter mode;
  - `status`: mode-aware `open_positions`; don't surface adapter `realized_pl` as truth.
- `arbiter/execution/reconciler.py` — wire into the engine (the file itself likely needs no change; the
  engine owns the pending→filled promotion + idea transition).
- INTERFACES.md — §9 + §10b.5: add `executor_backend`, amend the default-executor note (flag as a
  cross-lane frozen-interface change; get sign-off, don't silently diverge).
- Tests under `tests/execution/` and `tests/` engine — add the FakeAlpaca helper + the cases in §5.

---

## 8. Out-of-scope (restate)

- #2 exit/sell monitor (acting on `PaperOrder.exits`).
- #3 market-hours intraday runtime loop / intraday fill polling.
- Real-money `live_trading=true` path.
- Broker realized-P&L source.

---

## 9. Open risks

1. **INTERFACES.md is a frozen contract** ("do not redefine these names elsewhere; cross-lane event —
   flag it"). Adding `executor_backend` to `Config` (§10b.5) and amending the §9 selection note is a
   frozen-interface change. RISK: silently diverging. MITIGATION: treat it as a deliberate, documented
   amendment to INTERFACES in the same change; have the audit lane verify the field list matches.
2. **`submit_order` return-type change** ripples to existing callers/tests (it currently returns a
   `str`). RISK: breaking the ~1743 green suite. MITIGATION: keep a thin compatibility shim or update
   all callers in the same WP; sim path must stay behavior-identical.
3. **Async fills crossing cycle boundaries.** With a daily-only schedule (out-of-scope #3), a `pending`
   order placed near close may fill after the process exits; it only reconciles on the *next* day's
   run. RISK: idea sits pre-MONITORED for up to a day; horizon clock semantics. ACCEPTABLE for paper
   MVP; documented. The reconciler closes the loop on the next run.
4. **Retry double-order window.** Even with `client_order_id` dedupe, a pathological broker could
   behave unexpectedly. MITIGATION: client_order_id + pre-submit checks + reconciliation divergence
   audit. Residual risk is low and paper-only.
5. **Whole-share rounding / 0-share orders on $10k.** If the sizing path does not already floor to
   whole shares and skip 0-share orders, limit orders will be rejected by Alpaca (no fractional limit
   orders). MUST verify in `decide`/sizing; small fix if missing. Flagged as a correctness item, not
   just a guardrail.
6. **`realized_pl` unavailable from /v2/account.** Status cannot report true realized P&L in adapter
   mode without a separate P&L source (out of scope). RISK: misreading status. MITIGATION: omit/label
   it; surface only `equity`, `cash`, `daily_pl`.
7. **Could NOT determine** whether the sizing/`decide` path already floors to whole shares (risk #5) —
   the build agent must read `arbiter/policy/` sizing before implementing and fix if absent. This spec
   author did not open the policy sizing module; everything else above was verified against source.
