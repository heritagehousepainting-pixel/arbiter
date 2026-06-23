# A3 Engine Wiring, Shadow-Mode Learning Loop, and Cockpit Integration Design

**Date:** 2026-06-22  
**Lane:** Engine Wiring + Learning-Loop (Shadow-Mode) + Cockpit Integration  
**Status:** Design spec — no code written

---

## 1. Advisor ID

`A3.news`

This is already the correct ID — it appears in `cockpit/api/graph.py` (line 28) and in the calibration stance-base table (`stance_base.py` prefix `"A3"`). The ID is stable and pre-registered everywhere that matters.

---

## 2. advisor_map vs. Separate-Channel Decision

**Recommendation: `advisor_map` pattern (like A1), NOT the separate-channel pattern (like A2).**

### Why A2 had to be a separate channel

A2 (MiroFish) is idea-specific — it takes a fully-constructed `Idea` object and returns 0..N opinions, one per idea analyzed. Because the `advisor_map` contract is `() -> Opinion | None` (zero-arg, returns at most one opinion), A2 could not fit in the map without either:
- Collapsing N per-idea opinions into one (information loss), or
- Building one map entry per idea × advisor (N² combinatorial explosion at build time).

A2 therefore lives as `engine.a2_mirofish_fn: Callable[[Idea], list[Opinion]]`, called after ideas are constructed, appended to `valid_opinions`, and replayed into the cycle via the synthetic `A2.mirofish#{i}:{bucket}` key trick in `_opinion_provider_map()`.

### Why A3 fits naturally in `advisor_map`

A3 is a **market-wide sweep** advisor — it scans news/social feeds for any ticker with actionable signal, then picks the best one (or abstains). This is structurally identical to A1.insider and A1.congress:

- `A1.insider`: scan DB for form4 signals → pick best → `Opinion | None`
- `A1.congress`: scan DB for congress signals → pick best → `Opinion | None`
- `A3.news`: scan news/smart-money feed for any ticker → pick best → `Opinion | None`

A3 does NOT need the idea first; it generates its own lead independent of what the rest of the cycle is thinking about. The map pattern is the right fit.

**Consequence:** A3 opinions participate in the existing `run_named_advisors_parallel` call at line 452 of `_engine.py`, are included in `raw_opinions`, flow naturally into `valid_opinions`, persist via `_persist_cycle_opinions`, and replay via the standard path in `_opinion_provider_map`. Zero A2-style special-casing required.

---

## 3. How A3 Opinions Reach Fusion

The end-to-end flow with A3 in `advisor_map`:

```
build_engine()
  └─ advisor_map["A3.news"] = _build_a3_news_fn(config.db_path, pit, clock)

run_cycle()
  ├─ raw_opinions = run_named_advisors_parallel(self.advisor_map, ...)
  │    ├─ "A1.insider" -> Opinion | None
  │    ├─ "A1.congress" -> Opinion | None
  │    ├─ "A1.activist" -> Opinion | None
  │    └─ "A3.news" -> Opinion | None        ← NEW (runs in parallel thread)
  │
  ├─ valid_opinions = [op for op in raw_opinions.values() if op is not None]
  ├─ live_advisor_count = len(valid_opinions)   ← A3 counted here already
  ├─ _run_exit_monitor(now, valid_opinions)     ← A3 conviction seen by exit monitor
  ├─ [idea construction, A2 MiroFish injection]
  ├─ _persist_cycle_opinions(now, valid_opinions, ideas)  ← A3 opinion persisted
  ├─ weight_bundle, calibrator = _build_learning_inputs(now)
  │    └─ resolve_weight_bundle(ledger_bundle, live_ids=[..., "A3.news"], ...)
  │         └─ "A3.news" aw=None (cold start) → weight=EQUAL_FLOOR=0.25, shadow=False
  │
  └─ _bound_fuse(opinions, bucket) → FusionOutput
       └─ A3.news opinion fused at floor weight (shadow=False → included in pool)
```

The `_opinion_provider_map()` replays cached opinions as zero-arg callables. A3 maps directly as `"A3.news" -> lambda: cached_op`. No synthetic key needed (unlike A2's N-opinion problem) because A3 emits at most one opinion per cycle.

**Horizon assignment:** A3 news signals are short-to-medium latency plays (news catalysts decay fast). The A3 builder should emit opinions with `horizon_days` in the SHORT range (5–30 days). This means A3 opinions will link to ideas by `(ticker, HorizonBucket.SHORT)` in `_persist_cycle_opinions`. The engine's idea constructor currently assigns `horizon=90` for congress signals and `horizon=180` for form4/form13d. A3-originated ideas would need `horizon=14` or similar (SHORT bucket). This is the ONE place the engine's idea-construction block may need updating: if A3 is the signal source that triggered `detect_signals`, the horizon assignment branch needs to recognize the A3 source.

---

## 4. Shadow-Mode First — The Exact Path by Which A3 Graduates

**A3 needs zero special-casing to be in shadow mode.** Here is the precise machinery that handles it:

### 4a. Cold-start bootstrap (day 0)

When `build_engine()` adds `"A3.news"` to `advisor_map` for the first time, there are no rows in `trust_weights` for `"A3.news"`. In `_build_learning_inputs()`:

```python
outcomes_by_advisor = trust_store.load_outcomes_for_learning(engine.conn, now)
# → "A3.news" absent (no outcomes yet)

ledger_bundle = engine.ledger.update(outcomes_by_advisor, ...)
# → "A3.news" absent in ledger_bundle.weights (ledger only knows what it has outcomes for)
```

Then in `resolve_weight_bundle()`:
```python
for advisor_id in live_ids:  # includes "A3.news"
    aw = ledger_weights.get("A3.news")  # → None (no ledger row)
    # → takes the cold-start branch:
    resolved["A3.news"] = AdvisorWeight(weight=EQUAL_FLOOR=0.25, shadow=False)
```

So A3 is NOT shadow (shadow=False) but IS at the probationary floor (0.25). This means A3 **participates in fusion at 0.25 weight from day one** — the same bootstrap posture as every new A1 advisor. This is the documented "keep trading while cold" policy (weight_resolver.py docstring, Decision D3).

**However**, the callout in weight_resolver.py's D3 rationale is that EQUAL_FLOOR (0.25) is strictly below the 0.50 graduated ceiling, so A3 cannot outvote a fully-graduated A1 advisor even on day 1.

### 4b. What "shadow mode" means in this codebase

The term "shadow" in the codebase means `AdvisorWeight.shadow=True`, which causes `pool.py` to EXCLUDE the advisor from fusion entirely (it cannot influence any sizing). The existing machinery uses shadow=True for two cases:
1. **Negative-skill suppression** (`cap_reason == "negative_skill"`): a demonstrably harmful advisor is muted.
2. **A2 MiroFish pre-launch**: the MiroFish adapter was wired with `shadow=True` in the ledger while the endpoint was unset. This was a temporary configuration choice, not a structural requirement.

For A3, we have two sub-options:

**Option A (recommended): Floor-weight, non-shadow from day 1**
Match the A1 pattern exactly. A3 participates at EQUAL_FLOOR=0.25 weight. It scores outcomes and calibrates through the normal learning loop. If it accumulates negative skill, it gets suppressed. This is the "keep trading while cold" policy, which the existing system uses for every new advisor.

**Option B: Explicit shadow until configured**
Gate A3 to `shadow=True` (weight=0, excluded from fusion) until a config flag `A3_ENABLED=true` is set. This is more conservative — A3 emits and persists opinions (so the learning loop can accumulate outcomes) but contributes zero to actual sizing decisions until the operator explicitly promotes it. The mechanism: the `_build_a3_news_fn` builder returns a function that emits valid `Opinion` objects (so they persist and train the ledger), but the `Config` check in `build_engine` sets an initial `AdvisorWeight(weight=0, shadow=True)` seed row in `trust_weights` for `"A3.news"` when `A3_ENABLED` is not set.

**Recommendation: Option A** is simpler and consistent with how A1.activist was added. Option B adds complexity for marginal gain. The floor weight of 0.25 is already probationary; the negative-skill suppression path handles the case where A3 turns out to be harmful.

If the other parallel agents designing A3's ingest pipeline need hard confirmation that A3 won't size real paper positions until reviewed, Option B provides that guarantee at the cost of another config gate.

### 4c. Graduation path

Graduation is driven entirely by `TrustLedger.update()` and the significance gating in `should_update()`. No advisor-specific code is needed. The flow:

1. A3 emits opinions; `_persist_cycle_opinions` links them to ideas.
2. Ideas close → `outcome_runner.run_outcome_sweep` → `resolve_advisor_outcomes` fans out one `ResolvedOutcome` per contributing advisor, including `"A3.news"`.
3. `load_outcomes_for_learning` returns A3's outcomes on subsequent cycles.
4. `engine.ledger.update(outcomes_by_advisor, ...)` includes A3 in the update once it has ≥ the ledger's activation threshold of outcomes.
5. `persist_weight_bundle` writes a `trust_weights` row for `"A3.news"`.
6. On the next warm cycle, `resolve_weight_bundle` finds a non-None `aw` for `"A3.news"` with `shadow=False` and `weight > 0` → uses the learned weight (floor at EQUAL_FLOOR_GRADUATED=0.02).
7. Eventually, if A3's skill CI exceeds the significance gate (seeded bootstrap CI in the ledger), the weight climbs above EQUAL_FLOOR toward the 0.50 ceiling.

**A3 needs no changes to the ledger, the weight resolver, the calibration system, or the attribution pipeline.** It is fully generic machinery.

---

## 5. Attribution and Scoring Parity with A1/A2

`attribution.resolve_advisor_outcomes()` (in `arbiter/arbiter/evaluation/attribution.py`) works purely from `query_opinions_for_idea(conn, idea_id)`. It deduplicates to one opinion per `advisor_id` and fans out one `ResolvedOutcome` for each. Since A3 opinions are persisted via the same `opinion_store.persist_opinion` call in `_persist_cycle_opinions`, and linked to ideas by `(ticker, HorizonBucket)` just like A1 opinions, A3 outcomes are attributed identically.

The fallback proxy (`PROXY.A3.news`) fires only if A3 had no persisted opinion linked to the idea — the same edge case as for A1. No A3-specific attribution code.

The `MultiAdvisorCalibrator.transform_for("A3.news", ...)` path: if no calibrator is in `engine.calibrators` for A3, it falls back to `(raw_stance + 1.0) / 2.0` (linear stance→prob map). Once A3 has ≥ `MIN_APPLY_NONZERO_OUTCOMES = 15` non-zero outcomes per bucket, its fitted calibrator is applied. Same path as A1/A2.

The `stance_base.py` prior table already has an `"A3"` prefix entry (lines 92–106), used by the `Calibrator`'s cold-start prior lookup. The `Calibrator` extracts the prefix from `advisor_id.split(".")[0]`, so `"A3.news"` → prefix `"A3"` → finds the existing prior table. **No changes to stance_base.py needed.**

---

## 6. Config Flags, Inert-Until-Configured, Fail-Closed

The established pattern for both MiroFish and EDGAR:
- EDGAR: inert when `EDGAR_USER_AGENT` env var is unset (checked in the EDGAR ingest adapter).
- MiroFish: inert when `MIROFISH_ENDPOINT` env var is unset (`_get_endpoint()` returns None in `http_client.py`; `_build_a2_mirofish_fn` returns a noop `_fn` that returns `[]`).

A3 should follow the same pattern. The `_build_a3_news_fn` builder in `engine/advisors.py` should:

```
def _build_a3_news_fn(db_path, pit, clock) -> Callable[[], Opinion | None]:
    # 1. Check for A3_NEWS_ENABLED env var (or A3 source API key)
    if not _a3_is_configured():
        log.info("a3_news.disabled", reason="A3_NEWS_ENABLED not set or no API keys; A3 inert")
        def _noop() -> Opinion | None:
            return None
        return _noop

    # 2. Return the live fn that reads from the news source
    def _fn() -> Opinion | None:
        ...
    return _fn
```

The check (`_a3_is_configured()`) should gate on whichever credential the ingest pipeline requires — e.g., `A3_NEWS_ENABLED=true` combined with the required API keys. If unset, `_noop` returns `None` (abstain). This means:
- A3 appears in `advisor_map` → `run_named_advisors_parallel` calls it.
- It returns `None` → excluded from `valid_opinions` → NOT counted in `live_advisor_count`.
- NOT persisted (None opinions are skipped in `_persist_cycle_opinions`).
- NOT present in `outcomes_by_advisor` → NOT in the ledger update.
- `resolve_weight_bundle` is called with `live_ids` including `"A3.news"`, but since A3 is returning None every cycle and never persisting, the weight stays at EQUAL_FLOOR but the calibrator gets no data. This is safe because a zero-data calibrator stays passthrough-equivalent.

**Alternative (cleaner for counting):** Only add `"A3.news"` to `advisor_map` when configured. This avoids polluting `live_advisor_count` with an advisor that will always abstain and avoids calling its thread each cycle. This is the cleaner approach — match MIROFISH_ENDPOINT's pattern: `_build_a2_mirofish_fn` is always called and returns noop, but it is NOT added to `advisor_map`; it's a separate channel. For A3 in `advisor_map`, the cleanest implementation is: only add the entry to the map if configured.

```python
# In build_engine():
a3_fn = _build_a3_news_fn(config.db_path, pit, clock)
if a3_fn is not None:  # _build_a3 returns None when unconfigured
    advisor_map["A3.news"] = a3_fn
```

This means when A3 is unconfigured, `advisor_map` has 3 entries (A1 only), `live_ids` passed to `resolve_weight_bundle` has 3 entries, and A3 is entirely absent from the cycle. **No crashes, no false weight accumulation, completely inert.**

---

## 7. No-Lookahead / PIT Constraint at the Engine Seam

The PIT contract is maintained by the clock, not by A3 specifically. The key constraint (documented throughout the codebase as R6/D0):

> Outcomes are written AFTER `_build_learning_inputs` in `run_cycle`. The strict `created_at < as_of` cutoff in `load_outcomes_for_learning` ensures the learning step never reads outcomes from the current cycle.

A3 must obey the same constraint its ingest predecessors do:
1. **`as_of` ceiling**: The `_build_a3_news_fn` callable must take `clock.now()` as its information ceiling and must NOT return opinions for articles/posts published after `as_of`. The clock is passed at build time (as with A1/A2).
2. **Source timestamp discipline**: A3's news adapter must filter its results to `source_ts <= as_of` before emitting. This is the same contract as `TipSource.fetch(ticker, as_of)` (see `tips/source.py` line 148: "The adapter MUST NOT return tips with `ts > as_of`").
3. **Backtest safety**: When `BacktestClock` is used, `clock.now()` returns the replay date. A3's adapter must respect this ceiling. If A3 uses a live API, it must either cache historical data or skip in backtest mode (just like MiroFish's `is_backtest` flag in `_build_a2_mirofish_fn`).

No engine-level changes are needed to enforce PIT for A3 — the `clock` injection already provides the ceiling, and the pattern is documented.

---

## 8. Cockpit Activation — How A3 Un-dims

The cockpit `graph.py` already has:
```python
("A3.news", "A3 · News/X (future)", None),
```
with `meta={"future": aid == "A3.news"}` (line 111 of `graph.py`). The `future=True` meta flag is what dims it in the UI.

`state.py`'s `_advisor_intensities()` reads from `trust_weights WHERE is_superseded=0` (line 108). The cold-start fallback loop (line 148) hardcodes only A1+A2 advisor IDs:
```python
for adv_id in ("A1.insider", "A1.congress", "A1.activist", "A2.mirofish"):
```

**When A3 goes live:**

1. **Trust-weights path (primary):** Once A3 emits opinions and accumulates outcomes, `persist_weight_bundle` writes a `trust_weights` row for `"A3.news"`. `_advisor_intensities` reads it without any code changes — the loop is over `rows` from the DB query, not a hardcoded list. A3's weight becomes its intensity, `shadow=False` → `status="active"`.

2. **Cold-start fallback path (secondary):** If trust_weights is empty (early cycles before any update), the cold-start branch reads from `opinions WHERE advisor_id='A3.news'` — also a generic query. So even before graduation, A3 lights up proportionally to its opinion volume. **However**, A3.news is not in the hardcoded fallback dim list, so if it has no opinions yet AND no trust_weights rows, it simply won't appear in `nodes` (no entry vs. dim entry). This is acceptable; the `graph.py` `future=True` flag handles the pre-live visual.

3. **The `future=True` graph flag:** This flag lives in `graph.py` as a build-time constant. It must be flipped to `False` when A3 goes live. This is the **only code change needed in the cockpit** — a one-line edit to `graph.py`. Specifically, change:
   ```python
   meta={"future": aid == "A3.news"}
   ```
   to not mark A3 as future once it's wired.

4. **The data-source node for A3:** `graph.py` has no `src.a3_news` data source node yet. When A3 goes live, a new entry in `_DATA_SOURCES` (e.g., `("src.news", "News/X feeds")`) and a matching entry in `_SOURCE_TO_ADVISOR` would complete the constellation topology. This is cosmetic/informational — the cockpit already draws the `A3.news → core.fusion` edge from the `_ADVISORS` loop (line 127-128 of `graph.py`).

5. **Dynamic edges from A3 opinions:** `_dynamic_flow_edges()` already queries `opinions WHERE advisor_id IS NOT NULL` generically (line 397). A3 opinions with `idea_id IS NOT NULL` will produce `advisor→idea` edges automatically. No changes needed.

**Summary:** A3 going live in the cockpit requires exactly one change: removing the `future=True` flag from `graph.py`. Everything else is data-driven and generic.

---

## 9. Files That Change vs. Files That Don't

### Files that change when A3 is wired

| File | Change |
|------|--------|
| `arbiter/arbiter/engine/advisors.py` | Add `_build_a3_news_fn()` builder (the only new advisor-builder function) |
| `arbiter/arbiter/engine/_engine.py` | In `build_engine()`: call `_build_a3_news_fn`, conditionally add `"A3.news"` to `advisor_map`; import the new builder |
| `cockpit/api/graph.py` | Remove `future=True` meta from A3.news node; optionally add `src.news` data source node and edge |
| `arbiter/arbiter/engine/_engine.py` (docstring) | Update the module docstring that says "A3 absent in MVP" |

### Files that change for idea-horizon assignment

| File | Change |
|------|--------|
| `arbiter/arbiter/engine/_engine.py` (line 499-500) | Extend the horizon branch: currently `180 if sig.source in ("form4", "form13d") else 90`. Add a check for the A3 signal source tag (e.g., `"news"`) → 14 or 30 days. The idea-construction horizon must match the Opinion's `horizon_days` for PIT-clean `(ticker, HorizonBucket)` linking in `_persist_cycle_opinions`. |

### Files that need NO changes

| File | Why it's unchanged |
|------|-------------------|
| `arbiter/arbiter/trust/weight_resolver.py` | Fully generic over `live_ids`; handles new advisor automatically |
| `arbiter/arbiter/trust/store.py` | Generic DB queries keyed by `advisor_id`; works for any string |
| `arbiter/arbiter/trust/ledger.py` | Advisor-agnostic; iterates over whatever advisors are in `outcomes_by_advisor` |
| `arbiter/arbiter/calibration/multi_advisor.py` | Routes by `advisor_id` string; A3 gets the passthrough-equivalent until 15 outcomes |
| `arbiter/arbiter/calibration/stance_base.py` | Already has an `"A3"` prefix entry; `advisor_id.split(".")[0]` → `"A3"` → correct prior |
| `arbiter/arbiter/evaluation/attribution.py` | Reads `opinions` by `idea_id`; generic fan-out, no advisor-specific code |
| `arbiter/arbiter/engine/learning.py` | Passes `live_ids = list(engine.advisor_map.keys())`; A3 auto-included |
| `cockpit/api/state.py` | Generic DB queries; A3 auto-appears in `_advisor_intensities` and `_dynamic_flow_edges` |
| `arbiter/arbiter/calibration/calibrator.py` | Generic per-advisor fitting |
| All DB migration files | The `trust_weights`, `opinions`, `outcomes` tables are already schema-generic |

---

## 10. Summary: What A3 Wiring Requires in One View

```
1. New file: arbiter/adapters/a3_news/  (or similar)
     └── built by the ingest/source agent(s) — not this lane
     └── must emit valid Opinion objects with advisor_id="A3.news"
         and horizon_days matching the SHORT bucket (e.g., 14-30d)

2. Edit: arbiter/arbiter/engine/advisors.py
     └── Add _build_a3_news_fn(db_path, pit, clock) following the A1 builder pattern
         Returns noop (returns None) when A3_NEWS_ENABLED not set + no API keys
         Otherwise returns _fn() -> Opinion | None (shadow=False, floor weight from day 1)

3. Edit: arbiter/arbiter/engine/_engine.py
     ├── In build_engine(): call _build_a3_news_fn; if not None → add to advisor_map
     ├── Extend horizon branch in idea construction to handle the A3 signal source tag
     └── Update the module docstring (remove "A3 absent in MVP" line)

4. Edit: cockpit/api/graph.py
     └── Remove meta={"future": True} from A3.news node when live
     └── Optionally add src.news data-source node + ingest edge

Everything else (learning loop, attribution, calibration, cockpit state)
works without changes by existing generic machinery.
```

---

## 11. Key Design Decisions Recorded

**D1 — advisor_map not separate channel:** A3 is a market-wide sweep (like A1), not idea-specific (unlike A2). It emits at most one `Opinion | None` per cycle. The zero-arg callable contract fits exactly. The A2 synthetic-key trick for replay is not needed.

**D2 — floor-weight, non-shadow from day 1 (Option A):** Matches A1.activist wiring. Probationary at 0.25 (below 0.50 graduated ceiling). Negative-skill suppression path handles the downside. If the operator wants stricter control, add a `A3_PROMOTE=false` flag that seeds an initial `trust_weights` row with `shadow=True` before the first cycle; the resolver will then suppress A3 until that row is manually cleared.

**D3 — inert when unconfigured:** Consistent with EDGAR and MiroFish patterns. `_build_a3_news_fn` returns `None` when unconfigured; `build_engine` only adds A3 to `advisor_map` when the builder returns non-None. No crashes, no count distortion, no false weight accumulation.

**D4 — PIT constraint is clock-injection, not engine-specific:** A3 must obey `as_of = clock.now()` as its information ceiling. For backtest, it must either respect the clock's replay date or short-circuit to None (like MiroFish's `is_backtest` guard). This is a constraint on the A3 adapter, not on the engine.

**D5 — cockpit un-dims automatically:** The only manual step to un-dim A3 in the cockpit is removing the `future=True` flag from `graph.py`. All dynamic state (advisor intensity, flow edges, outcome-teaches edges) is data-driven from the DB and works without code changes.

**D6 — horizon assignment for A3 ideas:** A3 opinion `horizon_days` (e.g., 14d → SHORT bucket) must match the idea's `horizon` used in `make_idea()`. The `_persist_cycle_opinions` linking is by typed `(ticker, HorizonBucket)` equality — a mismatch silently orphans the opinion (no idea_id link), which breaks attribution. The engine's idea-construction block must be extended to assign the correct horizon when the triggering signal has source type "news" (or whichever tag the A3 ingest pipeline uses).
