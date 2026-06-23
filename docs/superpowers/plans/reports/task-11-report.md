# Task 11 Report: Runner Ingest + People Registration + Source Wiring

## Status: DONE

## Changed Files

- `/Users/jonathanmorris/poly_bot/arbiter/arbiter/ingest/runner.py` — primary change
- `/Users/jonathanmorris/poly_bot/arbiter/tests/ingest/test_runner_form13f.py` — tests appended

## What Was Changed

### `runner.py`

1. **Default `sources` tuple updated** (line ~103):
   ```python
   sources: Sequence[str] = ("form4", "form13d", "congress", "form13f"),
   ```

2. **Dispatch added in `run_ingest`** after the congress block:
   ```python
   if "form13f" in sources_tuple:
       _ingest_form13f(config, conn=conn, clock=clock, summary=summary)
   ```

3. **`_make_edgar_for_form13f(config) -> EdgarClient`** — module-level helper returning `EdgarClient(config=config)`. Monkeypatchable by name in tests.

4. **`_alpaca_asset_lookup(config) -> Callable[[], dict[str, str]]`** — returns a lazily-evaluated closure that:
   - Guards: returns `lambda: {}` immediately if `config.executor_backend != "alpaca_paper"` or keys are missing.
   - When live: imports `AlpacaAdapter` and `_default_http_get` from `arbiter.execution.alpaca_adapter`, calls `GET /v2/assets?asset_class=us_equity&status=active&tradable=true`, builds `{name.upper(): symbol}` dict.
   - On ANY exception: logs warning and returns `{}` (never crashes, never blocks ingest).
   - Caches the result via nonlocal so it's fetched once per ingest run.

5. **`_normalize_filing_date(filed_at) -> str`** — converts bare EDGAR date strings (`"2026-05-15"`) to tz-aware ISO (`"2026-05-15T00:00:00+00:00"`).

6. **`_ingest_form13f(config, *, conn, clock, summary)`** — mirrors `_ingest_sc13`:
   - `src = SourceSummary(); summary.per_source["form13f"] = src`
   - UA-empty guard: warns + returns inert (no crash, other sources still run)
   - Determines CIK set: `config.form13f_manager_ciks or manager_ciks()`
   - Registers each manager: `resolve_person(m.name, "form13f", {}, conn, clock)` → builds `{cik: person_id}`
   - Per CIK (fault-isolated): `refs = client.search_form13f_filings(cik)`
   - **Amendment dedupe**: groups refs by `report_date`, keeps only the one with the latest `filed_at` → `by_report: dict[str, dict]`. Processes most recent 1–2 report_dates.
   - For each selected ref: normalizes `filing_date`, fetches `get_form13f_info_table`, parses with `parse_form13f_infotable`, calls `store_holdings`, calls `compute_deltas`, writes each delta via `write_filing` with `_count_filings` before/after accounting.
   - `client.close()` in a `finally` block.

## `_alpaca_asset_lookup` Failure Fallback

- If `executor_backend != "alpaca_paper"`: returns `{}` immediately (sim mode).
- If Alpaca keys missing: returns `{}`.
- If network error / HTTP 4xx/5xx: catches exception, logs warning, returns `{}`.
- In all fallback cases, `store_holdings` → `resolve_cusip` will use the `cusip_map` DB cache and the seed CUSIP→ticker mappings only. Unresolved CUSIPs are dropped per the spec.

## Amendment Dedupe Approach

Before processing, refs are grouped by `report_date` into a dict. For each group, only the ref with the lexicographically largest `filed_at` string is kept (ISO date strings sort correctly). This prevents mixing original + amendment holdings for the same quarter. The two most recent distinct `report_dates` are then processed (enough to diff current vs prior quarter).

## Source Wiring

- `"form13f"` added to the default `sources` tuple: `("form4", "form13d", "congress", "form13f")`.
- Dispatch in `run_ingest`: `if "form13f" in sources_tuple: _ingest_form13f(...)`.

## Test Commands + Output

### Target suite:
```
KILL_SWITCH_URL="" ALERT_WEBHOOK_URL="" .venv/bin/python -m pytest tests/ingest/test_runner_form13f.py -q
4 passed in 3.66s
```

### Full ingest suite (no regression):
```
KILL_SWITCH_URL="" ALERT_WEBHOOK_URL="" .venv/bin/python -m pytest tests/ingest/ -q
572 passed, 3 warnings in 32.17s
```

### Linters:
```
bash scripts/check_no_lookahead.sh
All no-lookahead checks passed (AST-based, comment/docstring-aware).

bash scripts/check_insert_only.sh
All insert-only checks passed (AST-based; §11.2 carve-outs allowlisted).
```

## Tests Added (in `tests/ingest/test_runner_form13f.py`)

1. `test_roster_shape_and_ciks` — existing (Task 3), kept intact
2. `test_ingest_form13f_writes_people_holdings_and_filing` — monkeypatches `_make_edgar_for_form13f` and `_alpaca_asset_lookup`; asserts people(form13f) >= 11, form13f_holdings >= 1, filings(source='form13f') >= 1
3. `test_ingest_form13f_ua_empty_guard` — empty UA → no EdgarClient created, no holdings written
4. `test_form13f_in_default_sources` — introspects `run_ingest` signature to confirm `"form13f"` in default sources

## Deviations from Plan

- The plan's test referenced `from arbiter.types import FixedClock` which does not exist (it's `BacktestClock` in `arbiter.data.clock`). Per the task instructions, the runner clock is `Callable[[], str]` — tests use `lambda: NOW_ISO` directly instead.
- `test_ingest_form13f_inert_under_backtest` was dropped per the task instructions (no BacktestClock guard in the runner; ingest is live-only).
- `Config.load()` does not exist; tests use direct `Config(...)` construction, consistent with the rest of the ingest test suite.
- The `_FakeEdgar` in the test has a `close()` method added (the real client has one; the runner calls it in `finally`).

## Concerns

None. All gates pass cleanly.
