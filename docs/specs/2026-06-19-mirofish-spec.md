# MiroFish A2 Client — Hardening + Contract-Test + Wiring Spec

**Date:** 2026-06-19
**Owner lane:** `arbiter/adapters/mirofish/**` + `tests/adapters/mirofish/**`
**Status of live integration:** NOT-DONE — blocked on `MIROFISH_ENDPOINT` (service is not running; cannot verify the wire end-to-end).
**Advisor family:** A2 (the second "brain"). Self-hosted quantitative reality-check engine, called over **local HTTP only** (AGPL isolation, INTERFACES.md §11.5 conv. 5 — never `import mirofish`).

This spec is design-only. No code is changed here. It cites concrete file:line for every gap and specifies file-by-file hardening, the configured-or-noop behavior, an offline contract-test plan, the frozen wiring contract handed to the Wave-2 engine owner, and the honest NOT-DONE statement for `SETUP_NEEDED.md`.

---

## 1. Current state — what each file does + concrete gaps

The client already exists (~849 lines across 5 files) and is **inert** because `MIROFISH_ENDPOINT` is unset. There is also an existing test module at `tests/adapters/test_mirofish.py` (540 lines, all offline/mocked) — it is **misplaced** relative to the stated ownership (`tests/adapters/mirofish/**`) and will be relocated (see §4).

### `arbiter/adapters/mirofish/__init__.py` (581 bytes)
Docstring-only package marker. Names the public entry point (`adapter.run`), shadow-mode posture, hard weight cap 0.35, advisor id `A2.mirofish`. No code. **Gap:** does not re-export `run` / `ADVISOR_ID`, so callers must reach into `adapter` directly. Minor; harmless.

### `arbiter/adapters/mirofish/adapter.py` (`run()` — the public entry point)
- `_idea_fingerprint(idea)` — SHA-256 of `f"{ticker}|{thesis}|{horizon_days}"` (`adapter.py:78`). Stable, good.
- `_opinions_from_response(...)` — converts raw dicts → validated `Opinion`s; per-opinion try/except skips invalid ones (`adapter.py:119-145`). Sets `confidence_source=ConfidenceSource.MODELED`, `advisor_id=A2.mirofish`, shared `run_group_id`. Good.
- `run(idea, as_of, *, conn, client, breaker, is_backtest)` — cache lookup → fresh HTTP call → cache write → convert. Fail-closed: returns `[]` on `MirofishUnavailable` and on any `Exception` from `client.analyze` (`adapter.py:246-259`). Good baseline.

**Gaps / fragilities:**
1. **`run()` assumes `response` is a well-formed dict.** `response.get("opinions", [])` at `adapter.py:261` will raise `AttributeError` if MiroFish returns a JSON list, a string, or `null`. That `AttributeError` is *not* caught (the try/except only wraps the `client.analyze` call, lines 240-259), so a malformed top-level body **crashes `run()`** — violating the "never raises" contract at `adapter.py:188`.
2. **Negative-stance passthrough is correct but untested + undocumented.** `_opinions_from_response` does `float(raw["stance_score"])` (`adapter.py:124`) with no clamping, and `validate_opinion` accepts `[-1.0, 1.0]` (`opinion.py:96`). So negatives DO pass through today — but there is **no test** proving it, and the frozen contract makes this load-bearing. Must be locked by a regression test.
3. **`raw["stance_score"]`/`["confidence"]`/`["horizon_days"]` are required keys**; a missing key raises `KeyError`, which *is* caught per-opinion (`adapter.py:137`). Fine. But `horizon_days` of e.g. `0` or `400` is caught only via `validate_opinion`'s `ValueError` — also fine. No gap, but the contract test must cover it.
4. **Cache-replay run_group_id reconstruction is fragile** (`adapter.py:229`): `cached[0].get("run_group_id", fingerprint)` — depends on the write path having stamped `run_group_id` into every dict (it does, `adapter.py:270-272`). Correct, but couples read to write shape; a test must roundtrip it.
5. **No bound on opinion count.** A pathological response with thousands of opinions is materialized unbounded. Low risk for a localhost service; add a soft cap + warning.
6. **`idea` duck-typing is unguarded.** `idea.ticker` / `.thesis` / `.horizon_days` (`adapter.py:78,193`) raise `AttributeError` before any try/except if the caller passes a malformed idea. Since the engine builder (§5) owns idea-shape, this is acceptable, but `run()` should fail-closed rather than propagate.

### `arbiter/adapters/mirofish/http_client.py` (`MirofishHTTPClient`)
- Reads endpoint from `MIROFISH_ENDPOINT` (`_get_endpoint`, `http_client.py:49-52`) — empty/whitespace → `None`.
- `analyze()` raises `MirofishUnavailable` if endpoint unset (`http_client.py:143`); calls `check_egress(url)` before any I/O (`http_client.py:149`); `httpx.post` with 1200s timeout; `raise_for_status()`; `.json()`.
- Circuit breaker: `_record_failure` / `_record_success`, fires `breaker()` once per failure streak at threshold (`http_client.py:190-214`). Good.

**Gaps / fragilities:**
1. **`MirofishUnavailable` is defined at module bottom (`http_client.py:222`) but referenced in `analyze` at line 144** — works because it's resolved at call time, but it's declared *after* the class. Move it above the class for clarity (no behavior change).
2. **No retry/backoff.** A single transient blip (one dropped connection) counts as a full failure and pushes toward the breaker. For a localhost 15–20-min run this is defensible (you do NOT want to silently re-run a 20-min job), but a **bounded, fast-failure retry for connection-establishment errors only** (not for timeouts of an in-flight run) is worth adding: retry `httpx.ConnectError` up to N=2 with short backoff, never retry `TimeoutException` or HTTP 4xx/5xx. This keeps "expensive run already started" semantics safe while smoothing over a cold socket.
3. **`.json()` can raise `json.JSONDecodeError`** on a non-JSON 200 body; currently caught by the broad `except Exception` at `http_client.py:168` → counts as a failure and re-raises → `run()` swallows to `[]`. Acceptable, but it pollutes the failure counter for a *malformed-but-reachable* service. Distinguish "unreachable" from "reachable-but-garbage": a parse failure should NOT advance the breaker (the service is up; the breaker is for outages).
4. **Egress only guards the `/analyze` URL host, not the scheme.** `check_egress` accepts `http://` and `https://` for localhost — fine. But there's no enforcement that the configured endpoint is *local* (see egress §2 below). The wire contract says **localhost-only egress** for the inference endpoint; today `MIROFISH_ENDPOINT=http://data.sec.gov/...` would pass egress (sec.gov is allowlisted) even though that is not a MiroFish endpoint. Add an **endpoint-locality assertion** for the inference URL specifically.
5. **Timeout is a single scalar (1200s connect+read).** A cold/missing service then blocks for up to 20 min on connect. Split into `httpx.Timeout(connect=5.0, read=1200.0, write=10.0, pool=5.0)` so an *absent* service fails fast on connect while a *running* job still gets its 20 min.

### `arbiter/adapters/mirofish/egress.py` (`check_egress`)
- `ALLOWED_HOSTS` frozenset (`egress.py:58-79`): SEC EDGAR, localhost/127.0.0.1/::1, Simfin, FinancialModelingPrep, Alpaca data.
- `_BLOCKED_KEYWORDS` deny list (news/social/transcript/etc., `egress.py:86-109`) checked **first**.
- `check_egress(url)` parses host, blocks on keyword, then enforces allowlist, returns url unchanged (`egress.py:112-160`).

**Gaps / fragilities:**
1. **No localhost-only enforcement for the inference endpoint.** The allowlist is the right design for A2's *data sources* (filings/factor vendors), but the **MiroFish inference POST** specifically must go to localhost/loopback per the frozen contract. A non-local but allowlisted host (e.g. `data.sec.gov`) configured as `MIROFISH_ENDPOINT` would slip through. Add `check_inference_egress(url)` that requires the host ∈ `{localhost, 127.0.0.1, ::1}` (a strict subset), used by `http_client.analyze` for the `/analyze` call; keep the broad `check_egress` for any future data-source fetches.
2. **Substring keyword matching is over-broad but safe-by-design.** `"x.com" in host` also blocks e.g. `xx.com`; `"fool" in host` blocks `foolproof.io`. This is intentional (fail-closed toward independence) and documented (`egress.py:18-25`). No change; just note it in tests so the behavior is pinned, not accidental.
3. **`urlparse` hostname is `None` for scheme-less inputs** (`"localhost:8765"` parses `localhost` as the *scheme*). `check_egress` raises `ValueError` (`egress.py:138`). Good — but `http_client` builds the URL from a configured base, so the base MUST include a scheme. The configured-or-noop path (§3) must validate scheme presence and fail-closed (treat as disabled) rather than raise.

### `arbiter/adapters/mirofish/run_cache.py` (forward-test-only cache)
- `get(conn, fingerprint, date_str, *, is_backtest)` — raises `BacktestCacheError` if `is_backtest` (`run_cache.py:73`); else `SELECT ... LIMIT 1`; returns `json.loads(row["raw_opinions_json"])` or `None`.
- `put(...)` — insert-only, ULID PK, `is_forward_test_only=1`, `created_at` defaults to `"NO_CLOCK"` sentinel (no `datetime.now()`). UNIQUE(fingerprint, as_of_date).
- Backed by migration `007_mirofish.sql` (table matches; index present).

**Gaps / fragilities:**
1. **`get()` requires `conn.row_factory = sqlite3.Row`** because it does `row["raw_opinions_json"]` (`run_cache.py:94`). Production `db/connection.py:36` sets this; the test helper sets it (`test_mirofish.py:56`). But if a caller passes a tuple-factory connection, this raises `TypeError`. `adapter.run` wraps cache reads in try/except (`adapter.py:212`) → degrades to a cache miss. Acceptable; document the row_factory precondition in the docstring.
2. **`put()` calls `conn.commit()` internally** (`run_cache.py:147`) — couples cache writes to the caller's transaction boundary. For the standalone forward path this is fine; flag it so a future batched-cycle caller knows it commits.
3. **`put` raises `sqlite3.IntegrityError` on duplicate** (`run_cache.py:108-111` docstring). `adapter.run` only writes after a cache `get` miss, but two racing forward cycles for the same idea+day could collide. `adapter.py:277` catches the write error and logs (non-fatal). Correct; pin with a test.
4. **`created_at="NO_CLOCK"`** when caller omits it. `adapter.run` never passes `created_at`, so every cache row is stamped `"NO_CLOCK"`. The migration comment claims `created_at` is "tz-aware UTC ISO string" (`007_mirofish.sql:24`) — **mismatch**. Either thread `as_of.isoformat()` from `run()` into `put()` (preferred — `as_of` is already available and is the honest information timestamp), or update the migration comment. **Recommend: pass `as_of.isoformat()` as `created_at`** from `adapter.run`'s write path so the column means something.

---

## 2. Hardening design (file-by-file, within `arbiter/adapters/mirofish/**`)

### `egress.py`
- **Add** `LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})`.
- **Add** `check_inference_egress(url: str) -> str`: runs `check_egress(url)` first (keyword + allowlist), then asserts `urlparse(url).hostname` ∈ `LOOPBACK_HOSTS`, raising `EgressViolation` otherwise with a message naming the localhost-only inference contract. Returns url on success.
- Keep `check_egress` (host allowlist + keyword denylist) for data-source fetches (forward-compatible).
- **Scheme guard (added in security-audit hardening):** `check_egress` rejects any scheme outside `{"http", "https"}` with `EgressViolation` — closing the `ftp://` / `gopher://` / `dict://`-to-loopback SSRF vector at the single enforcement point (not just at the `http_client` base-URL check). `_ALLOWED_SCHEMES` is the frozenset that defines this. `check_inference_egress` inherits it (it calls `check_egress` first).
- No change to `ALLOWED_HOSTS` / `_BLOCKED_KEYWORDS`.

### `http_client.py`
- **Move** `MirofishUnavailable` above `MirofishHTTPClient` (clarity; no behavior change).
- **Split timeout:** replace scalar `self._timeout` use in `httpx.post` with `httpx.Timeout(connect=5.0, read=self._timeout, write=10.0, pool=5.0)`. Keep `DEFAULT_TIMEOUT_S=1200.0` as the *read* timeout (the long in-flight run). Connect stays short so an absent service fails fast.
- **Use** `check_inference_egress(url)` (not `check_egress`) for the `/analyze` POST — enforces localhost-only for the inference endpoint specifically.
- **Endpoint scheme guard:** in `analyze`, before building the URL, verify `self._endpoint` starts with `http://` or `https://`; if not, raise `MirofishUnavailable` (fail-closed, treated as disabled) rather than letting `urlparse` mis-parse.
- **Bounded connect-only retry:** wrap the `httpx.post` in a loop that retries **only** `httpx.ConnectError` (cold socket) up to `CONNECT_RETRIES = 2` with `0.2s, 0.4s` backoff. **Never** retry `httpx.TimeoutException` (an in-flight 20-min run must not be silently re-launched) nor HTTP status errors. On exhausting retries, `_record_failure()` and re-raise.
- **Parse-failure is not an outage:** catch `json.JSONDecodeError` (and a non-dict body) separately; log `mirofish.analyze.bad_body`, raise a new `MirofishBadResponse(MirofishUnavailable)` subclass that the breaker path treats as **abstain without advancing the consecutive-failure counter** (service is reachable; it's the payload that's wrong). `_record_success()`-equivalent for breaker purposes (reset streak) but still raise so `run()` returns `[]`.
- Keep the existing `breaker`/`_record_failure`/`_record_success`/`reset_breaker` semantics for genuine network/HTTP failures.

### `adapter.py`
- **Top-level response validation:** after `client.analyze`, guard that `response` is a `dict`; if not, log `mirofish.adapter.bad_response_shape` and `return []`. Then `raw_opinions = response.get("opinions", [])` and guard that it is a `list`; non-list → `[]`. This closes the `AttributeError`-crash gap (current `adapter.py:261`).
- **Wrap the whole body of `run()` defensively at the boundary:** an outer try/except `Exception` around idea-attribute access + the pipeline that logs `mirofish.adapter.unexpected` and returns `[]`, so a malformed `idea` (missing `.ticker`/`.thesis`/`.horizon_days`) fails closed instead of propagating. The fail-closed contract (`adapter.py:188`) becomes literally true for all inputs.
- **Negative-stance passthrough:** explicitly DO NOT clamp. Add an inline comment at `_opinions_from_response` noting that `stance_score < 0` is a first-class SHORT/bearish signal and must reach fusion unchanged. (No code change to the conversion — it already passes through; the comment + a contract test make it intentional and regression-proof.)
- **Soft opinion-count cap:** if `len(raw_opinions) > MAX_OPINIONS_PER_RUN` (e.g. 32), log a warning and truncate. Defends against a runaway response.
- **Thread `created_at`:** call `run_cache.put(..., created_at=as_of.isoformat())` so cache rows carry the real information timestamp (fixes the migration-comment mismatch, §1.run_cache.4).
- Keep cache-read try/except (degrade to miss) and cache-write try/except (non-fatal) as-is.

### `run_cache.py`
- **Document the `row_factory = sqlite3.Row` precondition** in `get()`'s docstring (it already relies on it at `run_cache.py:94`).
- **Accept and store `created_at`** is already supported (`put` signature has it); no change needed beyond the adapter now passing it.
- No schema change required — migration `007` already matches.

### `__init__.py`
- Re-export the stable surface: `from .adapter import run, ADVISOR_ID`. Cosmetic but lets `engine.py` import `arbiter.adapters.mirofish` cleanly.

---

## 3. Configured-or-noop design (behavior when `MIROFISH_ENDPOINT` is unset/empty)

**Invariant:** when the endpoint is absent, A2 is a clean no-op — zero opinions, one disabled log, never a crash, never advances the breaker.

Concrete behavior:
1. `_get_endpoint()` returns `None` for unset/empty/whitespace (`http_client.py:49-52`) — already correct.
2. A `MirofishHTTPClient` built with no endpoint has `self._endpoint is None`. `analyze()` raises `MirofishUnavailable` *before* any I/O (`http_client.py:143`). No egress check, no socket, no breaker tick.
3. `adapter.run` catches `MirofishUnavailable` and returns `[]` (`adapter.py:246-252`) — abstain, no exception.
4. **Add a one-time disabled log at the wiring boundary** (the engine-owned builder, §5): when constructing the advisor fn, if `_get_endpoint() is None`, log `mirofish.disabled` once at build time and return a fn that **short-circuits to `[]` without constructing a client or touching the network** — so the per-cycle hot path doesn't spam logs. The fn still returns `[]` cleanly (matching the existing fail-closed return shape).
5. **Breaker is never armed when disabled:** because `analyze` raises before `_record_failure`, the disabled state cannot trip the breaker. Verified by test (§4, disabled-noop).

Result: unset endpoint ⇒ A2 contributes nothing, logs that it is disabled exactly once per build, and the engine runs A1-only exactly as today.

---

## 4. Contract test plan (OFFLINE — mock the HTTP layer)

**Location:** relocate the existing `tests/adapters/test_mirofish.py` → `tests/adapters/mirofish/` (per ownership), splitting into:
- `tests/adapters/mirofish/__init__.py`
- `tests/adapters/mirofish/test_egress.py`
- `tests/adapters/mirofish/test_http_client.py`
- `tests/adapters/mirofish/test_adapter.py`
- `tests/adapters/mirofish/conftest.py` (the `_FakeIdea`, `_make_memory_db`, `_mirofish_response` helpers from the current file).

**No real network in any test** (INTERFACES.md §11.7 — mock). `httpx.post` is patched; `client.analyze` is mocked with `MagicMock(spec=MirofishHTTPClient)`.

Existing tests to keep (already passing, just relocated): egress allow/block (10 cases), shared-run_group_id, MODELED confidence, cache hit/miss/roundtrip, breaker fire/not-fire/reset, unreachable→[], fingerprint stability, empty-opinions, skip-invalid, no-`datetime.now()` AST guard, backtest-cache guard.

**New tests this spec requires:**

1. **Happy path** (already covered by `test_run_returns_valid_opinions_with_shared_run_group_id`) — keep.

2. **NEGATIVE stance passthrough (NEW, load-bearing):** mock `analyze` to return an opinion with `stance_score = -0.7`; assert the resulting `Opinion.stance_score == -0.7` (NOT clamped to 0, NOT abs'd), `validate_opinion` passes, and the bearish opinion is returned. A second case at the boundary `stance_score = -1.0` passes; `-1.0001` is skipped (out of range).

3. **Malformed response (NEW):** parametrize `analyze` return value over `{}`, `{"opinions": None}`, `{"opinions": "nope"}`, `["list", "not", "dict"]`, `None`, `"raw string"`. Each must yield `run(...) == []` and **must not raise** (closes the §2 `AttributeError` gap). Plus a per-opinion malformed case: `{"opinions": [{"confidence": 0.5}]}` (missing `stance_score`) → skipped, `[]`.

4. **Timeout (NEW):** patch `httpx.post` with `side_effect=httpx.TimeoutException("read timeout")`; assert `run(...) == []`, the breaker counter advanced (timeout IS a real failure), and **no retry occurred** (assert `httpx.post.call_count == 1` — timeouts must not re-launch the 20-min run).

5. **Connect-retry (NEW):** patch `httpx.post` to raise `httpx.ConnectError` twice then succeed; assert it retried and ultimately returned opinions (or, if all 3 attempts fail, returned `[]` and advanced the breaker by one streak). Pins the connect-only-retry policy.

6. **Disabled-noop (NEW):** with `MIROFISH_ENDPOINT` unset (monkeypatch `os.environ`), the engine-owned builder's fn (or a `MirofishHTTPClient(endpoint=None)`) yields `run(...) == []`, raises nothing, and **does not touch `httpx`** (assert `httpx.post` was never called via a patch that would fail the test if hit). Breaker counter stays 0.

7. **Localhost-egress rejection of non-local URLs (NEW):** `check_inference_egress("https://data.sec.gov/analyze")` raises `EgressViolation` (allowlisted-but-not-loopback). `check_inference_egress("http://localhost:8765/analyze")` passes. `check_inference_egress("http://169.254.169.254/analyze")` (cloud metadata) raises. Also assert a non-local `MIROFISH_ENDPOINT` flowing through `MirofishHTTPClient.analyze` raises `EgressViolation` (and `run()` swallows it to `[]`, fail-closed). **Scheme guard + DNS-rebind (added in security audit):** `check_egress`/`check_inference_egress` reject non-http(s) schemes (`ftp://localhost/...`, `gopher://...`) with `EgressViolation`; and loopback look-alikes (`127.0.0.1.attacker.com`, `localhost.attacker.com`, `localhost@evil.com`, decimal `2130706433`, `0.0.0.0`) all fail closed (exact-match allowlist).

8. **Bad-body-is-not-an-outage (NEW):** mock a reachable 200 with non-JSON body → `run()` returns `[]` AND the breaker consecutive-failure counter is **unchanged** (distinguishes malformed-but-up from down). Pins §2.http_client parse-failure policy.

9. **created_at is the information timestamp (NEW):** after a cache write, query the row and assert `created_at == as_of.isoformat()` (not `"NO_CLOCK"`). Pins §2.adapter created_at threading.

All tests run fully offline and deterministic; no `MIROFISH_ENDPOINT` is ever set during the suite.

---

## 5. WIRING CONTRACT (frozen) — FOR WAVE 2 (engine owner)

> **Ownership note:** everything in this section requires editing `engine.py`, which this lane does NOT own. It is specified here as the frozen interface the Wave-2 engine owner implements. This lane guarantees `arbiter.adapters.mirofish.adapter.run` matches it.

**The tension to resolve:** the engine's `advisor_map` is `dict[str, Callable[[], Opinion | None]]` and `run_named_advisors_parallel` returns `dict[str, Opinion | None]` — strictly **one** opinion per advisor_id (`engine.py:224`, `scheduler.py:93-130`, `cycle.py:223-229`). MiroFish emits a **list** (SHORT + MEDIUM, sharing a `run_group_id`). A single `Opinion | None` slot cannot carry a list. So MiroFish must NOT be jammed into the existing single-opinion `advisor_map`.

**Frozen wiring shape** — the engine owner adds a parallel, list-valued advisor channel:

```python
# Owned by mirofish lane — already matches today:
def run(idea, as_of, *, conn=None, client=None, breaker=None, is_backtest=False) -> list[Opinion]: ...

# FOR WAVE 2 (engine owner) — build a per-cycle, zero-arg, list-valued fn,
# mirroring the _build_a1_*_fn pattern (engine.py:135-192) but returning a LIST:
def _build_a2_mirofish_fn(
    db_path: str,
    clock: Clock,
    breaker: Callable[[], None] | None,
) -> Callable[[Idea], list[Opinion]]:
    def _fn(idea: Idea) -> list[Opinion]:
        as_of = clock.now()
        thread_conn = get_connection(db_path)   # fresh per-call (thread-safe, like A1)
        try:
            return mirofish.run(idea, as_of, conn=thread_conn, breaker=breaker)
        finally:
            thread_conn.close()
    return _fn
```

Key differences from `_build_a1_*_fn` and the integration points the engine owner must honor:
1. **Signature takes `idea`** — A1 advisors are idea-agnostic scanners (`() -> Opinion | None`); MiroFish analyzes a *specific* idea, so its fn is `(Idea) -> list[Opinion]`. It belongs in the **per-idea** loop, not the once-per-cycle `advisor_map`. Wire it inside `cycle.py`'s `for idea in pending_ideas:` block (around `cycle.py:247`), calling `_build_a2_mirofish_fn(...)(idea)` and **extending** `valid_opinions` (or the per-bucket grouping) with the returned list before fusion.
2. **Returns `list[Opinion]` (0..N), never `None`.** Empty list = abstain. The engine owner appends each opinion to the existing `opinions_by_bucket` grouping (`cycle.py:242-245`); the buckets are derived per-opinion via `op.horizon_bucket`, so multi-bucket MiroFish output fuses correctly with A1.
3. **`advisor_id` scheme:** the family/channel id is **`A2.mirofish`** (constant `ADVISOR_ID`, `adapter.py:51`). Every emitted `Opinion.advisor_id == "A2.mirofish"`. Sub-horizon opinions are distinguished by `horizon_bucket` + `source_fingerprint`, **not** by a different advisor_id — they are the same advisor speaking about multiple horizons, sharing one `run_group_id`. Registry: register `A2.mirofish` with `hard_weight_cap=0.35` (INTERFACES.md §5; `opinion.py:171`).
4. **Shadow/weight=0:** A2 stays shadow (recorded, weight 0 in fusion) until Lane 11 promotion. The shadow flag lives on the trust ledger's `AdvisorWeight`, NOT in this client — the client always emits its true opinions.
5. **Configured-or-noop at the builder:** the engine owner gates construction on `_get_endpoint() is not None`; when unset, log `mirofish.disabled` once and register a fn returning `[]` (§3). A2 then contributes nothing and the cycle runs A1-only — exactly today's behavior.
6. **Backtest:** pass `is_backtest=isinstance(clock, BacktestClock)` so `run_cache.get` raises `BacktestCacheError` (forward-test-only). In backtest, A2 either runs fresh against a historical `as_of` or abstains — never replays cached forward results.

**One-line contract:** `arbiter.adapters.mirofish.adapter.run(idea, as_of, *, conn, client, breaker, is_backtest) -> list[Opinion]`; engine wraps it as a per-idea `(Idea) -> list[Opinion]` fn with a fresh DB connection, registers `A2.mirofish` at `hard_weight_cap=0.35`, extends the per-bucket opinion pool with the result, and no-ops cleanly when `MIROFISH_ENDPOINT` is unset.

---

## 6. NOT-DONE statement (for SETUP_NEEDED.md, item #6)

The client can be fully hardened and contract-tested **offline**, but the following **cannot be verified** until the user stands up `MIROFISH_ENDPOINT` (the self-hosted MiroFish service is confirmed NOT running):

- **Live wire-schema conformance.** That the real service returns `{opinions:[{stance_score, confidence, horizon_days, rationale, source_fingerprint}], run_id}` exactly. Tests mock this shape; only a live call proves the server honors it.
- **Negative-stance emission end-to-end.** The client passes negatives through (proven by unit test), but that the *service actually emits* `stance_score < 0` is unverifiable without it running.
- **Real timeout / latency behavior.** The 15–20-min run duration, the split connect(5s)/read(1200s) timeout tuning, and breaker behavior under genuine slow/dropped runs can only be validated against the real service.
- **Localhost egress in practice.** `check_inference_egress` enforces loopback in code; that the operator's actual endpoint is loopback (and that AGPL isolation holds — no `import mirofish`) is a deployment fact, not a testable one here.
- **Cache forward-test correctness under real runs** (run_id stability across the real `/analyze`, dedup across a real cycle).

**Proposed SETUP_NEEDED.md update (item #6):** keep `🔒` but annotate:
> **Client HARDENED + contract-tested offline (mocked, fail-closed, localhost-egress-enforced incl. http(s)-only scheme guard + DNS-rebind/alt-encoding rejection, negative-stance passthrough proven).** A2 is a clean no-op while `MIROFISH_ENDPOINT` is unset — zero opinions, logs `mirofish.disabled` once, never crashes, never trips the breaker. **NOT verified live:** wire-schema conformance, real negative emission, real timeout/latency, and end-to-end fusion — all blocked on the user running the self-hosted MiroFish service and setting `MIROFISH_ENDPOINT=http://localhost:<port>`.

The engine wiring (§5) is **deferred to Wave 2** (engine owner) and is independent of the service being up — it can be built and tested with a mocked `run`, but A2 stays shadow/weight-0 and no-op until both the wiring lands AND the endpoint is live.
