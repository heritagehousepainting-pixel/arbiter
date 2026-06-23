# Task 4 Report: CUSIP→ticker resolver

## Status
DONE — all gates green.

## Changed files
- **Created:** `/Users/jonathanmorris/poly_bot/arbiter/arbiter/ingest/edgar/cusip_resolver.py`
- **Created:** `/Users/jonathanmorris/poly_bot/arbiter/tests/ingest/edgar/test_cusip_resolver.py`
- **Linter allowlist file modified:** None — used `# insert-only-ok` inline marker on the `_cache_put` `.execute()` call instead.

## Test command + output
```
cd /Users/jonathanmorris/poly_bot/arbiter && KILL_SWITCH_URL="" ALERT_WEBHOOK_URL="" .venv/bin/python -m pytest tests/ingest/edgar/test_cusip_resolver.py -q
```
Output: `4 passed in 0.88s`

## Linter output
```
bash scripts/check_insert_only.sh   -> All insert-only checks passed (AST-based; §11.2 carve-outs allowlisted).
bash scripts/check_no_lookahead.sh  -> All no-lookahead checks passed (AST-based, comment/docstring-aware).
```

## Insert-only linter: did you modify it?
**No.** The `INSERT OR REPLACE` in `_cache_put` was sanctioned using the inline `# insert-only-ok` marker on the `.execute()` call line (the linter's built-in per-site opt-in mechanism). The comment also documents why: "cusip_map is a resolution CACHE, not trade/ledger state". The `scripts/check_insert_only.sh` script itself was NOT modified.

## Corrections applied
1. Used `get_connection(":memory:")` + `run_migrations(c)` (not the plan's `apply_migrations`) in the test `_conn()` helper — per the task override instructions.
2. Test file placed at `tests/ingest/edgar/test_cusip_resolver.py` (not `tests/ingest/edgar/test_cusip_resolver.py` — path matches instruction exactly).
3. Implementation placed at `arbiter/ingest/edgar/cusip_resolver.py` (nested package path).

## Deviations
None from the plan code. Implementation is verbatim from Task 4 Step 3, with `# insert-only-ok` added on the `_cache_put` execute line.

## Concerns
None. Resolver is hermetic, drop-safe, and cache-growing. Task 7 (delta engine) can now import `resolve_cusip` directly.
