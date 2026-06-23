# A4 — Circuit Breakers, Kill-Switch & Auto-Pause — READ-ONLY Audit

- **Auditor lane:** A4 (safety: breakers, kill-switch, auto-pause, durable pause)
- **Date:** 2026-06-19
- **Scope:** `arbiter/safety/breakers.py`, `arbiter/safety/kill_switch.py`, `arbiter/safety/alerting.py`, durable `paused` flag (migration `024_engine_state.sql` + engine restore), and the `live_trading or kill_switch_url` gate predicate (`engine.py:619`).
- **Spec anchors:** INTERFACES.md §3.9 / §8 / §11.4 (line 244: "Paused/kill-switched/breaker-tripped engine does NOT sell (paused = no autonomous orders)"); §11.4 line 304 ("gate unreachable -> no trade").

## VERDICT

**SHIP WITH FIXES.** The latching/persistence machinery is well-built and correct in isolation: breakers latch idempotently and survive restart via `breaker_state`; the durable `paused` flag persists and is restored on `build_engine`; the kill-switch and alerting paths are genuinely fail-closed and cannot brick paper mode. **However, four of the six declared breakers have no production caller and can never trip, and the kill-switch is not consulted at all in the actual deployed paper posture** (`live_trading=false`, `kill_switch_url=""`). The "paused = no stops" behavior is intentional and spec-sanctioned, but is a real, latent capital-risk worth an explicit operator decision. Net: the *framework* is solid; the *wiring* leaves most of the declared safety surface inert.

---

## FINDINGS

### P1 — Four of six breakers have no production caller — cannot trip — `breakers.py:302,333,365,458` — INTERFACES.md §3.9 declares six latching breakers; only two are wired
A full-tree grep for the `check_*` helpers and `.trip(` outside the module and tests finds production callers for **only**:
- `check_broker_non_200` → `execution/submit.py:318`
- `check_a3_volume_anomaly` → documented in `defenses/volume_anomaly.py:35` as a Wave-C wiring point, but `VolumeAnomalyGate.is_anomalous()` is **never actually invoked** by the engine or orchestrator (only referenced in docstrings of `volume_anomaly.py`, `defenses/__init__.py`, `tips/__init__.py`). So this one is also effectively dead in practice.

There is **no caller anywhere** for `check_daily_loss`, `check_per_position`, `check_mirofish_consecutive_fail`, or `check_confidence_distribution_shift`. The engine's `_safety_gate` (`engine.py:631`) only reads `breaker.any_tripped(conn)` — i.e. it consults *already-latched* state but nothing in the cycle ever computes daily P&L, per-position P&L, MiroFish consecutive-fail counts, or confidence-shift magnitude and feeds them to the corresponding `check_*`. These breakers are therefore structurally incapable of tripping in the running system.
- **Why it matters:** The two headline portfolio-protection breakers from §3.9 — daily-loss (>=2%) and per-position (-5% intraday) — are pure dead code. An operator reading §3.9/§8 reasonably believes these guards are active. They are not.
- **Recommended action:** Either (a) wire each `check_*` into the cycle (daily_loss/per_position from the account+position P&L already loaded in `run_cycle`; mirofish from a consecutive-fail counter in `orchestrator/triage.py`/scheduler; volume-anomaly by actually calling `VolumeAnomalyGate.is_anomalous()` per held name as the docstring promises), or (b) explicitly downgrade §3.9 to mark these as "framework present, wiring deferred to Wave-C" so the gap is honest and tracked. Do not ship a SHIP claim that implies all six are live.

### P1 — Kill-switch is not consulted in the deployed paper posture — `engine.py:619` + `config/arbiter.toml:6,10,36` — gate predicate `(live_trading or kill_switch_url)` is False for the real paper config
The gate at `engine.py:619` is:
```python
if (self.config.live_trading or self.config.kill_switch_url) and self.kill_switch.is_halted(as_of=now):
```
The shipped config is `live_trading=false`, `executor_backend="sim"` (overridable to `alpaca_paper`), `kill_switch_url=""`. In the actual paper-broker deployment (`alpaca_paper` + `live_trading=false` + empty URL) the predicate short-circuits to **False**, so `is_halted` is never called. The kill switch — the one off-box halt that "keeps working even when the Python process is dead" — is therefore **completely inert against the live paper broker** unless an operator remembers to set `KILL_SWITCH_URL`.
- **Why it matters:** The system places real (paper) orders against Alpaca in this posture (per MEMORY: "7 live paper orders"). The off-box kill switch is the operator's emergency-stop for exactly that path, and it is silently disabled. The gate's design comment ("paper-sim only when a URL is set") treats the URL as the opt-in, but nothing forces the URL to exist when a *real broker* is selected.
- **Recommended action:** Tie the kill-switch consultation to the *broker* being live, not to `live_trading`: e.g. consult whenever `executor_backend == "alpaca_paper"` (real orders leave the box) OR `live_trading` OR a URL is set. At minimum, emit a loud startup warning (or refuse to start) when `executor_backend=alpaca_paper` and `kill_switch_url` is empty.

### P2 — `broker_non_200` only trips on an explicit `rejected` place(), not on genuine non-200s elsewhere — `submit.py:309-324`, `kill_switch.py:118-133` — breaker name/intent vs. actual trigger mismatch
The `broker_non_200` breaker is named and documented (§3.9, `breakers.py:428`) for "any broker response is non-200." In practice it is latched only inside `submit.py` when `report.status == "rejected"`, and even then with a **hardcoded `503`** sentinel (`submit.py:319`) because the real status code isn't threaded through. Genuine non-200 responses observed elsewhere — `get_account`, `get_order` reconciliation (`engine.py:311`), the kill-switch's own `_fetch` HTTPStatusError path (`kill_switch.py:118`), and the `AlpacaCurrentPriceSource`/calendar reads — do **not** route to `check_broker_non_200`. A flapping broker that returns 500s on account reads will fail-closed *that cycle* (A2 equity guard) but never *latch* the breaker, so it won't durably halt.
- **Why it matters:** The breaker's coverage is far narrower than its name implies; a persistently sick broker is handled cycle-by-cycle rather than latched, and recovers silently without an operator ack.
- **Recommended action:** Route the real broker status code from the adapter into `check_broker_non_200` at the actual HTTP boundary (adapter layer), not just the submit-reject path; thread the true status code instead of the `503` placeholder.

### P2 — A paused engine does not fire protective stop-losses — `engine.py:661-682`, `engine.py:921-924` — intentional per §11.4 but a latent capital risk
`_safety_gate` is called *first* in both `run_cycle` (`engine.py:864`) and `run_fast_iteration` (`engine.py:661`), and returns early on paused / kill-switch / breaker **before** `_run_exit_monitor` runs. The code comments (`engine.py:921-924`) and INTERFACES.md §11.4 line 244 explicitly affirm this: "paused = no autonomous sells." The reasoning ("no autonomous orders while halted") is defensible — you don't want a broken engine flailing — but the consequence is that a held position whose price is crashing through its stop will **not** be exited while the engine is paused, and pauses **latch indefinitely** until a manual `resume()` (or breaker `reset()`). Combined with the durable-pause restore (correctly persisting across relaunch), an unattended auto-pause can leave positions naked through an arbitrarily long outage.
- **Why it matters:** The breaker that *caused* the pause (e.g. broker rejection, vol anomaly) may be orthogonal to a separate position that needs its stop; the operator may be asleep. "Fail to no-action" protects against runaway trading but not against market drawdown on existing inventory.
- **Recommended action:** Decide explicitly and document the trade-off. Consider a distinct "protective-exit-only" mode where a paused engine still permits *stop-loss / horizon SELLs* (de-risking) while blocking all BUYs and reversal-driven sells — i.e. split the gate so risk-reducing exits survive a pause. If the current behavior is intended for v1, add an operator-facing note that pauses require human attention because stops are suspended.

### P3 — Kill-switch caches only successes; a recovered endpoint that flaps will re-halt every cycle, and stale cache is never honored — `kill_switch.py:13,110-138` — fail-closed-but-noisy; correct, flagged for awareness
On any fetch failure the kill-switch returns `True` immediately and does **not** cache (by design, `kill_switch.py:13`). This is correctly fail-closed. The side effect: an intermittently-unreachable endpoint produces a fresh HTTP attempt every order-check (no backoff) and a halt each time it fails, with no stale-value grace. This is the safe direction, but worth noting it means a transient network blip halts trading for the full blip duration with per-check request amplification.
- **Why it matters:** Operationally noisy and can convert a brief endpoint hiccup into a trading outage; not a correctness defect.
- **Recommended action:** Optional: add a short bounded fail-closed backoff or a small "consecutive-failure before hard-halt" tolerance ONLY if it does not weaken the fail-closed guarantee. Leave as-is if simplicity is preferred.

### P3 — `_persist_paused` / `restore_persisted_pause` swallow all DB errors silently — `engine.py:252-281` — a pause that silently fails to persist resumes on relaunch
Both `_persist_paused` (`engine.py:263`) and `restore_persisted_pause` (`engine.py:272`) catch bare `Exception` and only `log.error`. The whole point of migration 024 (per its header: "the daemon would silently resume trading after a fatal condition") is defeated if the *write* of `paused=1` fails — the engine pauses in-memory, the process is killed by `KeepAlive`, and the relaunched daemon restores `paused=0` (or no row) and resumes. The failure is logged but nothing escalates or blocks.
- **Why it matters:** A silent persistence failure reintroduces exactly the "silently resume after fatal" bug the migration exists to prevent.
- **Recommended action:** On `_persist_paused` write failure for a pause (paused=True), escalate — fire a critical alert and/or keep the in-memory pause AND refuse to clear it; at minimum surface the failure to the daemon health/status so it's not buried in logs.

### P3 — `reset()` clears latched breaker but leaves the engine's `paused` flag set (and vice-versa) — `breakers.py:259`, `engine.py:241-250` — two separate latches with no coordinated clear
Latched breakers (`breaker_state`) and the engine auto-pause (`engine_state.paused`) are independent. `breaker.reset(name)` clears a breaker but does not clear `engine.paused`; `engine.resume()` clears `paused` but not any latched breaker. Because `_safety_gate` checks `paused` first and *then* `any_tripped`, an admin must clear BOTH to actually resume, and clearing only one yields confusing "still halted" behavior. This is arguably correct (defense in depth) but is an easy operational footgun with no single "all-clear" admin action.
- **Why it matters:** Operator confusion during incident recovery; risk of a partial resume that looks resumed but isn't (or appears halted for an unexpected reason).
- **Recommended action:** Provide a single admin "clear-all-safety" path (or document the required two-step order: reset breakers, then resume) in the Wave-C admin endpoint, and have `resume()` log any still-latched breakers it detects.

---

## WHAT IS CORRECT (verified)

- **Latching is idempotent and durable.** `trip()` no-ops if already latched (`breakers.py:208`), preserving the first reason; state lives in `breaker_state` and survives restart. `reset()` is structurally unreachable from advisor/fusion (`__init__` does not re-export it — confirmed by the safety package docstring).
- **Kill-switch is genuinely fail-closed.** No URL → `True` (`kill_switch.py:91`); HTTP error, RequestError, JSON/Value/AttributeError → `True` (`kill_switch.py:118-133`); missing `"halted"` key defaults to `True` (`kill_switch.py:117`). Stale cache is never served on failure. Matches §11.4 "gate unreachable -> no trade."
- **Cannot brick paper mode.** With `live_trading=false` and no URL, the gate predicate is False and `is_halted` is never called, so the fail-closed default cannot strand the default sim/paper config. (This is also the root of the P1 above — correct for sim, wrong for the real paper broker.)
- **Auto-pause persists across relaunch.** `_fire_critical_alert` sets `paused=True` and calls `_persist_paused(True, ...)` (`engine.py:580-583`); `build_engine` calls `engine.restore_persisted_pause()` (`engine.py:1401`); the daemon builds the engine once via `build_engine` (`daemon.py:314`) so the restore runs before the loop. Migration 024 models it as a singleton mutable row (id=1), correctly exempted from the insert-only rule.
- **Alerting → AutoPauseSentinel wiring is fail-safe.** `critical` tier returns a sentinel regardless of webhook delivery success (`alerting.py:143-145,181-184`); the engine even pauses if the alerting call itself raises (`engine.py:571-576`) — pause-on-the-side-of-caution.
- **Clock discipline holds.** No `datetime.now()` in any of the four in-scope files; all timestamps flow from injected clock / `as_of`.

---

## OPPORTUNITIES TO ADD

1. **A "trip-readiness" self-test / startup assertion** that verifies every name in `BREAKER_NAMES` has a live production call path — would have caught the four dead breakers (P1) mechanically.
2. **A heartbeat/health row in `engine_state`** (last cycle ts) so an externally-observed paused-or-dead engine can be distinguished, and an off-box monitor can alert when a pause has persisted beyond N minutes (mitigates the "stops suspended indefinitely" risk, P2).
3. **Thread the real broker HTTP status code through the adapter** into `check_broker_non_200` at the HTTP boundary so the breaker means what its name says (P2).
4. **Protective-exit-only degradation tier** (sell-to-derisk allowed, buys blocked) as a first-class `DegradationLevel`, integrated with the existing `degradation.py` ladder, so a pause can still honor stops.
5. **Single coordinated admin "all-clear"** that resets latched breakers and clears `paused` atomically with a combined audit entry (P3).
6. **Consecutive-failure tracking for MiroFish and broker reads** persisted in `engine_state` (or a small counters table) so `check_mirofish_consecutive_fail` / a richer `broker_non_200` have real inputs across cycles and survive restart.
