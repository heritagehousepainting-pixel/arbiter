# Arbiter — Go-Live Checklist (paper execution)

**Status: NOT FLIPPED.** Per your locked decision #1, the build is complete but `EXECUTOR_BACKEND=sim`.
This is the exact, ordered procedure for **you** to flip to the **$10k Alpaca PAPER** account. Claude will
not flip it; you run these steps when ready.

> What "go-live" means here: real order flow to the **Alpaca paper** endpoint (fake money, real fills/latency).
> It is **not** real-money trading — `LIVE_TRADING` is reserved for a real-money path that does not exist yet
> and stays `false`. The adapter is structurally paper-only.

---

## The switch (what actually changes execution)
`arbiter/execution/alpaca_adapter.py::build_executor` picks the broker:
```
EXECUTOR_BACKEND == "alpaca_paper"  AND  both Alpaca keys present  → AlpacaAdapter (paper)
otherwise                                                          → SimExecutor (fail-closed default)
```
So the flip is a single env change: **`EXECUTOR_BACKEND=alpaca_paper`** in `arbiter/.env` (keys are already staged & verified). Leave `LIVE_TRADING=false`.

---

## BLOCKING prerequisites (set BEFORE the flip, or the system can't trade safely)

1. **`KILL_SWITCH_URL` — fails CLOSED.** The engine calls `KillSwitch.is_halted()` before every cycle; if
   `KILL_SWITCH_URL` is **unset or unreachable, the engine HALTS and places no orders.** So today, flipping to
   `alpaca_paper` with an empty `KILL_SWITCH_URL` would just halt. Provision a tiny always-on endpoint
   (returns "go"/"halt") and set the URL. Verify it's reachable from this box first.
2. **`ALERT_WEBHOOK_URL` — set before any UNATTENDED run.** Empty ⇒ no critical-alert delivery. Use an
   ntfy/Pushover/Slack webhook. (Note SETUP_NEEDED #5: `Alerting` is built+tested but auto-fire from the cycle
   loop is a documented follow-up; the broker-failure breaker already handles the halt path.)
3. **Optional advisors (leave inert if undecided):**
   - `EDGAR_USER_AGENT="Name email@host"` → activates Form-4 + the new 13D/13G `A1.activist` advisor. Inert
     (one WARNING, no crash) until set.
   - `MIROFISH_ENDPOINT=http://localhost:<port>` → activates the `A2.mirofish` brain. Clean no-op until set.
   Neither is required to flip; without them the system runs on the **congress-only** signal it runs on today.

---

## Pre-flip verification (all currently TRUE)
- [ ] Full suite green: `cd arbiter && .venv/bin/python -m pytest tests/ -q` → **2335 passed**.
- [ ] Linters clean: `bash scripts/check_no_lookahead.sh` && `bash scripts/check_insert_only.sh`.
- [ ] Account live: `.venv/bin/python -m arbiter.cli status` (paper account ACTIVE, $10k).
- [ ] Risk caps set in `.env`: `ARBITER_MAX_OPEN_POSITIONS=8`, `ARBITER_MAX_GROSS_PCT=0.50` (present).
- [ ] `KILL_SWITCH_URL` provisioned + reachable (prereq #1).

## The flip + supervised first run
1. Set `EXECUTOR_BACKEND=alpaca_paper` in `arbiter/.env`. Keep `LIVE_TRADING=false`.
2. Confirm selection: `.venv/bin/python -c "from arbiter.engine import build_engine; e=build_engine(); print(type(e.executor).__name__)"` → expect **`AlpacaAdapter`**.
3. **Supervised** single cycle, watch every line: `.venv/bin/python -m arbiter.cli run-cycle` (or `run` for ingest+cycle). Confirm: orders route to the paper endpoint, the reconciler sees them, no breaker trips, `status` reflects reality.
4. Reconcile the Alpaca paper dashboard against `arbiter.cli status` (positions/orders match).

## Rollback (instant, safe)
- Set `EXECUTOR_BACKEND=sim` in `.env` → next cycle uses `SimExecutor`. No state corruption (the book re-seeds
  from positions each cycle). Or hit the kill switch (set its endpoint to "halt").

## Then: unattended
- Only after a few clean supervised cycles. Ensure `KILL_SWITCH_URL` + `ALERT_WEBHOOK_URL` are set.
- The market-hours daemon is the unattended entrypoint: `.venv/bin/python -m arbiter.cli daemon`.

## The real-money gate (future, Phase 7 — not in scope now)
`--approve-live` sign-off (manual, expires every 30 days) + 60 days / ≥30 trades / Sharpe ≥ 1.0 / max DD ≤ 8%.
This checklist is **paper only**; real money is a separate, later gate.

---

## Known operational risk to watch (found during this session's backfill)
The Alpaca **IEX free-tier data feed throttles (HTTP 429)** under load, and the Stooq fallback returned
`not_found` for some tickers — so PIT price labeling is **fragile under burst load**. For unattended operation
this can cause outcome-labeling gaps (seen: 1 of 14 backfill outcomes failed to label). Consider a paid data
tier or added backoff/caching before heavy unattended use. Not a blocker for supervised paper runs.
