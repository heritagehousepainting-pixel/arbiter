# B5 — No-Look-Ahead LINT Adequacy Audit

**Lane:** B5 (READ-ONLY auditor)
**Target:** `scripts/check_no_lookahead.sh` — the AST-based no-look-ahead guard
**Scope:** the GUARD itself (what it catches vs. misses), comment/docstring soundness, INSERT-ONLY enforcement gap, recommended additional checks.
**Date:** 2026-06-19
**Auditor model:** claude-opus-4-8
**Interface basis:** `INTERFACES.md` §3 (PIT), §10/§10b (storage, insert-only), §11.1 (no `get_latest`/`datetime.now`), §11.2 (insert-only carve-out).

---

## VERDICT

**INADEQUATE as a structural guarantee — currently sound only as a "no literal `datetime.now`/`get_latest`" tripwire.**

The linter is correctly written for the two narrow literal patterns it targets, and its comment/docstring awareness is genuinely sound (it parses with `ast`, so comments and string literals can never be mistaken for calls — verified empirically). The PIT-purity design around it (clock injection, `CurrentPriceProvider` with no `as_of`, `BacktestClock` clock-type gating) is the real defense and is well-constructed. **But the linter as a standalone check is trivially bypassable**: at least 9 distinct wall-clock / look-ahead vectors pass it cleanly (`time.time()`, `date.today()`, `pd.Timestamp.now()`, `datetime.fromtimestamp(...)`, aliased `from datetime import datetime as dt; dt.now()`, module-aliased `import datetime as d; d.datetime.now()`, `os.environ` time, a wall-clock helper, `np.datetime64('now')`). A real look-ahead written through any of these would ship green. There is also **no INSERT-ONLY linter at all**, despite §11.2 making insert-only a hard, CI-grepped convention with explicit carve-outs — and raw `UPDATE`/`DELETE`/`INSERT OR REPLACE` statements already exist in production modules outside the sanctioned `db/helpers.py`.

The codebase happens to be clean today (the live run passes, and the design discipline holds), so these are latent gaps, not active violations. But the guard does not *enforce* the property it claims to; it documents it.

---

## FINDINGS

### P1 — Aliased `datetime` import fully bypasses the clock rule — `scripts/check_no_lookahead.sh:~70` — `from datetime import datetime as dt; dt.now()` and `import datetime as d; d.datetime.now()` both pass — the `datetime.now`/`utcnow` check only matches an `ast.Attribute` whose `func.value` is an `ast.Name` literally equal to `"datetime"`; any alias defeats it. Empirically confirmed: `dt.now()` → PASSES, `d.datetime.now()` → PASSES.
**Recommended action:** Resolve imports during the AST pass: walk `ast.Import`/`ast.ImportFrom` to build the set of local names that bind to `datetime.datetime` (and the module `datetime`), then flag `.now()`/`.utcnow()`/`.today()` on any of those bound names. Do not match on the literal token `"datetime"`.

### P1 — Other wall-clock primitives entirely unguarded — `scripts/check_no_lookahead.sh:~58-72` — `time.time()`, `time.monotonic()`, `time.localtime()`, `date.today()`, `datetime.today()`, `pd.Timestamp.now()`, `pd.Timestamp.today()`, `datetime.fromtimestamp(...)`, `datetime.utcfromtimestamp(...)`, `np.datetime64('now')` — none are in `FORBIDDEN_OUTSIDE_CLOCK`, so all pass. `time.monotonic()` is in fact already used in prod (`arbiter/ingest/edgar/client.py:166,169`) — benign (rate-limit elapsed, not a PIT timestamp) but it demonstrates the class is reachable and unchecked. The guard's own docstring claims it makes "look-ahead structurally impossible," which over-states what it verifies.
**Recommended action:** Add to the forbidden-attr set (outside `clock.py`): `today`, `fromtimestamp`, `utcfromtimestamp` on datetime-bound names; flag `time.time`/`time.localtime`/`time.gmtime`/`time.clock` calls; flag `pandas`/`pd` `Timestamp.now`/`Timestamp.today`/`Timestamp.utcnow`; flag `numpy.datetime64('now')`. Allow `time.monotonic`/`time.perf_counter` ONLY via an explicit allowlist comment (they are durations, not timestamps) or whitelist `edgar/client.py` specifically.

### P1 — No INSERT-ONLY linter exists, yet §11.2 is a CI-enforced carve-out convention — `scripts/` (absent) / `INTERFACES.md:263,302` — §11.2 says "Insert-only; the ONLY in-place UPDATE is the `is_superseded` flag flip inside `supersede_row()`," and §11 headers it "enforced; CI greps for violations." There is no grep/AST check for raw `UPDATE`/`DELETE`/`INSERT OR REPLACE` outside `db/helpers.py`. Several prod modules already issue raw mutations outside the helper: `arbiter/engine.py:336,417` (`UPDATE orders SET status`), `arbiter/engine.py:258` (`ON CONFLICT … DO UPDATE SET paused`), `arbiter/engine.py:1104` (`UPDATE orders SET idea_id`), `arbiter/safety/breakers.py:~115` (`INSERT OR REPLACE … DO UPDATE`), `arbiter/execution/position_store.py:66` (`DELETE FROM sim_positions`), `:76` (`ON CONFLICT … DO UPDATE`). These appear to be *sanctioned* carve-outs (orders table has no supersede columns per §10; breaker/position/engine_state are mutable runtime state, not the insert-only audit ledger) — but nothing encodes which tables are exempt, so a future regression on `opinions`/`filings`/`ideas`/`outcomes`/`trust_weights` would not be caught.
**Recommended action:** Add `scripts/check_insert_only.sh` (AST/regex over SQL string literals): flag `UPDATE`/`DELETE FROM`/`INSERT OR REPLACE`/`REPLACE INTO`/`ON CONFLICT … DO UPDATE` against the insert-only tables (`opinions, filings, ideas, outcomes, trust_weights, advisor_registry`), with an explicit allowlist of the mutable-state tables (`orders, breaker_state, sim_positions, engine_state, audit_meta`) and the one sanctioned `is_superseded` flip. Wire into `make lint`.

### P2 — A wall-clock helper or service indirection defeats the guard — `scripts/check_no_lookahead.sh` (design limit) — the guard is purely syntactic; any indirection (`from myclock import now; now()`, a `WallClock` class returning real time, reading a live price source inside a historical/backtest code path, or pulling "now" from `os.environ`) passes. The real protection is the injected-clock + `CurrentPriceProvider`-has-no-`as_of` design, NOT the linter. The `current_price` seam (`arbiter/data/current_price.py`) is well-built (no `as_of`, never registered with `PITGateway`, `NullCurrentPriceProvider` injected for sim + all backtests via clock-type gate), but the linter contributes nothing to enforcing that boundary.
**Recommended action:** Add an architectural check that no module under the backtest/PIT-read path constructs a live `Clock()` or `AlpacaCurrentPriceSource` directly (only `build_engine` may), and that `current_price` never appears in `pit._SUPPORTED_FIELDS`. A targeted grep test asserting `"current_price" not in _SUPPORTED_FIELDS` and that `Clock()` is instantiated only in `clock.py`/`build_engine` would convert design intent into an enforced invariant.

### P2 — Linter runs under system `python3`, not the project interpreter; `make lint` does not `cd` — `scripts/check_no_lookahead.sh:21` (`python3 - "$PACKAGE_DIR"`), `Makefile:10` — the heredoc invokes bare `python3`, not `.venv/bin/python`; on a machine where system `python3` is <3.9 the `list[str]` annotations in the script would `SyntaxError` and the check could be skipped or misreported. Also a `SyntaxWarning` already surfaces from `arbiter/ingest/congress/index.py:55` during the run, showing the script imports/compiles target files' bytes only via `ast.parse` (fine) but the warning noise can mask real output in CI logs.
**Recommended action:** Pin the interpreter (`"${PYTHON:-python3}"` defaulting to `.venv/bin/python`) and run with `-W ignore::SyntaxWarning` for the linter subprocess, or fix the invalid escape in `index.py:55` (out of this lane's write scope — flag only).

### P2 — `get_latest` attribute match is receiver-blind (false-positive surface) — `scripts/check_no_lookahead.sh:~58` — the check flags ANY `.get_latest()` call regardless of receiver (`cache.get_latest()`, `queue.get_latest()`, a third-party client method). Confirmed: `cache.get_latest()` → FLAGGED. Today there are zero such calls so it is harmless, but adding any library with a `get_latest` method would produce a spurious CI failure with no suppression mechanism.
**Recommended action:** Either keep the broad ban (it is conservative and currently clean — acceptable) but document that any future `get_latest`-named third-party method must be renamed at the call site or wrapped; or restrict to receivers known to be PIT gateways. Conservative-broad is defensible here; just record the decision.

### P3 — Tests directory is outside the linter's scan root — `scripts/check_no_lookahead.sh:18` (`PACKAGE_DIR=.../arbiter`) — tests live in `./tests/` (sibling of the `arbiter/` package), so `rglob("*.py")` never scans them. This is the *right* default (tests legitimately use `datetime.now`, `BacktestClock`, frozen times). The latent risk: a test helper/fixture that wraps a real clock and is then imported by production code would be invisible to the guard. No such import exists today.
**Recommended action:** No change required; note the boundary. If any prod module ever imports from `tests/`, extend the scan or fail that import explicitly.

---

## OPPORTUNITIES TO ADD (new lint rules worth adding)

1. **Resolve-and-flag aliased datetime** — bind-tracking AST pass covering `datetime`, `datetime.datetime`, and any `as`-alias; flag `.now/.utcnow/.today/.fromtimestamp/.utcfromtimestamp` outside `clock.py`. (Closes P1 #1.)
2. **Wall-clock primitive ban** — `time.time/localtime/gmtime`, `pd.Timestamp.now/today/utcnow`, `np.datetime64('now')`, with an explicit allowlist for `time.monotonic`/`perf_counter` (durations). (Closes P1 #2.)
3. **INSERT-ONLY linter** (`check_insert_only.sh`) — forbid raw `UPDATE/DELETE/INSERT OR REPLACE/ON CONFLICT DO UPDATE` against the insert-only ledger tables outside `db/helpers.py`, with a documented mutable-table allowlist. (Closes P1 #3 — the single highest-value addition since the convention is declared CI-enforced but isn't.)
4. **PIT-purity architectural assertions** — pytest-level invariants: `Clock()` instantiated only in `clock.py`/`build_engine`; `AlpacaCurrentPriceSource` constructed only when backend is `alpaca_paper` AND clock is live; `"current_price"` never in `pit._SUPPORTED_FIELDS`; no `as_of` parameter ever absent on a `PITGateway.get` call site. (Closes P2 #1.)
5. **Naive-datetime / future-date comparison sniff** — flag comparisons of a parsed/stored date against a wall-clock value, and flag tz-naive `datetime(...)` constructions feeding clock/PIT paths (P3-level heuristic; high noise, run advisory-only).
6. **Interpreter pin + warning suppression** in the guard and a `cd` in `make lint`. (Closes P2 #2.)
7. **Self-test fixture for the guard** — a tests-only file of known-bad snippets the linter MUST flag, so regressions in the guard's own coverage are caught. Currently the guard has no test proving it still detects `datetime.now()`.
