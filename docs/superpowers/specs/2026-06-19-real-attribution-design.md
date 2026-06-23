# Real Outcome Attribution — Design Spec (sub-project #5a)

Status: DESIGN ONLY (no implementation). Author target: builder agent.
Date: 2026-06-19. Next migration number: **026**.

---

## 0. POST-AUDIT BINDING AMENDMENTS (these SUPERSEDE any conflicting text below)

The plan audit returned **GO-WITH-AMENDMENTS** (no P0). Binding; do not relitigate.

**E0 — [P1] Stranded-retry selection must be a STRICT SUBSET check, not `NOT EXISTS (any outcome)`.** With
per-advisor fan-out, a partial write (advisor 1 stored, crash before advisor 2) leaves the idea with ≥1 outcome
row. The existing `_retry_stranded_closeouts` selection `NOT EXISTS (any outcome for idea)` would then NEVER
re-select it → advisor 2 stranded forever AND the idea stuck MONITORED (CLOSED never flips). FIX: re-select a
MONITORED idea whenever its **stored-advisor set ⊊ its linked-opinion-advisor set** (i.e. the resolver still has
work). The resolver writes the missing advisors (per-`(idea,advisor)` existence guard makes it safe) and flips
CLOSED only AFTER all linked advisors have outcomes. The spec's "keep NOT EXISTS + a comment" simplification is
WRONG — discard it.

**E1 — [P1] Fallback observability (don't let attribution silently go inert).** The `_advisor_id_for`
neutral-stance fallback must fire ONLY when no opinion is recoverable. Emit a `MetricsWriter` counter
`attribution.fallback_proxy` (not just a log WARNING) so a silent opinion-persist regression (everything →
neutral attribution = #4 inert again) is visible. The opinion-persist hook must NOT swallow exceptions silently
— on failure, increment an error counter / surface it. Add a test asserting the fallback rate is ≈0 after a
normal cycle in which a non-abstaining advisor's opinion WAS persisted and recovered.

**E2 — [P1] Existing "exactly one outcome" tests must SEED PERSISTED OPINIONS.** `test_outcome_runner.py`,
`test_exit_monitor*.py` build ideas/orders directly without `run_cycle`'s opinion-persist hook, so
`query_opinions_for_idea` returns `[]` → they hit the FALLBACK path and the `== 1` count still passes — for the
WRONG reason, masking whether real fan-out works. These tests MUST persist opinion rows for the idea and assert
the outcome's `advisor_id`/`stance_score` come from the real opinion (not the proxy). Add a genuine fan-out test:
TWO persisted opinions for one idea → TWO outcomes with each advisor's own stance.

**E3 — [P1] Attribution link is `idea_id` (stored on the opinion) by `op.ticker == idea.ticker AND
HorizonBucket(op.horizon_days) == HorizonBucket(idea.dedupe_key[1])` — NEVER `advisor_contributions`.**
`advisor_contributions` is bucket-wide (cross-ticker) and would mis-attribute. Use typed `HorizonBucket`
equality on both sides (a str-vs-enum mismatch silently links nothing). This is what keeps the learned weights
meaningful once A2 lands a MEDIUM-bucket opinion on a congress ticker (then A2 + A1.congress both correctly
match the same `(ticker, MEDIUM)` idea → two outcomes).

**E4 — Negative-skill must fire on genuine skill, not noise.** A1 emits ONLY positive (BUY) stances, floored at
0.1 (`emit.py`), never negative — so even a weak long that drifts down yields `bss<0`. Add a **near-break-even
control test**: a mostly-correct advisor with occasional losses must NOT be suppressed below the activation
threshold (the recency-weighted aggregate, not a single bad call, governs suppression). Document this A1
positive-stance limitation as a known sensitivity (real short signals would come with A2/A3).

**Doc corrections (P2):** the D6 backward-compat note is inaccurate — legacy proxy rows with `binary==±1` and
`stance_score=0.0` give `p_hat=0.5` → `BSS=0` (chance), NOT "skipped" (only `binary==0` is skipped); they dilute
toward neutral, which is benign. Also: idea-per-ticker dedup (`engine.py` `seen_tickers`) makes the
NULL-`idea_id` opinion (no matching idea) the NORMAL case on source-overlapping tickers, not a rare edge — those
opinions are persisted-but-never-attributed (acceptable; flagged). Fan-out does NOT improve coverage realism
(coverage stays ≈1.0 by construction) — don't claim it does.

**Build structure:** #5a adds `signals/opinion_store.py` + `evaluation/attribution.py` + migration 026 but also
edits the shared `engine.py`, `trust/brier.py`, `outcome_runner.py`, `exit_monitor.py`, `contract/seams.py`
(`ResolvedOutcome.stance_score`). ONE focused build agent (TDD); audit follows.

---

## Goal

Make the trust/learning loop (#4) score each advisor against **its own emitted
opinion** — the actual stance/confidence it produced for the idea — instead of a
forecast reconstructed from the realized outcome and an advisor attributed by a
horizon proxy. Concretely:

1. **Persist** every gathered `Opinion` at decision time, linked to the idea it
   informed (PIT-clean).
2. When an idea resolves, **fan out** one `ResolvedOutcome` *per contributing
   advisor*, each carrying that advisor's own `stance_score` + `confidence` as the
   forecast.
3. **Score the Brier** against the advisor's actual directional forecast
   (`p_hat = _stance_to_prob(stance_score)`), so a wrong-direction advisor earns
   BS high → BSS < 0 → genuinely suppressible.
4. Retire the `_advisor_id_for` horizon proxy (fallback only).

This is what makes #4's learning real: negative skill becomes *detectable* (the
ledger's `bss < 0.0 → "negative_skill"` branch becomes reachable) and weights
reflect real per-advisor skill. The design must work for **N advisors** so #5b
(MiroFish A2) slots in without rework.

---

## Current state (VERIFIED in code)

| Claim | Verified at |
|---|---|
| Opinions are gathered in-cycle and **never persisted** to `opinions`. The only `INSERT INTO opinions` candidates in the tree are MiroFish HTTP JSON keys (`adapters/mirofish/*`), unrelated. | `engine.py:864` gathers via `run_named_advisors_parallel`; grep finds no opinion INSERT/`insert_row(conn,"opinions",…)` / opinion store module. |
| Brier reconstructs the forecast from the **outcome's own `binary`**: `p_hat = _stance_to_prob(float(outcome.binary) * outcome.advisor_confidence)`, scored against `p_outcome = _outcome_to_prob(outcome.binary)`. Both derive from the same `binary` → BS is bounded structurally small → **BSS ≥ 0 always**. | `trust/brier.py:105-114` (`p_hat`), `:114` (`p_outcome`). |
| `ResolvedOutcome` has `advisor_id` + `advisor_confidence` but **no `stance_score`**. | `contract/seams.py:165-187`. |
| Outcomes attributed via horizon proxy `_advisor_id_for` (`>=180d → A1.insider` else `A1.congress`) in **both** the end-of-cycle sweep and the exit-monitor close-out, and **exactly one** outcome is written per idea. | `engine.py:1130-1141` (sweep), `engine.py:473-487` + `532-547` (close-out / monitor wiring); `outcome_runner.run_outcome_sweep` writes one outcome per ready idea; `exit_monitor.close_idea_on_sell_fill` writes one per closed idea. |
| The ledger **already** flips `cap_reasons[advisor]="negative_skill"` when `bss < 0.0`, and the resolver suppresses on that reason — but it is **unreachable** today because BSS ≥ 0. | `trust/ledger.py:339-344`, `weight_resolver.py:67,107-109`. |
| Fusion is **bucket-pooled, not ticker-specific**. An idea for `(ticker T, bucket B)` is fused against ALL bucket-B opinions; `FusionOutput.advisor_contributions` keys are every advisor who emitted into bucket B. | `cycle.py:242-261`, `fusion/pool.py:102-123`. |
| For the 2-advisor MVP the proxy is ≈1:1 (insider=180d=LONG, congress=90d=MEDIUM, ideas minted per source), which is exactly why the bug is invisible today and breaks the moment multi-horizon / A2 advisors land. | `engine.py:908-916` (idea per source), `signals/emit.py:46-47,60-61`. |
| Migration runner has an idempotent `ADD COLUMN` duplicate-column guard; additive migrations are the established pattern (see 023, 025). | `db/migrations/023_orders_idea_id.sql`, `025_trust_weights_cap_reason.sql`. |
| Strict cutoff: learning reads outcomes with `created_at < now` (`strict_lt=True`); same-cycle outcomes stamped at `now` are excluded. | `trust/store.py:56-80`, `outcome_store.query_outcomes:194-196`. |

**Net:** at resolution time the advisor's true stance is gone, so the loop scores a
forecast it invents from the answer. Skill is unfalsifiable. We fix it by
persisting opinions and carrying the real stance onto the outcome.

---

## Design decisions

### D1 — Persist opinions at decision time, linked to the idea

**Where.** In `Engine.run_cycle`, opinions are already gathered once up front into
`raw_opinions` (`engine.py:864`) and reused. Persist them **after** ideas are built
but at the decision `as_of = now`. Concretely: add a step inside `run_cycle` right
after the idea list is finalized (`engine.py:~918`, after `if not ideas: return`)
and before/around `run_cycle(...)` orchestration, OR — cleaner — persist inside the
existing `_on_new_idea` / a new `_persist_opinions_for_idea` hook so linkage is
established as each idea is created. Recommended: a dedicated helper
`_persist_cycle_opinions(now, valid_opinions, ideas)` called once, after ideas are
built, before the `run_cycle(...)` orchestration call.

**Linkage key — the hard part (fusion is bucket-pooled).** An opinion belongs to a
bucket; an idea is `(ticker, bucket)`. Two relationships exist:

- *Same-ticker opinions* (`op.ticker == idea.ticker AND op.horizon_bucket == idea.bucket`)
  — these are genuinely **about** the idea's ticker. These are the opinions whose
  realized outcome we want to attribute to the advisor.
- *Other-ticker, same-bucket opinions* — these were pooled into the bucket fusion
  that produced the idea's conviction, but they are not forecasts about *this*
  ticker. Attributing this ticker's realized alpha to them would be wrong
  (an A1.insider opinion on NVDA must not be scored by AAPL's outcome).

**Decision: attribute by (ticker, bucket), not by bucket-pool membership.** The
opinion→idea link is `op.ticker == idea.ticker AND bucket_for_days(op.horizon_days)
== idea.dedupe_key.bucket`. This is the **only** correct attribution: an advisor is
scored on the security it actually made a directional call on. `advisor_contributions`
(bucket-level) is therefore NOT the attribution key — it is retained only as a
diagnostic/audit signal (and to detect that an advisor *participated* in the bucket).

**Storage — store `idea_id` on the opinion row (preferred over match-by-key).**
Reasons: (a) the (ticker, bucket, run_group_id, as_of) tuple is not guaranteed
unique once A2 emits multiple opinions per ticker/bucket in one run_group; (b) an
explicit FK is unambiguous at resolution and survives schema/heuristic changes;
(c) it mirrors the already-shipped `orders.idea_id` pattern (B5, migration 023).

- Migration **026** adds `opinions.idea_id TEXT` (nullable; legacy/abstain rows
  stay NULL) + index `idx_opinions_idea_id`.
- A new module `arbiter/signals/opinion_store.py` (mirrors `evaluation/outcome_store.py`)
  exposes:
  - `store_opinion(opinion, conn, *, idea_id, created_at, audit_path=None) -> str`
    — insert-only via `insert_row(conn, "opinions", …)`; sets `idea_id`,
    `created_at = as_of.isoformat()` of the decision (so a backtest stamps the
    replay date, PIT-clean), `is_superseded=0`, `supersedes_id=NULL`.
  - `query_opinions_for_idea(conn, idea_id, *, include_superseded=False) -> list[dict]`
    — the resolution-time recovery query: "the opinions that drove idea X."
  - `query_opinions(conn, *, ticker=None, advisor_id=None, idea_id=None, as_of=None,
    strict_lt=False)` — for tests/diagnostics; same shape as `query_outcomes`.

**Linkage assignment.** When persisting, for each `idea` and each
`op in valid_opinions` where `op.ticker == idea.ticker AND op.horizon_bucket ==
idea.dedupe_key.bucket`, write one opinion row with that `idea_id`. An opinion that
matches no idea this cycle (e.g. its ticker was held/deduped and no idea minted) is
**still persisted** but with `idea_id = NULL` (audit completeness; never attributed).
An advisor that abstained emitted no `Opinion` (None) → nothing persisted (abstention
= absence, per INTERFACES §11).

**PIT cleanliness.** `created_at` = decision `now` (from `clock.now()`), never
wall-clock. Backtests persist at the replay date. No `datetime.now()` introduced
(all timestamps threaded from `clock`). `check_no_lookahead.sh` stays clean.

**Idempotency of opinion persistence.** `run_cycle` for a given `as_of` is already
idempotent for orders via `dedup_hash`. Opinions are insert-only and a re-run at the
same `as_of` could double-insert. Guard: before inserting, the store checks for an
existing non-superseded row matching `(advisor_id, idea_id, source_fingerprint,
as_of)` and skips if present (cheap SELECT; opinions table is small per cycle). This
keeps re-runs / retries from duplicating opinion rows. (Alternatively a UNIQUE index
on `(advisor_id, idea_id, source_fingerprint)` where `idea_id IS NOT NULL` — but a
SELECT-guard avoids a partial-index migration and is sufficient.)

---

### D2 — Per-advisor outcome fan-out

**Replace the single proxy outcome.** Today both resolution paths call a
`advisor_id_for(idea) -> str` and write exactly one outcome. New behavior: at
resolution, recover **every persisted opinion for the idea** via
`opinion_store.query_opinions_for_idea(conn, idea_id)` and write **one
`ResolvedOutcome` per contributing advisor**, each carrying that advisor's own
`stance_score` + `confidence`.

**Which advisors get an outcome.** Every advisor that emitted a **non-abstain**
opinion linked to the idea (i.e. has a persisted opinion row with this `idea_id`).
Abstentions never produced a row, so they are naturally excluded — consistent with
the existing brier rule that drops abstained rows. We do **not** synthesize abstain
outcomes for advisors who said nothing (keeps the table honest; matches `_eligible_by_advisor`
coverage-≈1.0 assumption in #4). If a single advisor emitted multiple opinions for
the same idea_id (possible for A2 swarm with one stance per call but same ticker),
collapse to one outcome per advisor by taking the opinion with the **latest
`created_at`** for that `(idea_id, advisor_id)` (the live stance that informed the
decision); the others are diagnostic.

**Mechanics — new shared resolver.** Introduce
`arbiter/evaluation/attribution.py::resolve_advisor_outcomes(conn, idea, *, pit,
cutoff_as_of, exit_price=None, exit_as_of=None, label_kind, audit_path) ->
list[str]` that:
  1. loads `opinions_for_idea = query_opinions_for_idea(conn, idea.idea_id)`;
  2. dedups to one row per `advisor_id` (latest `created_at`);
  3. for each, calls `outcome_labeler.label(idea, …, advisor_id=row.advisor_id,
     advisor_confidence=row.confidence, stance_score=row.stance_score, …)`;
  4. `outcome_store.store_outcome(...)` for each;
  5. returns stored ids.
Note the alpha/binary computed by the labeler is **identical across advisors for the
same idea** (same entry/exit/beta) — what differs is each advisor's `stance_score` +
`confidence`, which is exactly the forecast the Brier needs. (The continuous
`alpha_bps` that drives the trust composite is the same realized alpha; per-advisor
differentiation comes from the Brier forecast term in D3 and from each advisor's own
set of resolved outcomes across many ideas.)

**Both paths call the resolver.**
- `outcome_runner.run_outcome_sweep` (horizon sweep): replace the single
  `label(...) + store_outcome(...)` block (`outcome_runner.py:124-174`) with a call
  to `resolve_advisor_outcomes(...)`. Drop the `advisor_id_for` / `advisor_confidence_for`
  params (or keep as last-resort fallback, see D4).
- `exit_monitor.close_idea_on_sell_fill` (exit close-out): replace the single
  `label(...) + store_outcome(...)` block (`exit_monitor.py:330-358`) with the same
  resolver call (passing `exit_price` / `exit_as_of` / trigger `label_kind`).
- `exit_monitor._retry_stranded_closeouts` calls `close_idea_on_sell_fill`, so it
  inherits the fan-out for free.

**Idempotency — no duplicate outcome rows per (idea, advisor).** This is critical
given the exit-monitor retry sweep, the reconcile path, and the strict-cutoff
interaction with #4. Rules:
  - The resolver, **before storing**, checks existing non-superseded outcomes for
    the idea: `existing = {row.advisor_id for row in outcome_store.query_outcomes(
    conn, idea_id=idea.idea_id)}`. It only writes outcomes for advisors **not** in
    `existing`. This makes resolution idempotent at the `(idea_id, advisor_id)`
    grain across re-runs, retries, and the stranded-closeout sweep.
  - The FSM state guards already prevent re-entry for the common case (idea must be
    MONITORED to be labeled, and is flipped to OUTCOME_READY→CLOSED on success), but
    the per-advisor existence check is the authoritative idempotency guard because
    fan-out writes N rows and a partial failure (rare) could otherwise re-emit a
    subset. The state transition to CLOSED happens **once**, after all advisor
    outcomes for the idea are stored (move the `OUTCOME_READY → store(s) → CLOSED`
    sequence so the CLOSED flip is after the loop).
  - `_retry_stranded_closeouts` already selects ideas with **no** stored outcome
    (`NOT EXISTS (SELECT 1 FROM outcomes ou WHERE ou.idea_id = i.idea_id …)`,
    `exit_monitor.py:425-426`). Update this guard semantics note: with fan-out, an
    idea that got a *partial* set of outcomes (some advisors stored, then a crash)
    would still satisfy `NOT EXISTS` only if zero rows exist; if a subset exists the
    idea is no longer selected. To be safe, the retry should re-run the resolver
    (which is idempotent per (idea, advisor)) for any MONITORED idea whose stored
    advisor set is a **strict subset** of its linked opinion advisor set. Practical
    simplification: keep the existing `NOT EXISTS` selection (covers the dominant
    all-or-nothing case since the loop stores all advisors in one transaction); add
    a code comment that the per-advisor existence check inside the resolver makes a
    re-run safe even if the selection ever broadens.

**No-opinion fallback.** If `query_opinions_for_idea` returns empty (legacy idea
predating opinion persistence, or an unattributable close), fall back to D4.

---

### D3 — Add `stance_score` to `ResolvedOutcome` + fix the Brier forecast

**`ResolvedOutcome` (`contract/seams.py`).** Add a field
`stance_score: float` (the advisor's actual directional forecast, `[-1,1]`). Place
it after `advisor_confidence` for readability. The dataclass is frozen — purely
additive; all construction sites must pass it. Construction sites:
  - `outcome_labeler.label(...)` (two `ResolvedOutcome(...)` returns: the abstain
    branch `:122-133` and the normal branch `:188-198`) — accept a new
    `stance_score: float` param and thread it into both returns. For the abstain
    branch `stance_score=0.0` (or the persisted stance if present; abstain rows are
    excluded from Brier regardless).
  - any test fixtures building `ResolvedOutcome` directly (suite migration, D7).

**`outcomes` table — migration 026 adds `stance_score`.** `outcomes` needs the
column so the strict-cutoff learning read reconstructs the real forecast.
  - `db/migrations/026_outcome_stance_attribution.sql`:
    `ALTER TABLE outcomes ADD COLUMN stance_score REAL NOT NULL DEFAULT 0.0;`
    (additive, idempotent via the runner's duplicate-column guard; the DEFAULT 0.0
    backfills legacy rows — legacy rows are scored at stance 0 ⇒ they fall in the
    no-call/`binary==0` skip path or contribute a neutral forecast, acceptable for
    pre-existing proxy rows; see D7).
  - `outcome_store._outcome_to_row` (`outcome_store.py:209-222`) adds
    `"stance_score": outcome.stance_score`.
  - `outcome_store.query_outcomes` returns the column (it `SELECT *`s, so automatic).
  - `trust/store._row_to_resolved_outcome` (`trust/store.py:41-53`) reads
    `stance_score=float(row["stance_score"])` into the reconstructed `ResolvedOutcome`.

**Same migration adds `opinions.idea_id`** (one migration file, two ALTERs +
indexes) — 026 covers all of #5a's schema. Keep the two concerns clearly commented.

**Brier fix (`trust/brier.py:105-114`).** Replace the reconstructed forecast with the
advisor's real stance:

```
# BEFORE (structural BSS >= 0 — direction taken from the answer):
p_hat = _stance_to_prob(float(outcome.binary) * outcome.advisor_confidence)
p_outcome = _outcome_to_prob(outcome.binary)

# AFTER (forecast = advisor's ACTUAL directional stance, scaled by confidence):
p_hat = _stance_to_prob(outcome.stance_score * outcome.advisor_confidence)
p_outcome = _outcome_to_prob(outcome.binary)
```

- `p_hat` now comes from `stance_score` (the opinion the advisor emitted), **not**
  from `binary` (the realized answer). `* advisor_confidence` preserves the existing
  confidence handling: a low-confidence call pulls `p_hat` toward 0.5 (less penalized
  when wrong, less rewarded when right), exactly as today, but anchored on the real
  direction.
- `p_outcome` continues to come from realized `binary` (the ground truth) — unchanged.
- Recency decay (`_decay_weight`, 182-day half-life) and the `binary == 0` skip and
  abstain skip are **all unchanged** (`brier.py:94-103,116-119`).
- Update the module docstring (`brier.py:1-20`) and the stale inline comment at
  `:105-113` (which currently claims "stance_score is not stored in ResolvedOutcome").

**Why this makes negative skill reachable (confirm).** Suppose A1.insider is long
(`stance_score = +0.9`, confidence 0.8) on a name that goes **down** past −25bps so
`binary = -1`:
  - `p_hat = _stance_to_prob(0.9 * 0.8) = _stance_to_prob(0.72) = 0.86`
  - `p_outcome = _outcome_to_prob(-1) = 0.0`
  - `BS = (0.86 − 0.0)^2 = 0.74` (vs `BS_REF = 0.25`)
  - `BSS = 1 − 0.74/0.25 = 1 − 2.96 = -1.96` → **negative**.
Across a population of consistently wrong-direction calls the recency-weighted BSS is
< 0, so `ledger.update` sets `cap_reasons[advisor] = "negative_skill"`
(`ledger.py:340,344`) and `weight_resolver` flips the advisor to suppression
(`weight_resolver.py:107-109`). Under the OLD code `p_hat` and `p_outcome` both came
from `binary`, so `BS = (_stance_to_prob(±conf) − {0 or 1})^2 ≤ 0.25` always ⇒ BSS ≥ 0
⇒ the suppression branch was dead. The fix makes it live. (Sanity: a perfectly
right, confident advisor: stance +0.9, conf 0.9, binary +1 ⇒ p_hat≈0.905,
p_outcome=1.0, BS≈0.009, BSS≈0.96 — strongly positive, as it should be.)

---

### D4 — Retire / replace `_advisor_id_for`

The horizon proxy `_advisor_id_for` (defined inline 3×: `engine.py:473-474`,
`532-533`, `1130-1131`) is **removed from the primary path** — attribution now comes
from persisted opinions (D1/D2). It survives only as a **last-resort fallback** inside
`resolve_advisor_outcomes` for the case `query_opinions_for_idea(...) == []`:

- **Fallback policy.** When an idea has **no recoverable opinions** (legacy idea
  minted before opinion persistence, or a manual/orphan close), write **one** outcome
  using the horizon proxy advisor_id, `stance_score = 0.0`, `confidence = 1.0`,
  `label_kind` as given. A stance of 0.0 maps to `p_hat = 0.5` (neutral) — it neither
  rewards nor penalizes, and `binary==0` rows are skipped anyway, so legacy/unattributable
  closes contribute ~nothing to skill rather than poisoning it. The proxy lambda is
  passed into the resolver as `fallback_advisor_id_for: Callable[[Idea], str] | None`
  so the engine still owns the heuristic (kept in `engine.py` exactly as today, but
  only consulted on the empty-opinion branch). Log a WARNING (`attribution.fallback_proxy`)
  whenever the fallback fires, so we can see how often legacy ideas resolve.
- Once all live ideas are created post-026 (which always persist opinions), the
  fallback is effectively never hit; it exists purely for backward-compatible
  resolution of in-flight legacy ideas and for safety.

---

### D5 — No-look-ahead

- **Opinions persisted at `as_of`.** `store_opinion` stamps `created_at = decision
  now` (backtest: replay date). Attribution at resolution reads the opinion row **as
  it was persisted** — the stance is frozen at decision time, never recomputed from
  future data.
- **Labeler unchanged for look-ahead.** `outcome_labeler.label` still reads prices
  only via PIT bounded by `cutoff_as_of` (`outcome_labeler.py:154-155`); the new
  `stance_score` param is pure metadata carried from the past opinion — it touches no
  price read.
- **#4 strict cutoff still holds with fan-out.** All N per-advisor outcomes for an
  idea are stamped `created_at = resolution now` (same instant), exactly like the
  single outcome today. `load_outcomes_for_learning` uses `created_at < now`
  (strict), so this cycle's resolution outcomes are excluded from this cycle's
  learning — for **all** N rows, not just one. The fan-out changes the *count* of
  rows stamped at `now`, not the *timestamp*, so the D0 guarantee is preserved
  unchanged. (The end-of-cycle sweep still runs AFTER `_build_learning_inputs`,
  `engine.py:928` then `:1134` — do not reorder.)
- **Backtests reproduce.** Opinion persistence and fan-out are deterministic
  functions of (signals, prices, clock); a replay re-mints the same ideas, persists
  the same opinions at the same replay `as_of`, and resolves the same N outcomes.
- **`check_no_lookahead.sh` stays clean.** No new `datetime.now()` / `get_latest(`;
  all timestamps threaded from `clock`. The new `opinion_store` mirrors `outcome_store`
  (clock-injected `as_of`, no wall-clock).

---

### D6 — Migrations + insert-only

- **One new migration: `026_outcome_stance_attribution.sql`** (next number after 025):
  ```sql
  -- 026 — real attribution (#5a)
  ALTER TABLE outcomes  ADD COLUMN stance_score REAL NOT NULL DEFAULT 0.0;  -- advisor's forecast
  ALTER TABLE opinions  ADD COLUMN idea_id TEXT;                             -- opinion→idea link
  CREATE INDEX IF NOT EXISTS idx_opinions_idea_id ON opinions (idea_id);
  ```
  Both ALTERs are additive and idempotent via the runner's duplicate-column guard
  (same pattern as 023/025). `stance_score` is NOT NULL with DEFAULT 0.0 so legacy
  outcome rows backfill cleanly. `opinions.idea_id` is nullable (legacy/unlinked rows).
- **Insert-only preserved.** `opinions` and `outcomes` stay insert-only:
  `store_opinion` and `store_outcome` use `insert_row`; the only in-place UPDATE is
  the existing `supersede_row` flip. New opinion/outcome rows never need supersede
  (a fresh decision/resolution is a new row). The `idea_id` link is set at insert
  time, never updated (unlike `orders.idea_id`, which is updated post-submit — we
  avoid that here by knowing the idea at opinion-persist time).
- **Numbering.** 026 is correct and unused (existing: …024, 025).

---

### D7 — Backward compat / suite

The suite (~1858 tests) assumes (a) one proxy-attributed outcome per idea and (b) the
binary-reconstructed Brier. Both assumptions are **intentionally changed**; tests are
migrated to the real-attribution behavior, not preserved.

- **`ResolvedOutcome` constructor.** Every direct `ResolvedOutcome(...)` in tests
  and fixtures must add `stance_score=`. Provide a test helper / default in fixture
  factories. (grep `ResolvedOutcome(` across `tests/` and update.)
- **Brier tests** (`tests/.../test_brier*` or trust tests): expectations recomputed.
  Add a **new** decisive test: a wrong-direction advisor (stance +0.9, all outcomes
  `binary=-1`) yields `brier_skill_score < 0` (was impossible before). Add a
  right-direction control yielding BSS > 0. This is the headline regression-proof.
- **Ledger suppression test**: feed the ledger ≥ activation-threshold outcomes for a
  consistently wrong-direction advisor and assert `last_cap_reasons[advisor] ==
  "negative_skill"` and the resolved weight is suppressed (0/shadow) — proving the
  previously-dead branch is live end-to-end.
- **Outcome-count tests**: any test asserting "exactly one outcome per idea" updates
  to "one outcome per contributing advisor." For the 2-advisor MVP where ideas are
  minted per source, an idea typically has exactly one matching same-ticker opinion,
  so most counts stay 1 — but tests must assert against the persisted opinion set,
  not the proxy.
- **Engine/integration tests**: add opinion-persistence assertions
  (`query_opinions_for_idea` non-empty after a cycle with a non-abstain advisor) and
  fan-out assertions (one outcome per linked advisor with the advisor's real stance).
- **PIT / idempotency tests**: re-running a cycle at the same `as_of` does not
  duplicate opinion rows; the stranded-closeout retry does not duplicate outcomes
  (per-(idea,advisor) existence guard).
- **Suite stays OFFLINE + green**: opinion_store/attribution use injected `conn`,
  `clock`, `pit` only — no network. Run `.venv/bin/python -m pytest` from
  `/Users/jonathanmorris/poly_bot/arbiter` and `scripts/check_no_lookahead.sh`.

---

## Test strategy (OFFLINE — what proves the fix)

1. **Negative skill is now reachable (unit, brier).** Construct N `ResolvedOutcome`
   with `stance_score=+0.9`, `confidence=0.8`, `binary=-1` (advisor consistently
   long, market consistently down). Assert `brier_skill_score(...) < 0`. Mirror with
   stance −0.9 / binary +1 (also wrong) → < 0. Control: stance +0.9 / binary +1 → > 0.
2. **End-to-end suppression (integration, ledger).** Seed ≥ `PHASE3_ACTIVATION_THRESHOLD`
   (60) outcomes for a wrong-direction advisor via `outcome_store`; run
   `ledger.update`; assert `cap_reasons[advisor]=="negative_skill"` and
   `resolve_weight_bundle` returns weight 0 / shadow for it.
3. **Per-advisor fan-out (integration).** Two advisors emit opinions on the same
   ticker/bucket idea; resolve it; assert two outcome rows, each with the emitting
   advisor's own `stance_score` + `confidence`, same `alpha_bps`.
4. **Opinion→idea linkage (integration).** After a cycle, `query_opinions_for_idea`
   returns exactly the non-abstain opinions whose `(ticker,bucket)` matches the idea;
   an abstaining advisor contributes no row; an other-ticker same-bucket opinion is
   NOT linked to this idea.
5. **PIT / idempotency.** (a) Re-run `run_cycle(as_of)` twice → opinion rows not
   duplicated. (b) Force a `LookupError` then retry → exactly one outcome per advisor
   after the stranded-closeout retry. (c) Backtest replay reproduces the same opinion
   + outcome rows.
6. **Fallback path.** A legacy idea with no persisted opinions resolves via the proxy
   fallback with `stance_score=0.0` and logs `attribution.fallback_proxy`; that row
   does not move skill (neutral / skipped).
7. **No-lookahead.** `scripts/check_no_lookahead.sh` exits 0.

---

## Out of scope

- MiroFish A2 wiring (#5b). The design is N-advisor-correct (attribution by
  persisted opinion, fan-out over all contributors), so A2 slots in by registering
  in `advisor_map` + emitting opinions — no attribution rework.
- Going live (paper-only guarantee unchanged).
- Eligible-idea roster beyond the v1 "ideas the advisor produced an outcome on"
  (`engine._eligible_by_advisor`) — a true roster including abstained-but-eligible
  ideas is #4 R2, still deferred. (Note: persisted opinions now make a richer roster
  *possible* later — an advisor that emitted an opinion on an idea is "eligible" even
  if its outcome is a no-call — but wiring that into the ledger's eligibility is out
  of scope here.)
- Ticker-specific fusion (replacing bucket-pooling). Attribution is fixed by the
  (ticker,bucket) link regardless; fusion granularity is a separate concern.

---

## Open risks

1. **BIGGEST RISK — bucket-pooled fusion vs (ticker,bucket) attribution mismatch.**
   Fusion pools all bucket-B opinions to size an idea, but we attribute outcomes only
   to the same-ticker advisors. This is *correct* (don't score NVDA's opinion by
   AAPL's result), but it means an advisor whose opinion was pooled into a bucket's
   conviction yet was about a *different* ticker gets no outcome from this idea — its
   own ticker's idea (if minted) attributes it instead. For the 2-advisor per-source
   MVP this is clean. For N advisors emitting on overlapping tickers it is still
   correct but the builder must NOT fall back to `advisor_contributions` (bucket-level)
   as the attribution key — that would cross-attribute. The spec mandates the
   (ticker,bucket)+`idea_id` link; the builder must verify this holds when A2 emits
   multiple tickers per run.
2. **Same alpha across advisors per idea.** All advisors on one idea share the
   realized `alpha_bps` (same entry/exit). Differentiation of *skill* comes entirely
   from the per-advisor `stance_score` in the Brier term and from each advisor's full
   cross-idea outcome set. If two advisors always agree on direction, their BSS will
   track together — by construction, not a bug — but correlation deflation (#5 Phase-5)
   is the intended remedy, out of scope here.
3. **Legacy backfill stance=0.0.** Pre-026 outcome rows get `stance_score=0.0` →
   `p_hat=0.5` neutral. If a meaningful population of legacy proxy outcomes exists in
   a long-running DB, they dilute (toward chance) rather than corrupt skill. Acceptable;
   flagged so the builder can optionally exclude pre-026 rows by `created_at` if needed.
4. **Idempotency across the three resolution entrypoints** (sweep, sync close-out,
   stranded retry). The per-(idea,advisor) existence guard in the resolver is the
   single authoritative dedup; the builder must ensure all three go through
   `resolve_advisor_outcomes` and none store outcomes directly.
5. **Opinion volume / write amplification.** Persisting every gathered opinion per
   cycle adds writes. For the 2-advisor MVP this is trivial; for an A2 swarm it could
   be many rows per cycle. Insert-only + small per-cycle counts make this a non-issue
   at MVP scale, but worth a note for #5b.

---

## File / function change index (for the builder)

| File | Change |
|---|---|
| `arbiter/db/migrations/026_outcome_stance_attribution.sql` | NEW: `outcomes.stance_score` + `opinions.idea_id` + index. |
| `arbiter/contract/seams.py` | `ResolvedOutcome`: add `stance_score: float`. |
| `arbiter/trust/brier.py` | `recency_weighted_brier`: `p_hat = _stance_to_prob(outcome.stance_score * outcome.advisor_confidence)`; fix docstring + stale comment. |
| `arbiter/evaluation/outcome_labeler.py` | `label(...)`: add `stance_score` param; thread into both `ResolvedOutcome(...)` returns. |
| `arbiter/evaluation/outcome_store.py` | `_outcome_to_row`: add `stance_score`. (query_outcomes auto via `SELECT *`.) |
| `arbiter/trust/store.py` | `_row_to_resolved_outcome`: read `stance_score`. |
| `arbiter/signals/opinion_store.py` | NEW: `store_opinion`, `query_opinions_for_idea`, `query_opinions` (insert-only, clock-injected, idempotency SELECT-guard). |
| `arbiter/evaluation/attribution.py` | NEW: `resolve_advisor_outcomes(...)` — fan-out + per-(idea,advisor) idempotency + proxy fallback. |
| `arbiter/orchestrator/outcome_runner.py` | `run_outcome_sweep`: replace single label/store with `resolve_advisor_outcomes`; CLOSED flip after the per-advisor loop. |
| `arbiter/execution/exit_monitor.py` | `close_idea_on_sell_fill`: replace single label/store with `resolve_advisor_outcomes`; `_retry_stranded_closeouts` inherits. |
| `arbiter/engine.py` | `run_cycle`: persist opinions linked by (ticker,bucket)→idea_id after ideas built; pass `fallback_advisor_id_for` (the kept proxy) into the resolver via the sweep/close-out wiring; remove primary use of `_advisor_id_for`. |
| `tests/**` | Migrate `ResolvedOutcome(` constructors (+stance), brier expectations, add negative-skill + fan-out + linkage + idempotency tests; assert one outcome per contributing advisor. |
```
