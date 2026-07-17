# Arbiter Unfreeze Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore daily trading on the arbiter paper account via decision tracing, trust parole, an idea revisit sweep, and deployment pressure — per `docs/specs/2026-07-17-arbiter-unfreeze-spec.md`.

**Architecture:** Four independent stages layered onto the existing engine: (1) optional `trace` callbacks threaded through `policy/decision.py` → `policy/sizing.py` plus a `cycle_funnel` audit event from the engine; (2) a `parole` cap_reason in `trust/ledger.py` resolved to a half-floor weight in `trust/weight_resolver.py`; (3) a `run_revisit_sweep` in `orchestrator/outcome_runner.py` that recycles unexecuted FINAL_DECIDED ideas into fresh NASCENT ideas each day; (4) a conviction-qualified minimum size floor in `compute_size` and an idle-capital alert in the daemon's post-close path.

**Tech Stack:** Python 3.12, sqlite3, pytest (repo suite ~2700 tests, hermetic conftest), structlog-style `log` + `arbiter.db.audit.audit()`.

## Global Constraints

- Working dir: `/Users/jonathanmorris/poly_bot/arbiter` (package nested at `arbiter/arbiter/`).
- Run tests with `python -m pytest tests/<path> -q` from `/Users/jonathanmorris/poly_bot/arbiter`.
- NEVER call `datetime.now()` in library code — all time from injected `clock`/`as_of` (PIT lint).
- All new config fields must have defaults so bare `Config(...)` test constructions keep working.
- Env-var naming: `ARBITER_<UPPER_SNAKE>` parsed in `arbiter/config.py::load_config` via `_env_float`/`_env_int`.
- Trace/audit failures must NEVER abort a cycle (wrap callbacks fail-safe).
- Each task = its own commit on branch `arbiter-unfreeze`. Full suite + linters green before merge to main.

---

### Task 1: `compute_size` trace callback

**Files:**
- Modify: `arbiter/policy/sizing.py`
- Test: `tests/policy/test_sizing.py`

**Interfaces:**
- Produces: `compute_size(..., trace: Callable[[str, dict], None] | None = None) -> float`.
  Trace reasons emitted (event name is always `"size"`, payload has `reason`):
  `gate_blocked`, `zero_conviction`, `caps_exhausted`, `position_count_full`, `adv_missing`.
  Payload always includes `ticker`.

- [ ] **Step 1: Write failing tests** — in `tests/policy/test_sizing.py` add a `_collect` helper and tests asserting: (a) ADV-None path emits `("size", {"reason": "adv_missing", "ticker": ...})`; (b) position-count-full path emits `position_count_full`; (c) headroom-exhausted (gross cap 0 headroom) emits `caps_exhausted`; (d) no trace callback → unchanged behavior (no error). Reuse the file's existing fixture style for `FusionOutput`/config/gate.
- [ ] **Step 2: Run to verify fail** — `python -m pytest tests/policy/test_sizing.py -q -k trace` → FAIL (unexpected keyword `trace`).
- [ ] **Step 3: Implement** — add `trace=None` keyword; add `def _t(reason, **kw)` internal helper that no-ops on None and swallows exceptions; call at each `return 0.0` site with the matching reason; `caps_exhausted` fires when post-cap `size <= 0.0` before the count gate.
- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/policy/test_sizing.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(policy): compute_size trace callback — size-zero reasons visible"`.

### Task 2: `decide()` trace callback

**Files:**
- Modify: `arbiter/policy/decision.py`
- Test: `tests/policy/test_decision.py`

**Interfaces:**
- Consumes: Task 1's `compute_size(trace=...)`.
- Produces: `decide(..., trace: Callable[[str, dict], None] | None = None)`.
  Emits `("decide", {"reason": "gate_blocked", "ticker"})` on gate denial;
  `("decide", {"reason": "flat_conviction", "ticker", "bucket", "conviction"})` when
  `_conviction_to_side` returns None; `("decide", {"reason": "size_zero", "ticker", "bucket"})`
  when `compute_size` returns 0; forwards `trace` into `compute_size`.

- [ ] **Step 1: Write failing tests** — flat-conviction fusion (conviction 0.01) emits `flat_conviction` with the value; sized-zero (ADV provider returns None) emits both the sizing `adv_missing` and decide-level `size_zero`; no-trace call unchanged.
- [ ] **Step 2: Verify fail** — `python -m pytest tests/policy/test_decision.py -q -k trace` → FAIL.
- [ ] **Step 3: Implement** — same `_t` helper pattern; wire at the three sites; pass `trace=trace` to `compute_size`.
- [ ] **Step 4: Verify pass** — `python -m pytest tests/policy -q` → PASS.
- [ ] **Step 5: Commit** — `feat(policy): decide() trace — flat-conviction and size-zero reasons visible`.

### Task 3: Engine trace closure + `cycle_funnel` audit event

**Files:**
- Modify: `arbiter/orchestrator/cycle.py` (add optional `trace` param; emit `no_opinions` per idea; count in `CycleResult.ideas_no_opinions`)
- Modify: `arbiter/engine/_engine.py` (build audit-writing trace closure; funnel counters; emit `cycle_funnel` audit + log after `run_cycle`)
- Test: `tests/orchestrator/test_cycle.py`, `tests/engine/test_run_cycle_funnel.py` (new)

**Interfaces:**
- Consumes: Tasks 1–2 trace params.
- Produces: `run_cycle(..., trace: Callable[[str, dict], None] | None = None)`;
  `CycleResult.ideas_no_opinions: int = 0` (new dataclass field);
  audit events `decide.skip` `{idea_id, ticker, bucket, reason, ...}` and
  `cycle_funnel` `{ideas, no_opinions, flat_conviction, size_zero, submitted, skipped_dedupe}`;
  engine attribute `self.last_cycle_funnel: dict` (consumed by Task 9's idle alert).

- [ ] **Step 1: Failing test (cycle)** — `run_cycle` with one idea and an empty advisor_map calls `trace("cycle", {"reason": "no_opinions", ...})` and sets `ideas_no_opinions == 1`.
- [ ] **Step 2: Verify fail** → unexpected keyword `trace`.
- [ ] **Step 3: Implement cycle.py** — add param + field; in the `if not bucket_opinions:` branch call the fail-safe trace and increment.
- [ ] **Step 4: Failing test (engine)** — build the minimal sim engine fixture used by existing engine tests; run one cycle with a signal-producing idea whose conviction is flat; assert `audit.jsonl` (tmp path) contains one `decide.skip` with `reason=flat_conviction` and one `cycle_funnel` event with `ideas >= 1`.
- [ ] **Step 5: Implement engine** — in `run_cycle` (engine method), before `run_cycle(...)` call, create:
  ```python
  _funnel = {"ideas": 0, "no_opinions": 0, "flat_conviction": 0, "size_zero": 0, "submitted": 0}
  def _trace(event: str, payload: dict) -> None:
      try:
          reason = payload.get("reason", "")
          if reason in _funnel: _funnel[reason] += 1
          audit("decide.skip", payload, ts=now.isoformat(), audit_path=_audit_path)
      except Exception: pass
  ```
  Pass `trace=_trace` into `_bound_decide`'s `_decide(...)` call and `run_cycle(trace=_trace)`. After the cycle: fill `ideas`/`submitted` from `result`, `audit("cycle_funnel", _funnel, ...)`, `log.info("engine.cycle_funnel", **_funnel)`, and `self.last_cycle_funnel = dict(_funnel)`.
- [ ] **Step 6: Verify pass** — `python -m pytest tests/orchestrator/test_cycle.py tests/engine -q` → PASS.
- [ ] **Step 7: Commit** — `feat(engine): decision tracing + cycle_funnel audit event`.

### Task 4: Trust parole — ledger side

**Files:**
- Modify: `arbiter/trust/ledger.py`
- Test: `tests/trust/test_ledger.py`

**Interfaces:**
- Produces: `PAROLE_REASON: str = "parole"` module constant. `cap_reasons[advisor_id]`
  becomes `"negative_skill"` only when significantly negative AND `n_non_abstain >= SHADOW_THRESHOLD`;
  `"parole"` when significantly negative below that sample; else None. `_apply_caps` hard-zeroes
  only on the full-sample mute (parole advisors keep their ramp weight — 0 while in shadow,
  which the resolver floors).

- [ ] **Step 1: Failing tests** — construct outcome sets producing significant negative skill with (a) `n_non_abstain < 30` → `ledger.last_cap_reasons[aid] == "parole"`, weight row NOT hard-muted flag; (b) `n_non_abstain >= 30` → `"negative_skill"` and weight 0/shadow (existing behavior). Reuse the file's outcome-builder helpers.
- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement** — in `TrustLedger.update`: compute `n_non_abstain` BEFORE the cap_reasons assignment; set the three-way reason; pass `mute = is_negative_skill and n_non_abstain >= SHADOW_THRESHOLD` into `_apply_caps` (rename its param usage accordingly — callers inside ledger only).
- [ ] **Step 4: Verify pass** — `python -m pytest tests/trust/test_ledger.py -q`.
- [ ] **Step 5: Commit** — `feat(trust): parole cap_reason — thin-sample negative skill no longer hard-mutes`.

### Task 5: Trust parole — resolver + config knobs

**Files:**
- Modify: `arbiter/trust/weight_resolver.py`, `arbiter/config.py`, `arbiter/engine/learning.py`
- Test: `tests/trust/test_weight_resolver.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: Task 4's `PAROLE_REASON`.
- Produces: `resolve_weight_bundle(..., parole_fraction: float = 0.5)`; `cap_reason == "parole"`
  → `weight = equal_floor * parole_fraction`, `shadow=False`. Config fields
  `trust_parole_fraction: float = 0.5` (env `ARBITER_TRUST_PAROLE_FRACTION`); confirm
  `trust_equal_floor` env override exists (`ARBITER_TRUST_EQUAL_FLOOR`) — add if missing.
  `learning.build_learning_inputs` passes `parole_fraction=float(getattr(engine.config, "trust_parole_fraction", 0.5))`.

- [ ] **Step 1: Failing tests** — resolver: cap_reasons `{"A9": "parole"}` with floor 0.25 → weight 0.125, shadow False; negative_skill still 0/shadow; config: env round-trip for the two knobs.
- [ ] **Step 2: Verify fail.** — `python -m pytest tests/trust/test_weight_resolver.py -q -k parole`
- [ ] **Step 3: Implement** — resolver branch between the negative_skill and cold checks:
  ```python
  if reason == PAROLE_REASON:
      w = equal_floor * parole_fraction
      resolved[advisor_id] = AdvisorWeight(advisor_id=advisor_id, weight=w, ci_low=w, ci_high=w, shadow=False)
      continue
  ```
  (import `PAROLE_REASON` locally or redefine constant to avoid a ledger import cycle — check imports; `weight_resolver` must not import `ledger`, so define `PAROLE_REASON = "parole"` in the resolver and have the ledger import it from there, mirroring `NEGATIVE_SKILL_REASON`.)
- [ ] **Step 4: Verify pass** — `python -m pytest tests/trust tests/test_config.py -q`.
- [ ] **Step 5: Commit** — `feat(trust): parole floor in weight resolver + config knobs`.

### Task 6: Revisit sweep — `outcome_runner.run_revisit_sweep`

**Files:**
- Modify: `arbiter/orchestrator/outcome_runner.py`
- Test: `tests/orchestrator/test_revisit_sweep.py` (new; copy fixture style from `test_stuck_idea_sweep.py`)

**Interfaces:**
- Produces:
  ```python
  def run_revisit_sweep(conn, *, clock, min_age_hours: float = 24.0, limit: int = 50,
                        audit_path=None) -> list[Idea]
  ```
  Selects non-superseded `FINAL_DECIDED` ideas where: `updated_state_at` older than
  `min_age_hours`; horizon NOT elapsed (`idea.as_of + horizon_days > now` — expired ones belong
  to `run_unexecuted_sweep`); no order row in `('pending','partial','filled')` references the
  idea. Oldest-first, capped at `limit`. Each selected idea: transition → `ABANDONED`
  (audit via `idea_store.update_idea_state`), emit audit `idea_revisit.reopened`
  `{old_idea_id, ticker, bucket}`, and append a FRESH `make_idea(ticker=..., thesis=old
  thesis + " (revisit)", horizon_days=..., as_of=now)` to the return list. `limit <= 0`
  disables (returns []).

- [ ] **Step 1: Failing tests** — (a) eligible idea → abandoned + one fresh NASCENT idea returned with same ticker/horizon; (b) idea younger than min_age untouched; (c) horizon-elapsed idea untouched (left for unexecuted sweep); (d) idea with a filled order untouched; (e) limit respected.
- [ ] **Step 2: Verify fail.** — `python -m pytest tests/orchestrator/test_revisit_sweep.py -q`
- [ ] **Step 3: Implement** — follow `run_stuck_idea_sweep` row-reading pattern (read `updated_state_at` off rows; naive→UTC), `run_unexecuted_sweep` order-guard SQL verbatim.
- [ ] **Step 4: Verify pass.**
- [ ] **Step 5: Commit** — `feat(orchestrator): revisit sweep — unexecuted decided ideas become a standing book`.

### Task 7: Engine + config wiring for the revisit sweep

**Files:**
- Modify: `arbiter/engine/_engine.py` (call sweep before idea-building so revived ideas process THIS cycle), `arbiter/config.py`
- Test: `tests/engine/test_revisit_wiring.py` (new), `tests/test_config.py`

**Interfaces:**
- Consumes: Task 6's `run_revisit_sweep`.
- Produces: config `idea_revisit_limit: int = 50` (env `ARBITER_IDEA_REVISIT_LIMIT`),
  `idea_revisit_min_age_hours: float = 24.0` (env `ARBITER_IDEA_REVISIT_MIN_AGE_HOURS`).
  In `run_cycle` (engine), after `signals = detect_signals(...)` and idea-building, extend
  `ideas` with the revived list (wrapped fail-safe try/except; sweep failure logs
  `engine.run_cycle.revisit_sweep_failed` and never aborts). Revived tickers respect the
  same `held_tickers`/`_addon_ok` skip as fresh signals.

- [ ] **Step 1: Failing test** — engine fixture with one FINAL_DECIDED idea aged >24h and no orders: next `run_cycle` processes ≥1 idea for that ticker (assert via `ideas_processed` or the persisted new idea row) and the old idea is ABANDONED.
- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement** — insertion point directly after the `for sig in signals:` idea-building loop:
  ```python
  try:
      revived = outcome_runner.run_revisit_sweep(
          self.conn, clock=self.clock,
          min_age_hours=getattr(self.config, "idea_revisit_min_age_hours", 24.0),
          limit=getattr(self.config, "idea_revisit_limit", 50),
          audit_path=self.config.audit_path)
      for r_idea in revived:
          if r_idea.ticker in seen_tickers: continue
          if r_idea.ticker in held_tickers and not _addon_ok(r_idea.ticker): continue
          seen_tickers.add(r_idea.ticker); ideas.append(r_idea)
  except Exception as exc:
      log.error("engine.run_cycle.revisit_sweep_failed", error=str(exc))
  ```
- [ ] **Step 4: Verify pass** — `python -m pytest tests/engine tests/test_config.py -q`.
- [ ] **Step 5: Commit** — `feat(engine): wire revisit sweep into run_cycle`.

### Task 8: Deployment pressure — minimum size floor

**Files:**
- Modify: `arbiter/policy/sizing.py`, `arbiter/config.py`
- Test: `tests/policy/test_sizing.py`, `tests/test_config.py`

**Interfaces:**
- Produces: config `min_position_pct: float = 0.02` (env `ARBITER_MIN_POSITION_PCT`; toml
  `[sizing] min_position_pct`). In `compute_size`, AFTER the gate/cold-start multipliers and
  BEFORE the ADV cap:
  ```python
  floor_notional = getattr(config, "min_position_pct", 0.0) * portfolio_equity
  if size > 0.0 and floor_notional > 0.0:
      size = max(size, floor_notional)
      size = min(size, name_headroom, sector_headroom, gross_headroom)  # floor never breaches caps
  ```
  ADV cap still applies last (unchanged). `min_position_pct = 0` → exact legacy behavior.

- [ ] **Step 1: Failing tests** — (a) conviction 0.05 (raw quarter-Kelly $125 on $10k) with `min_position_pct=0.02` → size 200.0; (b) floor clamped by name headroom (headroom $150 → 150.0); (c) `min_position_pct=0` → legacy 125.0; (d) ADV cap still binds after floor.
- [ ] **Step 2: Verify fail.** — `python -m pytest tests/policy/test_sizing.py -q -k floor`
- [ ] **Step 3: Implement** (keep headroom variables in scope for the re-clamp).
- [ ] **Step 4: Verify pass** — `python -m pytest tests/policy tests/test_config.py -q`.
- [ ] **Step 5: Commit** — `feat(policy): conviction-qualified minimum position size floor`.

### Task 9: Deployment pressure — idle-capital alert

**Files:**
- Modify: `arbiter/runtime/daemon.py` (`DaemonState` + `_run_post_close_sweep`)
- Test: `tests/runtime/test_daemon_idle_alert.py` (new; fixture style from `test_daemon.py`)

**Interfaces:**
- Consumes: `engine.last_cycle_funnel` (Task 3; may be absent → `{}`), `engine.executor.get_account()`
  (`cash`, `equity` attrs), `engine.alerting.alert(level, msg, ctx=...)`.
- Produces: `DaemonState.idle_sessions: int = 0`. In `_run_post_close_sweep` (new `state`
  parameter threaded from the loop's call site): deployment `= 1 - cash/equity` (guard
  `equity <= 0` → skip); `< 0.50` → `idle_sessions += 1` else reset to 0; on reaching 3 →
  `engine.alerting.alert("warning", "Capital idle: deployment {pct}% for 3 sessions",
  ctx={"deployment_pct": ..., **getattr(engine, "last_cycle_funnel", {})})` and reset to 0.
  All wrapped fail-safe (existing pattern in that function).

- [ ] **Step 1: Failing tests** — fake engine with account cash 8600/equity 10000: three post-close calls → exactly one warning alert with `deployment_pct` ≈ 14; a deployed account (cash 2000) resets the counter.
- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement** — add field, thread `state` into `_run_post_close_sweep(engine, now, state)` (update the single call site at the open→closed transition), append the check after the outcome sweep block.
- [ ] **Step 4: Verify pass** — `python -m pytest tests/runtime -q`.
- [ ] **Step 5: Commit** — `feat(daemon): idle-capital alert — 3 closed sessions under 50% deployed pings phone`.

### Task 10: Full verification + merge

- [ ] **Step 1:** `python -m pytest tests -q` (full suite) → all green.
- [ ] **Step 2:** `make lint` (or the repo's ruff invocation from Makefile) → clean.
- [ ] **Step 3:** Merge `arbiter-unfreeze` → `main`, push origin.
- [ ] **Step 4:** Restart daemon (`launchctl kickstart -k gui/$UID/com.arbiter.daemon`) so the new code is live; verify heartbeat + a `cycle_funnel` event appears next cycle.
