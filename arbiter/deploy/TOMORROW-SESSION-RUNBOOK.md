# Arbiter — Supervised First Paper Session Runbook

Mode: `EXECUTOR_BACKEND=alpaca_paper`, `LIVE_TRADING=false` (paper-only, $10k Alpaca paper account).
Working dir: `/Users/jonathanmorris/poly_bot/arbiter`. Venv: `.venv`. Run EVERYTHING from the project root.

Verified green state at audit time: broker `/v2/account` 200, equity=$10,000, 0 positions, 0 tripped
breakers, engine not paused, reconciler clean, kill switch live (`{"halted":false}`).

---

## 1. PRE-FLIGHT (run the night before / before open)

```bash
cd /Users/jonathanmorris/poly_bot/arbiter

# 1a. Kill switch is live and OFF (must print {"halted":false}  HTTP 200)
curl -s -w '\nHTTP %{http_code}\n' "$(grep -E '^KILL_SWITCH_URL=' .env | cut -d= -f2-)"

# 1b. Broker + config preflight (must print is_sim:False, equity 10000, breakers none)
.venv/bin/python -m arbiter.cli status

# 1c. Subscribe your PHONE to the ntfy topic NOW so alerts reach you:
#     open the ntfy app -> subscribe to topic  arbiter-alerts-REDACTED
#     (or browse https://ntfy.sh/arbiter-alerts-REDACTED in a browser tab)

# 1d. Confirm the pre-go-live DB restore point exists
ls -la data/arbiter.db.pre-golive-bak
```

## 2. FRESH DATA (run pre-open, ~08:30–09:00 ET)

Fresh disclosures were ingested 2026-06-21 (form4 72, form13d 59, congress 513). Re-run pre-open for the latest:

```bash
.venv/bin/python -m arbiter.cli ingest --sources form4,form13d,congress --lookback-days 7
# expect: "written : N" > 0. If "written : 0" twice in a row, a source is down (see watchdog note).
```

## 3. RUN THE SESSION (supervised — favor watched single cycles)

Recommended for a SUPERVISED first session: run ONE cycle, watch it, repeat manually.
Do NOT use `daemon` for the first session (it auto-relaunches and long-sleeps; harder to watch).

```bash
# One full decision cycle (reconcile -> breakers -> fuse -> decide -> submit -> exit-monitor):
.venv/bin/python -m arbiter.cli run-cycle 2>&1 | tee -a data/session-$(date +%F).log
```

Watch the printed result each time:
- `orders_submitted` / `opinions_gathered` — what it did this cycle.
- `mode:` MUST read `PAPER (alpaca_paper) — REAL paper orders`. If it says `SIM (no real orders)`, the flip didn't take — STOP and check `EXECUTOR_BACKEND` in `.env`.
- Cross-check fills in the Alpaca paper dashboard (paper-api.alpaca.markets account).

Re-run `run-cycle` periodically during the session (e.g. every 15–30 min). It is idempotent and safe
to re-run (orders dedup by hash; pending fills are promoted on the next cycle; positions reconciled first).

Alternatives (NOT recommended for first session):
- `arbiter run`  = ingest + one cycle (the scheduled/cron entrypoint). Fine, but re-ingests each time.
- `arbiter daemon` = market-hours loop (fast iteration every interval + full cycle at set ET times).
  This is the eventual unattended mode; use it only after the first supervised session is clean.

## 4. WATCH (second terminal, optional)

```bash
# Live audit tail (all alerts/events land here):
tail -f data/audit.jsonl

# Or the read-only dashboard + /health:
.venv/bin/python -m arbiter.cli dashboard --port 8798   # then open http://127.0.0.1:8798/health
```

What WILL page your phone (critical -> ntfy webhook + auto-pause):
- Kill switch reports halted (or unreachable -> fail-closed).
- Any circuit breaker tripped (daily-loss / per-position / broker-failure).
- Broker-fatal error while SUBMITTING an order (BUY or exit SELL).
- Broker account-read failure at cycle start (added 2026-06-21 — fires a critical alert; the cycle
  safely halts with 0 orders).

What will NOT page you (audit-log/console only — watch manually):
- Reconciler ledger/broker divergence (fires `warning`, not delivered to webhook).
- Watchdog findings (stale heartbeat, ingest-0-rows, idle-no-orders) — the watchdog module is
  NOT wired into `run-cycle`/`daemon`, so these do not fire automatically. Watch the tail.

## 5. EMERGENCY HALT (kill switch)

```
Cloudflare dashboard -> Workers -> stockbot -> Settings -> Variables ->
set  HALTED = true  -> Save/Deploy.
```
Propagation: Worker deploy is ~near-instant globally; the engine caches the kill-switch result for
5s (KillSwitch.cache_ttl_seconds) and re-checks at the START of every cycle/fast-iteration. So the
NEXT cycle (or within ~5s of a fast iteration) will see halted=true, fire a critical alert, and
auto-pause. It blocks NEW orders only — it does NOT auto-liquidate open positions (manual action).
A cycle already mid-submission is not interrupted; the halt takes effect on the next cycle boundary.

To resume after a halt: set `HALTED=false` + redeploy, then (if the engine auto-paused) clear the
durable pause latch — re-run is gated by `engine_state.paused`. Resume via a one-shot:
```bash
.venv/bin/python -c "from arbiter.engine import build_engine; build_engine().resume()"
```

## 6. ROLLBACK (instant)

Any ONE of these stops trading:
1. Kill switch -> `HALTED=true` (above). Fastest, off-box.
2. Revert to simulator: edit `.env` -> `EXECUTOR_BACKEND=sim` (line is already commented in .env;
   uncomment the `sim` line and comment the `alpaca_paper` line). Next run no longer touches Alpaca.
3. Restore the DB: stop all runs, then
   `cp data/arbiter.db.pre-golive-bak data/arbiter.db`  (restore point confirmed present).

## 7. RECOVERY (if a cycle crashes mid-run)

Safe to just re-run `run-cycle`. The engine, on the next cycle:
- restores the durable pause latch from `engine_state` (won't silently resume after a fatal pause),
- reconciles pending broker orders first (async fills promoted pending->filled, idempotent),
- reconciles positions (ledger vs broker) and surfaces divergences,
- dedups orders by hash so a half-finished submission round does not double-submit.
No manual cleanup needed for a clean crash. If equity reads as 0 (broker down), the cycle halts with
0 orders — wait for the broker, then re-run.

---

## GO / NO-GO (ops angle): GO — paper-only, broker/kill-switch/alerts verified live; one known gap
(broker-read-failure and reconciler-divergence page only the console, not the phone) is acceptable
for a SUPERVISED session where you are watching the audit tail.
