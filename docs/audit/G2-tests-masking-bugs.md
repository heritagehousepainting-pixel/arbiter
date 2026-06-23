# G2 Audit — Tests That MASK Bugs + Offline-Test Integrity

- **Lane:** G2 (false-confidence tests + offline integrity)
- **Auditor:** READ-ONLY (no source/test/config modified)
- **Date:** 2026-06-19
- **Suite size:** 1877 tests collected
- **Verdict:** **AMBER-RED.** Offline-test *integrity* is strong (no real network, no real blocking sleeps in unit paths — all HTTP is injected/transport-mocked). But the suite contains at least **two confirmed P0 false-confidence holes** where the green bar actively hides production P0 bugs (ADV/volume use a Bar-only fixture path that production never takes; the live engine never feeds book-state into the exposure caps, yet those caps are "tested"). A meaningful slice of the "1877 green" is theater around the data-extraction and risk-cap seams. Estimate: ~85–90% of the suite is real safety; the remaining ~10–15% (the data-shape + portfolio-state seams) is confidence-positive but bug-masking.

---

## FINDINGS

### [P0] — ADV tests use Bar fixtures while production `price_close` source returns a SCALAR PRICE — `tests/data/test_adv.py:63` (and all of `TestADV20d*`)
- **What the test does:** Every ADV test builds the PIT via `make_adv_fixture_pit(...)` which registers **`Bar` objects**. `adv_20d` → `_extract_dollar_volume` then hits the `isinstance(value, Bar)` branch (`arbiter/data/adv.py:158`) and correctly computes `close * volume`.
- **What production does:** The live source is `AlpacaPriceSource.get("price_close", ...)` which returns **`latest.close` — a bare float price** (`arbiter/data/sources/alpaca.py:249-250`), NOT a `Bar`. So `_extract_dollar_volume` falls into the *scalar* branch (`adv.py:135-141`) and treats the **closing price (~$200)** as if it were dollar-volume. Production ADV becomes "mean closing price," off by ~6 orders of magnitude from true dollar volume (~$300M).
- **Why false confidence:** The ADV liquidity cap is the LAST sizing transform (`sizing.py:141-149`, `adv_cap = adv_cap_pct * adv`). With ADV mis-valued as a price, `adv_cap` is ~$200·pct — effectively pinning every order to a few dollars, or (depending on cap_pct) silently neutering the liquidity guard. The tests never exercise the scalar/price path that production actually takes, so the bug is invisible and the cap *looks* validated.
- **Recommended fix:** Add a test that registers the **real** `AlpacaPriceSource` (with `FakeAlpaca`/transport-mocked bars) and asserts `adv_20d` returns dollar-volume-scale numbers, OR make the scalar branch fail-closed (return None) so price-as-volume can never be silently accepted. Production must guarantee `price_close` carries volume (return a `Bar`, or add a dedicated `volume`/`dollar_volume` field).

### [P0] — VolumeAnomalyGate tests rely on the same Bar-vs-scalar ambiguity; production volume extraction is unverified on the live source — `arbiter/defenses/volume_anomaly.py:68-86`, tests in `tests/defenses/test_volume_anomaly.py`
- **What:** `_extract_volume` has the identical Bar/scalar fork as ADV. Tests feed it `Bar`s (or scalars matched to expectations); production `price_close` returns a **price scalar**, so the gate computes a z-score over **closing prices, not volumes**. The anomaly gate (a LIVE safety breaker per the module docstring) is effectively detecting price moves while claiming to detect volume spikes.
- **Why false confidence:** Same masking mechanism — the only data shape exercised is the one production never emits. A "passing" volume-anomaly suite gives false assurance that the A3 circuit-breaker works.
- **Recommended fix:** Same as above — test against the real `price_close` source, and fail-closed when the value is not a `Bar` carrying volume.

### [P0] — Live engine never passes book-state into the exposure caps; sizing tests pass them EXPLICITLY — `arbiter/engine.py:1030` (call site) vs `tests/policy/test_sizing.py:386-424`
- **What production does:** `_bound_decide` (engine.py:1029-1040) calls `_decide(...)` and **omits** `current_sector_exposure`, `current_gross_exposure`, and `current_open_positions`. These default to `0.0 / 0.0 / 0` all the way down to `compute_size` (`sizing.py:70-72`). Consequently in the live path: the **sector cap, gross-exposure cap, and open-position-count cap are inert** — every order is sized as if the book is empty (sector_headroom = full, gross_headroom = full, open-position cap never trips).
- **What the tests do:** The ONLY tests exercising those caps (`tests/policy/test_sizing.py:386` sector, `:400` gross, `:412/:424` open-count) pass the exposure values **explicitly as kwargs**, proving the cap math works in isolation. No engine/orchestrator test asserts the engine *threads real portfolio state in*. (`grep` for `current_gross/current_sector/current_open` across `tests/` returns only `test_sizing.py` and `test_decision.py`.)
- **Why false confidence:** The risk caps look fully covered and green, but the production wiring that would feed them is missing, so three of the system's portfolio-level risk limits do nothing live. This is the textbook "engine-never-passes-book" P0 the suite is blind to.
- **Recommended fix:** Add an engine/cycle-level test that runs a multi-position cycle and asserts later orders are capped by accumulated gross/sector/open-count (i.e., that the engine queries the position store and passes it through). Then fix `_bound_decide` to source real exposures.

### [P1] — Attribution "count" assertion can be satisfied by the fallback-proxy path — `tests/evaluation/test_attribution.py:105`
- **What:** `assert len(ids) == 2` checks only the *number* of resolved outcomes. The resolver has a last-resort proxy fallback that increments `attribution.fallback_proxy` and emits a synthetic neutral outcome (`arbiter/evaluation/attribution.py:82-119`). A count-only assertion would still pass if a real opinion silently degraded to the proxy path.
- **Mitigant:** The surrounding test *does* additionally assert per-advisor `stance_score`/`advisor_confidence` (lines 111-114) and a sibling test asserts `# real opinion, not proxy` (line 135), so this specific case is mostly defended. The risk is the *pattern*: any future count-only attribution assertion masks proxy-degradation.
- **Recommended fix:** In every attribution test, additionally assert `attribution.fallback_proxy` was NOT incremented (or assert the advisor_id is the real one), so the proxy path can never silently satisfy a count check.

### [P2] — No global network kill-switch in conftest; offline safety depends entirely on per-test discipline — `tests/conftest.py` (no socket guard)
- **What:** There is no `pytest-socket`/`disable_socket` or autouse network-blocking fixture. Offline integrity is real *today* (verified: `test_alerting`/`test_kill_switch` `@patch("httpx.post/get")`; `test_sources` patches `arbiter.data.sources.alpaca.httpx.Client`; `test_congress_client`/`test_mirofish` inject a `FakeTransport`; `alpaca_adapter` uses injectable `http_post/get/delete`), but it is enforced only by convention. One forgotten patch → a real outbound call in CI.
- **Why false confidence:** "All offline" is a property the suite *currently* has but does not *guarantee*. A regression would not be caught.
- **Recommended fix:** Add an autouse session fixture that disables sockets (allow only the SQLite/in-memory paths), forcing any un-mocked HTTP to fail loudly.

### [P2] — Scheduler timeout tests use REAL `time.sleep(5)` worker threads — `tests/orchestrator/test_scheduler.py:103,114`
- **What:** `TestTimeoutIsolation` spawns advisors that `time.sleep(5)` and relies on a 0.1s timeout to kill them. These are genuine blocking sleeps (not injected). They don't make the suite slow *if* cancellation works — but if the timeout mechanism regresses, these tests would hang for 5s each rather than fail fast, and they exercise wall-clock timing (flaky under CI load).
- **Why this is a (minor) integrity gap:** The lane's "no real sleep" expectation is technically violated here; the sleeps are load-bearing for the test semantics, not mocked.
- **Recommended fix:** Use an injectable/`Event`-based blocker the test controls, asserting cancellation deterministically instead of racing a real 5s sleep against a 0.1s deadline.

### [P3] — `arbiter.safety` import-guard test self-passes if the package is absent — `tests/safety/test_breakers.py:440`
- **What:** The test `pytest.skip(...)` when `import arbiter.safety` raises ImportError ("pre-integration"). The package now exists, so it runs — but the construct means the guard *would* silently skip rather than fail if the package were ever removed/renamed.
- **Recommended fix:** Drop the skip now that the package exists; assert the import succeeds, then assert `not hasattr(safety_pkg, "reset")`.

### [NOTE — RESOLVED] Trust fixture stance default
- The prompt flagged that the trust fixture "once defaulted stance=binary." Current fixtures set `stance_score=float(binary)` (e.g. `tests/trust/test_trust_store.py:40`, `test_calibrator.py:31`, `test_multi_advisor.py:20`). Coupling stance to the binary outcome direction is realistic (`stance = binary * confidence`), so this is no longer a masking default. No action needed; noted for completeness.

---

## OFFLINE-TEST INTEGRITY — SUMMARY
- **Real network calls:** NONE found. `httpx`/`requests` imports in `tests/` are all for mocking (`@patch`, `MockTransport`/`FakeTransport`, injected `http_*` callables). Adapter (`alpaca_adapter.py`) and sources (`alpaca.py`) are designed for injection.
- **Real sleeps:** Only `tests/orchestrator/test_scheduler.py` uses real `time.sleep(5)` (load-bearing for timeout tests — see P2). `tests/ingest/test_senate.py` correctly **patches** `senate.time.sleep`.
- **Real `datetime.now()`:** None in test logic — all timestamps injected via `BacktestClock`/`as_of`/hard-coded; the `datetime.now()` grep hits are all in docstrings asserting its *absence*.
- **Gap:** No enforced socket guard (P2) — integrity is correct but unguaranteed.

---

## OPPORTUNITIES TO ADD
1. **Production-shape ADV/volume test:** register the real `AlpacaPriceSource` (FakeAlpaca-backed) and assert `adv_20d` / `VolumeAnomalyGate` produce dollar-volume / volume-scale numbers — the single highest-value test the suite is missing.
2. **Engine book-state integration test:** multi-position cycle asserting that gross/sector/open-count caps actually bite via the engine (would have caught the inert-caps P0).
3. **Autouse socket-disable fixture** in `conftest.py` to make "offline" a guaranteed invariant.
4. **`fallback_proxy`-not-incremented assertion** baked into every attribution test (and ideally a metric-counter fixture).
5. **Negative scalar-path test** for `adv.py`/`volume_anomaly.py`: explicitly assert that a bare-price scalar is rejected/fail-closed, locking in the eventual fix.
6. **Deterministic scheduler cancellation test** replacing the real-sleep races.
