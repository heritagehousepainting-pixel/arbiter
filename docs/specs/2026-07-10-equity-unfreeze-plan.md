# Equity Unfreeze + Deploy-More — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unfreeze arbiter's equity entries (frozen since 2026-06-26) and open capital deployment toward 80%, by fixing a dedupe horizon-lock and an asymmetric advisor-muting bug, plus three config changes — all code+config, no DB mutation.

**Architecture:** Two localized code fixes (trust-ledger significance gate; dedupe cooldown threaded through the cycle) + `.env` risk-cap changes. The locked ideas and muted advisor both heal automatically on the next daemon cycle. Design: `docs/specs/2026-07-10-equity-unfreeze-design.md`.

**Tech Stack:** Python 3.14, pytest, SQLite. Repo venv: `arbiter/.venv/bin/python`. Package nested at `arbiter/arbiter/`.

## Global Constraints

- Live paper-trading system. **No manual DB writes.** No order placement from tests/scripts. Tests stay hermetic (conftest guard; never real HTTP/ntfy).
- Run tests with: `cd /Users/jonathanmorris/poly_bot && arbiter/.venv/bin/pytest <path> -q`.
- PIT purity: never `datetime.now()` in engine code — use the injected `clock`/`as_of`. `scripts/check_no_lookahead.sh` must stay green.
- **Commits are deferred**: do NOT commit during execution. This work will move to a fresh branch off `main` at the end (currently on `cockpit-watchlist-charts`, wrong home). The commit steps below are for history structure once branched; batch them and await the user's go-ahead.
- Cooldown is measured in **calendar days** (`timedelta(days=...)`) for simplicity (~2–3 trading days).

---

### Task 1: Significance-gate the advisor demotion (Component 2)

Fix the asymmetry: an advisor is muted (`negative_skill`, weight 0) only when it is *significantly* worse than chance, mirroring `is_significant_skill` used for graduation.

**Files:**
- Modify: `arbiter/arbiter/trust/ledger.py` (add `is_significant_negative_skill` near `is_significant_skill:340`; rework the per-advisor loop ~`527-590` so the gate reads `skill_ci`/`n_eff`)
- Test: `arbiter/tests/trust/test_ledger.py`

**Interfaces:**
- Produces: `is_significant_negative_skill(ci_high: float | None, n_eff: float, *, min_effective_n: float = MIN_EFFECTIVE_N) -> bool`
- Consumes: existing `bootstrap_skill_ci(outcomes, dates, as_of) -> tuple[float,float] | None`, `effective_sample_size(...) -> float`, `MIN_EFFECTIVE_N`.

- [ ] **Step 1: Write the failing test** (append to `test_ledger.py`)

```python
def test_is_significant_negative_skill():
    from arbiter.trust.ledger import is_significant_negative_skill, MIN_EFFECTIVE_N
    # Significantly negative AND well-sampled -> mute.
    assert is_significant_negative_skill(-0.10, MIN_EFFECTIVE_N) is True
    # CI upper bound reaches/exceeds 0 (straddles zero) -> do NOT mute (thin/insignificant).
    assert is_significant_negative_skill(0.05, MIN_EFFECTIVE_N * 2) is False
    # ci_high < 0 but effective-n too small -> do NOT mute.
    assert is_significant_negative_skill(-0.10, MIN_EFFECTIVE_N - 1) is False
    # None CI -> do NOT mute.
    assert is_significant_negative_skill(None, MIN_EFFECTIVE_N) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `arbiter/.venv/bin/pytest arbiter/tests/trust/test_ledger.py::test_is_significant_negative_skill -q`
Expected: FAIL with `ImportError: cannot import name 'is_significant_negative_skill'`.

- [ ] **Step 3: Add the pure function** (in `ledger.py`, directly after `is_significant_skill`)

```python
def is_significant_negative_skill(
    ci_high: float | None,
    n_eff: float,
    *,
    min_effective_n: float = MIN_EFFECTIVE_N,
) -> bool:
    """Demotion significance gate: skill is real AND significantly NEGATIVE.

    Symmetric mirror of ``is_significant_skill``. True only when BOTH hold:
      (a) the bootstrap CI UPPER bound on skill is below zero (ci_high < 0),
          i.e. the advisor is distinguishably WORSE than chance, and
      (b) effective (decay-weighted) sample size exceeds ``min_effective_n``.

    A thin/NULL advisor (CI straddles 0, or n_eff too low) fails and is floored
    rather than muted — so it keeps trading and keeps accruing outcomes to learn
    from.  Prevents benching an advisor on a statistically-insignificant blip.
    """
    if ci_high is None:
        return False
    return ci_high < 0.0 and n_eff >= min_effective_n
```

- [ ] **Step 4: Rework the demotion decision in `TrustLedger.update`** — replace the point-estimate at `ledger.py:533-538`:

```python
            bss = brier_skill_score(outcomes, dates, as_of)
            is_negative_skill = bss is not None and bss < 0.0
            cap_reasons[advisor_id] = "negative_skill" if is_negative_skill else None
```

with (moving the CI/n_eff computation ABOVE the gate, and deleting the now-redundant duplicate `skill_ci`/`n_eff` lines that currently sit at ~`564-565`):

```python
            # Statistical power: bootstrap CI on SKILL + effective-n (computed
            # once, used by BOTH the demotion gate and the graduation gate).
            skill_ci = bootstrap_skill_ci(outcomes, dates, as_of)
            n_eff = effective_sample_size(outcomes, dates, as_of)
            skill_ci_low = skill_ci[0] if skill_ci is not None else None
            skill_ci_high = skill_ci[1] if skill_ci is not None else None

            # Demotion is significance-gated (symmetric with graduation): mute
            # ONLY on significantly-negative skill, never on a thin point estimate.
            is_negative_skill = is_significant_negative_skill(skill_ci_high, n_eff)
            cap_reasons[advisor_id] = "negative_skill" if is_negative_skill else None
```

Then at the later block (formerly ~`563-566`) delete the duplicate `skill_ci = ... / n_eff = ... / skill_ci_low = ...` lines (now computed above) and keep the `graduated = is_significant_skill(skill_ci_low, n_eff)` call using the hoisted values. If `bss` is now unused elsewhere in the function, delete its assignment.

- [ ] **Step 5: Run the ledger suite**

Run: `arbiter/.venv/bin/pytest arbiter/tests/trust/test_ledger.py -q`
Expected: PASS (new test + all existing). If an existing test asserted muting on a thin negative point estimate, update it to reflect the corrected (floored, not muted) behavior and note it in the commit.

- [ ] **Step 6: Full trust + fusion regression**

Run: `arbiter/.venv/bin/pytest arbiter/tests/trust/ arbiter/tests/fusion/ -q`
Expected: PASS. `arbiter/.venv/bin/bash scripts/check_no_lookahead.sh` clean.

- [ ] **Step 7: Commit** (deferred — batch per Global Constraints)

```bash
git add arbiter/arbiter/trust/ledger.py arbiter/tests/trust/test_ledger.py
git commit -m "fix(trust): significance-gate advisor demotion (symmetric with graduation)"
```

---

### Task 2: Dedupe short-cooldown instead of full-horizon lock (Component 1)

A never-executed `FINAL_DECIDED` idea should stop blocking its `(ticker,bucket)` after a short cooldown, while remaining `FINAL_DECIDED` for full-horizon outcome labeling.

**Files:**
- Modify: `arbiter/arbiter/config.py` (add `dedupe_cooldown_days` field + loader)
- Modify: `arbiter/arbiter/orchestrator/idea.py:104` (`is_duplicate` — add `now`/`cooldown_days`)
- Modify: `arbiter/arbiter/orchestrator/cycle.py` (`run_cycle` — add `dedupe_cooldown_days` param; pass `now`+cooldown to `is_duplicate` at :191)
- Modify: `arbiter/arbiter/engine/_engine.py:971` (pass `dedupe_cooldown_days=self.config.dedupe_cooldown_days`)
- Test: `arbiter/tests/orchestrator/test_idea.py`

**Interfaces:**
- Produces: `is_duplicate(idea: Idea, active_ideas: list[Idea], *, now: datetime, cooldown_days: int) -> bool`
- Produces: `Config.dedupe_cooldown_days: int` (default 3)
- Consumes: `run_cycle(..., dedupe_cooldown_days: int = 3)`; `IdeaState.FINAL_DECIDED`; `Idea.as_of`, `Idea.state`.

- [ ] **Step 1: Write the failing test** (append to `test_idea.py`; reuse the file's existing `Idea` construction helper/style)

```python
def test_final_decided_past_cooldown_does_not_block():
    from datetime import datetime, timedelta, timezone
    from arbiter.orchestrator.idea import is_duplicate
    from arbiter.types import IdeaState
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)

    def _idea(idea_id, state, as_of):
        return Idea(idea_id=idea_id, ticker="NVDA", thesis="t", horizon_days=180,
                    state=state, as_of=as_of, dedupe_key=("NVDA", "SWING"))

    candidate = _idea("new", IdeaState.NASCENT, now)
    stale_no_trade = _idea("old", IdeaState.FINAL_DECIDED, now - timedelta(days=5))
    fresh_no_trade = _idea("recent", IdeaState.FINAL_DECIDED, now - timedelta(days=1))
    held = _idea("held", IdeaState.MONITORED, now - timedelta(days=30))

    # Stale never-executed FINAL_DECIDED past 3d cooldown -> no longer blocks.
    assert is_duplicate(candidate, [stale_no_trade], now=now, cooldown_days=3) is False
    # Fresh FINAL_DECIDED within cooldown -> still blocks (avoid churn).
    assert is_duplicate(candidate, [fresh_no_trade], now=now, cooldown_days=3) is True
    # Held position -> always blocks regardless of age.
    assert is_duplicate(candidate, [held], now=now, cooldown_days=3) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `arbiter/.venv/bin/pytest arbiter/tests/orchestrator/test_idea.py::test_final_decided_past_cooldown_does_not_block -q`
Expected: FAIL — `is_duplicate() got an unexpected keyword argument 'now'`.

- [ ] **Step 3: Update `is_duplicate`** (`idea.py:104`) — replace the body:

```python
def is_duplicate(
    idea: Idea,
    active_ideas: list[Idea],
    *,
    now: datetime,
    cooldown_days: int,
) -> bool:
    """Return True if *idea* duplicates an active idea's (ticker, bucket).

    A never-executed ``FINAL_DECIDED`` idea (a prior no-trade decision) only
    blocks while younger than ``cooldown_days``; past that it no longer blocks
    (it stays FINAL_DECIDED for full-horizon outcome labeling — see
    outcome_runner).  Held/in-flight states block regardless of age.
    """
    cooldown = timedelta(days=cooldown_days)
    for existing in active_ideas:
        if (
            existing.idea_id == idea.idea_id
            or existing.dedupe_key != idea.dedupe_key
            or existing.state not in _ACTIVE_STATES
        ):
            continue
        if existing.state is IdeaState.FINAL_DECIDED and now - existing.as_of > cooldown:
            continue  # stale no-trade idea: cooldown elapsed, no longer blocks
        return True
    return False
```

Add imports at the top of `idea.py` if missing: `from datetime import datetime, timedelta` and ensure `IdeaState` is imported.

- [ ] **Step 4: Run the test to verify it passes**

Run: `arbiter/.venv/bin/pytest arbiter/tests/orchestrator/test_idea.py::test_final_decided_past_cooldown_does_not_block -q`
Expected: PASS.

- [ ] **Step 5: Add config knob** (`config.py`) — add field beside the other sizing/runtime defaults (near `dedupe`/runtime block, e.g. after `full_cycle_times_et`):

```python
    # Dedupe cooldown (2026-07-10 unfreeze): a never-executed FINAL_DECIDED idea
    # blocks its (ticker,bucket) for only this many days, then frees the slot
    # (outcome labeling still runs at full horizon).  Default 3.
    dedupe_cooldown_days: int = 3
```

and in `load_config` (beside `max_open_positions` at ~`config.py:490`):

```python
        dedupe_cooldown_days=_env_int(
            "ARBITER_DEDUPE_COOLDOWN_DAYS", int(sizing.get("dedupe_cooldown_days", 3))
        ),
```

- [ ] **Step 6: Thread it through the cycle** — in `cycle.py`, add `dedupe_cooldown_days: int = 3` to the `run_cycle` keyword args (beside `active_ideas`), and update the call at `cycle.py:191`:

```python
        if is_duplicate(idea, all_active, now=now, cooldown_days=dedupe_cooldown_days):
```

Then in `_engine.py` at the `run_cycle(...)` call (~`:971`) add:

```python
            dedupe_cooldown_days=self.config.dedupe_cooldown_days,
```

- [ ] **Step 7: Fix existing `is_duplicate`/`run_cycle` callers & tests** — the signature now requires `now`/`cooldown_days`. Search and update:

Run: `cd /Users/jonathanmorris/poly_bot && grep -rn "is_duplicate(" arbiter/ | grep -v "def is_duplicate"`
Update each call/test to pass `now=<clock time>, cooldown_days=3`.

- [ ] **Step 8: Run orchestrator + engine suites**

Run: `arbiter/.venv/bin/pytest arbiter/tests/orchestrator/ arbiter/tests/engine/ -q`
Expected: PASS.

- [ ] **Step 9: Commit** (deferred — batch)

```bash
git add arbiter/arbiter/config.py arbiter/arbiter/orchestrator/idea.py arbiter/arbiter/orchestrator/cycle.py arbiter/arbiter/engine/_engine.py arbiter/tests/orchestrator/test_idea.py
git commit -m "fix(dedupe): short cooldown for no-trade ideas instead of full-horizon lock"
```

---

### Task 3: Deploy-80% config changes (Component 3)

**Files:**
- Modify: `arbiter/.env` (three lines)

- [ ] **Step 1: Edit `.env`** — change/add:

```
ARBITER_MAX_GROSS_PCT=0.80
ARBITER_MAX_OPEN_POSITIONS=20
ARBITER_ALLOW_FRACTIONAL=0
```

(`ARBITER_MAX_GROSS_PCT` and `ARBITER_MAX_OPEN_POSITIONS` already exist at .env:18-19 — change values; add the `ARBITER_ALLOW_FRACTIONAL` line. Do NOT print secrets.)

- [ ] **Step 2: Verify config loads the new values** (read-only, no test file needed)

Run:
```bash
cd /Users/jonathanmorris/poly_bot/arbiter && ../arbiter/.venv/bin/python -c "
from arbiter.config import load_config
c = load_config()
print('gross', c.max_gross_pct, '| positions', c.max_open_positions, '| fractional', c.allow_fractional)
assert c.max_gross_pct == 0.80 and c.max_open_positions == 20 and c.allow_fractional is False
print('OK')
"
```
Expected: `gross 0.8 | positions 20 | fractional False` then `OK`.

---

### Task 4: Activate & verify the unfreeze (Component 5)

- [ ] **Step 1: Empirical A3.news check (the design's mandatory gate)** — confirm the un-mute is correct, not forced. Read-only:

```bash
cd /Users/jonathanmorris/poly_bot && arbiter/.venv/bin/python - <<'PY'
import sqlite3
from arbiter.trust.ledger import bootstrap_skill_ci, effective_sample_size, is_significant_negative_skill
# Load A3.news outcomes from the live DB (read-only), build (outcomes, dates), then:
# ci = bootstrap_skill_ci(outcomes, dates, as_of); n = effective_sample_size(outcomes, dates, as_of)
# print(is_significant_negative_skill(ci[1] if ci else None, n))
PY
```
Expected: `False` → A3.news floors (un-mutes) correctly. If `True`, STOP and report to the user (A3.news is genuinely significantly bad; do not force-un-mute).

- [ ] **Step 2: Restart the daemon** to load Tasks 1–2 code + Task 3 `.env` + the feed hardening:

```bash
launchctl kickstart -k "gui/$(id -u)/com.arbiter.daemon"
```

- [ ] **Step 3: Verify acceptance criteria** (read-only, over the next market-hours cycle(s)):
  - `trust_weights`: A3.news `weight > 0` (floored) after the next trust update.
  - Dedupe skips per cycle drop sharply; `engine.run_cycle.done` shows `ideas_processed > 0`.
  - First new equity order appears in `orders` (first since 2026-06-26).
  - Gross exposure trends up from 15.7% toward 80% over subsequent sessions.
  - No regression: exits still evaluate (no `feed_outage` alert), breakers unlatched.

---

## Self-Review

- **Spec coverage:** C1 dedupe → Task 2 ✓; C2 significance gate → Task 1 ✓; C3 config (gross/positions/fractional) → Task 3 ✓; C4 guardrails = non-goals (no task, correct) ✓; C5 activation+verification+A3.news empirical check → Task 4 ✓.
- **Placeholders:** none (Task 4 Step 1 intentionally leaves the outcome-loading query to the executor since the exact `outcomes` table shape is read at build time — the significance call is fully specified).
- **Type consistency:** `is_significant_negative_skill(ci_high, n_eff)` defined in Task 1, consumed in Task 4; `is_duplicate(..., now=, cooldown_days=)` defined in Task 2, threaded in the same task; `Config.dedupe_cooldown_days` defined and consumed in Task 2. Consistent.
