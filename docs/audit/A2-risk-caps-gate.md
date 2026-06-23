# A2 — Risk caps & the trading gate

- **Lane:** A2 (risk caps + quorum/gate enforcement)
- **Date:** 2026-06-19
- **Auditor mode:** READ-ONLY
- **Scope:** Enforcement of `max_position_pct`, `max_sector_pct`, `max_gross_pct`,
  `max_open_positions`, `adv_cap_pct`; quorum/gate DEGRADED/HALT logic; sector mapping; whether
  caps account for EXISTING positions. (Sizing math = A1; circuit breakers = A4 — not graded here.)
- **Files reviewed:** `arbiter/policy/decision.py`, `arbiter/policy/sizing.py`,
  `arbiter/safety/gate.py`, `arbiter/safety/quorum.py`, `arbiter/safety/degradation.py`,
  `arbiter/config.py`, `arbiter/engine.py` (decision wiring), `arbiter/orchestrator/cycle.py`, `.env`.

## HEALTH VERDICT: ❌ FAIL (caps configured + unit-tested, but NOT enforced live)

The five caps are all *referenced* in `sizing.compute_size` (none is dead at the unit level), and the
quorum/gate/degradation ladder is correct. **But the live engine path
(`engine._bound_decide` → `policy.decide` per idea) never passes the book-state arguments**, so the
three book-aware caps (`max_open_positions`, `max_gross_pct`, `max_sector_pct`) silently default to a
zero/empty book on every order and therefore **never bind at runtime**. The `$10k` `.env` guardrails
(`MAX_OPEN_POSITIONS=8`, `MAX_GROSS_PCT=0.50`) are exactly the two caps that are inert. This is a P0
for a system already firing live paper orders.

Severity counts: **P0 = 1, P1 = 2, P2 = 2, P3 = 1.**

---

## FINDINGS

### P0 — Book-aware caps (open-positions, gross, sector) never enforced live — `arbiter/engine.py:1029-1041` (also `arbiter/orchestrator/cycle.py:247-289`) — caps default to an empty book on every order

`_bound_decide` calls `_decide(...)` with only `ticker, bucket_outputs, account, gate, adv_provider,
clock, config, portfolio_equity, live_advisor_count`. It passes **none** of
`current_sector_exposure`, `current_gross_exposure`, `current_open_positions`. In `policy.decide`
(decision.py:93-97) those parameters default to `0.0 / 0.0 / 0`, and `decide` forwards those defaults
into `compute_size` (decision.py:163-165). In `sizing.compute_size`:

- `max_open_positions` check (sizing.py:131): `current_open_positions (=0) >= config.max_open_positions`
  is never true → **the 8-position cap can never trip**.
- gross headroom (sizing.py:126-128): `gross_max - 0` = full headroom every order → **80%/50% gross cap
  never binds**.
- sector headroom (sizing.py:121-123): `sector_max - 0` = full headroom every order → **20% sector cap
  never binds**.

The orchestrator (`cycle.py:247`) loops ideas one at a time calling `decide(fusion_output, idea)` and
does **not** accumulate running exposure between iterations, so even within a single cycle N
simultaneous orders are each sized as if the book is empty (no intra-cycle gross/sector/count
accumulation — the `decide_all` accumulator at decision.py:230-269 is the function that *does* this,
and the engine never calls it).

Why it matters: with `EXECUTOR_BACKEND=alpaca_paper` and a $10k account, the system can open far more
than 8 positions and exceed 50% gross with no guardrail. Only the per-name (5%) and ADV (2%) caps —
the two that don't depend on book state — actually constrain a live order. The data needed is already
in hand: `position_store.open_position_count(self.conn)` (sim) / `len(self.executor.get_positions())`
(broker) are used at engine.py:1227-1231 for `status()`, and gross/sector dollar exposure is derivable
from `get_positions()`. They are simply never threaded into the decision call.

**Recommended action:** In `_bound_decide`, compute and pass the live book state:
`current_open_positions` from the mode-aware count, `current_gross_exposure` = Σ position market values,
and per-ticker sector exposure; OR switch the engine to `policy.decide_all` (which already accumulates
gross/sector/count across the batch) seeded with the pre-existing book. Add an engine-level test that
asserts an at-capacity book (8 positions / 50% gross) produces zero new orders.

### P1 — Sector cap has no real sector mapping; collapses to a single "UNKNOWN" bucket — `arbiter/policy/decision.py:208,238` — sector-concentration risk is structurally unmeasured

The only sector source in the codebase is the `sector_by_ticker` parameter of `decide_all`
(decision.py:208), defaulting every ticker to `"UNKNOWN"` (decision.py:238). There is no
ticker→sector lookup anywhere in the source (grep for `get_sector`/`sector_map`/`SECTOR` finds only the
`congress_sector` *signal type*, unrelated). Consequently, even if the P0 wiring were fixed via
`decide_all`, every name would land in one `"UNKNOWN"` sector — the 20% "sector" cap would actually
behave as a *second gross cap*, not a sector cap, and true sector concentration (e.g. 100% energy)
would be invisible. The docstring frames "UNKNOWN" as "conservative," but conservatism here means the
cap does not do what its name claims.

**Recommended action:** Introduce a real sector classifier (static map or data-provider field) feeding
`sector_by_ticker`; document explicitly that until then the sector cap is a degenerate gross cap.

### P1 — `max_open_positions` counts ideas/orders, not held positions, even where it is checked — `arbiter/policy/sizing.py:130-132` + `arbiter/engine.py:1038` — cap semantics drift from "open positions"

The cap is named for *open positions*, but the value the engine would feed (once P0 is fixed) and the
value used for `portfolio_equity` reveal a semantic gap: `portfolio_equity` falls back to a hardcoded
`100_000.0` when `account.equity` is falsy (engine.py:1038). On the $10k account, if the broker ever
returns equity as `0`/`None`/`""` transiently, every percentage cap (`max_position_pct`,
`max_gross_pct`, `max_sector_pct`, `adv` is independent) is computed against **$100k, not $10k** — a
10× inflation of every dollar cap, fail-*open*. The per-name cap would become $5k (50% of the real
account) on a single name.

**Recommended action:** Fail-closed on missing/zero equity (return no orders or HALT) rather than
substituting a $100k default; never size against a fabricated equity figure on a live account.

### P2 — `.env` does not override `max_position_pct` / `max_sector_pct` / `adv_cap_pct` for the $10k account — `.env:16-18` — only 2 of 5 caps are tuned for the small account

The `$10k-account guardrails` block sets only `ARBITER_MAX_OPEN_POSITIONS=8` and
`ARBITER_MAX_GROSS_PCT=0.50`. Per-name stays 5% ($500), sector 20% ($2,000 → but degenerate per P1),
ADV 2%. These defaults are not unreasonable, but the two caps explicitly chosen as the small-account
guardrails (open count + gross) are precisely the two that don't bind at runtime per the P0 finding —
so the operator's intended risk envelope is entirely unenforced.

**Recommended action:** After fixing P0, re-verify the chosen `.env` envelope end-to-end against a
populated book; consider an explicit `max_position_pct` override if 5% of $10k is too coarse.

### P2 — ADV cap is fail-open if `adv_provider` is wired to return 0.0 rather than None — `arbiter/policy/sizing.py:142-149`; provider at `arbiter/engine.py:1025-1027` — only None/NaN are fail-closed

`compute_size` fail-closes on `adv is None` or `math.isnan(adv)` (good, and the NaN guard is a nice
catch). But a provider returning `0.0` (or a tiny positive value) yields `adv_cap = adv_cap_pct * 0 =
0` → `size = min(size, 0) = 0`, which happens to be safe *here*; however a negative or spurious small
ADV would silently zero-or-distort sizing without an explicit signal. The engine's `_adv_provider`
(engine.py:1025-1027) returns `float(val)` for any non-None PIT value, so a stored `0.0`/garbage ADV is
passed straight through. This is a softer issue than P0 but worth a guard.

**Recommended action:** Treat `adv <= 0` as fail-closed (missing data) explicitly in `compute_size`,
mirroring the None/NaN handling, and validate the PIT `adv_20d` values on ingest.

### P3 — Gate's `account` param is accepted but unread; position-level / equity checks deferred — `arbiter/safety/gate.py:18-20,88-93` — gate cannot enforce per-position or equity-derived limits

`is_trading_allowed` documents that `account` is "passed through to the audit … but is not read in this
Wave" (gate.py:18-20). The gate therefore enforces only quorum + breaker signals; any cap or
loss-rate logic that should live at the gate (vs sizing) is absent. This is consistent with the design
split (caps live in sizing) and is low-severity given that, but it means the gate is purely a
quorum/breaker gate today and the docstring's "position-level checks" remain a TODO.

**Recommended action:** Either implement the documented account-aware checks or remove the
forward-looking docstring so future maintainers don't assume the gate enforces position limits.

---

## What IS correct (verified)

- **Quorum (quorum.py:43-91):** 2+ → 1.0/NORMAL, 1 → 0.25/DEGRADED, 0 → 0.0/HALTED; negative count
  raises. Matches INTERFACES.md §8 exactly. Engine also short-circuits to HALTED at 0 live advisors
  (engine.py:993-999) and passes the *actual* live count (engine.py:1039), not a hardcoded 2.
- **Gate combine logic (gate.py:121-187):** breaker fault → fail-closed HALTED; any tripped breaker →
  bump to HALTED + allowed=False; levels 3–4 supersede via `level_supersedes_trading`; `not allowed ⇒
  size 0` consistency guard. Sound and fail-closed.
- **Degradation ladder (degradation.py):** RESTRICTED/HALTED force size 0 + block; `highest_level`
  picks max severity; `effective_multiplier` lets forced 0.0 win over quorum. Correct.
- **Per-name cap & ADV cap:** these two do not depend on book state and DO bind on every live order.
  ADV is correctly the last transform (sizing.py:141-149) and NaN-guarded.
- **Gate is wired with the real breaker provider** in the live path (engine.py:1017-1023) — not the
  test None default.

---

## OPPORTUNITIES TO ADD

1. **End-to-end cap-enforcement test at the engine layer.** Current cap tests
   (`tests/policy/test_sizing.py:400-424`, `tests/policy/test_decision.py:53-91`) pass explicit
   book-state args and therefore pass — they cannot detect the P0 wiring gap. Add a test that drives
   `engine.run_cycle` (or `_bound_decide`) with a populated book and asserts gross/sector/count caps
   actually bind. This is the single highest-value addition: it would have caught the P0.
2. **Switch the engine to `decide_all` (or a running-exposure accumulator).** `decide_all`
   (decision.py:194-269) already does the per-batch gross/sector/count accumulation the engine needs;
   adopting it removes the per-idea independence bug and centralizes book accounting.
3. **Real sector classifier** feeding `sector_by_ticker` so the 20% cap is a sector cap, not a second
   gross cap (resolves P1).
4. **Post-trade cap invariant assertion** (defense in depth): after submitting a cycle's orders,
   assert resulting gross ≤ `max_gross_pct·equity` and open count ≤ `max_open_positions`; latch a
   breaker on violation. Caps belong in sizing, but a cheap post-condition check would have surfaced
   the P0 in production logs immediately.
5. **Fail-closed equity guard** (resolves P1's $100k fallback) — a shared helper that refuses to size
   against a missing/zero equity, used by both the gate and sizing.
