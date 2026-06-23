# G1 — Test COVERAGE Gaps Audit (Arbiter)

**Auditor lane:** G1 — what is UNDER-tested (distinct from G2 = tests that MASK bugs).
**Date:** 2026-06-19
**Repo:** `/Users/jonathanmorris/poly_bot/arbiter`
**Method:** `pytest --cov=arbiter --cov-report=term-missing` (coverage 7.14.1) over the full suite.
**Suite state:** 1877 passed, 0 failed. **Line coverage: 88% (885 / 7105 stmts uncovered).**

> Headline coverage is healthy, but it is concentrated in pure functions and the
> heavily-fixtured SimExecutor happy path. The uncovered 12% is disproportionately
> the code that only runs **in production**: the daemon loop, the cron one-shot
> entrypoint, the Alpaca live/paper executor's failure branches, ingest fault
> isolation, and the broad `except` blocks that are the system's last line of
> defense. Those are exactly the surfaces where an untested bug does the most
> damage (silent halt, double order, swallowed exception, stuck position).

---

## VERDICT

**CONDITIONAL PASS.** No critical logic is wholly unguarded *at the unit level*,
but the **production-only seams are thin**: the real cron entrypoint
(`loop_runner.main`), the daemon's heartbeat/flock/post-close machinery, and the
Alpaca adapter's failure-mapping branches are the highest-risk untested surfaces.
The numbers that matter most for a money system — partial-fill reconciliation at
the *engine* level, the IntegrityError dedup race, and the "swallow and continue"
exception handlers — are under-tested relative to the harm a regression would cause.
Recommend closing the P0/P1 items below before the paper→live gate (Phase 7).

---

## FINDINGS

### P0 — Real cron entrypoint `loop_runner.main()` is entirely untested — `arbiter/orchestrator/loop_runner.py` (57%, missing 149-186)
`run_once()` is well covered, but `main()` — the function `arbiter run` actually
calls from cron/launchd — is not exercised at all. It contains the load-bearing
**flock coexistence guard**: it acquires the daemon's single-instance lock and
*no-ops the one-shot if the daemon already holds it* (lines 162-166), then runs
ingest+cycle and releases in `finally`. A bug here means either two processes
mutate the same SQLite DB concurrently, or the 18:30 fallback silently never runs.
*Recommended test:* inject a fake `_acquire_single_instance_lock` returning `None`
→ assert `main()` returns an empty `RunReport` and never calls `run_cycle`; then
returning a fake fd → assert ingest+cycle run and `fd.close()` is called in `finally`.

### P0 — Alpaca adapter failure-mapping branches untested — `arbiter/execution/alpaca_adapter.py` (84%, missing 214-223, 252-254)
The `place`/retry happy and reject paths are tested, but two failure mappings are not:
`cancel()` 's exception→`status="rejected"` branch (219-221) and `get_order()`'s
network-error→`status="pending"` fail-safe (252-254). These are the branches that
decide whether a real broker hiccup is treated as "order gone" vs "still live" — a
mis-map here can orphan or double a live position. The partial-vs-filled mapping in
`get_order` (line 283, `fill_qty >= req_qty`) has no direct adapter-level test
asserting a `partially_filled` Alpaca payload yields `status="partial"`.
*Recommended test:* with a fake `http_delete` that raises → assert cancel report
`status=="rejected"`; fake `http_get` raising → assert get_order `status=="pending"`;
fake `http_get` returning `{"status":"partially_filled","filled_qty":"5","qty":"10"}`
→ assert `status=="partial"`.

### P1 — Daemon heartbeat / flock / signal / post-close-sweep helpers under-tested — `arbiter/runtime/daemon.py` (63%, missing 44-46, 75-84, 93-104, 215-217, 245-247, 255, 270, 280-281, 295-332)
The 7 `TestDaemonLoop` tests cover the loop skeleton, but the resilience plumbing
the docstring sells is largely uncovered: `_heartbeat` atomic-write + its failure
branch (75-84), `_acquire_single_instance_lock` success+held (93-104), the
shutdown reconcile (215-217), `_run_post_close_sweep` reconcile/sweep failure
swallows (245-247), `_until_next_open_capped`'s `next_open is None` / past-open
branches (270/273), the `_hb` get_positions-fails branch (280-281), the signal
handlers (295-306), and `main()` (309-332). If the heartbeat silently stops or the
flock is mis-handled, an operator loses the one signal that the daemon is alive.
*Recommended test:* unit-test `_heartbeat` (writes JSON atomically; tolerates an
unwritable dir without raising); `_acquire_single_instance_lock` twice on the same
pidfile in-process (second returns None); `_until_next_open_capped` with
`next_open=None` and with a past `next_open`; drive `run_daemon` with an engine
whose `get_positions` raises to hit the `_hb` fallback.

### P1 — Engine partial-SELL reconcile branch not directly tested — `arbiter/engine.py` (`_close_out_filled_sell`, 462-469)
The `exit_monitor` partial-residual *sweep* is tested, but the engine-level guard
that a `status=="partial"` SELL must NOT close/label the idea (and leaves it
MONITORED for next cycle) has no dedicated test. If this branch regresses, a partial
exit would be labeled as a full close — corrupting the outcome ledger that trust and
calibration are computed from. (Engine partial *grep* hits are all in ingest, not
this reconcile path.)
*Recommended test:* seed a MONITORED idea + a pending SELL; reconcile with a
`get_order` report `status="partial"` → assert idea stays MONITORED, no outcome row
written, audit logs `engine.reconcile_pending.sell_partial`.

### P1 — Submit IntegrityError dedup race path untested — `arbiter/execution/submit.py` (92%, missing 340-353)
The `sqlite3.IntegrityError` race branch (two writers inserting the same dedup_hash
between check and insert) returns the `_SKIP_SENTINEL` with `duplicate=True` and
writes an `order.race_skip` audit. This is a concurrency-correctness guarantee with
no test. Under the daemon + 18:30 one-shot overlap this is a *realistic* race.
*Recommended test:* monkeypatch `_insert_order_row` to raise `sqlite3.IntegrityError`
once → assert `submit_order` returns `status==_SKIP_SENTINEL`, `duplicate is True`,
and the `order.race_skip` audit line is emitted.

### P1 — Ingest fault-isolation error paths thin — `arbiter/ingest/runner.py` (76%, missing 211-215, 285, 292-312, 410-420, 452-456)
The per-filing "fetch/parse error → log, record, continue" and "write error →
n_skipped" branches (292-312) and the congress-side equivalents are the resilience
contract (one bad filing must not abort the run), yet the error branches are largely
uncovered. A regression that lets one parse failure abort ingest would silently stop
all signal generation for the day.
*Recommended test:* feed `_ingest_form4` a client whose fetch raises for one
accession and succeeds for another → assert the good one is written, `n_skipped`
incremented, `src.errors` populated, and the run completes.

### P2 — Congress `parser.py` at 0% coverage — `arbiter/ingest/congress/parser.py` (0%, missing 48-373)
This entire module (House/Senate raw-dict → intermediate schema, amount-bracket
mapping, buy/sell detection, amendment detection) is uncovered. It appears the live
path uses `senate.py`/`ptr_pdf.py`/`normalize.py` instead and `parser.py` may be a
legacy/alternate parser. Either way, 325 uncovered statements in a parse module is a
latent-bug reservoir; if it IS wired anywhere, the amount-bracket and
buy/sell logic is unverified (mis-bracketing skews sizing).
*Recommended action:* confirm whether `parser.py` is dead code. If live → add
bracket-table + buy/sell + amendment-detection unit tests. If dead → flag for
deletion (note for G2/cleanup lane).

### P2 — Congress `normalize.py` parse-failure / drop paths under-tested — `arbiter/ingest/congress/normalize.py` (63%, missing 89-108, 167-169, 240-249, 298-328, 356-362, 414-451)
The happy normalization is covered, but the *rejection* paths — non-equity
asset-type drops, missing-ticker drops, bad-date handling, the `_KEEP_TXN_TYPES`
filtering, and the bottom block (414-451) — are mostly uncovered. These decide
which disclosures become tradeable signals; an over-broad keep (e.g. options/bonds
leaking through as equity) directly creates bad trades.
*Recommended test:* table-driven cases asserting non-equity asset_types are dropped,
None-ticker rows are dropped, and exchange/E-type transactions are excluded.

### P2 — EDGAR client network/error branches untested — `arbiter/ingest/edgar/client.py` (67%, missing 130-137, 156-159, 173/176/179, 202, 210-234)
The retry/backoff, non-200, and the bottom fetch block (210-234) of the Form-4
client are uncovered (network is correctly not hit in tests, but the error-mapping
logic should still be exercised with a fake transport). A swallowed/ mis-handled
EDGAR error silently drops insider signals.
*Recommended test:* inject a fake HTTP transport returning 429/500 then 200 → assert
retry/backoff path and eventual success; returning persistent error → assert it is
logged and surfaced (not silently treated as "no filings").

### P2 — N-advisor (>2) fusion only unit-tested, never integration-tested — `arbiter/fusion/*`, `arbiter/engine.py`
Unit tests in `test_fusion.py` do exercise 3 advisors (A1/A2/A3, incl. a shadow A3
excluded from contributions) and correlation deflation — good. But the **live
engine only fuses two A1 sub-advisors** (A1.insider + A1.congress); A2 (MiroFish)
and A3 (news/X) are shadow/stub per ROADMAP Phases 4/6. So the *end-to-end* path
where a third real advisor's opinion flows ingest→fusion→sizing→execution is never
run. When A2 goes live this is the regression-prone seam (effective-N deflation +
lone-bull tax interacting with a real third weight).
*Recommended test:* an engine-level cycle with three non-shadow advisor opinions on
the same ticker → assert pooled conviction, correlation deflation, and that sizing
consumes the fused (not per-advisor) signal. Add now as a guard before Phase 4.

### P3 — CLI dispatch largely untested — `arbiter/cli.py` (23%, missing 26, 36-181)
Subcommand wiring (`run`, `daemon`, `serve`, migrate, etc.) is 23% covered. Low harm
(thin glue) but a broken arg-parse means an operator command silently no-ops.
*Recommended test:* invoke the CLI dispatcher with each subcommand name against
stubbed entrypoints → assert the right function is called.

### P3 — `loop_runner`/`daemon` `_run_post_close_sweep` advisor-routing lambda untested — `arbiter/runtime/daemon.py` (252-261)
The `_advisor_id_for` horizon split (`>=180 → A1.insider else A1.congress`) inside
the post-close sweep is duplicated logic (also in engine) and uncovered here; a
divergence between the two copies would mislabel outcomes only on the daemon path.
*Recommended test:* covered transitively if the post-close-sweep test above asserts
the advisor_id routing.

### P3 — Web server error/edge handlers — `arbiter/web/server.py` (85%, missing 363-370, 406-431, 439-454) & `web/queries.py` (78%)
The dashboard index error-fallback (renders an error page instead of 500) and
several query empty/null branches are uncovered. Low harm (read-only dashboard) but
the error-page path is exactly what an operator sees when the DB is wedged.
*Recommended test:* point the handler at a corrupt/missing DB → assert it returns the
minimal error HTML, not a traceback/500.

---

## OPPORTUNITIES TO ADD

- **Add `--cov-fail-under=88` to CI** (pyproject/Makefile) so coverage can't silently
  regress, with a per-file floor for the money-path modules (execution/, engine.py,
  daemon.py, submit.py, reconciler.py — target ≥90%).
- **A "live-path smoke" test tier** gated on `executor_backend=alpaca_paper` with a
  fully faked HTTP transport, run in CI, so every Alpaca branch (place/cancel/
  get_order/get_positions/get_account incl. all failure mappings) is exercised
  without network. Today these branches only run against the real broker.
- **A concurrency test** that runs `submit_order` from two threads on one in-memory/
  file SQLite DB to actually trip the IntegrityError race (currently only reachable
  by monkeypatch).
- **Branch coverage** (`--cov-branch`) not just line coverage — many of the broad
  `except Exception` handlers in engine.py count as "covered" on the happy line but
  their except arm never fires. Branch mode will expose those honestly.
- **A partial-fill end-to-end scenario** through the engine reconcile path (entry
  partial → residual re-buy; exit partial → residual re-sell; outcome labeled only on
  full close) — the single most consequential under-tested money behavior.
- **A daemon resilience integration test**: drive `run_daemon` across an
  open→closed→open transition with one iteration raising mid-session, asserting
  backoff grows, the loop survives, the heartbeat keeps updating, and the post-close
  sweep fires exactly once.
- **Confirm/retire `congress/parser.py`** (0%): if dead code, delete it so the 325
  uncovered statements stop diluting the signal.

---

## Lowest-coverage modules (reference table)

| Module | Cover | Key missing |
|---|---|---|
| `ingest/congress/parser.py` | 0% | whole module (48-373) |
| `cli.py` | 23% | subcommand dispatch |
| `orchestrator/loop_runner.py` | 57% | `main()` cron entrypoint + flock guard |
| `runtime/daemon.py` | 63% | heartbeat/flock/signals/post-close/`main()` |
| `ingest/congress/normalize.py` | 63% | drop/rejection paths |
| `ingest/edgar/client.py` | 67% | retry/error branches |
| `evaluation/backtest/runner.py` | 71% | 442-519 block |
| `ingest/runner.py` | 76% | per-filing fault isolation |
| `web/queries.py` | 78% | empty/null branches |
| `ingest/congress/__init__.py` | 78% | error branches |
| `execution/alpaca_adapter.py` | 84% | cancel/get_order failure maps |
| `engine.py` | 84% | broad `except` arms, partial-SELL guard, `main()` |
