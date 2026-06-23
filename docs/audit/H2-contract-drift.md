# H2 — INTERFACES Contract Drift & DI Discipline

**Auditor lane:** H2 (READ-ONLY)
**Date:** 2026-06-19
**Target:** `/Users/jonathanmorris/poly_bot/arbiter/INTERFACES.md` ("the frozen contract bible") cross-checked against the live code after 5 sub-projects + Phase-2.
**Scope:** contract-name fidelity, owning-module accuracy, the §11.2 insert-only rule vs the new in-place UPDATE carve-outs, Config field list (§10b.5), Executor ABC, §9 default-executor note, dedup/idempotency, outcome/opinion schemas. Engine internals/size are H1's lane.

---

## VERDICT

**INTERFACES.md is PARTIALLY TRUSTWORTHY — the *type shapes* are reliable, the *prose around them is not.**

The actual frozen dataclasses (Opinion, FusionOutput, AdvisorWeight, WeightBundle, ResolvedOutcome, Idea, TradingDecision, PaperOrder, SubmitResult, the Executor ABC, Config) almost all match the documented field lists — an agent that reads a struct definition will build correctly. **But the document is wrong about three structural facts that an agent trusts blindly:** (1) where these types live (every §4–§9 "OWNED BY" path is stale — they were consolidated into the *undocumented* `arbiter/contract/seams.py`, and `arbiter/fusion/output.py` does not even exist); (2) the package layout (§ header claims a nested `arbiter/arbiter/` that does not exist); and (3) the cardinal §10/§11.2 invariant "the ONLY in-place UPDATE is `supersede_row`," which is now violated by **six** distinct code paths that INTERFACES never reconciles in §10/§11. Plus two frozen schemas drifted silently (ResolvedOutcome grew a `stance_score` field; Config grew `trust_equal_floor`) and two docstrings still claim the dead `LIVE_TRADING`-selects-executor behavior. The bible needs a reconciliation pass before it can be cited as ground truth.

---

## FINDINGS

### P0 — §11.2 "ONLY in-place UPDATE is supersede_row" is now violated by six paths, never reconciled in §10/§11 — `INTERFACES.md §10/§11.2` vs many files
INTERFACES §10 states flatly: *"**Insert-only.** The ONLY in-place UPDATE is the `is_superseded` flag flip inside `supersede_row()`."* §11.2 repeats *"Insert-only; corrections via `supersede_row`."* The code now issues in-place UPDATE / upsert in at least **six** other places:
- `arbiter/execution/position_store.py:66,76` — `DELETE FROM sim_positions` + wipe-and-rewrite, and `INSERT ... ON CONFLICT(id) DO UPDATE` on `sim_account` (Phase-2 WP-B).
- `arbiter/engine.py:258` — `engine_state` upsert (`ON CONFLICT(id) DO UPDATE`, amendment C4 durable pause).
- `arbiter/engine.py:336,417` — `UPDATE orders SET status = ? WHERE order_id = ?` (fill reconciliation).
- `arbiter/engine.py:1104` — `UPDATE orders SET idea_id = ?` (B5 idea linkage).
- `arbiter/orchestrator/idea_store.py:169` — `UPDATE ideas SET state = ?, updated_state_at = ?` (FSM in-place transition).
- `arbiter/safety/breakers.py:126` — `breaker_state` `ON CONFLICT(breaker_name) DO UPDATE`.

Each carve-out is locally documented in its own module docstring as "a documented §11.2 carve-out," but **INTERFACES.md §10/§11.2 itself was never amended** — it still asserts the absolute "ONLY". §10 mentions only the `orders` table's lack of `supersedes_id` (in-memory exit correction); it does not acknowledge `sim_positions`/`sim_account`, `engine_state`, `ideas.state`, `orders.status/idea_id`, or `breaker_state`. **This is the single most dangerous drift:** the document's headline invariant is false, and a new agent told "insert-only, the only UPDATE is supersede" will either (a) be confused by the existing code or (b) "fix" a legitimate carve-out back to insert-only and break runtime-state persistence.
**Action:** Amend §10/§11.2 to a two-tier rule: *fact tables* (`opinions`, `filings`, `ideas`*content*, `orders`*content*, `outcomes`, `trust_weights`, `advisor_registry`, `audit`) are insert-only/supersede-only; *runtime-state tables* (`sim_positions`, `sim_account`, `engine_state`, `breaker_state`, plus the `ideas.state` lifecycle column and `orders.status/idea_id` status columns) are explicitly mutable. List the carve-outs by table+column so the boundary is auditable, not folklore.

### P1 — Every §4–§9 "OWNED BY" module path is stale; the real home `arbiter/contract/seams.py` is undocumented — `INTERFACES.md §4,§5,§6,§7` vs `arbiter/contract/seams.py`
INTERFACES names a distinct owning module for each cross-lane type, but they were consolidated into one file the document never mentions:
- §4 FusionOutput "OWNED BY `arbiter/fusion/output.py`" — **that file does not exist**; FusionOutput is defined in `arbiter/contract/seams.py`.
- §5 AdvisorWeight/WeightBundle "OWNED BY `arbiter/trust/ledger.py`" — `ledger.py:48` *imports them from* `arbiter.contract.seams`; it does not own them.
- §6 ResolvedOutcome "OWNED BY `arbiter/evaluation/outcome_labeler.py`" — defined in `seams.py:166`; the labeler only constructs instances.
- §7 Idea "OWNED BY `arbiter/orchestrator/idea.py`" — `idea.py:14` re-imports `Idea` from `arbiter.contract.seams`.

`arbiter/contract/seams.py` is the de-facto single source of truth for FusionOutput, AdvisorWeight, WeightBundle, ResolvedOutcome, Idea, TradingDecision, and PaperOrder — and INTERFACES.md does not reference it once. An agent told "redefine the FusionOutput in fusion/output.py" would create a duplicate type and a real import-divergence bug — the exact "do not redefine these names elsewhere" failure the bible exists to prevent.
**Action:** Add a sentence to the top of §2 / each of §4–§9: "all cross-lane contract dataclasses now live in `arbiter/contract/seams.py`; the lane module re-exports them." Delete the reference to the non-existent `arbiter/fusion/output.py`.

### P1 — ResolvedOutcome frozen schema drifted: undocumented `stance_score` field — `arbiter/contract/seams.py:184` / `INTERFACES.md §6`
§6 documents ResolvedOutcome's fields ending at `label_kind` (idea_id, advisor_id, ticker, alpha_bps, binary, advisor_confidence, abstained, horizon_days, label_kind). The live dataclass inserts a 7th field `stance_score: float` (seams.py:184, "advisor's ACTUAL directional forecast; Brier forecast, sub-project #5a") between `advisor_confidence` and `abstained`. This is a frozen-schema change to a positionally-constructed dataclass that §6 does not show. Any agent constructing a `ResolvedOutcome` positionally from the §6 field order will mis-assign arguments.
**Action:** Add `stance_score: float` to §6 with its (#5a) provenance note.

### P1 — Config `trust_equal_floor` field absent from the "exact" §10b.5 field list — `arbiter/config.py:134` / `INTERFACES.md §10b.5`
§10b.5 prefixes its Config field list with "(exact)". The live `Config` dataclass adds `trust_equal_floor: float = 0.25` (config.py:134) and the strict TOML parser whitelists it in `_KNOWN_KEYS["core"]` (config.py:51) with env override `ARBITER_TRUST_EQUAL_FLOOR` (config.py:261). It is a sub-project-#4 (D3) addition and is documented in *code*, but the "exact" §10b.5 list and the §4 calibrator-seam paragraph never mention it. A reader trusting "exact" will believe the field set is closed when it is not.
**Action:** Add `trust_equal_floor` to §10b.5 (with `[core]` section + `ARBITER_TRUST_EQUAL_FLOOR` override), and the `trust_equal_floor < 0.50` ceiling-coupling rationale.

### P1 — Stale docstrings claim dead `LIVE_TRADING`-selects-executor behavior, contradicting §9 and the code below them — `arbiter/engine.py:9-11`, `arbiter/execution/alpaca_adapter.py:3` / `INTERFACES.md §9`
§9 correctly states executor selection is by `executor_backend` (sim → SimExecutor; alpaca_paper + keys → AlpacaAdapter) and "LIVE_TRADING stays false." The authoritative selector `build_executor` (alpaca_adapter.py:366-399) matches §9 exactly and even comments "`live_trading` is NOT consulted here." But two module docstrings still describe the *old* behavior:
- `engine.py:9-11`: *"The `SimExecutor` is always used unless LIVE_TRADING=true (which is never set...)"* — false; AlpacaAdapter is selected on `executor_backend`, not LIVE_TRADING.
- `alpaca_adapter.py:3`: *"Selected ONLY when `LIVE_TRADING=true`..."* — directly contradicts the `build_executor` function 360 lines below it in the same file.
These are exactly the "stale docstrings claiming old behavior (LIVE_TRADING selection)" the lane brief flagged. They are doc-only (the code is correct) but actively mislead.
**Action:** Rewrite both docstrings to "executor is selected by `config.executor_backend` (sim | alpaca_paper); `live_trading` is reserved for a future real-money path and does not select the executor."

### P2 — Package-layout claim is self-contradictory and wrong — `INTERFACES.md` line 7 / repo root
Header line 7: *"Package layout is **flat**: importable package is `arbiter.arbiter` living at `arbiter/arbiter/`."* There is **no `arbiter/arbiter/` directory**; the package is flat at `arbiter/` (e.g. `arbiter/config.py`, `arbiter/types.py`). Line 8's own import example `from arbiter.types import HorizonBucket` matches the *actual* flat layout, so the sentence contradicts itself. Harmless to running code but misleads any agent reasoning about import roots or sys.path.
**Action:** Fix to "importable package is `arbiter` living at `arbiter/`."

### P2 — §10b.5 env-override rule "`ARBITER_<FIELD_UPPER>`" is overbroad; many fields use bare names — `arbiter/config.py:240-253` / `INTERFACES.md §10b.5`
§10b.5 says "Env overrides: `ARBITER_<FIELD_UPPER>` (Alpaca keys + `LIVE_TRADING` + `EXECUTOR_BACKEND` use their bare names)." In reality the bare-name set is larger than the three exceptions listed: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER_BASE_URL`, `ALPACA_DATA_BASE_URL`, `ALPACA_TIMEOUT`, `EDGAR_USER_AGENT`, `KILL_SWITCH_URL`, `ALERT_WEBHOOK_URL` all use bare (non-`ARBITER_`-prefixed) env names (config.py:240-253), while storage/sizing/daemon fields use the `ARBITER_` prefix. The stated rule + its 3-item exception list is incomplete.
**Action:** Restate as: "`ARBITER_<FIELD_UPPER>` for storage/sizing/daemon/trust fields; bare names for `LIVE_TRADING`, `EXECUTOR_BACKEND`, all `ALPACA_*`, `EDGAR_USER_AGENT`, `KILL_SWITCH_URL`, `ALERT_WEBHOOK_URL`."

### P3 — Post-Phase-2 migrations 025/026 (and several core ones) undocumented in §10 table list — `arbiter/db/migrations/` / `INTERFACES.md §10`
§10 lists the core tables and explicitly calls out `023_orders_idea_id.sql` and `024_engine_state.sql`. Two newer fragments are unmentioned: `025_trust_weights_cap_reason.sql` and `026_outcome_stance_attribution.sql` (the latter is the schema side of the P1 `stance_score` drift). §10 also never lists `022_positions.sql`'s `sim_positions`/`sim_account` tables in its "core tables" enumeration even though §10b.5/position_store treat them as authoritative status source. Low risk (migrations self-apply in order) but the §10 inventory is no longer a complete map.
**Action:** Add `sim_positions`, `sim_account`, `engine_state` to the §10 table inventory and note 025/026 alongside 023/024.

### P3 — DI discipline holds; the one acknowledged ABC gap is honestly documented — `arbiter/execution/idempotency.py:73-98`
DI is clean: `build_engine`/`build_executor` are the sole composition root, lanes never import each other's implementations (verified: `trust/ledger.py`, `orchestrator/idea.py` import *types* from `contract/seams`, not behavior). The Executor ABC (`shared/executor.py:77-103`) matches §10b.2 exactly (`place`/`cancel`/`get_positions`/`get_account`). One soft spot: idempotency's broker-side dedup check uses `get_positions()` as a proxy because the ABC has no `get_open_orders()` (idempotency.py:73-79, self-documented). The `CurrentPriceProvider` PIT-purity seam (§10b.5) is correctly injected as `Null...` under BacktestClock or non-alpaca backends (engine.py:1365-1368) — matches the documented C0 amendment. No silent DI violation found.
**Action:** None required; optionally note the `get_open_orders` ABC gap in §10b.2 so the positions-as-proxy idempotency check isn't mistaken for a bug.

---

## OPPORTUNITIES TO ADD

1. **Document `arbiter/contract/seams.py` as the canonical contract home in §2.** It is the actual single source of truth for 7 cross-lane dataclasses and is currently invisible to the bible. This is the highest-leverage fix — it collapses four stale "OWNED BY" paths into one true statement.
2. **Promote the §11.2 invariant to a two-tier table-classification matrix** (fact-tables = insert-only/supersede; runtime-state-tables = mutable, enumerated). Folklore carve-outs scattered across six module docstrings should be one authoritative list in §10 so the CI grep for "no UPDATE" can be scoped correctly instead of producing false positives/negatives.
3. **Add a "drift ledger" / amendment-changelog section to INTERFACES.md.** Five sub-projects + Phase-2 each amended frozen items; the amendments are noted inline in scattered prose (R4/D5, C0, C4, B0/B5, #5a). A dated table of "what was frozen, what changed, which sub-project, which §" would let an auditor verify currency in one read instead of cross-checking every module.
4. **Add a CI/test assertion that every "OWNED BY `<path>`" in INTERFACES.md resolves to an existing file** (and ideally that the named type is *defined or re-exported* there). The dead `arbiter/fusion/output.py` reference would have been caught mechanically.
5. **Snapshot the frozen dataclass field lists into a generated fixture** (e.g. assert `dataclasses.fields(ResolvedOutcome)` matches the §6 list). The `stance_score` and `trust_equal_floor` additions would have tripped it, forcing the doc update at the same commit as the schema change.
