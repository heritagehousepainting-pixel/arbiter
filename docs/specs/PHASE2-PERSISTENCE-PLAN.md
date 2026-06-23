# Phase-2 Persistence & Lifecycle — Master Plan (FROZEN CONTRACT)

Status: 2026-06-19. Driven by parallel Sonnet agent waves (plan → build → audit, looping).
Baseline: 1689 tests green, `check_no_lookahead.sh` clean. **Always use `.venv/bin/python`.**

## Problem (from the live run)
A live `arbiter run` works end-to-end, but **only the `orders` table persists**. Ideas are built
and FSM-transitioned in-memory inside `engine.run_cycle` / `orchestrator/cycle.run_cycle` and then
discarded (`ideas` table = 0 rows). SimExecutor positions are in-memory per process, so `arbiter
status` shows `open_positions: 0` after fills. The outcome sweep + labeler are never called. Net:
the bot is **stateless across runs** — tomorrow re-buys held names, and no outcomes are labeled.

## FROZEN design decisions (do not relitigate)

1. **Idea lifecycle persistence = INSERT once on creation, then in-place UPDATE of `state` +
   `updated_state_at` keyed by `idea_id`.** This is a *deliberate, documented carve-out* from the
   §11.2 insert-only rule. Rationale: §11.2 governs immutable FACT tables (filings, opinions,
   outcomes, trust_weights); the `ideas` row is a mutable lifecycle record — the schema ships an
   `updated_state_at` column and `Idea.state` is explicitly the one mutable field. `idea_id` stays
   STABLE for the idea's whole life (orders & outcomes reference it). No new migration for ideas
   (table already exists). All state changes emit an audit line.

2. **Position continuity = persist SimExecutor state to a new `sim_positions` + `sim_account`
   table, and seed the executor from it at `build_engine`.** We do NOT reconstruct from `orders`
   because the `orders` table stores no fill price (no cost basis recoverable). The durable snapshot
   is the source of truth for `status`.

3. **Cycle stays dependency-injected.** `orchestrator/cycle.run_cycle` must NOT import the store
   modules. Persistence is wired in via NEW optional callback params (default `None` → current
   behavior, so all 1689 existing tests keep passing unchanged).

4. **Tests stay OFFLINE and fast.** No agent may make the suite hit the network. Live verification
   is done by the human-invoked `arbiter run`, not by pytest.

## Work packages & DISJOINT file ownership

### WP-A — Idea store  (NEW module; depends on nothing)
Owns: `arbiter/orchestrator/idea_store.py` (new) + `tests/orchestrator/test_idea_store.py` (new).
Public API (FROZEN — other WPs code against this):
```python
def persist_new_idea(conn, idea: Idea, *, created_at: datetime) -> None
    # INSERT into ideas: idea_id, ticker, thesis, horizon_days, state=idea.state.value,
    # as_of, dedupe_key_ticker, dedupe_key_bucket, created_at, updated_state_at=created_at.
    # Idempotent: INSERT OR IGNORE on idea_id PK (re-persisting same idea is a no-op).

def update_idea_state(conn, idea_id: str, new_state: IdeaState, *, updated_state_at: datetime) -> None
    # In-place UPDATE ideas SET state=?, updated_state_at=? WHERE idea_id=?. Emits audit line
    # "idea_state_transition" {idea_id, new_state}. (The carve-out UPDATE — only this + supersede.)

def load_ideas_by_state(conn, states: set[IdeaState]) -> list[Idea]
    # SELECT * FROM ideas WHERE is_superseded=0 AND state IN (...). Reconstruct Idea() directly
    # (NOT via make_idea — that forces NASCENT): dedupe_key=(dedupe_key_ticker, dedupe_key_bucket),
    # as_of parsed tz-aware via datetime.fromisoformat, state=IdeaState(row["state"]).

def load_active_ideas(conn) -> list[Idea]
    # Convenience = load_ideas_by_state(conn, NON-terminal states) i.e. everything except
    # CLOSED and ABANDONED. Used by the cycle for cross-run dedupe and by the sweep.
```

### WP-B — Position store + executor snapshot  (NEW module + new migration + additive executor edit)
Owns: `arbiter/execution/position_store.py` (new), `arbiter/db/migrations/022_positions.sql` (new),
ADDITIVE methods on `arbiter/shared/sim_executor.py` (new methods only — do not touch existing
methods), + `tests/execution/test_position_store.py` (new).
- Migration `022_positions.sql`: `sim_positions(ticker TEXT PRIMARY KEY, shares REAL, avg_price REAL,
  updated_at TEXT)` and `sim_account(id INTEGER PRIMARY KEY CHECK(id=1), cash REAL, realized_pl REAL,
  updated_at TEXT)` (singleton row). Use `CREATE TABLE IF NOT EXISTS`. (Next free number is 022.)
- `SimExecutor` additive methods: `export_state() -> dict` (cash, realized_pl, positions list of
  {ticker, shares, avg_price}) and `restore_state(state: dict) -> None` (repopulate `_cash`,
  `_realized_pl`, `_positions`). Keep `__init__` unchanged.
- `position_store.py` API (FROZEN):
```python
def snapshot_executor(conn, executor: SimExecutor, *, as_of: datetime) -> None
    # Wipe + rewrite sim_positions from executor.export_state(); upsert sim_account singleton.
def load_account_state(conn) -> dict | None   # None if never snapshotted
def open_position_count(conn) -> int           # COUNT(*) FROM sim_positions WHERE shares>0
def seed_executor(conn, executor: SimExecutor) -> None  # restore_state from durable tables (no-op if empty)
```

### WP-C — Outcome runner  (NEW module; depends on WP-A API)
Owns: `arbiter/orchestrator/outcome_runner.py` (new) + `tests/orchestrator/test_outcome_runner.py` (new).
```python
def run_outcome_sweep(conn, *, pit, clock, advisor_id_for, audit_path=None) -> list[str]
    # 1. ideas = idea_store.load_ideas_by_state(conn, {MONITORED})
    # 2. events = sweep_outcomes(ideas, clock)  -> transitions MONITORED->OUTCOME_READY in memory
    # 3. for each event: idea_store.update_idea_state(... OUTCOME_READY ...);
    #       outcome = outcome_labeler.label(idea, pit=pit, cutoff_as_of=clock.now(),
    #                   advisor_id=advisor_id_for(idea), advisor_confidence=..., label_kind="normal")
    #       (on LookupError — price not yet available — log + skip, leave idea MONITORED-or-READY,
    #        do NOT crash the cycle; this is expected for fresh ideas inside horizon)
    #       outcome_store.store_outcome(outcome, conn, as_of=clock.now(), audit_path=audit_path)
    #       idea_store.update_idea_state(... CLOSED ...)
    # 4. return list of stored outcome ids.
    # advisor_id_for: callable(idea)->advisor_id; engine passes a stub returning "A1.congress"/"A1.insider"
    #   based on horizon for the MVP (document the heuristic; trust feed stays Phase-3-gated).
```

### WP-D — Engine/cycle integration  (SERIAL, single owner, AFTER A+B+C land green)
Owns edits to: `arbiter/engine.py`, `arbiter/orchestrator/cycle.py`, and any `cli.py` status wording.
- `orchestrator/cycle.run_cycle`: add kwargs `on_new_idea: Callable[[Idea], None] | None = None` and
  `on_transition: Callable[[Idea, IdeaState], None] | None = None`, default None. Call `on_new_idea`
  when an idea first enters the cycle, and `on_transition(idea, new_state)` immediately after every
  `transition(...)`. No store imports here. Existing tests must stay green (defaults = no-op).
- `engine.build_engine`: after building the SimExecutor, `position_store.seed_executor(conn, executor)`.
- `engine.run_cycle`: load `active_ideas = idea_store.load_active_ideas(conn)`, pass to cycle for
  cross-run dedupe; wire `on_new_idea`→`persist_new_idea`, `on_transition`→`update_idea_state`
  (using `self.clock.now()`); AFTER the cycle: `position_store.snapshot_executor(...)` then
  `outcome_runner.run_outcome_sweep(...)`. All wrapped so a failure logs but doesn't abort the run.
- `engine.status`: `open_positions` from `position_store.open_position_count(conn)` (durable), not
  the in-memory executor length.

## Wave schedule (the loop)
- **Build Wave 1 (parallel):** WP-A, WP-B  (fully disjoint new files).
- **Build Wave 2:** WP-C  (needs WP-A's frozen API).
- **Build Wave 3 (serial):** WP-D integration.
- **Audit Wave (parallel):** (1) correctness/security vs INTERFACES; (2) no-lookahead + insert-only
  carve-out + full offline suite; (3) live `arbiter run` ×2 + `arbiter status` shows persisted
  ideas/positions and the sweep runs clean.
- **Loop:** route P0/P1 audit findings back to the owning WP until the suite is green, lint clean,
  and a live run shows persisted state. Then update memory + handoff.

## Definition of done
1689+ tests green (offline, fast); `check_no_lookahead.sh` clean; `ideas` table populated after a
run with correct states; `sim_positions` populated; `arbiter status` shows real `open_positions`;
two consecutive `arbiter run`s do NOT double-buy held names; the outcome sweep runs without error.
