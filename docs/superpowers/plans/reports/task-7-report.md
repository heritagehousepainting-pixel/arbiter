# Task 7 Report: Holdings Store + Delta Engine

## Status: DONE

## Changed Files

- **Created:** `/Users/jonathanmorris/poly_bot/arbiter/arbiter/ingest/edgar/form13f_normalize.py`
- **Modified:** `/Users/jonathanmorris/poly_bot/arbiter/tests/ingest/edgar/test_form13f_normalize.py` (appended Task 7 tests)

## Rowcount Fix Applied

The plan's `if conn.total_changes: stored += 1` was replaced with cursor-based rowcount:

```python
cur = conn.execute("INSERT OR IGNORE INTO form13f_holdings (...) VALUES (...)", (...))
if cur.rowcount and cur.rowcount > 0:
    stored += 1
```

`conn.total_changes` is cumulative for the whole connection lifetime and is truthy after any prior insert, making it always increment `stored` after the first row — breaking idempotency counting. `cur.rowcount` is set to `1` on actual insert and `0` when `INSERT OR IGNORE` skips a duplicate UNIQUE row, so the count is accurate.

## Exit-Case Test Added

`test_new_exit_add_trim_flat` was strengthened beyond the plan's 3-holding scenario:

- Q1 includes `META PLATFORMS INC` (`30303M102`, 60M, 1000 shares) in addition to NVDA/AAPL/TSLA.
- Q2 omits META entirely (full exit).
- After `compute_deltas` the test asserts `deltas["META"]["txn_type"] == "S"`.
- All plan assertions preserved: NVDA flat (absent from deltas), AAPL add → `"P"`, TSLA trim → `"S"`, AMZN new → `"P"`.

## Corrections Applied

1. `load_config()` used throughout (not `Config.load()`); `Config` imported for typing only.
2. `_migrated_conn()` reused from Tasks 1+2 — not redefined.
3. Module-level imports placed after the existing test code with `# noqa: E402` to avoid import order issues in a single-file test module.

## Test Command + Output

```
KILL_SWITCH_URL="" ALERT_WEBHOOK_URL="" .venv/bin/python -m pytest tests/ingest/edgar/test_form13f_normalize.py -v
```

```
collected 6 items

tests/ingest/edgar/test_form13f_normalize.py::test_migrations_create_form13f_tables PASSED
tests/ingest/edgar/test_form13f_normalize.py::test_config_form13f_defaults PASSED
tests/ingest/edgar/test_form13f_normalize.py::test_first_filing_emits_top_k_conviction_snapshot PASSED
tests/ingest/edgar/test_form13f_normalize.py::test_new_exit_add_trim_flat PASSED
tests/ingest/edgar/test_form13f_normalize.py::test_noise_floors_drop_small_positions PASSED
tests/ingest/edgar/test_form13f_normalize.py::test_unresolved_cusip_dropped PASSED

6 passed in 2.04s
```

## Linter Outputs

```
bash scripts/check_no_lookahead.sh
All no-lookahead checks passed (AST-based, comment/docstring-aware).

bash scripts/check_insert_only.sh
All insert-only checks passed (AST-based; §11.2 carve-outs allowlisted).
```

Both clean. `form13f_normalize.py` uses plain `INSERT OR IGNORE` (a pure insert), so no marker was needed.

## Invariants Verified

- **PIT:** `filing_ts` is always `filing_date` passed in, never `report_date`. Asserted in `test_first_filing_emits_top_k_conviction_snapshot`.
- **Delta-only:** flat holdings within ±0.25 emit no signal. NVDA (same shares) absent from deltas in `test_new_exit_add_trim_flat`.
- **Noise floors:** `test_noise_floors_drop_small_positions` confirms $5M position yields empty result.
- **First filing top-K:** 6 holdings → exactly 5 emitted as `"P"`.
- **Full exit:** META absent from Q2 → `"S"` delta emitted.
- **Unresolvable CUSIP:** `ZZZ999999` returns `n == 0` from `store_holdings`.
- **Options stored but no delta:** `put_call` non-null rows get `ticker=None` and are stored without entering the delta loop (`put_call IS NULL` filter in `compute_deltas`).

## Deviations

None. All plan logic followed exactly, with the three documented corrections applied.

## Concerns

None blocking. The `asset_lookup` callable passed to `store_holdings` in tests is a static dict lambda; the real runner (Task 11) will pass the live Alpaca asset list.
