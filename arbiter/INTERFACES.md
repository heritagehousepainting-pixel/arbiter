# Arbiter — FROZEN INTERFACES (the contract bible)

> Every agent builds against THIS file. Do not redefine these names elsewhere. If you need a
> change, it is a cross-lane event — flag it, don't silently diverge. Specs trace to
> `docs/specs/2026-06-18-arbiter-decision-engine-design.md` and `...-build-plan.md`.

Package layout is **flat**: importable package is `arbiter.arbiter` living at `arbiter/arbiter/`.
All imports below are written relative to the package, e.g. `from arbiter.types import HorizonBucket`.

---

## 1. Canonical enums — OWNED BY `arbiter/types.py` (scaffold lane L1)

Everyone imports these; nobody else defines them.

```python
from enum import Enum

class HorizonBucket(str, Enum):
    INTRADAY = "INTRADAY"   # < 1 day
    SHORT    = "SHORT"      # 1–30 days
    MEDIUM   = "MEDIUM"     # 31–120 days
    LONG     = "LONG"       # 121–365 days

class ConfidenceSource(str, Enum):
    EMPIRICAL     = "empirical"
    MODELED       = "modeled"
    SELF_REPORTED = "self_reported"
    NONE          = "none"

class OrderSide(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"

class IdeaState(str, Enum):
    NASCENT = "NASCENT"; GATHERING = "GATHERING"; PROVISIONAL_DECIDED = "PROVISIONAL_DECIDED"
    FINAL_DECIDED = "FINAL_DECIDED"; EXECUTED = "EXECUTED"; MONITORED = "MONITORED"
    OUTCOME_READY = "OUTCOME_READY"; CLOSED = "CLOSED"; ABANDONED = "ABANDONED"

class DegradationLevel(int, Enum):
    NORMAL = 0; CAUTION = 1; DEGRADED = 2; RESTRICTED = 3; HALTED = 4

# Horizon (in days) -> bucket. Helper also in types.py:
def bucket_for_days(days: float) -> HorizonBucket: ...
```

`as_of` everywhere is an **information timestamp** (`datetime`, tz-aware UTC), never wall-clock.
Abstain is **`None`**, never `0.0`.

---

## 2. The Opinion contract — OWNED BY `arbiter/contract/opinion.py` (lane L9)

The single thing every advisor emits. **Frozen.** Advisors emit RAW stance only — never calibrated probabilities.

```python
@dataclass(frozen=True)
class Opinion:
    advisor_id: str                 # e.g. "A1.insider", "A1.congress", "A1.activist", "A2.mirofish"
    ticker: str
    stance_score: float             # directional, in [-1.0, 1.0]; +long / -short
    confidence: float               # in [0.0, 1.0]
    confidence_source: ConfidenceSource
    horizon_days: int               # advisor's stated horizon
    as_of: datetime                 # information timestamp (tz-aware UTC)
    rationale: str
    source_fingerprint: str         # for correlation detection (e.g. hash of underlying filing/event)
    run_group_id: str               # multi-opinion runs share this (MiroFish); else a fresh ULID
    @property
    def horizon_bucket(self) -> HorizonBucket: ...   # via bucket_for_days

def validate_opinion(op: Opinion) -> None:
    """Raise ValueError on contract violation: stance in [-1,1], confidence in [0,1],
    horizon_days>0, as_of tz-aware, non-empty advisor_id/ticker/source_fingerprint/run_group_id."""

# Abstention is represented by NOT emitting an Opinion (None), never a zero-stance Opinion.

# Live advisor IDs (Wave 2):
#   A1.insider   — Form 4 cluster/single insider buys      (source="form4",    180d LONG)
#   A1.congress  — Congressional sector buys                (source="congress",  90d MEDIUM)
#   A1.activist  — Schedule 13D/13G beneficial-ownership    (source="form13d",  180d LONG;
#                  bearish/NEGATIVE stance on a 13D/G exit, txn_type='S')
#   A2.mirofish  — per-idea, list-valued channel (Engine.a2_mirofish_fn, NOT advisor_map;
#                  hard_weight_cap=0.35; inert/noop when MIROFISH_ENDPOINT is unset).
# filings.source strings: "form4", "congress", "form13d" (TEXT, unconstrained).

class AdvisorRegistry:  # name -> metadata; advisors self-register
    def register(self, advisor_id: str, *, hard_weight_cap: float | None = None) -> None: ...
    def all_ids(self) -> list[str]: ...
```

---

## 3. Point-in-time gateway — OWNED BY `arbiter/data/pit.py` + `clock.py` (lane L3)

The ONLY way to read price/filing/news/trust. No `get_latest()`, no bare `datetime.now()` outside `clock.py`.

```python
class Clock:                      # clock.py — the ONLY source of "now"
    def now(self) -> datetime: ...            # live: real UTC now; backtest: simulated as_of
class BacktestClock(Clock): ...

class PITGateway:                 # pit.py
    def get(self, field: str, ticker: str, as_of: datetime): ...
    # fields (string keys): "price_open", "price_close", "adv_20d", "beta_252d",
    #   "spread", "filing", "news", "trust". Returns None if unknown as-of as_of (no look-ahead).

class PriceSource(Protocol):      # data/sources/  — Alpaca, Stooq implement this
    def bars(self, ticker: str, start: datetime, end: datetime) -> list["Bar"]: ...

def beta_252d(ticker: str, as_of: datetime, pit: PITGateway) -> float: ...   # beta.py; impute 1.0 + flag
def model_slippage(price: float, spread: float) -> float: ...                # slippage.py; 5bps + 0.5*spread
```

Per-source `as_of`: Form 4 = filing timestamp; Congress = disclosure date; price(exec) = next-day open;
news = publish ts; beta = 252-day window ending `as_of − 1`.

---

## 4. Fusion output — DEFINED IN `arbiter/contract/seams.py` (lane L9), produced by fusion L10, consumed by policy L12

> **Contract location (reconciled):** the §4–§9 seam dataclasses below
> (`FusionOutput`, `AdvisorWeight`, `WeightBundle`, `EqualWeightBundle`,
> `ResolvedOutcome`, `Idea`, `TradingDecision`, `PaperOrder`) ALL live in the
> single canonical module **`arbiter/contract/seams.py`** (Lane L9 core), imported as
> `from arbiter.contract.seams import FusionOutput, ...`. The older "OWNED BY
> `arbiter/fusion/output.py`" path is **stale — that file does not exist**; the
> per-section "OWNED BY" lines below name the *producing* lane/module, not where the
> type is declared. (`arbiter/contract/opinion.py` still owns the `Opinion` contract, §2.)

```python
@dataclass(frozen=True)
class FusionOutput:
    bucket: HorizonBucket
    conviction: float               # signed; signal_strength * diversity_factor - lone_bull_tax
    dispersion: float
    effective_n: float              # 1 / Σ Σ wi wj ρij
    n_opinions: int
    advisor_contributions: dict[str, float]   # advisor_id -> contribution
    vetoes: list[str]               # advisor_ids that hard-vetoed
    cold_start: bool                # True while calibration prior dominates
    # equal-weight (Phase 1) sets all weights equal; trust-weighted (Phase 3) consumes WeightBundle

# Engine output per cycle:  dict[HorizonBucket, FusionOutput]
```

**Calibrator seam — additive `transform_for` (sub-project #4, amendment R4/D5).**
`fuse`/`pool.py` consume a calibrator object exposing `transform(raw_stance, horizon_days) -> float`
plus a no-arg `is_cold_start` bool. Sub-project #4 adds an **additive, backward-compatible** method
`transform_for(advisor_id, raw_stance, horizon_days) -> float`, defaulting to `self.transform(...)`
on both `PassthroughCalibrator` and the real `Calibrator`. `pool.py` now calls
`calibrator.transform_for(op.advisor_id, …)` so a `MultiAdvisorCalibrator` (wrapping
`dict[str, Calibrator]`) can route per advisor; its `is_cold_start` is True iff EVERY wrapped
calibrator is cold. The original `transform` is untouched; any calibrator lacking `transform_for`
falls back to `transform` in `pool.py` (defensive `getattr`). No fitted calibrator is applied until
a meaningful per-bucket sample exists (`Calibrator._MIN_FIT_SAMPLES`), so thin samples stay
passthrough-equivalent; `predict_proba` is clamped to [0,1].

---

## 5. Trust ledger output — DEFINED IN `arbiter/contract/seams.py`; produced by `arbiter/trust/ledger.py` (lane L11), consumed by fusion L10

```python
@dataclass(frozen=True)
class AdvisorWeight:
    advisor_id: str
    weight: float                   # LOG-POOL weight (NOT a simplex); 0.0 disables
    ci_low: float; ci_high: float
    shadow: bool                    # True = recorded, zero live weight (onboarding)

@dataclass(frozen=True)
class WeightBundle:
    weights: dict[str, AdvisorWeight]
    correlation_matrix: dict[tuple[str, str], float]   # ρij; default 0.5 prior when sparse
# Caps: ceiling 0.50; MiroFish hard cap 0.35 forever; negative-skill -> 0.0 + hold; floor 0.02.
# Phase 1 ships an EqualWeightBundle (all weights equal, empty corr matrix).
```

---

## 6. Outcome label — DEFINED IN `arbiter/contract/seams.py`; produced by `arbiter/evaluation/outcome_labeler.py` (lane L14), feeds L9 + L11

```python
@dataclass(frozen=True)
class ResolvedOutcome:
    idea_id: str
    advisor_id: str
    ticker: str
    alpha_bps: float                # SPY-beta-adjusted alpha over horizon, net modeled slippage (continuous; drives trust)
    binary: int                     # +1 / 0 / -1  (±25bps band -> 0 "no-call"); DISPLAY/calibration label
    advisor_confidence: float
    stance_score: float             # advisor's ACTUAL directional forecast in [-1,1]; Brier forecast (sub-project #5a)
    abstained: bool
    horizon_days: int
    label_kind: str                 # "normal" | "early_exit" | "reversal" | "corporate_event" | "partial"

# alpha_i = R_i(t0,t1) - beta_i * R_SPY(t0,t1); beta_i = 252d rolling as of t0-1 (impute 1.0+flag)
# entry = filing-date+1 OPEN, net modeled slippage.
```

---

## 7. Idea object — DEFINED IN `arbiter/contract/seams.py`; lifecycle in `arbiter/orchestrator/` (lane L13)

```python
@dataclass
class Idea:
    idea_id: str                    # ULID
    ticker: str
    thesis: str
    horizon_days: int
    state: IdeaState
    as_of: datetime                 # original information timestamp (passed to L14 on OUTCOME_READY)
    dedupe_key: tuple[str, str]     # (ticker, horizon_bucket.value)
    # FSM transitions enforced in lifecycle.py; concurrent ideas on one ticker in DIFFERENT buckets allowed.
```

---

## 8. Safety gate — `TradingDecision` DEFINED IN `arbiter/contract/seams.py`; logic in `arbiter/safety/` (lane L4), called by policy L12 before EVERY order

```python
@dataclass(frozen=True)
class TradingDecision:
    allowed: bool
    size_multiplier: float          # 1.0 normal, 0.25 DEGRADED (1 advisor), 0.0 HALTED
    level: DegradationLevel
    reasons: list[str]

def is_trading_allowed(account, *, live_advisor_count: int) -> TradingDecision: ...
# Quorum: 2+ live advisors -> 1.0; 1 -> 0.25 DEGRADED; 0 -> 0.0 HALTED.
# Latching circuit breakers (infra-level, cannot be cleared by advisor/fusion code):
#   daily loss >=2%, per-position -5% intraday, MiroFish 3x consecutive fail, A3 vol anomaly on held name,
#   any broker non-200, confidence-distribution shift >30%.
# Kill switch is BROKER-SIDE (works with python dead); blocks NEW orders; does NOT auto-close (v1).
```

---

## 9. Execution / policy — `PaperOrder` DEFINED IN `arbiter/contract/seams.py`; logic in `arbiter/policy/` + `arbiter/execution/` (lane L12)

```python
# Sizing: quarter-Kelly * hard caps * ADV cap (ADV cap is the LAST transform).
# Caps: 5% per name, 20% per sector, 80% gross, 20 open positions, 2%-of-20d-ADV liquidity cap.
# Idempotency: ULID primary key + dedup_hash UNIQUE = sha256(ticker+side+horizon+entry_date+advisor_sig).
#   Pre-submit check vs local ledger AND broker. Max 1 retry then halt+alert.
# Executors copied/adapted from stockbot/src: executor.py, sim_executor.py, alpaca_paper_executor.py.
# Executor selection (RECONCILED — `executor_backend`, NOT `live_trading`, picks the executor; see
#   `build_executor` in execution/alpaca_adapter.py):
#     executor_backend == "alpaca_paper" AND both Alpaca keys present -> AlpacaAdapter (paper endpoint only —
#       there is no live-money trading URL; the adapter is structurally paper-only).
#     otherwise (default executor_backend="sim", or keys missing) -> SimExecutor (fail-closed default).
#   `live_trading`/LIVE_TRADING is NOT consulted by `build_executor`; it stays false and is reserved for a
#   future real-money path that does not exist yet. (The §10b.2 ABC method names still apply.)
#   submit_order returns a SubmitResult(order_id|None, status, duplicate, zero_share); the
#   engine advances an idea -> MONITORED only on a confirmed fill (status=="filled"). Exits (stop/horizon/
#   conviction-reversal) stored txnally w/ position.
# Exit/sell monitor (sub-project #2): once per cycle the engine inspects open positions and fires a
#   full-exit SELL on stop-loss / horizon-expiry / conviction-reversal, then drives the idea
#   MONITORED->OUTCOME_READY->CLOSED with the REAL exit price + label_kind (stop->early_exit,
#   reversal->reversal, horizon->normal). Carve-out from "exits never revised upward": the monitor does
#   NOT trust the stored stop_loss (it is a phantom $100-derived value); it recomputes the stop LIVE,
#   in memory, each cycle from the broker avg_price (true cost basis) * the bucket stop-fraction. This
#   corrects a phantom value (there was never a real stop), is deterministic+idempotent, and is NOT a
#   loosening of a real stop. submit_order gains presized_shares (skip the A0 notional->shares divide;
#   size in held SHARES) and is_exit (local-ledger-only idempotency — the broker position-presence check
#   is a buy-side guard and would block every sell). model_slippage(price, spread, side) biases the SELL
#   limit DOWN. Paused/kill-switched/breaker-tripped engine does NOT sell (paused = no autonomous orders).
#   [Deliberate, documented amendment to the frozen interface for sub-project #2: exit/sell monitor.]

@dataclass(frozen=True)
class PaperOrder:
    order_id: str        # ULID
    dedup_hash: str
    ticker: str; side: OrderSide; qty: float
    horizon_bucket: HorizonBucket
    entry_date: date
    advisor_signature: str
    exits: dict          # {"stop_loss": float, "horizon_expiry": date, "conviction_reversal": float}
```

---

## 10. Storage — OWNED BY `arbiter/db/` (lane L2)

Single `arbiter.db` (SQLite WAL) + append-only `data/audit.jsonl` (authoritative on divergence).
**Two-tier mutation rule (RECONCILED — see §11.2 for the full list and the enforced allowlist).**
*Immutable FACT tables* (`opinions`, `filings`, `outcomes`, `trust_weights`, `advisor_registry`, audit
log) are **insert-only**: corrections = NEW rows with `supersedes_id`, and the only in-place UPDATE is the
`is_superseded` flag flip inside `supersede_row()`. *Mutable lifecycle/cache tables* (`ideas.state`,
`orders.status`/`orders.idea_id`, `engine_state`, `sim_positions`, `breaker_state`) use **sanctioned
in-place UPDATE/upsert** — they record current STATE, not history. Every lane that needs tables contributes
a numbered migration fragment to `arbiter/db/migrations/NNN_<lane>.sql`; the L2 runner applies them in order.

```python
# db/connection.py (L1 provides factory):  def get_connection() -> sqlite3.Connection   (WAL, row_factory)
# db/helpers.py (L2):
def insert_row(conn, table: str, row: dict) -> str: ...          # returns ULID pk
def supersede_row(conn, table: str, old_id: str, new_row: dict) -> str: ...
# db/audit.py (L2):  def audit(event: str, payload: dict) -> None   # append-only jsonl
```

Core tables (L2 owns base schema; lanes add migrations): `opinions`, `filings`, `ideas`,
`orders`, `outcomes`, `trust_weights`, `advisor_registry`, `breaker_state`, `audit_meta`.
Mutable lifecycle/cache tables added by later sub-projects: `sim_positions` (sim-cache snapshot,
migration `022_positions.sql`) and `engine_state` (durable `paused` flag, sub-project #3 amendment C4).

`orders.idea_id` (migration `023_orders_idea_id.sql`, sub-project #2 B5): an optional exact link from an
order row to its owning idea, populated at BUY submit time by the engine. The sell/close-out path prefers
it over the (ticker, horizon_bucket) join; legacy rows stay NULL and fall back to the join. The `orders`
table has NO `supersedes_id`/`is_superseded` columns, so exit-level correction is done in-memory each
cycle (B0), never by superseding an order row.

`opinions.idea_id` (migration `026_outcome_stance_attribution.sql`, sub-project #5a): the optional
opinion→idea link (nullable; legacy / abstain / source-overlap rows stay NULL). Mirrors the
`orders.idea_id` pattern; lets outcome attribution carry the advisor's per-idea stance (see
`ResolvedOutcome.stance_score`, §6).

---

## 10b. Foundation deviations — AUTHORITATIVE (locked by Wave-A scaffold; all lanes obey)

1. `bucket_for_days(days)` **raises `ValueError` for `days > 365`** — treat >365d horizons as invalid, do NOT clamp to LONG.
2. **Executor ABC method names** (in `arbiter/shared/executor.py`): `place(...)`, `cancel(...)`, `get_positions()`, `get_account()`. Lane 12 uses these exact names (NOT stockbot's submit/positions/account).
3. **`SimExecutor`** fills at the `limit_price` passed on the `OrderIntent` (no separate exec-price field). Lane 3 slippage must compute the adjusted price and pass it as `limit_price`.
4. **`MetricsWriter.record(...)`** needs a `recorded_at` timestamp from the Lane-3 clock; omitting it writes the sentinel `"CLOCK_NOT_WIRED"`.
5. **`Config` fields** (exact — RECONCILED to `arbiter/config.py`): `live_trading, executor_backend, db_path, audit_path, metrics_path, max_position_pct, max_sector_pct, max_gross_pct, max_open_positions, adv_cap_pct, alpaca_api_key, alpaca_secret_key, alpaca_paper_base_url, alpaca_data_base_url, alpaca_timeout, edgar_user_agent, kill_switch_url, alert_webhook_url` — **plus the defaulted growth fields**: `fast_interval_s, full_cycle_times_et, daemon_heartbeat_path` (sub-project #3 daemon, see below) and `trust_equal_floor` (sub-project #4 learning loop). Env overrides: `ARBITER_<FIELD_UPPER>` (Alpaca keys + `LIVE_TRADING` + `EXECUTOR_BACKEND` use their bare names; `trust_equal_floor` via `ARBITER_TRUST_EQUAL_FLOOR`). `executor_backend ∈ {sim, alpaca_paper}` (default `sim`; invalid value → `ConfigError`). `trust_equal_floor` (default `0.25`): probationary floor weight a cold/shadow advisor trades at; a FRACTION kept strictly below the ledger graduated ceiling (0.50) so a cold advisor can't reach parity. [Deliberate, documented amendment to the frozen interface for sub-project #1: real Alpaca paper execution; growth fields added by sub-projects #3/#4.]

   **Sub-project #3 (market-hours runtime daemon) adds three `Config` fields** under a new `[daemon]` TOML section (all defaulted so existing direct `Config(...)` constructions need no change): `fast_interval_s: float` (default `180.0`; env `ARBITER_FAST_INTERVAL_S`) — seconds between cheap fast iterations (reconcile + live-price stop/horizon checks) while the market is open; `full_cycle_times_et: str` (default `"09:45,15:30"`; env `ARBITER_FULL_CYCLE_TIMES_ET`) — comma-separated ET times to run the slow full cycle (ingest + entries + reversal; entries stay disclosure-cadence, NOT day-trading); `daemon_heartbeat_path: str` (default `"data/arbiter-daemon.heartbeat"`; env `ARBITER_DAEMON_HEARTBEAT_PATH`) — atomic liveness file rewritten each iteration. [Deliberate, documented amendment for sub-project #3.]

   **`CurrentPriceProvider` seam (sub-project #3, the PIT-purity boundary).** `arbiter.data.current_price.CurrentPriceProvider` exposes `current_price(ticker) -> float | None` (and a batched `current_prices(tickers)`) with **no `as_of`** — a live "now" price for the exit monitor's LIVE stop check ONLY. It is **NOT a PIT field** (`"current_price"` is not in `pit._SUPPORTED_FIELDS`), is **never registered with `PITGateway`**, and **never enters a backtest**: `build_engine` injects `NullCurrentPriceProvider` whenever the clock is a `BacktestClock` OR the backend is not `alpaca_paper`, and the live `AlpacaCurrentPriceSource` ONLY when the backend is `alpaca_paper` AND the clock is the live `Clock` (amendment C0). The daemon runtime (`arbiter daemon`, `com.arbiter.daemon.plist`, `KeepAlive=true`) loops fast iterations while open and long-sleeps while closed; the durable `engine_state` table persists the `paused` flag (amendment C4).
6. `get_connection()` enables WAL + `row_factory=sqlite3.Row` + FK on.

## 11. Conventions (enforced; CI greps for violations)

1. No `get_latest()`; no `datetime.now()` outside `clock.py`. All reads via `PITGateway.get`.
2. **Two-tier insert-only rule (RECONCILED — superseded the old "the ONLY in-place UPDATE is
   `supersede_row`" claim).** Enforced by `scripts/check_insert_only.sh` (AST-based; greps `.execute*`
   call sites, not docstrings/prose).
   - **Immutable FACT tables stay insert-only:** corrections via new rows + `supersedes_id`; the only
     history-preserving in-place UPDATE is the `is_superseded=1` flip in `db/helpers.py`
     (`supersede_row`/`supersede_rows`). `evaluation/outcome_store.py` and `ingest/writer.py` correct via
     `supersede_row`, so they carry no raw mutation of their own.
   - **Mutable lifecycle/cache tables use sanctioned in-place UPDATE/upsert** — 6 explicitly allowlisted
     carve-outs (the `ALLOWLIST` in `scripts/check_insert_only.sh`, or a `# insert-only-ok` marker on the
     call line for new sites):
       1. `db/helpers.py` — `UPDATE … SET is_superseded=1` (the supersede flag flip itself).
       2. `execution/position_store.py` — `DELETE FROM sim_positions` + upsert (sim-cache snapshot, rebuilt
          each persist; state-not-history).
       3. `engine.py` — `UPDATE orders SET status=…` (pending → filled/partial promotion).
       4. `engine.py` — `UPDATE orders SET idea_id=…` (idea_id back-fill after submission).
       5. `orchestrator/idea_store.py` — `UPDATE ideas SET state=…` (FSM lifecycle transition).
       6. `engine_state` (paused flag) and `breaker_state` use the insert-shaped
          `INSERT … ON CONFLICT DO UPDATE` upsert, which is NOT matched by the raw-mutation patterns and
          needs no allowlist entry.
3. Abstain = `None`. Abstaining opinions excluded from the pool.
4. Fail-closed: `LIVE_TRADING=false` default; gate unreachable -> no trade; missing ADV -> size 0.
5. MiroFish over local HTTP only (never `import mirofish` — AGPL); egress firewalled to filing data.
6. Tests live in `arbiter/tests/` mirroring the package; each lane owns its subdir. Use `pytest`.
7. Python 3.11+, `from __future__ import annotations`, type hints, `structlog`. No network in unit tests (mock).

---

## 12. Code docstrings to fix (for the engine owner)

This WP is **docs-only** and did not edit code. The following docstrings still assert the OLD
"`LIVE_TRADING` selects the executor" rule, which is stale — executor selection is driven by
`executor_backend` (see §9 and `build_executor`, whose own docstring is already correct). The
engine owner should reconcile them:

1. **`arbiter/engine.py:10`** (module docstring, "Paper-only guarantee"): "The `SimExecutor` is always
   used unless LIVE_TRADING=true (which is never set …)". → Should say the executor is `SimExecutor`
   unless `executor_backend == "alpaca_paper"` (+ both Alpaca keys); `live_trading` is never consulted
   for executor selection.
2. **`arbiter/execution/alpaca_adapter.py:3`** (module docstring): "Selected ONLY when `LIVE_TRADING=true`
   AND both `alpaca_api_key` and …". → Should say "Selected ONLY when `executor_backend == "alpaca_paper"`
   AND both Alpaca keys present" (matches the already-correct `build_executor` docstring at
   `alpaca_adapter.py:366` and the §10b.2 note).
