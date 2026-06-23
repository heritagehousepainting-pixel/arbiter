# Wave 2 Wiring Spec — Build-Ready Consolidation

**Date:** 2026-06-19
**Status:** BUILT. This was the build-ready plan, verified at a post-Wave-1 baseline of 2241 tests green; the four items below are now wired (Wave 2 complete). Current full suite: **2335 tests green** (offline), `EXECUTOR_BACKEND=sim` / not live. The "2241" figure is the pre-build snapshot, not the current count.
**Scope:** Wire three frozen Wave-1 contracts into the live decision path, plus the
notional-vs-realized risk fold. Four work items:

1. **EDGAR-advisor** — `A1.activist` (`source="form13d"`) through detection → emit → engine → runner.
2. **MiroFish-A2-channel** — list-valued advisor pass that bypasses the single-opinion `advisor_map`.
3. **Notional fold** — fold realized (not requested) notional on partial fills.
4. **Runner-ingest** — `_ingest_sc13` pass; inert when `EDGAR_USER_AGENT` unset.

This spec is the mechanical handoff. Every insertion point is cited `file:line` against the
**current** tree. Where a source handoff design does not match current code, it is **FLAGGED**
and the corrected integration point is given.

---

## 0. The one architectural fact the MiroFish handoff got wrong (read first)

The MiroFish spec (§5) says A2 plugs into `orchestrator/cycle.py`'s **per-idea loop** (`cycle.py:247`),
calling `_build_a2_mirofish_fn(...)(idea)` and extending `valid_opinions` there. **That does not match
the real engine path.** Verified:

- `orchestrator/cycle.py::run_cycle` gathers opinions **once per cycle** via
  `run_named_advisors_parallel(advisor_map, …)` (cycle.py:216) — strictly **before** the per-idea loop
  (cycle.py:247). The per-idea loop only **fuses and decides** from an already-gathered, bucket-grouped
  pool (`opinions_by_bucket`, cycle.py:242-245). It never calls advisors per idea.
- The engine **does not let `run_cycle` gather opinions at all.** `Engine.run_cycle`
  (engine.py:1171) pre-gathers `raw_opinions = run_named_advisors_parallel(self.advisor_map, …)`,
  derives `valid_opinions` (engine.py:1172), feeds them to the exit monitor (engine.py:1183) and idea
  creation, persists them (engine.py:1238), then **replays** them into `run_cycle` via
  `_opinion_provider_map` (engine.py:1407-1409, 1425) — zero-arg lambdas returning the cached opinion.
  So `run_cycle`'s own `run_named_advisors_parallel` call re-runs trivial pass-through lambdas, never
  the real A1 fns.

**Consequence:** the correct, lowest-risk MiroFish integration point is a **new pass inside
`Engine.run_cycle`**, AFTER ideas are built (engine.py:1225) and BEFORE `_persist_cycle_opinions`
(engine.py:1238), that calls the per-idea list-valued A2 fn for each idea and **extends
`valid_opinions`**. From there A2 opinions flow automatically into: persistence
(`_persist_cycle_opinions`), the replay map (`_opinion_provider_map` → `run_cycle` → bucket grouping →
fusion), and the exit monitor (if we choose to feed them — see §2.5). **`orchestrator/cycle.py` and
`orchestrator/scheduler.py` need NO change** — their single-opinion `advisor_map` contract is untouched.
This is strictly better than the handoff's plan (which would have required threading `idea` into the
`advisor_map` callable signature, a breaking change to `AdvisorEmitter`).

---

# WORK ITEM 1 — EDGAR advisor `A1.activist` (`source="form13d"`)

The EDGAR ingest lane is **data-producing**: it writes `filings` rows with `source="form13d"`. The
engine reads that table via `detect_signals`. Wiring = a detection branch, an emit branch, an engine
builder fn, and registration. (The ingest entry points `parse_sc13`/`normalize_sc13`/`search_sc13_filings`/
`get_sc13_doc` are owned by the EDGAR lane and assumed delivered per the frozen §5 contract of the
edgar-insider spec.)

## 1.1 `signals/detection.py` — add a `form13d` SELECT + single-filing detector

**Verified current shape.** `detect_signals` (detection.py:137) builds `base_where` (detection.py:175:
`is_superseded = 0 AND is_10b5_1 = 0 AND filing_ts <= ?`), then runs two source-specific SELECTs:
form4 (detection.py:185-190, `AND source = 'form4' AND txn_type = 'P'`) and congress (detection.py:195-200).
`Signal` (detection.py:56-100) carries `source: str` (detection.py:90, currently documented `"form4" | "congress"`)
and is `frozen=True` with a required kw-only `as_of`. `SignalType` (detection.py:34-37) is a str-Enum.

**FLAG — `txn_type` filter:** form4/congress SELECTs hard-filter `txn_type = 'P'`. The activist detector
must NOT inherit that filter blindly: 13D/G exits arrive as `txn_type='S'` (per edgar spec §3.2) and are a
valid bearish signal. Select **both** `P` and `S` for `form13d`.

**Insertions:**

1. Add a member to `SignalType` (detection.py:37):
   ```python
   ACTIVIST_STAKE = "activist_stake"
   ```
2. Widen the `Signal.source` docstring (detection.py:67, :90) to include `"form13d"`. No structural change —
   `source` is already a free `str`.
3. After the congress SELECT (detection.py:200), add a third SELECT. **Note:** `base_where` already excludes
   `is_10b5_1 = 0`; `normalize_sc13` sets `is_10b5_1=False` (edgar spec §3.3) so 13D rows survive that filter:
   ```python
   sc13_sql = (
       "SELECT id, ticker, person_id, filing_ts, txn_type, amount_low, amount_high, raw_json "
       f"FROM filings WHERE {base_where} AND source = 'form13d' AND txn_type IN ('P','S') "
       "ORDER BY filing_ts ASC"
   )
   sc13_rows = conn.execute(sc13_sql, params_base).fetchall()
   ```
   (`raw_json` is added to the column list so the detector can read `schedule`/`percent_of_class`/`is_activist`
   — the form4/congress SELECTs omit it; this one needs it.)
4. Add the sub-detector (mirrors `_detect_single_insider`, detection.py:330 — a single filing is itself a
   signal, no clustering):
   ```python
   def _detect_activist_stake(rows: list, *, as_of: datetime) -> list[Signal]:
       results: list[Signal] = []
       for row in rows:
           ts = _parse_ts(row["filing_ts"])
           if ts > as_of:
               continue  # no look-ahead
           meta = json.loads(row["raw_json"]) if row["raw_json"] else {}
           pct = meta.get("percent_of_class")
           is_activist = bool(meta.get("is_activist", False))
           schedule = meta.get("schedule", "13G")
           # base conviction by schedule; sign carried via txn_type downstream in emit.
           base = 0.70 if is_activist else 0.35
           boost = min((pct or 0.0) / 50.0, 0.30)
           conviction = round(min(base + boost, 1.0), 4)
           results.append(Signal(
               signal_type=SignalType.ACTIVIST_STAKE,
               ticker=row["ticker"],
               source="form13d",
               person_ids=(row["person_id"],),
               filing_ids=(row["id"],),
               window_start=ts,
               window_end=ts,
               conviction_score=conviction,
               meta={"schedule": schedule, "percent_of_class": pct,
                     "is_activist": is_activist, "txn_type": row["txn_type"]},
               as_of=as_of,
           ))
       return results
   ```
5. Call it in `detect_signals` after the congress block (detection.py:240):
   ```python
   signals.extend(_detect_activist_stake(sc13_rows, as_of=as_of))
   ```

**WHY correct:** the conviction math lives in the detector because `Signal.conviction_score` is the single
scalar emit consumes (emit.py:101, :129, :137); putting the §5.3 base/boost there keeps emit's source-branch
thin and matches how `_single_conviction`/`_cluster_conviction` already pre-compute conviction. The `txn_type`
is carried in `meta` so emit can set the stance **sign** without re-querying.

## 1.2 `signals/emit.py` — add the `A1.activist` source branch

**Verified current shape.** `emit_opinion` (emit.py:68) maps source→advisor_id/horizon at emit.py:108-115
(only `congress` vs else-form4). Advisor-id constants at emit.py:60-61. Horizon constants emit.py:46-47.
**Critical:** stance is currently **hard-forced positive** (emit.py:126-130: `raw_stance = max(conviction, 0.1)`),
because the form4/congress detectors only ever emit BUY (`txn_type='P'`). A 13D/G **exit** (`txn_type='S'`)
must produce a **negative** stance — so the activist branch must override the sign.

**Insertions:**

1. Add a constant (emit.py:62):
   ```python
   _ADVISOR_ID_ACTIVIST: str = "A1.activist"
   _HORIZON_DAYS_ACTIVIST: int = 180  # LONG bucket
   ```
2. Extend the source→advisor map (emit.py:109-115):
   ```python
   if signal.source == "congress":
       advisor_id = _ADVISOR_ID_CONGRESS
       horizon_days = _HORIZON_DAYS_CONGRESS
   elif signal.source == "form13d":
       advisor_id = _ADVISOR_ID_ACTIVIST
       horizon_days = _HORIZON_DAYS_ACTIVIST
   else:
       advisor_id = _ADVISOR_ID_FORM4
       horizon_days = _HORIZON_DAYS_FORM4
   ```
3. After computing `stance_score` (emit.py:130), apply the sign for activist exits:
   ```python
   if signal.source == "form13d" and signal.meta.get("txn_type") == "S":
       stance_score = -stance_score
   ```
   `validate_opinion` accepts `[-1.0, 1.0]` (confirmed: opinion.py contract), so a negative stance passes.

**WHY correct:** conviction/boost is already baked into `signal.conviction_score` by the detector (§1.1), so
`confidence` (emit.py:133-141, the `score_bundle is None` cold-start path uses `min(conviction,1.0)`) and
`stance_score` both derive from it with no new math here — the emit branch only sets id/horizon/sign. The
`_MIN_CONVICTION`/`_MIN_COMBINED_SCORE` abstain gates (emit.py:101-105) apply unchanged, satisfying "abstains
correctly" (a `<5%` row is dropped upstream by `normalize_sc13`; a zero-conviction row abstains here).

## 1.3 `engine.py` — `_build_a1_activist_fn` + idea horizon + registration

**Verified current shape.** `_build_a1_insider_fn` (engine.py:135-163) and `_build_a1_congress_fn`
(engine.py:166-192) are zero-arg `() -> Opinion | None` builders: open a fresh conn, `detect_signals`,
filter by `s.source`, take `max(...conviction)`, `score_signal`, `emit_opinion`. The `advisor_map` literal
is engine.py:1636-1639. Idea horizon is chosen at engine.py:1216 (`180 if sig.source == "form4" else 90`) —
**FLAG: this must learn about `form13d`** or every activist idea would get the 90-day MEDIUM horizon, mismatching
the 180-day LONG opinion the emit branch produces, and `_persist_cycle_opinions` (engine.py:1054-1057) would
fail to link the opinion to its idea (typed-bucket mismatch → `idea_id=None`).

**Insertions:**

1. Add the builder after engine.py:192 (byte-for-byte mirror of `_build_a1_congress_fn`, `s.source=="form13d"`):
   ```python
   def _build_a1_activist_fn(db_path: str, pit: PITGateway, clock: Clock) -> Callable[[], Opinion | None]:
       def _fn() -> Opinion | None:
           as_of: datetime = clock.now()
           thread_conn = get_connection(db_path)
           try:
               signals = detect_signals(thread_conn, as_of, cluster_min_people=2)
               activist = [s for s in signals if s.source == "form13d"]
               if not activist:
                   return None
               best = max(activist, key=lambda s: s.conviction_score)
               score_bundle = score_signal(best, as_of)
               return emit_opinion(best, as_of, score_bundle)
           finally:
               thread_conn.close()
       return _fn
   ```
   **VERIFY `score_signal`:** confirm `arbiter/signals/scoring.py::score_signal` does not hard-branch on
   `SignalType`/source in a way that rejects `ACTIVIST_STAKE`. If it keys cold-start accuracy by signal_type,
   a new enum member falls to cold-start — acceptable (matches §3.4 "cold-start priors"). This is the one
   spot to eyeball during build; if `score_signal` raises on an unknown type, pass `None` (emit handles
   `score_bundle=None`).
2. Register `A1.activist` in the `advisor_map` literal (engine.py:1636-1639):
   ```python
   "A1.activist": _build_a1_activist_fn(config.db_path, pit, clock),
   ```
3. Fix idea horizon (engine.py:1216):
   ```python
   horizon = 180 if sig.source in ("form4", "form13d") else 90
   ```

**WHY correct:** `A1.activist` participates in the learning loop automatically — it flows through the same
`run_named_advisors_parallel` (engine.py:1171), `_persist_cycle_opinions`, `resolve_weight_bundle`, and
significance-gated graduation as `A1.insider`/`A1.congress`. No registry call is needed: `default_registry`
(opinion.py:193) is **not consumed** by the engine/fusion path today (no `.register()` call exists in
non-test code — verified), so `hard_weight_cap` for A1.* is moot; weights are resolved purely from the trust
ledger via `resolve_weight_bundle`. (If a future build wires `default_registry`, add
`default_registry.register("A1.activist")` then — out of scope now.)

## 1.4 Offline test plan — EDGAR advisor

**`tests/signals/test_detection.py`** (extend):
- `test_detect_activist_stake_long` — insert a `filings` row `source='form13d'`, `txn_type='P'`,
  `raw_json={"schedule":"13D","percent_of_class":8.5,"is_activist":true}`; assert one `Signal` with
  `signal_type==ACTIVIST_STAKE`, `source=="form13d"`, `conviction_score==round(0.70+min(8.5/50,0.30),4)`.
- `test_detect_activist_exit_sign` — `txn_type='S'`; assert `meta["txn_type"]=="S"`.
- `test_detect_activist_passive_13g` — `is_activist=false`; base conviction 0.35.
- `test_detect_activist_no_lookahead` — `filing_ts > as_of` → no signal.

**`tests/signals/test_emit.py`** (extend):
- `test_emit_activist_long_opinion` — feed an ACTIVIST_STAKE `P` Signal; assert
  `advisor_id=="A1.activist"`, `horizon_days==180`, `stance_score>0`, `confidence_source==MODELED`,
  `validate_opinion` passes.
- `test_emit_activist_exit_negative_stance` — `meta["txn_type"]=="S"`; assert `stance_score<0` and the
  opinion validates (negative stance is in-range).
- `test_emit_activist_abstains_zero_conviction` — conviction `< _MIN_CONVICTION` → `None`.

**`tests/engine/` (or wherever build_engine is tested)** (extend):
- `test_build_engine_registers_activist` — `build_engine(...).advisor_map` contains `"A1.activist"`.
- `test_activist_idea_gets_long_horizon` — a `form13d` signal in the DB yields an idea with
  `horizon_days==180` (so opinion↔idea bucket linkage holds in `_persist_cycle_opinions`).
- End-to-end (mocked PIT + Sim executor): a single `form13d` `P` filing in the DB drives one
  `A1.activist` long opinion into `valid_opinions`; assert it persists with `idea_id` linked.

---

# WORK ITEM 2 — MiroFish A2 list-valued advisor channel

**Frozen entry (delivered by the mirofish lane, verified present):**
`arbiter.adapters.mirofish.adapter.run(idea, as_of, *, conn=None, client=None, breaker=None, is_backtest=False) -> list[Opinion]`
(adapter.py:161-169). `ADVISOR_ID="A2.mirofish"` (adapter.py:51). `_get_endpoint()` lives in
`http_client.py:87` and returns `None` when `MIROFISH_ENDPOINT` is unset. `Idea` (seams.py:210) has
`.ticker`/`.thesis`/`.horizon_days` (seams.py:227-229), satisfying `run`'s duck-type.

## 2.1 Integration point (CORRECTED vs handoff — see §0)

Add a per-idea, list-valued pass **inside `Engine.run_cycle`**, between idea construction (after
engine.py:1225 `if not ideas: return ...`) and `_persist_cycle_opinions` (engine.py:1238). Do **NOT** touch
`orchestrator/cycle.py` or `orchestrator/scheduler.py`.

## 2.2 `engine.py` — `_build_a2_mirofish_fn` builder

Add after `_build_a1_congress_fn` (engine.py:192). It is configured-or-noop at **build time** (per mirofish
spec §3.4): if no endpoint, return a fn that short-circuits to `[]` without touching the network.

```python
def _build_a2_mirofish_fn(
    db_path: str, clock: Clock, breaker: Callable[[], None] | None,
) -> Callable[[Idea], list[Opinion]]:
    from arbiter.adapters.mirofish import adapter as _mf          # noqa: PLC0415
    from arbiter.adapters.mirofish.http_client import _get_endpoint  # noqa: PLC0415
    if _get_endpoint() is None:
        log.info("mirofish.disabled", reason="MIROFISH_ENDPOINT unset; A2 inert")
        def _noop(idea: Idea) -> list[Opinion]:
            return []
        return _noop

    _is_backtest = isinstance(clock, BacktestClock)
    def _fn(idea: Idea) -> list[Opinion]:
        as_of = clock.now()
        thread_conn = get_connection(db_path)
        try:
            return _mf.run(idea, as_of, conn=thread_conn,
                           breaker=breaker, is_backtest=_is_backtest)
        except Exception as exc:  # noqa: BLE001  defense-in-depth; run() already fails closed
            log.warning("mirofish.fn_failed", ticker=idea.ticker, error=str(exc))
            return []
        finally:
            thread_conn.close()
    return _fn
```

**WHY:** mirrors `_build_a1_*_fn`'s fresh-conn-per-call thread-safety, but takes `idea` and returns a list
(mirofish spec §5.1). `is_backtest` is derived from clock type exactly as `build_engine` does at
engine.py:1648 — keeps `run_cache.get` from look-ahead replay in backtests (mirofish spec §5.6).

## 2.3 `engine.py` — wire the builder into `Engine`

1. Add an `Engine` field (after `advisor_map`, engine.py:224):
   ```python
   a2_mirofish_fn: "Callable[[Idea], list[Opinion]] | None" = field(default=None)
   ```
2. In `build_engine`, after the `advisor_map` literal (engine.py:1639), construct it:
   ```python
   a2_mirofish_fn = _build_a2_mirofish_fn(config.db_path, clock, breaker=None)
   ```
   (`breaker=None`: the A2 breaker callback is a `Callable[[],None]`, distinct from the
   `CircuitBreaker` object; pass `None` for now — A2 stays shadow/weight-0 and the live circuit breaker is
   not yet wired to A2. Flag for the live-MiroFish wave.)
3. Pass it to the `Engine(...)` constructor (engine.py:1665-1678): `a2_mirofish_fn=a2_mirofish_fn`.

## 2.4 `engine.py` — the per-idea A2 pass (the actual fusion injection)

Insert between idea construction and persistence (after engine.py:1225-1226, before engine.py:1238):

```python
# A2 (MiroFish) — list-valued, idea-specific advisor. Runs AFTER ideas are built
# (it analyzes a specific idea) and BEFORE persistence/fusion, extending the
# single-opinion A1 pool. No-op when MIROFISH_ENDPOINT unset (builder short-circuits).
if self.a2_mirofish_fn is not None:
    for idea in ideas:
        a2_ops = self.a2_mirofish_fn(idea)   # 0..N opinions, never raises
        for op in a2_ops:
            valid_opinions.append(op)
            live_advisor_count += 1   # A2 opinions count toward the live quorum
```

**WHY correct:** appending to `valid_opinions` (engine.py:1172) is the single choke point that feeds
**all three** downstream consumers with zero further wiring:
- `_persist_cycle_opinions(now, valid_opinions, ideas)` (engine.py:1238) — persists + links each A2 opinion
  to its idea by typed `(ticker, HorizonBucket)` (engine.py:1054-1057). A2's `horizon_bucket` derives from
  `op.horizon_days`, so multi-horizon A2 output links to the right idea/abstains cleanly.
- `_cached_opinions = dict(raw_opinions)` (engine.py:1405) — **FLAG:** this is built from `raw_opinions`
  (the dict), NOT `valid_opinions`, and a dict keyed by `advisor_id` collapses A2's multiple opinions into
  one. So the **A2 opinions must be injected into the cycle's bucket pool by a different route than
  `_cached_opinions`.** Two correct options:

  **(A — recommended)** Change `_opinion_provider_map` so the replay map also yields the A2 list. Since
  `run_cycle`'s `advisor_map` is `{id: ()->Opinion|None}` (single), the cleanest is to **append A2 opinions
  to the per-bucket pool that `run_cycle` fuses**. But `run_cycle` builds `opinions_by_bucket` internally
  from the advisor_map results (cycle.py:222-245) — it does not accept a pre-seeded pool. Therefore:

  **(A, concrete)** Give each A2 opinion its own synthetic key in the replay map so `run_named_advisors_parallel`
  surfaces every one of them. Replace `_opinion_provider_map` (engine.py:1407-1409) to fold `valid_opinions`
  (which now includes A2) rather than `raw_opinions`:
  ```python
  def _opinion_provider_map() -> dict[str, Callable[[], Opinion | None]]:
      m: dict[str, Callable[[], Opinion | None]] = {}
      # A1: one slot per advisor (None when abstained), preserving today's keys.
      for aid, op in raw_opinions.items():
          m[aid] = (lambda op=op: op)
      # A2: one synthetic slot per opinion so a LIST survives the single-opinion map.
      for i, op in enumerate(valid_opinions):
          if op.advisor_id == "A2.mirofish":
              m[f"A2.mirofish#{i}:{op.horizon_bucket.value}"] = (lambda op=op: op)
      return m
  ```
  The synthetic key only affects `run_named_advisors_parallel`'s dict keying (cycle.py:223 iterates
  `.items()` and appends each non-None to `valid_opinions` then groups by bucket) — fusion groups by
  `op.horizon_bucket`, NOT by advisor_id, so multiple A2 opinions in the same bucket fuse together
  correctly, and `op.advisor_id` stays `"A2.mirofish"` for weight resolution. **This is the load-bearing
  detail; get the key uniqueness right (include `i`).**

- The exit monitor (engine.py:1183) — see §2.5.

## 2.5 Exit monitor — decide whether A2 feeds it

The exit monitor (`_run_exit_monitor(now, valid_opinions)`, engine.py:1183) runs **before** the A2 pass
(engine.py:1183 vs the new pass after engine.py:1225). It computes a per-ticker mean signed stance for the
conviction-reversal trigger (engine.py:570-580). **Recommendation: leave the exit monitor on A1-only this
wave** (A2 is shadow/weight-0; letting an unproven A2 trigger live sells is risky). The A2 pass lands AFTER
the exit monitor, so this is the default — no action needed. Document it: "A2 does not yet inform exits
(shadow)." If a later wave wants A2 in reversals, move the A2 pass above engine.py:1183.

## 2.6 Offline test plan — MiroFish A2

**`tests/engine/test_mirofish_wiring.py`** (new; mock `adapter.run` — never hit the network):
- `test_a2_disabled_noop_when_unset` — `monkeypatch.delenv("MIROFISH_ENDPOINT")`; `build_engine(...)`;
  assert `a2_mirofish_fn(idea) == []`, logs `mirofish.disabled` once, and a full `run_cycle` produces the
  SAME orders as A1-only (A2 contributes nothing). Patch `httpx.post` to fail the test if called.
- `test_a2_list_flows_into_fusion` — monkeypatch `MIROFISH_ENDPOINT`; patch `adapter.run` to return TWO
  opinions (`A2.mirofish`, stances +0.6 SHORT-bucket and +0.4 LONG-bucket, shared `run_group_id`); run a
  cycle and assert **both** reach `_persist_cycle_opinions` (query the opinions table for 2 `A2.mirofish`
  rows) and both appear in the fused bucket pools — i.e. the LIST survived the single-opinion map (proves
  §2.4-A synthetic-key fix).
- `test_a2_does_not_disturb_a1` — with A2 returning `[]` for one idea but opinions for another, assert the
  A1 single-opinion slots are byte-for-byte unchanged (same `raw_opinions` dict keys/values).
- `test_a2_negative_stance_passthrough` — `adapter.run` returns `stance_score=-0.7`; assert the persisted
  A2 opinion keeps `-0.7` (no clamp) — pins the bearish path end-to-end through the engine.
- `test_a2_backtest_flag` — with a `BacktestClock`, assert `adapter.run` is called with `is_backtest=True`.
- `test_scheduler_unchanged` — assert `run_named_advisors_parallel`/`AdvisorEmitter` signatures are untouched
  (a static import-and-signature check; A2 added zero params to the single-opinion path).

---

# WORK ITEM 3 — Notional-vs-realized fold (LIVE RISK-ACCOUNTING CHANGE)

## 3.1 Verified current shape + the units trap

- `engine.py:1392-1393`: `if sub_result.order_id is not None: _book[0] = _book[0].add(order.ticker, float(order.qty))`.
  `order.qty` is the **requested notional USD** (confirmed by submit.py:251 `notional = float(order.qty)` and the
  comment engine.py:1390-1391). The `RiskBook` tracks notional USD (`as_decide_kwargs`, engine.py:1311).
- `SubmitResult` (submit.py:52-87) exposes `order_id`, `status`, `duplicate`, `zero_share`, `avg_fill_price` —
  **but NOT `filled_qty`.** `filled_qty` exists only locally inside `submit_order` as `report.filled_qty`
  (broker `ExecutionReport.filled_qty`, confirmed executor.py:40 `filled_qty: float`), used at submit.py:350
  for the partial-fill ledger qty and surfaced in audit (submit.py:383).
- **UNITS TRAP (FLAG):** `report.filled_qty` is in **SHARES**; `order.qty` and the `RiskBook` are in
  **notional USD**. The p2-refinements spec §2 says "fold realized notional = `avg_fill_price × filled_qty`."
  So we must surface a **realized-notional** number, not raw `filled_qty`. Do NOT fold `filled_qty` directly —
  that would corrupt the book with a share count where dollars are expected.

## 3.2 `execution/submit.py` — add `filled_notional` to `SubmitResult`

Add a field that means "realized notional USD for this submit" — computed once, where both `filled_qty` and
`avg_fill_price` are in scope (submit.py:344-402):

1. Add to the dataclass (after submit.py:82 `avg_fill_price`):
   ```python
   filled_notional: float | None = None
   ```
   With docstring: "Realized notional USD = avg_fill_price × filled_qty for a placed order (None when
   nothing placed or no fill price). On a full fill this equals the requested notional; on a partial it is
   smaller. Callers fold THIS into the risk book, not the requested order.qty."
2. Populate it in the success return (submit.py:398-402). Compute defensively (both may be None on an
   accepted-but-unfilled `pending`):
   ```python
   _filled_notional = (
       float(report.avg_fill_price) * float(report.filled_qty)
       if report.avg_fill_price is not None and report.filled_qty
       else None
   )
   return SubmitResult(
       order_id=order.order_id,
       status=status,
       avg_fill_price=report.avg_fill_price,
       filled_notional=_filled_notional,
   )
   ```

**WHY `filled_notional` not `filled_qty`:** the consumer (the book) speaks notional USD; surfacing the
already-multiplied notional keeps the units honest at the seam and means the engine fold is a one-liner with
no price re-lookup. (Surfacing raw `filled_qty` would force the engine to re-derive `avg_fill_price`, which
it does not have post-submit.)

## 3.3 `engine.py:1392-1393` — fold realized on partial, requested otherwise

```python
if sub_result.order_id is not None:
    # Fold REALIZED notional on a partial fill (book must not over-count headroom
    # consumed by the unfilled remainder); requested notional on a full/pending fill.
    if sub_result.status == "partial" and sub_result.filled_notional is not None:
        _book[0] = _book[0].add(order.ticker, sub_result.filled_notional)
    else:
        _book[0] = _book[0].add(order.ticker, float(order.qty))
```

**WHY the `partial`-only guard:** matches submit.py:350's existing partial-handling (`status == "partial"`).
On `filled`, `filled_notional ≈ requested order.qty` anyway, but the requested value is the exact intended
exposure and avoids any float drift — keep requested for full fills. On `pending` (accepted-unfilled,
Alpaca), nothing has actually filled yet; **FLAG: today's code folds full requested notional on `pending`**
(the `order_id is not None` branch fires for `pending`). This spec preserves that conservative behavior
(pending → requested), because the next cycle's reconciliation promotes/cancels and the book is re-seeded
from held positions each cycle (`_seed_risk_book`, engine.py:1291). Changing the pending path is out of scope.

## 3.4 Offline test plan — notional fold

**`tests/execution/test_submit.py`** (extend):
- `test_submit_result_filled_notional_on_full` — SimExecutor full fill; assert
  `result.filled_notional == pytest.approx(avg_fill_price * filled_qty)`.
- `test_submit_result_filled_notional_partial` — inject an executor report with `status="partial"`,
  `filled_qty < requested shares`; assert `filled_notional` reflects the **partial** shares (< requested
  notional).
- `test_submit_result_filled_notional_none_when_skipped` — duplicate/zero-share → `filled_notional is None`.

**`tests/engine/test_risk_fold.py`** (new; SimExecutor + a stub executor that returns a partial report):
- `test_fold_uses_filled_notional_on_partial` — drive one decide→submit where the broker partial-fills 40%;
  assert the post-submit `RiskBook` gross reflects ~0.4× requested notional, NOT full requested.
- `test_fold_uses_requested_on_full_fill` — full fill; book reflects requested notional (unchanged behavior).
- `test_fold_regression_two_orders` — partial fill on order 1 leaves MORE headroom for order 2 than the old
  full-notional fold did (the behavior-change is observable: caps bind later).

## 3.5 SHIP-OR-GATE recommendation

**Ship it, but it is genuinely behavior-changing on the live risk path** — it *loosens* caps relative to
today (folds less consumed headroom on partials). Today's over-count is the **conservative** direction
(tighter caps). The fix is correct, but it means a partial fill now lets a *second* order through that the
old code would have blocked. **Recommendation: SHIP in this wave** because (a) `EXECUTOR_BACKEND=sim` today
never produces `partial` (Sim fills synchronously/fully — submit.py comment line 1400: "place always returns
filled"), so the change is **inert under the current sim executor** and only activates under live Alpaca
partial fills; and (b) it is fully covered by the §3.4 tests with an injected partial report. **Gate caveat:**
because it is dormant under sim, it cannot be validated against a real partial until Alpaca is live — flag in
`SETUP_NEEDED.md` that the partial-fold path is offline-tested but not live-verified. If the reviewer wants
zero live-risk delta this wave, gate behind a config flag (`config.fold_realized_on_partial`, default
False) — but given it is sim-inert, an unconditional ship is acceptable and simpler.

---

# WORK ITEM 4 — Runner `_ingest_sc13` pass

## 4.1 Verified current shape

`run_ingest` (runner.py:96) defaults `sources=("form4","congress")` (runner.py:101), dispatching at
runner.py:148-159. `_ingest_form4` (runner.py:184) guards the UA at runner.py:197-205 (empty `edgar_user_agent`
→ warn + note + return, **inert, not crash**), constructs `EdgarClient(config=config)` (runner.py:210), and
per-ticker calls `search_form4_filings → get_form4_xml → parse_form4 → normalize → resolve_person →
write_filing` (runner.py:232-313). Imports at runner.py:29-33.

## 4.2 Insertions

1. Imports (after runner.py:31):
   ```python
   from arbiter.ingest.edgar import parse_sc13, normalize_sc13  # NEW (frozen edgar §5.2)
   ```
   (or `from arbiter.ingest.edgar.sc13_parser import parse_sc13` etc., matching whatever the edgar lane
   actually exports — verify against the delivered `__init__.py`.)
2. Default sources (runner.py:101): `sources: Sequence[str] = ("form4", "form13d", "congress")`.
3. Dispatch (after runner.py:149, the form4 block):
   ```python
   if "form13d" in sources_tuple:
       _ingest_sc13(config, conn=conn, clock=clock, summary=summary, tickers=tickers)
   ```
4. Add `_ingest_sc13` mirroring `_ingest_form4`/`_ingest_form4_ticker` (runner.py:184-313), with the **same UA
   guard** (the inert-when-no-UA behavior is mandatory):
   ```python
   def _ingest_sc13(config, *, conn, clock, summary, tickers) -> None:
       src = SourceSummary()
       summary.per_source["form13d"] = src
       if not config.edgar_user_agent or not config.edgar_user_agent.strip():
           msg = "form13d skipped: Config.edgar_user_agent is empty."
           log.warning("run_ingest.form13d_skipped_no_user_agent")
           summary.notes.append(msg); src.errors.append(msg)
           return
       watchlist = tickers if tickers else list(_DEFAULT_WATCHLIST)
       try:
           client = EdgarClient(config=config)
       except Exception as exc:
           src.errors.append(f"form13d: failed to create EdgarClient: {exc}")
           log.error("run_ingest.form13d_client_error", error=str(exc)); return
       try:
           for ticker in watchlist:
               _ingest_sc13_ticker(ticker, client, conn, clock, src)
       finally:
           client.close()

   def _ingest_sc13_ticker(ticker, client, conn, clock, src) -> None:
       try:
           refs = client.search_sc13_filings(ticker)
       except Exception as exc:
           src.errors.append(f"form13d/{ticker}: search failed: {exc}"); return
       for ref in refs:
           accession = ref.get("accession", ""); cik = ref.get("cik", "")
           schedule = ref.get("schedule", "13G")
           if not accession or not cik:
               src.n_skipped += 1; continue
           src.n_fetched += 1
           try:
               doc = client.get_sc13_doc(accession, cik, primary_document=ref.get("primary_document"))
               parsed = parse_sc13(doc, ticker, accession, schedule=schedule)
               raws = normalize_sc13(parsed)
               for raw in raws:
                   try:
                       hints = {"person_id": raw["person_id"]} if raw.get("person_id") else {}
                       person_id = resolve_person(raw["person_name"], "form13d", hints, conn, clock)
                       raw = dict(raw); raw["person_id"] = person_id
                       before = _count_filings(conn); fid = write_filing(conn, raw, clock); after = _count_filings(conn)
                       if fid is None: src.n_skipped += 1
                       elif after > before: src.n_written += 1
                       else: src.n_skipped += 1
                   except Exception as exc:
                       src.errors.append(f"form13d/{ticker}/{accession}: write error: {exc}"); src.n_skipped += 1
           except Exception as exc:
               src.errors.append(f"form13d/{ticker}/{accession}: fetch/parse error: {exc}"); src.n_skipped += 1
   ```

**FLAG — `resolve_person` source arg:** `resolve_person(name, "form4", …)`/`("congress", …)` takes a
source-kind string (runner.py:270, :382). Passing `"form13d"` is a NEW kind — **verify
`arbiter/ingest/identity/resolver.py` accepts an arbitrary source string** (it likely does — it is just an
identity namespace), else add a `"form13d"` branch there. If resolver hard-validates the source enum, that is
a one-line addition in the identity lane (out of this set — flag it).

## 4.3 Offline test plan — runner ingest

**`tests/ingest/test_runner.py`** (extend; mock the `EdgarClient` methods, no network):
- `test_form13d_inert_when_no_user_agent` — `config.edgar_user_agent=""`, `sources=("form13d",)`; assert
  `run_ingest` returns with `per_source["form13d"]` skipped, a note added, **no crash**, and Congress (if also
  requested) still runs. (Mirrors the existing form4 inert test.)
- `test_form13d_ingests_rows` — mock `search_sc13_filings`→one ref, `get_sc13_doc`→fixture XML,
  `parse_sc13`/`normalize_sc13`→one `form13d` RawFiling; assert one `filings` row written with
  `source='form13d'`.
- `test_default_sources_includes_form13d` — assert `run_ingest`'s default `sources` triggers a `form13d`
  per-source summary.
- `test_form13d_fault_isolated` — a parse exception on one ticker increments `n_skipped` and the loop
  continues to the next ticker.

---

# FROZEN PUBLIC INTERFACE (the subsequent refactor agent MUST preserve byte-for-byte in behavior)

The engine.py refactor (deferred H1) must keep these stable:

1. **`build_engine` signature** — `build_engine(config=None, *, conn=None, pit=None, clock=None,
   kill_switch=None, alerting=None) -> Engine` (engine.py:1542). Adding A2 wiring must NOT add a required
   param.
2. **`Engine.advisor_map` keys** — `{"A1.insider", "A1.congress", "A1.activist"}` after this wave. These are
   the single-opinion advisor IDs; `run_named_advisors_parallel`/`AdvisorEmitter` (`() -> Opinion | None`,
   cycle.py:45, scheduler.py:93) stay single-opinion. **A2 is NOT in `advisor_map`** — it is a separate
   `Engine.a2_mirofish_fn: Callable[[Idea], list[Opinion]] | None` field.
3. **`Opinion.advisor_id == "A2.mirofish"`** for ALL MiroFish opinions (no per-horizon sub-ids); distinguished
   by `horizon_bucket` + `source_fingerprint`, sharing one `run_group_id`. Registered conceptually at
   `hard_weight_cap=0.35` (mirofish §5.3) — though `default_registry` is presently unconsumed.
4. **`CycleResult`** (cycle.py:61-86) fields unchanged. A2 opinions are counted in `opinions_gathered` via the
   replay map; `run_cycle`'s public signature (cycle.py:93-106) is untouched.
5. **`run_cycle` / `run_named_advisors_parallel` / `run_advisors_parallel`** signatures (cycle.py:93,
   scheduler.py:28, :93) — untouched. The list-valued A2 path lives entirely in `engine.py`.
6. **`SubmitResult`** (submit.py:52) — additive only: new `filled_notional: float | None = None`. All existing
   fields/`.filled` property unchanged. `submit_order` signature (submit.py:134) unchanged.
7. **`detect_signals`** signature (detection.py:137) unchanged; `Signal` gains no new field (uses `meta`).
8. **`emit_opinion`** signature (emit.py:68) unchanged.
9. **`run_ingest`** signature (runner.py:96) — additive only: default `sources` tuple gains `"form13d"`;
   no positional/required-param change.
10. **`filings.source` strings** — `"form4"`, `"congress"`, `"form13d"` (no migration; TEXT unconstrained per
    edgar §0). **INTERFACES.md** must be updated to list `A1.activist` and `A2.mirofish` as live advisor IDs
    and the `form13d` source — a doc edit, behavior-frozen.

---

# OWNERSHIP LIST (files the Wave-2 build will touch)

**Production:**
- `arbiter/signals/detection.py` — `ACTIVIST_STAKE` enum, `form13d` SELECT, `_detect_activist_stake`.
- `arbiter/signals/emit.py` — `A1.activist` source branch + negative-stance-on-exit.
- `arbiter/engine.py` — `_build_a1_activist_fn`, `_build_a2_mirofish_fn`, `Engine.a2_mirofish_fn` field,
  `advisor_map` literal, idea-horizon `form13d` fix, A2 per-idea pass, `_opinion_provider_map` A2-fold,
  notional fold at 1392-1393.
- `arbiter/execution/submit.py` — `SubmitResult.filled_notional` + populate.
- `arbiter/ingest/runner.py` — `_ingest_sc13` / `_ingest_sc13_ticker`, default `sources`, imports.

**Tests:**
- `tests/signals/test_detection.py`, `tests/signals/test_emit.py`
- `tests/engine/test_mirofish_wiring.py` (new), `tests/engine/test_risk_fold.py` (new),
  engine/build_engine tests (activist registration + horizon)
- `tests/execution/test_submit.py`
- `tests/ingest/test_runner.py`

**Docs:** `INTERFACES.md` (advisor-id + source list), `SETUP_NEEDED.md` (A2 still endpoint-blocked; partial-fold
offline-tested-not-live-verified).

## Touches that would force work OUTSIDE this set — FLAG these to the user before building

- **`arbiter/ingest/edgar/**`** — the entire EDGAR lane (`parse_sc13`, `normalize_sc13`, `search_sc13_filings`,
  `get_sc13_doc`, `__init__.py` exports) is assumed **already delivered** by the Wave-1 EDGAR lane per its
  frozen §5 contract. If those symbols are NOT yet in the tree, the runner/engine wiring cannot import them —
  **this is a prerequisite, not part of this wave.** VERIFY they exist before starting Work Items 1 and 4.
- **`arbiter/ingest/identity/resolver.py`** — only if `resolve_person` rejects the new `"form13d"` source
  kind (§4.2 flag). Likely a no-op, but confirm; if it validates source against an enum, that is a one-line
  edit in the identity lane.
- **`arbiter/signals/scoring.py`** — only if `score_signal` raises on the `ACTIVIST_STAKE` enum (§1.3 flag).
  Expected to fall through to cold-start; verify, else pass `score_bundle=None`.
- **`arbiter/contract/opinion.py` / `default_registry`** — NOT touched this wave (registry is unconsumed).
  Flag for a future wave if hard_weight_cap enforcement is wired.

---

# Summary of mismatches found (handoff designs vs current code)

1. **MiroFish §5 integration point is WRONG.** It targets `cycle.py`'s per-idea loop, but the engine
   pre-gathers ALL opinions in `Engine.run_cycle` (engine.py:1171) and replays them; `cycle.py` never calls
   advisors per idea. Corrected: a new per-idea A2 pass in `Engine.run_cycle` (after engine.py:1225),
   extending `valid_opinions`, with `cycle.py`/`scheduler.py` UNCHANGED.
2. **MiroFish list collapses in `_opinion_provider_map`.** It rebuilds from `raw_opinions` (a dict) which can
   hold only one opinion per advisor_id. Corrected: fold `valid_opinions` with synthetic per-opinion keys so
   the LIST survives (§2.4-A). Load-bearing.
3. **Notional fold UNITS trap.** `report.filled_qty` is shares; the book is notional USD. p2 spec's "fold
   realized notional" must surface `avg_fill_price × filled_qty` as a new `filled_notional` field — NOT raw
   `filled_qty`. (§3.1–3.2)
4. **Idea-horizon omission.** The activist opinion is 180-day LONG, but idea creation (engine.py:1216) maps
   only `form4→180` else 90 — without a `form13d→180` fix, activist opinions wouldn't link to their ideas.
   Added to Work Item 1 (§1.3).
5. **Stance-sign hard-positive.** emit.py:126-130 forces stance ≥ 0.1 (A1 detectors only BUY). A 13D/G exit
   (`txn_type='S'`) needs a negative stance — the activist emit branch must override the sign (§1.2).
6. **`txn_type` filter.** The activist SELECT must include `'S'` (exits), unlike the `P`-only form4/congress
   SELECTs (§1.1).
7. **Notional fold is sim-inert.** SimExecutor never returns `partial`, so the live behavior change only
   activates under real Alpaca partials — recommend unconditional ship, flag as offline-tested-not-live
   (§3.5).
