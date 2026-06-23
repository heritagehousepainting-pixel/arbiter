# Form 13F (`A1.fund`) build — progress ledger

Plan: `docs/superpowers/plans/2026-06-23-form13f-fund-advisor.md`
Spec: `docs/specs/2026-06-23-form13f-fund-advisor-spec.md`
Mode: subagent-driven, **no-git adaptation** (file-based diffs; ledger here, not in .git).
Gate per task: `KILL_SWITCH_URL="" ALERT_WEBHOOK_URL="" .venv/bin/python -m pytest <task tests> -q` + relevant linter.

| Task | Title | Status |
|------|-------|--------|
| 1 | Schema migrations (027 holdings, 028 cusip_map) | ✅ complete |
| 2 | Config keys | ✅ complete |
| 3 | Manager roster seed (+ live CIK verify) | ✅ complete |
| 4 | CUSIP→ticker resolver | ✅ complete |
| 5 | 13F info-table parser | ✅ complete |
| 6 | EdgarClient 13F discover+fetch | ✅ complete |
| 7 | Holdings store + delta engine | ✅ complete |
| 8 | Detection `_detect_fund_holdings` | ✅ complete |
| 9 | Emit `A1.fund` + bearish sign flip | ✅ complete |
| 10 | Advisor fn + engine wiring | ✅ complete |
| 11 | Runner ingest + people registration | ✅ complete |
| 12 | Cockpit `A1.fund` node | ✅ complete |
| 13 | Full-suite gate + live smoke + deploy | ✅ complete (DEPLOYED LIVE) |

## Global plan corrections (apply to every task)
- Migration runner is `run_migrations` (NOT `apply_migrations`). Migrated conn: `get_connection(":memory:")` + `run_migrations(conn)`.
- Config: the constructor is the module fn `load_config()` (NOT `Config.load()`). `Config` is `@dataclass(frozen=True)`; add fields in the class body with defaults AND wire in `load_config()` via `_env_float`/`_env_int` (mirror `a3_min_stance`, config.py:186 + :337). Affects plan Tasks 2 and 7.
- EDGAR fixtures live in `tests/ingest/edgar/fixtures/` (NOT `tests/fixtures/`).

## Carry-forward for later tasks
- **Test fixed clock = `BacktestClock(as_of)`** from `arbiter.data.clock` (there is NO `FixedClock`).
- **Tasks 8 & 10 test INSERTs into `filings` MUST include `created_at`** (TEXT NOT NULL, no default). Plan's raw-INSERT test snippets omit it → would fail. Add `created_at` (use the filing_ts value).
- **Task 11 (runner): dedupe 13F-HR/A amendments** — `compute_deltas` selects ALL rows for a `report_date`; an amendment shares the report_date but has a different accession, so storing both would mix/double-count holdings. Runner should store only the latest accession per (manager, report_date), or skip amendments. (Edge case; amendments rare.)

## Minor findings (for final review triage)

- Task 4: `cusip_resolver.py` has dead `from arbiter.db.helpers import generate_ulid  # noqa: F401` — remove at final review (cusip is PK, no ULID needed).
- Task 1: `tests/ingest/edgar/test_form13f_normalize.py` has unused `import pytest` (later tasks append to this file and will likely use it; clean up if still unused at final review).

## Log

Task 1: complete (tests: 1 passed; insert-only clean; review clean inline — migrations verbatim per plan)
Task 2: complete (tests: 26 passed incl test_config; review clean inline — fields + load_config wiring + CIK parser correct)
Task 3: complete (done INLINE by controller — subagent hit a 500 + classifier outage, created nothing. All 11 CIKs verified live vs data.sec.gov 13F-HR. Notable: Baupost=0001054420 (not plan's 0001061768), Aschenbrenner DOES file 13F now=0002045724. tests: 1 passed)
Task 4: complete (tests: 4 passed; both linters clean; used inline `# insert-only-ok` marker on cusip_map cache write, did NOT touch the linter script; review clean inline)
Task 5: complete (tests: 2 passed; parser local-name-match + never-raise + whole-dollar values; fixture at tests/ingest/edgar/fixtures/; review clean inline)
Task 6: complete (tests: 15 new, full edgar suite 121 passed = no regression; _parse_submissions_json extended additively with report_date (defensive); _archives_base factored DRY; get_form13f_info_table never-raises; no-lookahead clean; review: read new methods + ran edgar suite myself)
Task 7: complete (tests: 6 passed; both linters clean; CORE delta engine — controller fixed plan's conn.total_changes bug→cur.rowcount, added full-exit test case; reviewed impl line-by-line; PIT/floors/first-filing-topK/delta-only all correct. Noted amendment-same-quarter edge for Task 11.)
Task 8: complete (tests: 1 new, full signals suite 100 passed = no regression; no-lookahead clean; FUND_HOLDING + cap 0.7 + meta sign + no-lookahead drop confirmed matching plan; implementer also fixed a UNIQUE-violation in plan test data + created_at)
Task 9: complete (tests: 2 new, full signals suite 102 passed = no regression; no-lookahead clean; A1.fund advisor-id + 180d + generalised bearish sign-flip (form13d,form13f) confirmed at emit.py:49/65/119/146)
Task 10: complete (engine wiring: _build_a1_fund_fn + __init__ export + _engine horizon set + advisor_map registration; 3 new engine tests incl orphan-attribution; both linters clean. Controller ran BROADER regression (engine+integration) and caught 2 advisor-count test-expectation regressions the task-scoped run missed — fixed inline (test_end_to_end advisor_count 3→4, test_learning_loop weights set +A1.fund/FUND const). test_gate live_advisor_count==3 is an explicit arg, unaffected (37 passed).)
Task 11: complete (runner _ingest_form13f + _make_edgar_for_form13f + _alpaca_asset_lookup (defensive, real adapter symbols verified) + _normalize_filing_date + source wiring (default tuple + dispatch); amendment-dedupe (latest accession per report_date, 2 recent quarters); 4 new tests, full ingest suite 572 passed, both linters clean; reviewed loop + alpaca symbols line-by-line)
Task 13 (in progress): full hermetic suite 2478 passed + both linters + cockpit web tsc/67 vitest + api 88 all GREEN. Live `arbiter ingest --sources form13f` SMOKE caught 2 real sim-invisible bugs (controller fixed both inline + added 2 regression tests, 12 form13f tests green):
  BUG-A: runner processed the 2 newest report_dates newest-first → each treated as first_filing_topk (duplicate signals). FIX: store both quarters oldest→newest as baseline, emit deltas ONLY for the newest (real Q/Q delta); single-quarter manager → first_filing top-K (intended cold-start).
  BUG-B: `_raw` set txn_idx=0 for every delta → write_filing dedups by (accession,txn_idx) → ALL tickers of one 13F collapsed to ONE signal (13 written despite 194 holdings). FIX: `_finalize_txn_idx` assigns unique ticker-sorted (stable→idempotent) txn_idx per delta.
  Re-ingest after fix: 19 clean signals, 0 duplicate (manager,ticker), real deltas (Buffett AMZN exit, Loeb GOOGL/META new, Wood META trim, Tepper AMZN add — bull+bear mix). DB backup data/arbiter.db.pre-form13f-fix-bak.
  DEPLOYED: full suite re-green 2480 passed; daemon kickstarted (pid 70975), engine.built advisors=[A1.insider,A1.congress,A1.activist,A1.fund] on alpaca_paper, heartbeat healthy (is_open=true, paused=false). A1.fund LIVE — participates next full-cycle slot (14:00/15:00 ET). BUILD COMPLETE.
Task 12: complete (cockpit graph.py 5 additions: src.form13f source node + A1·Funds advisor (future=False/live) + source→advisor + filing-source→node + figure-kind "fund manager"; events.py _VALID_ADVISORS + state.py cold-start loop & intensity map; cockpit api 88 passed, web tsc clean + 67 vitest; web contract untouched (no advisor-id enum). reviewed graph additions inline.)
