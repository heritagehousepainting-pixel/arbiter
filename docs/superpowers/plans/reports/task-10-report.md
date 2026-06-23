# Task 10 Report: Advisor fn + engine wiring (`A1.fund`)

## Status: COMPLETE

---

## Changed Files

1. `/Users/jonathanmorris/poly_bot/arbiter/arbiter/engine/advisors.py`
2. `/Users/jonathanmorris/poly_bot/arbiter/arbiter/engine/__init__.py`
3. `/Users/jonathanmorris/poly_bot/arbiter/arbiter/engine/_engine.py`
4. `/Users/jonathanmorris/poly_bot/arbiter/tests/engine/test_a1_fund_advisor.py` (new)

---

## Edits Made

### 1. `advisors.py` — Added `_build_a1_fund_fn`

Added `_build_a1_fund_fn(db_path, pit, clock)` exactly mirroring `_build_a1_activist_fn`.
The inner `_fn()` calls `detect_signals` then filters to `[s for s in signals if s.source == "form13f"]`.
Placed immediately before `_build_a2_mirofish_fn`.

### 2. `engine/__init__.py` — Export `_build_a1_fund_fn`

Added `_build_a1_fund_fn` to the import block from `arbiter.engine._engine` and to `__all__`.

### 3. `_engine.py` — Two changes

**Horizon mapping (~line 527):**
```python
# Before:
horizon = 180 if sig.source in ("form4", "form13d") else 90
# After:
horizon = 180 if sig.source in ("form4", "form13d", "form13f") else 90
```
This ensures form13f signals spawn LONG-bucket (180d) ideas, preventing the orphan-attribution bug.

**Import block (~line 103-106):**
Added `_build_a1_fund_fn` to the `from arbiter.engine.advisors import (...)` block.

**advisor_map (~line 1014-1018):**
```python
advisor_map: dict[str, Callable[[], Opinion | None]] = {
    "A1.insider": _build_a1_insider_fn(config.db_path, pit, clock),
    "A1.congress": _build_a1_congress_fn(config.db_path, pit, clock),
    "A1.activist": _build_a1_activist_fn(config.db_path, pit, clock),
    "A1.fund": _build_a1_fund_fn(config.db_path, pit, clock),   # NEW
}
```

### 4. `tests/engine/test_a1_fund_advisor.py` — New test file

Three tests:

- `test_a1_fund_fn_emits_opinion` — unit test: seeds a form13f filing, builds the advisor fn with `BacktestClock`, calls it, asserts `op.advisor_id == "A1.fund"` and `op.ticker == "NVDA"`.
- `test_a1_fund_fn_returns_none_when_no_signals` — empty DB → fn returns None.
- `test_a1_fund_spawns_long_idea_and_links_opinion` — orphan-attribution regression (mirrors `test_a3_wiring.py::test_a3_spawns_short_idea_and_links_opinion`): seeds a form13f filing, runs a full `run_cycle`, asserts the spawned idea has `dedupe_key_bucket == "LONG"` and the persisted A1.fund opinion's `idea_id` equals the idea's `idea_id`.

---

## Deviations from Plan

1. **`FixedClock` → `BacktestClock`**: Plan referenced `arbiter.types.FixedClock` which does not exist. Used `BacktestClock(NOW)` from `arbiter.data.clock` per CORRECTIONS.
2. **`apply_migrations` → `run_migrations`**: Plan used `apply_migrations`; the actual function is `run_migrations`. Used that throughout.
3. **`created_at` column**: Added `created_at` to the INSERT (same value as `filing_ts`) per CORRECTIONS — the column is `NOT NULL` with no default.
4. **DB construction**: Used `get_connection(db_path)` + `run_migrations(conn)` then `conn.close()` for the unit test; used the exact `_make_engine` pattern from `test_a3_wiring.py` for the orphan test.
5. **`__init__.py` imports from `_engine.py`**: `__init__.py` re-exports from `_engine.py` (not `advisors.py` directly). `_engine.py` re-exports from `advisors.py`. Added `_build_a1_fund_fn` to the import chain in both files.

---

## Test Commands and Output

### New tests

```
cd /Users/jonathanmorris/poly_bot/arbiter
KILL_SWITCH_URL="" ALERT_WEBHOOK_URL="" .venv/bin/python -m pytest tests/engine/test_a1_fund_advisor.py -v

tests/engine/test_a1_fund_advisor.py::test_a1_fund_fn_emits_opinion PASSED
tests/engine/test_a1_fund_advisor.py::test_a1_fund_fn_returns_none_when_no_signals PASSED
tests/engine/test_a1_fund_advisor.py::test_a1_fund_spawns_long_idea_and_links_opinion PASSED
3 passed in 19.51s
```

### Full engine suite (regression)

```
KILL_SWITCH_URL="" ALERT_WEBHOOK_URL="" .venv/bin/python -m pytest tests/engine/ -q
15 passed in 29.25s
```

(12 pre-existing + 3 new = 15 total engine tests.)

### Linters

```
bash scripts/check_no_lookahead.sh
All no-lookahead checks passed (AST-based, comment/docstring-aware).

bash scripts/check_insert_only.sh
All insert-only checks passed (AST-based; §11.2 carve-outs allowlisted).
```

---

## Concerns

None. All gates pass. `A1.fund` is live in the advisor council and will produce opinions from `form13f` signals once Task 11 (runner ingest) is complete and real form13f filings are written to the DB.
