# Market-Hours Intraday Runtime Loop — Design Spec (sub-project #3)

> Status: 2026-06-19. DESIGN ONLY — no implementation code is written by this spec author.
> Build agent: implement strictly from this document, plan→build→audit with disjoint file
> ownership, tests OFFLINE (injected clock + injected market-clock + fake data + injected sleep,
> no network in pytest), then verify on a live `arbiter run` / `arbiter daemon`. Always use
> `.venv/bin/python` from `/Users/jonathanmorris/poly_bot/arbiter`.
>
> Scope: make the autonomous (paper) trader run **while US markets are open** and check
> stop-losses / reconcile pending fills **more than once per day**, instead of the single daily
> launchd one-shot at 18:30 ET. The crux is giving the exit monitor a **current/last price** for
> LIVE stop checks WITHOUT corrupting the daily-only PIT path that entries, outcome labeling, and
> backtests depend on. Builds on sub-project #1 (real Alpaca paper execution) and #2 (exit/sell
> monitor), both BUILT.

---

## 0. POST-AUDIT BINDING AMENDMENTS (these SUPERSEDE any conflicting text below)

The plan audit returned **GO-WITH-AMENDMENTS**. These are binding; do not relitigate.

**C0 — [P0] Gate the live price provider on the CLOCK TYPE, not just the backend.** Selecting the live
`AlpacaCurrentPriceSource` on `executor_backend == "alpaca_paper"` alone leaks a live "now" price into
BACKTESTS run in the operator's shell (which has `EXECUTOR_BACKEND=alpaca_paper` in `.env`). `build_engine`
MUST inject `NullCurrentPriceProvider` whenever `isinstance(clock, BacktestClock)` — regardless of
`executor_backend` — AND only inject the live source when `executor_backend == "alpaca_paper"` AND the clock
is the live `Clock`. The backtest runner should additionally assert the provider is Null. The PIT-purity test
MUST cover the case "backtest config WITH `executor_backend=alpaca_paper` → Null provider" (not just sim).

**C1 — [P1] Intraday price reads use the MULTI-symbol latest-trades endpoint as the v1 default.** Use
`GET /v2/stocks/trades/latest?symbols=A,B,C` (feed=iex) — ONE call for all held tickers per iteration — not N
per-symbol calls. (The rate budget in the spec assumed 8–10 positions; the real `max_open_positions` default is
20, so per-symbol reads are 2× the assumed load.) Cache `GET /v2/clock` (refetch ~once per session boundary, not
every iteration). This makes cadence safety independent of position count.

**C2 — [P1] Map terminal broker order states to a terminal LOCAL status.** `_reconcile_pending_orders` MUST map
broker `expired` / `canceled` / `rejected` (e.g. a `day` order unfilled at close) to a terminal local order
status (e.g. `expired`/`canceled`) so the row is no longer selected as `pending` and re-queried every iteration
forever. An expired BUY does NOT advance its idea (stays pre-MONITORED); an expired SELL leaves the idea
MONITORED for a later re-attempt. Audit each terminal transition.

**C3 — [P1] Offline calendar degraded-fallback warning.** `OfflineMarketCalendar` reuses
`replay_clock` holidays, whose curated list ENDS in 2026; the daemon runs past that. It MUST log a loud WARNING
when `now.year` exceeds the curated holiday range (the fallback is then unreliable), and the spec/SETUP must list
"refresh the holiday list yearly" as a must-do. Prefer Alpaca `/v2/calendar` as the live source of truth.

**C4 — [P1] Persist the `paused` flag durably.** `engine.paused` is in-memory; with `KeepAlive=true`
auto-relaunch, an auto-pause that is NOT backed by a latched breaker (e.g. a broker-fatal SELL rejection that
sets `paused` via the alerting sentinel) is silently lost on crash/relaunch → the daemon resumes trading after a
fatal condition. Persist the paused state (a small durable row — reuse an existing table or add one) and restore
it on `build_engine`/daemon start. This is a v1 must-fix for an auto-relaunching unattended daemon.

**C5 — Factor a shared safety gate.** Extract the `run_cycle` gate block (paused → kill-switch → breaker, each
early-returning) into a single `_safety_gate(now) -> CycleResult | None` used by BOTH `run_cycle` and the new
`run_fast_iteration`, so they cannot drift. Add a test that a closeout performed on a FAST iteration writes a
complete, non-duplicated outcome row (the full-cycle outcome sweep does not run on fast iterations).

**C6 — Resolve the daemon vs 18:30 one-shot overlap.** Under the single-instance flock, the 18:30 backstop
no-ops when the daemon holds the lock, so its post-close sweep never runs. Decision: the DAEMON owns a
post-close sweep + final reconcile at the open→closed transition; the 18:30 launchd one-shot is downgraded to a
pure "daemon was down" fallback (flock-guarded, idempotent). Document this explicitly.

**C7 — Startup & ops edges.** (a) Partial-day startup: if the daemon starts mid-session after a configured
full-cycle time has passed AND no full cycle ran today, run ONE full cycle on startup; do NOT fire every missed
slot. (b) The daemon installer MUST `mkdir -p data/` (launchd won't create it), mirroring `scripts/schedule.sh`.

**Build structure:** #3 adds new modules (daemon loop, market calendar, current-price source) but also edits the
shared `engine.py` (run_fast_iteration, gate factor, provider injection, reconcile terminal states) and
`config.py`. Build with ONE focused build agent (TDD); audit follows as a separate lane.

---

## 0. BINDING CONSTRAINTS (carried from #1 / #2 / Phase-2 — do not relitigate)

- **PIT purity / no look-ahead (INTERFACES §3, §11.1).** `PITGateway.get(field, ticker, as_of)`
  NEVER returns data whose information timestamp is after `as_of`. No `get_latest()`; no
  `datetime.now()` outside `clock.py`. The no-look-ahead lint (`scripts/check_no_lookahead.sh`)
  enforces this via AST. **The intraday "current price" capability in this spec MUST NOT be exposed
  through `PITGateway.get` and MUST NOT be reachable from any backtest path** (§Decision 1).
- **Clock injection (INTERFACES §3, §11.1).** The ONLY source of "now" is `clock.now()`. The loop,
  the market-calendar provider, and the current-price provider all take the clock by injection and
  must be testable with `BacktestClock`/fakes — no real wall-clock sleeps, no network, in pytest.
- **Paper-only floor (#1 §2).** The system is structurally paper-only; `AlpacaAdapter._base()`
  returns ONLY `config.alpaca_paper_base_url`. This spec adds NO live endpoint and MUST NOT weaken
  that. `alpaca_data_base_url` (`data.alpaca.markets`) is market DATA, not trading.
- **Entries remain disclosure-cadence (slow).** #3 does NOT turn the bot into a day-trader. Signals
  (Form 4 / Congress disclosures) update on a multi-day lag; new-idea generation stays daily. #3
  adds a FREQUENT loop ONLY for the cheap, time-sensitive work: stop-loss checks + pending-fill
  reconcile. (#2 Decision 5 named this once-a-day cadence "the single most important limitation";
  this spec closes it for stops, not for entries.)
- **Paused = no autonomous orders (#2 Dec 6, INTERFACES §8).** A paused / kill-switched /
  breaker-tripped engine does NOT trade — including protective sells. The loop keeps polling so it
  resumes cleanly when the operator clears the condition, but it never trades while halted.
- **Tests stay OFFLINE and the suite stays green** (~1743+). Live runs are the human verification
  step, never pytest.

---

## 1. Goal

Replace the single daily 18:30-ET one-shot with a **market-hours-aware runtime** so that, while
the US equity market is open, the engine:

1. Checks stop-losses against a **current (intraday) price**, not yesterday's daily close.
2. Reconciles pending broker fills (BUY → MONITORED, SELL → close-out) every few minutes, so an
   order that fills mid-session is recognized the same session, not the next day.
3. Sleeps efficiently between iterations and shuts down gracefully at market close.
4. Survives a bad iteration (catch/log/continue with backoff) — one error never kills the loop.
5. Continues running the slow, daily work (ingest + new-idea/entry generation) at most a few times
   per day, NOT on every fast iteration (rate-limit discipline).

The crux (Decision 1): the exit monitor's stop check currently reads `pit.get("price_close", …)`
which is a DAILY bar — so an intraday loop would still compare against yesterday's close and the
whole point is lost. #3 gives the monitor a **live current price** that is explicitly OUTSIDE the
PIT/backtest path.

---

## 2. Current state (VERIFIED against source)

### 2.1 Price feed is DAILY-only
- `AlpacaPriceSource.bars()` (alpaca.py:108) fetches `timeframe="1Day"` bars from
  `/v2/stocks/{ticker}/bars`. Its `get_pit()` adapter (alpaca.py:205) returns the **latest daily
  bar at or before as_of** for `price_open`/`price_close`/`spread`. There is **NO** intraday /
  latest-trade / latest-quote source anywhere.
- `PITGateway` (pit.py:191) supports fields `price_open, price_close, adv_20d, beta_252d, spread,
  filing, news, trust`. All price fields resolve to daily bars. `build_price_gateway(config)`
  (_gateway.py:84) wires Alpaca (primary) + Stooq (fallback) behind a `_FallbackPriceAdapter`.
- **The exit monitor stop check** (exit_monitor.py:551): `px = pit.get("price_close", ticker, now)`
  with a `price_open` fallback — both DAILY. So in a market-hours loop the stop would be evaluated
  against the prior session's close. This is the gap #3 must close.

### 2.2 Market-hours check is COARSE
- `engine._us_market_open(now)` (engine.py:93): converts to a hardcoded `UTC-5` (EST) offset, checks
  weekday < 5 and `09:30 ≤ minutes < 16:00`. It is **warning-only** (engine.py:555 logs
  `market_closed`), and the docstring states it deliberately ignores **DST and holidays** and that a
  real scheduler is "out-of-scope (#3)". So today: wrong by an hour during EDT (Mar–Nov), wrong on
  every market holiday, and wrong on half-day early closes. It must be replaced for a loop that
  decides when to run.
- The offline backtest calendar (`replay_clock._is_trading_day`, replay_clock.py:129) has a curated
  hardcoded holiday list through 2026 and skips weekends. It is deterministic but does NOT model
  intraday open/close times or early-close days. It is the right offline FALLBACK source but not the
  live source of truth.

### 2.3 The loop is a one-shot, not a daemon
- `orchestrator/loop_runner.py::main()` builds the engine and calls `run_once(ingest_fn, cycle_fn,
  clock)` — ingest then ONE `engine.run_cycle`, then exits. `run_once` isolates ingest faults (logs,
  records, still runs the cycle) but does NOT loop and does NOT swallow cycle exceptions.
- `arbiter run` (cli.py:138) wires to `loop_main()`. `deploy/com.arbiter.daily.plist` fires it once
  per weekday at 18:30 local (machine TZ = America/New_York) with `KeepAlive=false`,
  `RunAtLoad=false`. `scripts/schedule.sh` installs/uninstalls/status/run-now.

### 2.4 `run_cycle` already does the right internal ordering (engine.py:461)
Per cycle, in order: auto-pause/kill-switch/breaker gates (early return) → `_reconcile_pending_orders`
(alpaca_paper only) → account read + A2 fail-closed → market-closed WARNING → gather opinions once →
**`_run_exit_monitor`** (stops/horizon/reversal) → entries (new BUYs) → SimExecutor snapshot →
`run_outcome_sweep`. The exit monitor and reconcile are already factored as methods. **#3 does NOT
re-architect `run_cycle`; it introduces a loop ABOVE it and a way to run a CHEAP subset of it
frequently** (Decision 4).

### 2.5 Alpaca latest-price + clock/calendar endpoints exist (broker, not yet wired)
- Data API (`alpaca_data_base_url`, `data.alpaca.markets`): `GET /v2/stocks/{ticker}/trades/latest`
  and `GET /v2/stocks/{ticker}/quotes/latest` (and `/bars/latest`) return the most recent
  trade/quote — gated by `feed=iex` on the free plan (same constraint AlpacaPriceSource already
  handles via `ALPACA_DATA_FEED`, default `iex`).
- Trading API (`alpaca_paper_base_url`, `paper-api.alpaca.markets`): `GET /v2/clock` →
  `{is_open, next_open, next_close, timestamp}` and `GET /v2/calendar` → list of
  `{date, open, close}` (handles DST, holidays, half-days/early closes — Alpaca returns the actual
  session open/close, e.g. `13:00` on early-close days).
- `AlpacaAdapter` (alpaca_adapter.py:91) already has injectable `http_get`/`http_post`/`http_delete`
  callables and a `_base()` for the trading host. Config has `alpaca_data_base_url`,
  `alpaca_paper_base_url`, `alpaca_timeout`, both keys.

---

## 3. Design decisions

### Decision 1 — Intraday "current price" for LIVE stop checks (THE CRUX)

**Goal.** Give the exit monitor a CURRENT price for its stop-loss comparison in `alpaca_paper`
mode, distinct from the daily `price_open`/`price_close` used for entries and outcome labeling, and
provably OUTSIDE the PIT / backtest path.

**Chosen approach: a separate `CurrentPriceProvider` Protocol, injected into the exit monitor,
NEVER routed through `PITGateway`.**

#### 1a. The seam
- **NEW module `arbiter/data/current_price.py`** defining:
  ```
  class CurrentPriceProvider(Protocol):
      def current_price(self, ticker: str) -> float | None: ...
  ```
  - There is **no `as_of` parameter** — that is the whole point: a current-price read is
    legitimately "now" (the wall-clock-now provided by `clock.now()` is implicit and irrelevant to
    the read; the broker returns its own latest tick). Omitting `as_of` makes it **structurally
    impossible** to misuse this as a historical PIT read and keeps it off the `PITGateway.get(field,
    ticker, as_of)` surface entirely.
  - Returns `None` when no current price is available (stale/closed market/unknown ticker) →
    monitor fails closed (no stop fire on missing data — exactly the existing `current_price is None`
    branch in `evaluate_triggers`).
- **`AlpacaCurrentPriceSource(config)`** in the same module implements the Protocol by calling the
  Alpaca **data** API `GET /v2/stocks/{ticker}/trades/latest?feed=<ALPACA_DATA_FEED|iex>` (fall back
  to `/quotes/latest` mid = (bid+ask)/2 if no recent trade). It uses an **injectable `http_get`
  callable** (default a thin `httpx` shim mirroring `AlpacaPriceSource`) so pytest injects a fake
  and never hits the network. Timeout = `config.alpaca_timeout`.
- **`NullCurrentPriceProvider`** (always returns `None`) is the default in `sim` mode and in
  backtests — the sim/backtest exit monitor keeps using the DAILY PIT close exactly as today (no
  behavior change in sim or backtest).

#### 1b. How the exit monitor consumes it (the ONLY consumer)
- Add an optional parameter to `run_exit_monitor(...)`:
  `current_price_provider: CurrentPriceProvider | None = None`.
- In the per-position loop (exit_monitor.py:551), the price used for the **stop check** becomes:
  ```
  current_px = None
  if current_price_provider is not None:
      current_px = current_price_provider.current_price(ticker)   # LIVE "now" — NOT PIT
  if current_px is None:
      # daily PIT fallback (today's behavior) — used in sim/backtest and when the live read fails
      px = pit.get("price_close", ticker, now) or pit.get("price_open", ticker, now)
      current_px = float(px) if px is not None else None
  ```
  This passes `current_px` into `evaluate_triggers(current_price=current_px, …)` unchanged.
- **`evaluate_triggers` is untouched** — it already takes `current_price: float | None` and fails
  closed on `None`. Only the *source* of that float changes (live in alpaca_paper, daily-PIT in
  sim/backtest).
- **Entries and outcome labeling are UNCHANGED.** The entry BUY price (`_bound_submit` reads
  `pit.get("price_open", …)`) and the labeler's alpha computation (entry-open / SPY / beta via PIT)
  stay on the daily PIT path. The live current price is used ONLY for the stop-loss trigger
  comparison, never persisted as a fill price, never fed to the labeler, never written to PIT.

#### 1c. Why this cannot corrupt PIT / backtests
- `CurrentPriceProvider` is **not** a PIT field and is **not** registered with `PITGateway`. There
  is no `register_source` call, so `pit.get(...)` can never return it. The no-look-ahead AST lint
  greps for `datetime.now()` and `get_latest()`; the provider has neither (it uses no clock, no
  `as_of`).
- The provider is injected by `build_engine` ONLY when `executor_backend == "alpaca_paper"`. In
  `sim` mode and in **every backtest** (`evaluation/backtest/runner.py`, which uses `BacktestClock`
  and the daily PIT), the provider is `None`/`NullCurrentPriceProvider` → the monitor falls back to
  daily PIT and behaves exactly as it does today. A backtest therefore **cannot** see a live "now"
  price; the wiring guarantees it.
- Add a **lint/audit assertion**: the build agent adds a test asserting (a) `"current_price"` is NOT
  in `pit._SUPPORTED_FIELDS`; (b) `build_engine` returns a `NullCurrentPriceProvider` (or `None`) for
  `sim` and any backtest config; (c) `current_price.py` contains no `datetime.now()` /
  `get_latest()`. This is the structural guard on the PIT-purity boundary.

#### 1d. Files
- NEW `arbiter/data/current_price.py` (`CurrentPriceProvider` Protocol, `AlpacaCurrentPriceSource`,
  `NullCurrentPriceProvider`).
- `arbiter/execution/exit_monitor.py::run_exit_monitor` — add `current_price_provider` param +
  the fallback ladder above. (Pure `evaluate_triggers` unchanged.)
- `arbiter/engine.py::build_engine` — construct the provider (Alpaca in alpaca_paper, Null
  otherwise) and store on `Engine`; `Engine._run_exit_monitor` passes it through.

> **Net:** a single new accessor `current_price(ticker)` — no `as_of`, never on the PIT surface,
> injected only in alpaca_paper, Null in sim/backtest. The daily PIT path is untouched and the
> no-look-ahead guarantee is structurally preserved.

---

### Decision 2 — Authoritative market-hours / calendar

**Chosen approach: a `MarketCalendar` Protocol with two implementations — Alpaca clock/calendar
(live source of truth) and a deterministic offline calendar (sim/tests/fallback) — injected into
the loop.** Replace `_us_market_open` for loop-scheduling decisions.

#### 2a. The Protocol
- **NEW module `arbiter/runtime/market_calendar.py`** defining:
  ```
  @dataclass(frozen=True)
  class MarketSession:
      is_open: bool
      next_open: datetime | None     # tz-aware UTC
      next_close: datetime | None    # tz-aware UTC

  class MarketCalendar(Protocol):
      def session(self, now: datetime) -> MarketSession: ...
  ```
  `session(now)` takes the injected `clock.now()` so it is testable with a frozen clock — no
  internal wall-clock read.

#### 2b. `AlpacaMarketCalendar` (live source of truth, alpaca_paper)
- Calls the trading API `GET /v2/clock` for `is_open`/`next_open`/`next_close`, via an **injectable
  `http_get`** (default the `AlpacaAdapter`'s shim; pytest injects a fake). This is authoritative for
  DST, holidays, and **early-close days** (Alpaca's `/v2/clock` reflects the real session boundaries;
  `next_close` on a half-day returns the actual early close, e.g. 13:00 ET).
- **Caching to respect rate limits:** cache the last `MarketSession` and only re-fetch when
  `now >= cached.next_close` (or the cache is older than a small TTL, e.g. 60s, whichever is
  sooner). `/v2/clock` is then hit O(once per session boundary), not every loop iteration.
  Optionally pre-fetch the day's `/v2/calendar` once at loop start to know the exact close for the
  graceful-shutdown decision; `/v2/clock` alone is sufficient for v1.
- On a fetch error: fall back to the offline calendar for THIS query (fail-safe — never crash the
  loop on a clock-API blip), and log a warning.

#### 2c. `OfflineMarketCalendar` (sim / tests / fallback)
- Deterministic, no network. Reuses `replay_clock._is_trading_day` for the holiday/weekend check and
  adds **regular-session hours** in US/Eastern with proper DST handling via `zoneinfo`
  (`ZoneInfo("America/New_York")`), NOT a hardcoded `UTC-5` offset (that is the `_us_market_open`
  bug). Regular session 09:30–16:00 ET; computes `is_open`/`next_open`/`next_close` from `now`.
- Half-day early closes (e.g. day-after-Thanksgiving, Christmas Eve 13:00 ET) are encoded in a small
  curated `_EARLY_CLOSE` map alongside the existing holiday list. Documented as best-effort for the
  offline path; the LIVE path uses Alpaca, which is always correct.
- This is the v1 replacement for `_us_market_open`. The engine's warning-only `_us_market_open` may
  be retired in favor of `OfflineMarketCalendar.session(now).is_open`, OR left as-is (it is only a
  log warning and harmless); the build agent should redirect the engine.py:555 warning to the
  injected calendar for consistency, but this is not load-bearing.

#### 2d. Injection
- `build_engine` (or the new daemon entrypoint) selects: `AlpacaMarketCalendar` when
  `executor_backend == "alpaca_paper"`, else `OfflineMarketCalendar`. The loop receives the calendar
  by injection (tests inject a fake `MarketCalendar` returning scripted sessions).

#### 2e. Files
- NEW `arbiter/runtime/market_calendar.py` (Protocol + both implementations).
- `arbiter/data/replay_clock.py` — expose `_is_trading_day` / the holiday set for reuse (it is
  already importable; add an `_EARLY_CLOSE` map or keep that in the new module).
- `arbiter/engine.py` — optionally redirect the `_us_market_open` warning to the calendar.

---

### Decision 3 — Runtime architecture: long-running daemon (RECOMMENDED) vs frequent cron

**Recommendation: a long-running daemon (`arbiter daemon`) launched once by launchd with
`KeepAlive=true`, that internally loops while the market is open and sleeps (a long sleep) while
closed.** Not a per-N-minutes launchd cron of short-lived processes.

**Rationale / trade-offs:**

| Factor | Daemon (`KeepAlive=true`) — CHOSEN | Frequent cron (`StartInterval`, short-lived) |
|---|---|---|
| Process startup cost | Pay once; `build_engine` (migrations, gateway, calendar fetch) runs once | Pays full `build_engine` + connection + migration churn every N min — wasteful, more SQLite open/close |
| Market-calendar caching | Cache lives in-process across iterations → O(1 clock fetch per boundary) | Each process re-fetches `/v2/clock` → more API calls (rate-limit pressure) |
| Sleep precision | Loop sleeps exactly until next iteration / next_open | launchd `StartInterval` only; can't easily "sleep until next_open" — fires uselessly all night unless it self-exits-when-closed (extra logic in every invocation) |
| Crash recovery | `KeepAlive=true` → launchd relaunches a crashed daemon automatically | Each tick is independent; a crash just skips one tick |
| State safety | Already restart-safe: Phase-2 persistence + reconcile-on-cycle make a relaunch idempotent (no double-buy; pending fills reconcile) | Same (state is durable) |
| Heartbeat / "is it alive?" | Natural: emit a heartbeat log each iteration | Harder: only logs when a tick fires |
| macOS fit | `KeepAlive=true` + `RunAtLoad=true` is the canonical long-running-agent pattern | `StartInterval`/`StartCalendarInterval` is the canonical cron pattern |

Crash recovery is the deciding factor *in favor of the daemon* combined with `KeepAlive=true`:
launchd restarts a dead daemon, and because state is durable + the cycle reconciles on start, a
relaunch is safe. The daemon also gives us cheap in-process calendar caching and precise
sleep-until-next-open, which directly serves the rate-limit goal (Decision 4).

**The daemon loop (pseudo-structure, in a NEW module — see Decision 5 for resilience):**
```
def run_daemon(engine, calendar, *, sleep_fn, stop_event, fast_interval_s, full_times):
    while not stop_event.is_set():
        now = engine.clock.now()
        session = calendar.session(now)
        try:
            if session.is_open:
                _maybe_run_full_cycle(engine, now, full_times)   # ingest+entries, gated (Dec 4)
                engine.run_fast_iteration(now)                   # reconcile + exit-monitor (Dec 4)
                heartbeat(now, session)
                sleep_fn(fast_interval_s)                        # e.g. 60–300s
            else:
                heartbeat(now, session)
                sleep_fn(_until_next_open_or_cap(now, session))  # long sleep, capped (Dec 5)
        except Exception as exc:        # one bad iteration must NOT kill the loop
            log.error("daemon.iteration_failed", error=str(exc)); backoff(); 
```
`sleep_fn` and `stop_event` are injected (Decision 6) so pytest drives the loop deterministically
with zero real time elapsed.

**Files:** NEW `arbiter/runtime/daemon.py` (the loop), NEW `arbiter daemon` CLI command
(cli.py), NEW `deploy/com.arbiter.daemon.plist` (`KeepAlive=true`, `RunAtLoad=true`).

---

### Decision 4 — Cadence: decompose the work; what runs how often

Three classes of work, three cadences. The key new idea: split `run_cycle` into a **FAST iteration**
(cheap, frequent) and a **FULL cycle** (slow, infrequent), without re-architecting the existing
ordering.

| Work | Cost | Cadence | Why |
|---|---|---|---|
| **Ingest** (Form 4 / Congress network pulls) | High (network, slow) | **~Daily**, near the start of the trading day (e.g. 1× at first open iteration) — and optionally once mid-day | Disclosures lag by days; pulling every few minutes is wasteful and rate-limited. |
| **Entries / new-idea generation** (detect_signals → opinions → fuse → decide → BUY) | Medium | **1–3× per day** at configured times (`full_times`, e.g. 09:45 + 15:30 ET) | Signals update slowly (disclosure cadence — binding constraint). Entries are NOT day-trading. |
| **Pending-fill reconcile + exit-monitor stop checks** | **Low** (broker reads + a current-price read per held name) | **Every N minutes intraday** (`fast_interval_s`, default 60–300s) | Time-sensitive: catch a fill mid-session, enforce a stop against the LIVE price. This is the whole point of #3. |

**Implementation — add two methods to `Engine` (keep `run_cycle` intact):**
- `Engine.run_fast_iteration(now)` — runs ONLY: the gate checks (paused/kill-switch/breaker →
  early-return, same as `run_cycle`), `_reconcile_pending_orders(now)` (alpaca_paper), the
  account/A2 fail-closed read, and `_run_exit_monitor(now, opinions=[])`. **No ingest, no
  detect_signals, no entries, no full opinion gather, no outcome sweep.** Because the exit monitor's
  conviction-reversal trigger needs fresh opinions and a fast iteration gathers none, **reversal
  simply does not fire on a fast iteration** (consistent with #2 Dec 2b: "absence of a fresh opinion
  is not a reversal"). Stop-loss (live price) and horizon-expiry (date-only) DO fire on every fast
  iteration — which is exactly what we want. Pass `stance_by_ticker={}` so reversal is inert.
- `Engine.run_full_cycle(now)` — the EXISTING `run_cycle(as_of=now)` unchanged (ingest is still
  driven by the loop_runner wrapper / daemon separately; see below). It gathers opinions, runs
  entries, runs the exit monitor *with* fresh opinions (so reversal can fire), snapshots, and runs
  the outcome sweep. This is what runs 1–3×/day.
- **Ingest** stays in the `loop_runner`/daemon wrapper (it is not part of `run_cycle` today —
  `loop_runner.main` calls `run_ingest` then `run_cycle`). The daemon calls ingest at most
  ~daily/mid-day, then a full cycle.

**Rate-limit discipline:**
- Fast iteration broker calls: `get_account` (1), `get_positions` (1), `get_order` per pending order
  (small N), `current_price` per held name (≤ `max_open_positions`, default 8–10 in #1's $10k
  config). At a 60s interval over a 6.5h session that is well within Alpaca's per-minute limits
  (200/min on the free plan). The market-calendar fetch is cached (Decision 2b).
- Make `fast_interval_s` and `full_times` config-driven (`ARBITER_FAST_INTERVAL_S`,
  `ARBITER_FULL_CYCLE_TIMES_ET`) so the operator can throttle. Default `fast_interval_s=180` (3 min)
  — frequent enough for paper stops, gentle on the API.

**Should the daily 18:30 one-shot be kept?** **Replace it for normal operation, keep it as a
belt-and-suspenders backstop.** The daemon handles intraday. But a single **post-close one-shot**
(the existing plist) is still valuable as: (a) a fallback if the daemon was down all day, and (b) the
clean place to run the **end-of-day outcome sweep + a final reconcile** after the session closes and
all fills have settled. Recommendation: keep `com.arbiter.daily.plist` running `arbiter run` at
18:30 (idempotent: reconcile + sweep + no double-buy via Phase-2 dedup), and ADD the daemon for
intraday. Document that running both is safe because every path is idempotent and state is durable.

**Files:** `arbiter/engine.py` (`run_fast_iteration`, optionally `run_full_cycle` alias);
`arbiter/runtime/daemon.py` (cadence scheduling: tracks `last_ingest_date`, `last_full_cycle_time`,
decides each iteration whether a full cycle / ingest is due).

---

### Decision 5 — Resilience for an unattended process

The daemon trades a real (paper) account unattended; one bad iteration must never kill it.

- **Catch/log/continue with backoff.** The loop body is wrapped in `try/except Exception`
  (broad, deliberate). On any iteration error: log `daemon.iteration_failed` with structured context,
  **increment a backoff** (exponential, capped, e.g. 30s → 60s → … → 600s), continue. On a clean
  iteration, reset backoff to `fast_interval_s`. A `BrokerError`/auth failure does not crash — it is
  logged and retried with backoff (and the engine's own auto-pause may latch independently).
- **Kill-switch + breakers consulted EVERY iteration via the engine gates.** `run_fast_iteration`
  and `run_full_cycle` both start with the existing gate block (engine.py:485–524): paused →
  early-return; kill-switch halted → fire critical alert + pause + return; any breaker tripped →
  pause + return. **Critically: a halted/paused engine returns early but the LOOP KEEPS POLLING** —
  it does not exit. When the operator clears the pause (`engine.resume()`) or the kill switch flips
  back to `halted:false`, the very next iteration trades again. (Note: `engine.paused` is in-memory;
  a daemon restart clears it — acceptable, because the underlying breaker state is durable in
  `breaker_state` and re-trips on the next gate check, and the kill switch is re-read live.)
- **Graceful shutdown at market close.** When `session.is_open` flips false, the daemon does NOT
  exit; it runs one final reconcile (catch late fills), emits a `daemon.session_closed` heartbeat,
  and **long-sleeps until `next_open`** (capped, see below). At `next_open` it resumes. The process
  stays alive across the overnight gap (cheap — it is sleeping).
- **Capped long sleep.** Never sleep a raw multi-hour `next_open - now` in one call (a clock skew or
  a missed wake is unrecoverable). Cap each sleep at e.g. 15 min and re-evaluate the session each
  wake. This also lets a `stop_event` (SIGTERM/SIGINT handler) interrupt promptly.
- **Signal handling.** Install SIGTERM/SIGINT handlers that set the injected `stop_event`; the loop
  checks it each iteration and exits cleanly (so `launchctl bootout` / Ctrl-C stops it without a
  kill -9). On exit, run a final reconcile + flush logs.
- **Structured logging + heartbeat.** Every iteration emits a `daemon.heartbeat` structlog line with
  `{now, is_open, next_open, next_close, mode, open_positions, paused, backoff_s,
  iteration_kind: fast|full}`. Optionally write a `data/arbiter-daemon.heartbeat` file (atomic
  rewrite each iteration) so the operator / a watchdog can check liveness without parsing logs. The
  existing `ALERT_WEBHOOK_URL` (#1 §6) still delivers critical auto-pause alerts.
- **Single-instance guard.** A pidfile/flock at `data/arbiter-daemon.pid` so two daemons (or a daemon
  + the daily one-shot overlapping) cannot both drive the engine concurrently against the same
  SQLite DB. If the lock is held, the second process exits with a clear log. (launchd `KeepAlive`
  already serializes a single label, but the daily one-shot is a *different* label — the lock is the
  cross-label guard. SQLite WAL tolerates concurrent readers but the engine's mutate paths should not
  interleave.)

**Files:** `arbiter/runtime/daemon.py` (loop, backoff, signal handlers, heartbeat, pidfile);
heartbeat file path under `data/`.

---

### Decision 6 — Offline testability (injection seams)

Hard rule (INTERFACES §11.7): pytest never hits the network and never sleeps on the real wall clock.
The seams:

- **`clock`** — `BacktestClock` (already exists). Drives `now()` deterministically; `advance()`
  steps simulated time between iterations.
- **`sleep_fn: Callable[[float], None]`** — injected into the daemon (default `time.sleep`). In
  tests, a fake that records the requested durations and, on each call, **advances the
  `BacktestClock`** by that duration and optionally sets `stop_event` after N iterations. Zero real
  time elapses. (This is the standard "injectable sleep" pattern; the real `time.sleep` is the only
  place real time is consumed, and it is never imported at module top so the lint stays clean.)
- **`stop_event`** — an injected `threading.Event` (default a fresh one). Tests set it to terminate
  the loop after a scripted number of iterations.
- **`MarketCalendar`** — inject a fake returning scripted `MarketSession`s (open then closed then
  open) to exercise open→close→reopen transitions, half-days, and the long-sleep path — no
  `/v2/clock` call.
- **`CurrentPriceProvider`** — inject a fake returning scripted prices (incl. `None`) to drive the
  stop trigger across a threshold and to assert the daily-PIT fallback when it returns `None`.
- **Broker** — the #1 `FakeAlpaca` (injected `http_get`/`http_post`/`http_delete`) for
  `get_account`/`get_positions`/`get_order`/`place`. The `AlpacaMarketCalendar` and
  `AlpacaCurrentPriceSource` also take injectable `http_get` → fully fakeable.

**Test cases (offline):**
- **Loop control:** with a fake calendar `[open, open, closed, open]` and a fake `sleep_fn` that
  advances the clock + stops after K iterations, assert the loop runs a fast iteration each open
  step, long-sleeps when closed, and exits when `stop_event` is set. No real sleep, no network.
- **Cadence:** assert ingest runs at most once/day, full cycle runs only at `full_times`, fast
  iteration runs every open step. A held-name stop fires on a fast iteration; reversal does NOT fire
  on a fast iteration (no fresh opinions) but DOES on a full cycle.
- **Intraday price seam (Decision 1):** with a `FakeCurrentPriceProvider`, a held long whose live
  price drops below the recomputed stop → `early_exit` SELL fires on a fast iteration even though the
  daily PIT close (FixtureSource) is still above the stop. Assert the daily PIT close was NOT used
  for the trigger. With provider returning `None`, assert fallback to daily PIT.
- **PIT-purity guard:** assert `"current_price" not in pit._SUPPORTED_FIELDS`; assert `build_engine`
  in `sim` and any backtest config yields a Null provider; assert no `datetime.now()`/`get_latest()`
  in `current_price.py` (string grep test, mirrors `check_no_lookahead.sh`).
- **Market calendar:** `OfflineMarketCalendar` returns correct open/close across a DST boundary
  (use a fixed `now` in March and November ET) and on a holiday and a half-day; `AlpacaMarketCalendar`
  parses `/v2/clock` JSON via the fake and caches (assert only one `http_get` across many
  same-session calls).
- **Resilience:** an iteration that raises is caught, logged, backoff increments, loop continues;
  a paused engine's iterations early-return but the loop keeps polling and resumes after
  `engine.resume()`; `stop_event` causes a clean exit with a final reconcile.
- **Regression:** existing ~1743 tests stay green; `run_cycle` is unchanged; the daily one-shot path
  (`loop_runner.main`) still works.
- **No-lookahead lint clean** (`scripts/check_no_lookahead.sh`).

---

### Decision 7 — Scope boundary

**IN:** the market-hours runtime daemon (`arbiter daemon` + `com.arbiter.daemon.plist`,
`KeepAlive=true`); the FAST iteration (reconcile + exit-monitor stop/horizon checks) on an N-minute
intraday cadence; the FULL cycle (ingest + entries + reversal) on a daily/few-times-a-day cadence;
the intraday `CurrentPriceProvider` for LIVE stop checks (Alpaca latest-trade) kept OFF the PIT path;
the authoritative `MarketCalendar` (Alpaca `/v2/clock` live + deterministic offline fallback, DST +
holidays + early-close); resilient unattended operation (catch/log/continue + backoff, per-iteration
gate checks, graceful close handling, signal handling, heartbeat, single-instance lock); offline
testability via injected clock/calendar/current-price/sleep/stop-event; keeping the daily 18:30
one-shot as an idempotent post-close backstop (reconcile + outcome sweep).

**OUT (explicit):**
- **Entries do NOT become intraday.** New-idea/entry generation stays disclosure-cadence
  (daily/few-times). #3 is not a day-trader (binding constraint). The fast iteration does NOT place
  BUYs.
- **#4 learning/trust-calibration loop** consuming the outcomes — outcomes are written, fed nowhere
  yet.
- **#5 MiroFish (A2)** self-host.
- Intraday entry signals, intraday rebalancing, scale-in/out strategies.
- Real-money (`live_trading=true`) path — reserved, not built; this remains structurally paper-only.
- A websocket/streaming price feed (the polling `current_price` REST read is sufficient for v1;
  streaming is a future optimization).
- Replacing SQLite with a server DB for concurrent multi-process access (the single-instance lock
  covers v1).

---

## 4. Files / functions to change (build-agent map)

- **NEW `arbiter/data/current_price.py`** — `CurrentPriceProvider` Protocol;
  `AlpacaCurrentPriceSource(config, http_get=…)` (data API `/v2/stocks/{ticker}/trades/latest`,
  `feed` from `ALPACA_DATA_FEED`); `NullCurrentPriceProvider`. No `as_of`, no `datetime.now()`, not
  registered with PITGateway.
- **NEW `arbiter/runtime/market_calendar.py`** — `MarketSession`, `MarketCalendar` Protocol,
  `AlpacaMarketCalendar(config, http_get=…)` (trading API `/v2/clock`, cached), `OfflineMarketCalendar`
  (zoneinfo ET, reuses `replay_clock` holidays + an `_EARLY_CLOSE` map).
- **NEW `arbiter/runtime/daemon.py`** — `run_daemon(engine, calendar, *, current_price_provider,
  sleep_fn, stop_event, fast_interval_s, full_times)`: the resilient loop (Decisions 3/5), cadence
  scheduling (Decision 4), backoff, signal handlers, heartbeat, pidfile/flock.
- **`arbiter/engine.py`** —
  - `build_engine`: construct `CurrentPriceProvider` (Alpaca in alpaca_paper, Null otherwise) and
    `MarketCalendar` (Alpaca vs Offline); store on `Engine`; pass provider into `_run_exit_monitor`.
  - NEW `Engine.run_fast_iteration(now)`: gate checks + `_reconcile_pending_orders` +
    account/A2 + `_run_exit_monitor(now, opinions=[])` (reversal inert). No ingest/entries/sweep.
  - `_run_exit_monitor`: thread `current_price_provider` through to `run_exit_monitor`.
  - Optionally redirect the engine.py:555 `_us_market_open` warning to the injected calendar.
- **`arbiter/execution/exit_monitor.py::run_exit_monitor`** — add
  `current_price_provider: CurrentPriceProvider | None = None`; use live current price for the stop
  check with daily-PIT fallback (Decision 1b). `evaluate_triggers` unchanged.
- **`arbiter/config.py`** — add `fast_interval_s` (`ARBITER_FAST_INTERVAL_S`, default 180),
  `full_cycle_times_et` (`ARBITER_FULL_CYCLE_TIMES_ET`, default "09:45,15:30"),
  `daemon_heartbeat_path` (default `data/arbiter-daemon.heartbeat`); add to `_KNOWN_KEYS`/toml.
  (Cross-lane: amend INTERFACES §10b.5 field list — flag, don't silently diverge.)
- **`arbiter/cli.py`** — NEW `daemon` command building the engine + calendar + provider and calling
  `run_daemon`; install SIGTERM/SIGINT → `stop_event`.
- **NEW `deploy/com.arbiter.daemon.plist`** — `KeepAlive=true`, `RunAtLoad=true`, runs
  `arbiter daemon`; logs to `data/arbiter-daemon.{stdout,stderr}.log`; `WorkingDirectory` set.
  **`scripts/schedule.sh`** — add `install-daemon`/`uninstall-daemon`/`daemon-status` (and a
  `Makefile` target). Keep the existing daily plist as the post-close backstop.
- **INTERFACES.md** — note the daemon runtime + the `CurrentPriceProvider` seam (explicitly: a live
  "now" price that is NOT a PIT field and never enters a backtest); add the new Config fields to
  §10b.5. Deliberate, documented amendment; get sign-off.
- **Tests** under `tests/runtime/` (daemon loop, market_calendar), `tests/data/` (current_price +
  PIT-purity guard), `tests/execution/` (exit_monitor live-price path), `tests/` engine
  (`run_fast_iteration`).

---

## 5. USER SETUP checklist (only the user can do these)

1. **Install the daemon agent:** `bash scripts/schedule.sh install-daemon` (after the build lands).
   Verify with `daemon-status`. Keep the existing daily one-shot installed as the post-close backstop.
2. **`EXECUTOR_BACKEND=alpaca_paper`** must be set (from #1) for the live current-price + Alpaca
   calendar paths to engage. In `sim` the daemon runs but uses the offline calendar + daily-PIT
   stops (fine for a dry run).
3. **(Optional) tune cadence** in `.env`: `ARBITER_FAST_INTERVAL_S` (default 180s),
   `ARBITER_FULL_CYCLE_TIMES_ET` (default `09:45,15:30`). Lower the interval cautiously (API limits).
4. **(Recommended) wire `KILL_SWITCH_URL` and `ALERT_WEBHOOK_URL`** (from #1 §6) — for an
   unattended *looping* process these matter more than for a daily one-shot: the kill switch lets you
   stop the bot remotely (it keeps polling and resumes when cleared), and the webhook tells you when
   it auto-pauses.
5. **Free-plan data feed:** `current_price` and bars use `feed=iex` by default (`ALPACA_DATA_FEED`);
   IEX latest-trade can be slightly stale vs SIP — acceptable for paper stops, documented.
6. **Leave the machine awake during market hours** (macOS sleep pauses launchd timers/agents); a
   `caffeinate`-style keep-awake or energy setting may be needed for true unattended operation.

---

## 6. Out-of-scope (restate)

- Intraday **entries** / day-trading (entries stay disclosure-cadence).
- #4 learning/trust-calibration loop; #5 MiroFish.
- Real-money (`live_trading=true`) path.
- Streaming/websocket price feed (REST polling is v1).
- Multi-process concurrent DB access beyond the single-instance lock.

---

## 7. Open risks

1. **Intraday-price-vs-PIT-purity boundary (the headline risk).** The whole design hinges on the
   live `current_price` never leaking into the PIT/backtest path. MITIGATION: it is a distinct
   Protocol with **no `as_of`**, never registered with `PITGateway`, injected only in alpaca_paper,
   Null in sim/backtest, plus an explicit lint/audit test (Decision 1c/6). The build agent MUST keep
   it off `pit.py` entirely — do NOT add a `"current_price"` PIT field. If a future refactor adds an
   `as_of` to the provider it reintroduces look-ahead risk — guard with the test.
2. **Alpaca data/trading API rate limits** (free plan ~200 req/min). A too-small `fast_interval_s`
   times `max_open_positions` current-price reads + clock fetches could approach the limit.
   MITIGATION: cache `/v2/clock` (Decision 2b), default 180s interval, $10k config caps positions at
   8–10, config-tunable interval. RESIDUAL: if the operator slams the interval to a few seconds with
   many positions, throttling. Document the budget; consider a future batch `latest/trades?symbols=…`
   multi-ticker call (Alpaca supports it) to collapse N reads into 1.
3. **IEX latest-trade staleness / thin-name gaps.** Free-plan IEX latest trade can lag SIP and some
   names trade thinly → `current_price` returns `None` or a stale tick → stop checked against
   daily-PIT fallback or skipped (fail-closed). ACCEPTABLE for paper; a future SIP/websocket upgrade
   improves it. Same gap-through-stop residual as #2 remains between iterations (now minutes, not a
   day — a large improvement).
4. **`engine.paused` is in-memory; a daemon crash/relaunch clears it.** A relaunch would resume
   trading even if the prior process auto-paused. MITIGATION: the durable `breaker_state` re-trips on
   the gate check and the kill switch is re-read live, so a *genuine* fault re-pauses immediately; an
   auto-pause from a transient (a single broker blip) clearing on restart is arguably desirable.
   Document; consider persisting a `paused` flag if undesired (out of v1 scope).
5. **macOS sleep / power management.** A sleeping Mac suspends the daemon; stops are not checked
   while asleep. MITIGATION: user setup item (keep-awake). This is an environment constraint, not a
   code bug.
6. **Single-instance lock vs the daily one-shot.** Running the daemon AND the 18:30 one-shot means
   two processes touch the same SQLite DB. MITIGATION: the pidfile/flock guard makes the second
   exit cleanly; every mutate path is idempotent + durable so even an interleave is non-corrupting
   under WAL, but the lock prevents concurrent engine mutation. The build agent MUST implement the
   lock before enabling both.
7. **Clock-API outage during the open-session decision.** If `/v2/clock` is unreachable the daemon
   falls back to `OfflineMarketCalendar`, whose early-close/holiday data is curated (could drift in
   future years). RESIDUAL: on a half-day during a clock outage the offline path might think the
   market is open until 16:00. Low impact (orders just expire / no current price), documented;
   refresh the offline early-close map yearly.
8. **Could NOT determine** the exact Alpaca free-plan per-minute rate limit and whether
   `/v2/stocks/trades/latest` multi-symbol batching is enabled on the paper data plan — the build
   agent should confirm against the live account and adjust the cadence defaults / adopt batching if
   available. Everything else above was verified against source.
```
