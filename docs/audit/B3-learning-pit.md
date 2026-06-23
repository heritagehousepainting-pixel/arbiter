# B3 — Learning-loop no-look-ahead (PIT discipline) audit

**Lane:** B3 (READ-ONLY). **Date:** 2026-06-19. **Auditor scope:** the point-in-time
discipline of the learning loop only. Trust math is E1; outcome attribution is E3 — both
explicitly out of scope and trusted here.

**Verdict: PASS — no look-ahead found.** A decision at T provably learns ONLY from
outcomes resolved strictly before T. The strict `created_at < as_of` cutoff is enforced
twice (Python assembler + SQL), the backtest path recomputes every step with no
cross-step cache, the calibrator fits only on the caller-filtered list, and there is no
`datetime.now()` anywhere in the trust/calibration layer. No second code path bypasses
`load_outcomes_for_learning` into the weight/calibration learning step. All targeted tests
green (`tests/integration/test_pit_same_cycle.py`, `tests/trust/test_trust_store.py` — 7
passed).

---

## What was verified (the no-look-ahead chain)

1. **Single cycle clock.** `engine.run_cycle` derives `now = self.clock.now()` ONCE
   (`engine.py:861`) and threads the identical value into every same-cycle outcome
   writer — `_reconcile_pending_orders(now)` (`engine.py:875`), `_run_exit_monitor(now,…)`
   (`engine.py:926`) — and into the learning step `_build_learning_inputs(now)`
   (`engine.py:990`). Because the close-out writers stamp `created_at = now`, the learning
   read at the same `now` must use a STRICT `<` to exclude them. It does.

2. **Strict cutoff, defense-in-depth.** `load_outcomes_for_learning` (`trust/store.py:58`)
   is the only sanctioned assembler; it calls `query_outcomes(conn, as_of=now,
   strict_lt=True)`. `query_outcomes` (`outcome_store.py:194`) emits SQL
   `created_at < ?` when `strict_lt`. So the cutoff is enforced at both the assembler and
   the SQL layer. An outcome stamped at exactly T is excluded; one stamped strictly before
   T is included. Directly tested by `test_outcome_stamped_at_T_excluded_from_same_cycle_learning`.

3. **Sweep ordering (R6).** The end-of-cycle outcome sweep runs strictly AFTER
   `_build_learning_inputs` within `run_cycle` (comment lock `engine.py:986-988`; verified
   by `test_outcome_sweep_runs_after_learning_step`). New outcomes minted this cycle are
   therefore never visible to this cycle's learning even before the strict cutoff matters.

4. **Backtest recompute-each-step, no stale cache (D2).** `_build_learning_inputs`
   (`engine.py:703,717-739`) branches on `isinstance(self.clock, BacktestClock)`. The
   backtest branch ALWAYS reassembles `outcomes_by_advisor` from the strict-cutoff
   assembler, calls `ledger.update(... force=should_update)`, and re-fits fresh
   `Calibrator` objects (`conn=None`, no persistence read-back) every step. The cross-step
   `_learning_cache` is read ONLY in the live branch (`engine.py:766-767`); it is never
   read under `BacktestClock`, so a recency-decay weight computed at an OLD `as_of` can
   never be served into a later backtest step.

5. **Recency decay keyed to as_of, monotone (brier).** `_decay_weight(as_of, date)`
   (`trust/brier.py:57`) computes `delta_days = max(0.0, (as_of - date)…)` and
   `2^(-delta/182)` fresh on every call — no memoization, no `now()`. `ledger.update`
   (`ledger.py:273`) threads the passed `as_of` into `brier_skill_score`,
   `compute_composite_trust`, and `CorrelationMatrix.build`; it consumes only the
   pre-filtered `outcomes_by_advisor` it is handed. The `max(0.0, …)` clamp would give a
   post-T outcome weight 1.0, but post-T outcomes are already excluded upstream, so the
   clamp never masks a leak in the sanctioned path.

6. **Calibrator never fits on unfiltered outcomes.** `Calibrator.fit(outcomes)`
   (`calibrator.py:116`) consumes ONLY the passed sequence — no DB query, no `as_of`, no
   `now()`. The engine feeds it `[o for o, _ in records]` where `records` come from
   `load_outcomes_for_learning` (strict cutoff). `persist(as_of)` (`calibrator.py:276`)
   uses the caller's clock and rejects naive datetimes.

7. **No `datetime.now()` in the learning layer.** Grep over `arbiter/trust/` and
   `arbiter/calibration/` finds zero `now()/utcnow()/time.time()/date.today()` calls; the
   only matches are doc comments asserting their absence. All timestamps are caller-injected.

8. **No second learning path.** Every production caller of `ledger.update` and `cal.fit`
   lives in `_build_learning_inputs` (`engine.py:722,733,744,754`), all fed from
   `load_outcomes_for_learning`. The other `query_outcomes` callers are in
   `evaluation/attribution.py` (lines 95/137/176) — idea_id-scoped attribution helpers that
   resolve/dedupe which advisor owns an outcome BEFORE it is written; they are upstream of
   the learning read and never compute weights/calibration, so they are not a bypass (and
   are E3's lane regardless).

---

## Findings

(None at P0/P1/P2. Two P3 notes — neither is a live look-ahead in the sanctioned path.)

- **P3 — Attribution read path has no as_of cutoff (out-of-lane, noted for completeness)
  — arbiter/evaluation/attribution.py:95,137,176 — `query_outcomes(conn,
  idea_id=idea.idea_id)` is called with no `as_of`/`strict_lt`, so it can see post-T
  rows.** This is E3's lane and is a WRITE-SIDE/attribution path (resolve which advisor
  owns an outcome), not the weight/calibration learning read, so it does not breach B3's
  no-look-ahead guarantee today. Risk surfaces only if a future change ever routes
  attribution output directly into trust learning without re-funnelling through
  `load_outcomes_for_learning`. — Recommended action: leave to E3; if attribution ever
  becomes an input to ledger/calibrator, route it through the strict assembler.

- **P3 — `load_latest_weight_bundle(backtest=True)` exists but is never called by the
  engine — arbiter/trust/store.py:136-204 — the as_of-windowed backtest read is dead from
  the engine's POV (engine recomputes each step, D2).** This is intentional (documented at
  store.py:152-164 as a diagnostic/external reader) and is PIT-correct (`WHERE as_of <= ?`),
  so it is not a leak. The only residual risk is a future contributor wiring this reader as
  a backtest warm-start cache, which would reintroduce the stale-decay problem D2 forbids.
  — Recommended action: keep; the docstring already warns. Optionally guard with a test
  asserting the engine's backtest branch never calls it.

---

## OPPORTUNITIES TO ADD

- **PIT lint as a test/CI gate.** The "no `datetime.now()` in trust/calibration" invariant
  is currently asserted only by docstrings + this manual grep. Add a unit test that greps
  `arbiter/trust/` and `arbiter/calibration/` for `datetime.now()|utcnow()|time.time()|
  date.today()` and fails on any hit — locks the invariant against regression.

- **Property/fuzz test on the strict cutoff.** Add a randomized test: scatter outcomes at
  timestamps around a random T (some == T, some < T, some > T), assert
  `load_outcomes_for_learning(T)` returns EXACTLY the `< T` set, for both same-second and
  sub-second deltas (ISO-string lexical comparison edge cases).

- **Backtest-no-cache assertion.** A focused test that spies on `_learning_cache` and
  asserts it is never read under `BacktestClock` across a multi-step replay — locks D2 at
  the engine seam (currently inferred from branch structure, not asserted).

- **Decay-monotonicity regression guard.** A test asserting `_decay_weight(T, d1) >=
  _decay_weight(T, d2)` whenever `d1 >= d2`, plus the `max(0.0,…)` clamp behaviour for a
  (hypothetical) future-dated row — documents that the clamp is a belt, not the only guard.
