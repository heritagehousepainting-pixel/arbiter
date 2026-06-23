# D3 Audit — Orphan Positions & Broker-vs-Local Divergence

**Lane:** D3 (READ-ONLY auditor)
**Date:** 2026-06-19
**Scope:** every way the local ledger and the real Alpaca paper broker can disagree, whether it is
detected/recovered, and the documented "accepted residual" orphan risk. Happy-path pending→filled
reconcile is D2's lane; this lane focuses on DIVERGENCE and failure windows.
**Reference:** `docs/superpowers/specs/2026-06-19-real-alpaca-paper-execution-design.md` §0 A3, §3 step 3, §4.3.
**Files reviewed:** `arbiter/execution/reconciler.py`, `arbiter/engine.py`
(`_reconcile_pending_orders`, `run_cycle`, `status`), `arbiter/execution/alpaca_adapter.py`
(`place`, `_post_with_retry`, `get_order`), `arbiter/execution/idempotency.py`.

---

## VERDICT

**The divergence reconciler is built and tested but NEVER WIRED into the live engine.** The spec's
§A3 "accepted residual" mitigation — "the per-cycle reconciler audits the orphan as `BROKER_ONLY`
for human review" — is **not actually in effect**: `reconciler.reconcile()` is called nowhere in
the engine, loop_runner, daemon, or CLI (only in `tests/execution/test_reconciler.py`). The engine's
own `_reconcile_pending_orders` is a *different, narrower* mechanism that iterates only local rows
with `status='pending'`; a lost-response orphan has NO local row, so it is structurally invisible to
the only reconciliation that actually runs. Net effect: a real broker position with no local order is
**created and then silently ignored forever** — not surfaced, not audited, not even logged. The
happy-path (pending→filled by order id) and source-of-truth reads (held_tickers / equity /
open_positions from the broker in paper mode) are correct. The orphan/divergence safety net the
design promised is the gap. P0 + P1 findings below.

---

## FINDINGS

### P0 — Divergence reconciler (`reconciler.reconcile`) is never invoked in the live system — `arbiter/execution/reconciler.py:104` / `arbiter/engine.py:843` (run_cycle)
**Why:** Spec §3 step 3 says "At the START of `run_cycle` in alpaca_paper mode … call
`reconciler.reconcile(self.conn, self.executor, as_of=now, audit_path=config.audit_path)`." A whole-tree
search (`grep -rn "reconcile(" arbiter/`) finds the only call site is the function's own definition
and the `from arbiter.execution.reconciler import reconcile` re-export in `execution/__init__.py:17`.
No caller in `engine.py`, `orchestrator/`, `cli.py`, `loop_runner`, or `daemon`. The module's
LOCAL_ONLY / BROKER_ONLY / QTY_MISMATCH audit (reconciler.py:142-180) and its `reconciler.pass`
audit record (reconciler.py:190) therefore never fire in production. The build instead implemented
`_reconcile_pending_orders` (engine.py:283), which the spec step 3 also describes — but that helper
only handles the pending→filled promotion path and does NOT perform divergence detection.
**Recommended action:** Wire `reconciler.reconcile(...)` once per cycle in `alpaca_paper` mode
(engine.py, right after `_reconcile_pending_orders(now)` at line 875), and route its `BROKER_ONLY` /
`QTY_MISMATCH` divergences to a critical-alert / human-review channel. Keep it diagnostic (no auto
state mutation), consistent with its docstring. Add a test asserting it is called once per adapter-mode cycle.

### P0 — Lost-response orphan position is created and never surfaced (A3 residual is unmitigated in practice) — `arbiter/execution/alpaca_adapter.py:123` (`_post_with_retry`) + `engine.py:875`
**Why:** Trace the A3 window. `place` builds a POST with `client_order_id = intent.order_id`
(alpaca_adapter.py:172) and calls `_post_with_retry` (max 1 retry). If attempt 0 *succeeds at Alpaca
but the HTTP response is lost*, the client sees an exception, retries; attempt 1 hits Alpaca which
rejects the duplicate `client_order_id` (non-200) → `_post_with_retry` raises `BrokerError`
(line 149) → `place` returns `status="rejected"` (line 184), and no `filled`/`pending` order row is
persisted. The position now exists at the broker with **zero local trace**. The spec's §A3 explicitly
accepts this residual ONLY because "the per-cycle reconciler audits the orphan as `BROKER_ONLY` for
human review." That reconciler is not wired (see finding above), and `_reconcile_pending_orders`
(engine.py:302, `WHERE status='pending'`) cannot see it — the orphan has no pending row. So the orphan
is real, undetected, unaudited, and unlogged. It will, however, correctly block a future duplicate BUY
(idempotency's `_check_broker` sees the position, idempotency.py:73) — so the risk is silent capital
deployment + a held position the system never sells (it has no idea/order to drive an exit), not a
double-buy.
**Recommended action:** Same fix as above (wire `reconciler.reconcile`) closes the detection gap.
Additionally consider: on a `BrokerError` during `place`, the retry should distinguish a
"duplicate client_order_id" rejection (which *proves* the original order landed → the orphan IS the
intended order; adopt/persist it) from a genuine reject. Adopting on duplicate-client-order-id would
turn the orphan into a tracked position rather than a residual. At minimum, log `order.possible_orphan`
with the `client_order_id` on the BrokerError path so the orphan is greppable.

### P1 — Orphaned broker position can never be exited by the system — `arbiter/engine.py:926` (`_run_exit_monitor`) / `arbiter/execution/exit_monitor.py`
**Why:** The exit/sell monitor drives stops and horizon exits off the *idea/order ledger*
(it closes ideas on SELL fills, engine.py:450 `_close_out_filled_sell`). A `BROKER_ONLY` orphan has no
idea and no order, so the monitor never considers it — no stop-loss, no horizon exit, no
conviction-reversal sell will ever protect that capital. On a $10k paper account a single orphan that
moves against you bleeds unmanaged. This compounds finding #2: not only is the orphan undetected, even
if a human spots it via Alpaca's UI there is no in-system path to close it.
**Recommended action:** Once divergence detection is wired, have the engine (not the reconciler)
either (a) auto-synthesize a minimal tracking order/idea for an adopted orphan so the exit monitor
manages it, or (b) raise a P0-equivalent alert instructing manual liquidation. Document the chosen
policy. Do not leave "broker holds shares the bot can't sell" as an accepted state.

### P1 — `QTY_MISMATCH` and `LOCAL_ONLY` divergences from manual broker-side changes are undetected — `arbiter/execution/reconciler.py:142,166`
**Why:** Same root cause as #1, but worth calling out the manual-intervention vectors the reconciler
was built to catch and currently does not: (a) a human manually closes/reduces a position in the
Alpaca UI → local ledger still shows `filled` (LOCAL_ONLY or QTY_MISMATCH), so the engine believes it
holds shares it no longer has, will skip "double-buy" on that ticker (held_tickers reads broker so this
specific case self-heals on held_tickers, but the idea/outcome ledger goes stale and mislabels the
outcome). (b) a broker-side partial liquidation or corporate action shifts share count → QTY_MISMATCH.
(c) a manually-placed order at the broker → BROKER_ONLY. None of these are surfaced. The `_QTY_EPSILON`
= 0.01 share threshold (reconciler.py:35) and the net-positive filter (reconciler.py:97) are reasonable;
the logic is sound — it simply never runs.
**Recommended action:** Covered by wiring `reconcile()`. When wired, ensure LOCAL_ONLY also triggers a
review of the stale idea so a phantom-held idea does not silently mis-record an outcome.

### P2 — `_reconcile_pending_orders` swallows the get_order failure window without escalation — `arbiter/engine.py:312`
**Why:** If `executor.get_order(order_id)` raises (broker unreachable mid-reconcile), the row is logged
`get_order_failed` and `continue`d (engine.py:312-318). The order stays `pending` and is retried next
cycle — correct for a transient blip. But there is no bound on how long an order may sit `pending`
while the broker is unreachable, and no alert if EVERY get_order in a cycle fails (a total broker
outage during reconcile looks identical to "nothing filled yet"). On a day order this is mostly
self-limiting (it expires at close → C2 terminal path), but a persistent partial-outage could leave a
real fill un-promoted (idea stuck pre-MONITORED) indefinitely with only a `warning` log.
**Recommended action:** Track consecutive reconcile failures per order (or per cycle) and escalate to a
critical alert after N cycles, so a silent broker-read outage during reconcile is surfaced rather than
masquerading as "no fills."

### P2 — Partial-fill residual is left to `day`-order expiry with no active re-sweep — `arbiter/engine.py:462` (`_close_out_filled_sell`, partial branch) + spec §3 step 3 partials
**Why:** On a partial SELL the idea is left MONITORED for "the residual sweep next cycle" (engine.py:462,
docstring at 450-456). On a partial BUY fill the order is marked `partial` and the idea advanced to
MONITORED on the filled portion (per spec), with the unfilled remainder intentionally NOT re-submitted.
This is a deliberate, documented choice for a small account — but it means local `qty` (full intended)
and broker `qty` (partial) diverge until the `day` order expires, and if the process does not run again
before expiry the divergence persists as a real QTY_MISMATCH that, again, nothing detects (finding #4).
The reconciler's epsilon would flag it — if it ran.
**Recommended action:** Acceptable as a policy, but the persisted local `qty` should reflect the *filled*
quantity (not the intended notional/share count) once a partial is observed, so the ledger and broker
agree in shares per the spec's own invariant (§0 A1: "ledger, reconciliation, and the broker all agree
in SHARES"). Verify `_reconcile_pending_orders` writes `filled_qty` to the row on `partial` — current
code at engine.py:335 only updates `status`, NOT `qty`, so the row keeps the original (full) qty →
permanent local-vs-broker share divergence on every partial fill. **This is an active QTY_MISMATCH
generator.** Recommend updating `qty = report.filled_qty` alongside the status on the partial branch.

### P3 — Source-of-truth reads ARE correct in paper mode (verified, no defect) — `arbiter/engine.py:935, 877, 1231`
**Why (positive verification):** Per spec §4.3, in `alpaca_paper` mode: `held_tickers` reads
`self.executor.get_positions()` (engine.py:935 — broker truth, so the don't-double-buy gate self-heals
against manual closes); sizing equity reads `account.equity` from the broker with the A2 fail-closed
gate (engine.py:884-892) blocking the cycle when equity is None/≤0; and `status()` open_positions reads
`len(self.executor.get_positions())` for the adapter vs the durable snapshot for sim (engine.py:1227-1231).
`realized_pl` is correctly nulled for the adapter (not faked, engine.py:1252). No divergence defect here.
**Residual nit:** the phantom `100_000.0` fallback at engine.py:1038 (`portfolio_equity=... else 100_000.0`)
is technically unreachable in adapter mode because the A2 gate (line 886) already returned a zero-order
CycleResult when equity is falsy — but it is dead/dangerous code that would silently size against $100k
if the gate were ever removed or reordered. Recommend deleting the fallback (let it raise) now that A2
guards it, so the two safety layers cannot drift apart.

---

## OPPORTUNITIES TO ADD

1. **Wire-and-alert the reconciler (closes P0×2 + P1×2 at one seam).** A single `reconcile()` call per
   adapter-mode cycle plus a `BROKER_ONLY`/`QTY_MISMATCH` → `Alerting` route converts the entire
   "accepted residual" from *undetected* to *surfaced-for-human-review*, which is exactly what §A3
   claimed already happens. Lowest-effort, highest-value fix in this lane.
2. **Orphan adoption on duplicate-client_order_id reject.** When `place` gets a BrokerError whose cause
   is a duplicate `client_order_id`, the original order provably landed — persist it as `filled`/`pending`
   instead of `rejected`. This eliminates the orphan class entirely rather than auditing it after the fact.
3. **Persist `filled_qty` on partial fills** (P2 above) so the ledger and broker agree in shares and the
   reconciler is not perpetually flagging self-inflicted QTY_MISMATCHes.
4. **A reconcile-health metric / audit counter** (`reconciler.pass` divergence_count, broker-read
   failures per cycle) exposed via `status()` so an operator can see "ledger and broker last agreed at T"
   without grepping audit logs.
5. **A `reconcile`/`doctor` CLI subcommand** that runs `reconciler.reconcile` on demand and prints
   divergences — gives the human the "review" the §A3 residual depends on, even before the per-cycle
   wiring lands.
6. **Outcome-integrity guard for LOCAL_ONLY.** When a held idea's broker position has vanished
   (manual close), the next outcome label is computed against a stale exit — detect LOCAL_ONLY and quarantine
   the outcome rather than recording a fabricated P/L.
