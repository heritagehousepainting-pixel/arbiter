# Task 1 Report: Schema Migrations (holdings + cusip cache)

## Status: DONE

## Files Created or Modified

- **CREATED** `/Users/jonathanmorris/poly_bot/arbiter/arbiter/db/migrations/027_form13f_holdings.sql`
- **CREATED** `/Users/jonathanmorris/poly_bot/arbiter/arbiter/db/migrations/028_cusip_map.sql`
- **CREATED** `/Users/jonathanmorris/poly_bot/arbiter/tests/ingest/edgar/test_form13f_normalize.py`

No existing files were modified.

## Test Commands and Output

### Step 1: Failing test (before migrations)

Command:
```
KILL_SWITCH_URL="" ALERT_WEBHOOK_URL="" .venv/bin/python -m pytest tests/ingest/edgar/test_form13f_normalize.py::test_migrations_create_form13f_tables -v
```

Output (abbreviated):
```
FAILED tests/ingest/edgar/test_form13f_normalize.py::test_migrations_create_form13f_tables
AssertionError: assert {'accession',...ut_call', ...} <= set()
1 failed in 0.05s
```

Confirmed: fails with empty set from PRAGMA (tables don't exist).

### Step 2: Passing test (after migrations)

Command (gate command exactly as specified):
```
cd /Users/jonathanmorris/poly_bot/arbiter && KILL_SWITCH_URL="" ALERT_WEBHOOK_URL="" .venv/bin/python -m pytest tests/ingest/edgar/test_form13f_normalize.py -q
```

Output:
```
.                                                                        [100%]
1 passed in 0.02s
```

## Insert-Only Linter Output

Command: `bash scripts/check_insert_only.sh`

Output:
```
All insert-only checks passed (AST-based; §11.2 carve-outs allowlisted).
```

Clean — both new tables are DDL-only; no UPDATE or DELETE expressions introduced.

## Deviations from Plan

1. **`apply_migrations` → `run_migrations`:** The plan's Task 1 code sample uses `apply_migrations`, but the actual function in `arbiter/db/migrate.py` is `run_migrations`. Used `run_migrations` per the correction override.

2. **Connection pattern:** Used `get_connection(":memory:")` + `run_migrations(conn)` rather than a bare `sqlite3.connect(":memory:")`, matching the established project pattern from `tests/integration/test_pit_same_cycle.py` and the correction override.

3. **No conftest fixture reuse:** The existing `tests/ingest/edgar/conftest.py` provides no migrated-connection fixture (only `config`, `make_resp`, `read_fixture`), so a module-local `_migrated_conn()` helper was written as the plan specified.

## Concerns

None. Both migration files exactly match the CREATE TABLE statements from the plan's Task 1 section. The test verifies all required columns for both tables. The insert-only linter is clean.
