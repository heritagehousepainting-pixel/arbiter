# K1 — Observability & Operability for Unattended Running

**Auditor lane:** K1 (read-only)
**Date:** 2026-06-19
**Scope:** Can an operator tell what the unattended bot is doing, and would they KNOW when something silently breaks?
**Files reviewed:** `arbiter/logging_setup.py`, `arbiter/metrics.py`, `arbiter/db/audit.py`, `arbiter/safety/alerting.py`, `arbiter/web/server.py`, `arbiter/runtime/daemon.py`, `arbiter/cli.py` (`status`), `arbiter/engine.py` (alert/status), `arbiter/orchestrator/loop_runner.py`, `scripts/schedule.sh`, `deploy/`.

---

## VERDICT

**FAIL for unattended operation.** The system has competent *plumbing* — structured JSON logs, an append-only audit log, a metrics.jsonl writer, an atomic heartbeat, a read-only dashboard, and a tiered alerting class with an auto-pause sentinel. But the alerting is wired to **exactly four loud, broker-fatal conditions** (kill-switch halted, breaker already tripped, broker-fatal BUY reject, broker-fatal SELL reject), and **the alert webhook URL is empty** (`ALERT_WEBHOOK_URL=` in `.env`), so even those four fire only into the audit log — no push reaches a human. **Every silent-degradation surface this overall audit found (broken ADV cap, inert risk caps, dead Stooq fallback, Form-4 returning 0, fallback-attribution, unwired reconciler) fails INVISIBLY**: none of them trips a breaker, raises a `BrokerError`, or calls `alert()`, and none is surfaced in `arbiter status` or the dashboard as an anomaly. There is **no health-check anywhere** that asserts "ingest wrote >0 rows," "not every order was skipped," "fallback rate is low," "a data source answered," or "we've had a fill in N days." The `/health` endpoint is a static `{"status":"ok"}` literal. The daemon heartbeat is written but **nothing ever reads it back to detect staleness** — `daemon-status` just `cat`s the raw JSON and asks the operator to eyeball the timestamp. The operator's blind spot is essentially the entire space of *quiet wrongness*: the bot will keep running, keep logging "cycle_complete: orders_submitted=0," and look healthy while silently doing nothing useful.

---

## FINDINGS

### P0 — Alert webhook is unconfigured; no alert ever reaches a human — `.env` / `safety/alerting.py:166-169`
`ALERT_WEBHOOK_URL=` is empty in `.env` (and `.env.example`). `_post_webhook()` early-returns with a single `log.warning("alerting.no_webhook_url")` when the URL is blank. Therefore **no critical alert is ever delivered out-of-band** — the only record is a line in `audit.jsonl` that no human is watching. For an *unattended* bot this is the headline failure: the entire alerting tier is decorative until a real URL (Slack/Discord/PagerDuty/email-gateway) is set. **Action:** set a real `ALERT_WEBHOOK_URL`; add a startup check that logs at WARNING (or refuses to start in live mode) when live_trading is true and the URL is empty.

### P0 — Silent-degradation surfaces fire NO alert and show NO status anomaly — `engine.py:555/621/633/1083` (only alert sites)
`alert("critical", …)` is reachable from exactly four conditions, all "loud" broker/breaker faults. The degradations this audit found are all *quiet*: broken ADV cap (orders sized wrong but still "filled"), inert risk caps (no rejection raised), dead Stooq fallback (price simply unavailable → idea skipped silently), Form-4 returning 0 rows (ingest "succeeds" with `n_written=0`), fallback-attribution mislabel, and the unwired reconciler (fills never promoted). **None** of these raises `BrokerError`, trips a breaker, or calls `alert()`. They produce, at most, a `log.debug`/`log.info` line buried in JSON stdout. **Action:** introduce a "data/decision quality" alert path (tier `warning`/`critical`) driven by invariant checks (see OPPORTUNITIES) so quiet wrongness becomes loud.

### P0 — No health-check detects "ingest wrote 0 rows" or "ingest errored" — `orchestrator/loop_runner.py:99-121`, `cli.py:127-135`
`run_ingest` returns an `IngestSummary` with `n_fetched/n_written/n_skipped/errors`, and `loop_runner` captures `ingest_ok`/`ingest_error` — but **nothing acts on them.** `n_written == 0` (the exact symptom of the Form-4-returns-0 bug) is treated identically to a healthy run; the cycle proceeds on stale stored filings. `cli.py ingest` merely echoes the counts to a terminal no operator is reading at 09:45. **Action:** in `loop_runner`/daemon, when `n_written == 0` for an enabled source, or `summary.errors` is non-empty, fire a `warning` alert and record a metric; surface "last ingest: N written / M errors" in `status` and the dashboard.

### P0 — `/health` is a hardcoded `{"status":"ok"}` — `web/server.py:373-377`
`_handle_health()` always returns 200 with a static literal regardless of whether the DB is reachable, the daemon is alive, the engine is paused, breakers are tripped, or ingest is stale. Any external uptime monitor pointed at `/health` will report green while the bot is comatose. This is the single most misleading surface in the system — it actively manufactures false confidence. **Action:** make `/health` assert real liveness: DB query succeeds, heartbeat age < threshold, no tripped breaker / not paused → 200; otherwise 503 with a reason. (Note: it is also bound to `127.0.0.1` only, so a remote monitor cannot reach it at all without a tunnel — fine for security, but means there is currently *no* externally reachable health signal.)

### P1 — Heartbeat is written but never read for staleness — `runtime/daemon.py:71-84,277-291`; `scripts/schedule.sh:158-163`
`_heartbeat()` atomically rewrites a JSON file every iteration (good), and it carries useful fields (`now`, `is_open`, `open_positions`, `paused`, `backoff_s`). But **nothing consumes it to detect a dead/stuck daemon.** `daemon-status` just `cat`s the file; the operator must manually parse the `now` timestamp and compute "is this stale?" in their head. A daemon wedged in the broad `except` backoff loop (`daemon.py:204-210`) keeps rewriting the heartbeat with a growing `backoff_s` but no alert fires. **Action:** add a `heartbeat-check` (cron/launchd or part of `/health`) that flags age > 2× `fast_interval_s` while the market is open, and treat a large `backoff_s` as a degraded signal.

### P1 — `arbiter status` shows no time-based or pipeline-health signal — `cli.py:84-102`, `engine.py:1218-1254`
`engine.status()` returns mode, executor, tripped breakers, open positions, advisor count, equity/cash, paused. It deliberately and correctly hides fake `realized_pl` for the broker — good honesty. But it exposes **no heartbeat age, no last-cycle time, no last-fill date, no last-ingest counts, no fallback/skip rates.** An operator running `arbiter status` cannot answer "is the pipeline actually doing work?" — only "is the process structurally configured?" A bot that has submitted 0 orders for two weeks because every idea is silently skipped looks identical to a healthy idle bot. **Action:** add `last_cycle_at`, `last_fill_at`, `last_ingest {written,errors}`, `heartbeat_age_s`, and `paused_reason` to `status()` and the CLI output.

### P1 — Engine auto-pause is invisible except via the dashboard/CLI; pause itself raises no alert beyond the triggering event — `engine.py:564-583`
When a critical condition latches `self.paused = True`, the pause is persisted (good, survives daemon relaunch) and the *triggering* alert was fired — but if the operator misses that one audit line, the only way to learn the bot is paused is to actively run `status` or load the dashboard. There is no recurring "still paused after N hours" reminder. With the webhook empty (P0), a live bot can sit auto-paused indefinitely while the operator believes it's trading. **Action:** emit a periodic `warning` while `paused` is true; show `paused`/`paused_reason` prominently on the dashboard (currently `paused` is in the heartbeat dict but not rendered as a banner on the web page).

### P2 — Metrics coverage is thin and not monitored — `metrics.py`, `engine.py:824,1159`, `evaluation/attribution.py:125`
Only three event types are ever recorded: `cycle_complete`, `attribution.opinion_persist_error`, and one attribution event. There is no metric for ingest rows, skip/fallback rate, data-source failures, order rejections, or fills. metrics.jsonl is append-only with **no reader, no aggregation, and no alerting on any metric** — it is a write-only sink. The `recorded_at="CLOCK_NOT_WIRED"` sentinel (`metrics.py:43`) means a caller that forgets to pass the clock writes an un-time-orderable line, silently. **Action:** expand the event vocabulary to cover the pipeline stages above, and add a lightweight reader (in `/health` or a cron) that alerts on absence/anomaly.

### P2 — Fallback / data-source degradation is `log.debug`/`log.info` only — `data/sources/stooq.py:124,211`
A Stooq miss logs `stooq_bars_not_found` at INFO and a row skip at DEBUG. There is no counter, no metric, and no alert when the fallback rate spikes or the source goes fully dark. With JSON logs going to stdout/rotating files that nobody tails, a data source silently dying is undetectable until positions stop being entered. The market-calendar staleness warning (`runtime/market_calendar.py:75-89`) is the same pattern — a `log` line, not an alert. **Action:** count fallback/skip events per cycle, record as a metric, and alert when the per-cycle skip rate crosses a threshold.

### P2 — Reconciler is invoked but a no-promotion outcome is silent — `engine.py:283,667,875`; `execution/reconciler.py:104`
`_reconcile_pending_orders` IS called from fast iteration and post-close (so the "unwired reconciler" symptom, if real, is a behavioral bug elsewhere, not a missing call). Regardless, if reconciliation runs and promotes **zero** pending orders cycle after cycle while pending rows pile up, nothing notices — there is no "pending orders older than N minutes" or "fills stuck" alarm. **Action:** alert when pending-order count grows monotonically or when an order has been pending past a freshness window.

### P3 — Logs are structured JSON to stdout with no shipping/retention policy in-repo — `logging_setup.py`, `deploy/com.arbiter.daemon.plist`
Logging is clean JSON (good for machine parsing) but goes to stdout, captured by launchd into `arbiter-daemon.stdout.log`/`.stderr.log` with no rotation/shipping defined in-repo and no index/search. For unattended running this is a forensic resource only — you go to it *after* you already know something broke. It is not a detection mechanism. **Action:** document log rotation; optionally ship to a queryable sink. Lower priority than wiring alerts.

---

## OPPORTUNITIES TO ADD (the health-checks / alerts that SHOULD exist)

These are the missing detectors that would convert today's silent failures into operator-visible signals. Highest leverage first.

1. **Live `/health` (replace the static literal).** 503 + reason when: DB unreachable, heartbeat age > 2× fast_interval while market open, any breaker tripped, engine paused, or last successful cycle older than expected. This single change gives any external uptime monitor a true signal.

2. **"Ingest wrote 0 rows" / "ingest errored" alert.** Driven off the existing `IngestSummary.n_written` and `.errors` in `loop_runner`/daemon. Directly catches the Form-4-returns-0 class.

3. **"Every order skipped / no orders in N cycles" alert.** Compare `orders_submitted` vs `ideas_processed` over a rolling window (data already in the `cycle_complete` metric). Catches inert-risk-cap / broken-ADV-cap / dead-fallback symptoms where ideas exist but nothing ships.

4. **"No fills in N market-days" watchdog.** Cron/health check over the orders table; alert if the newest FILLED order is older than a threshold while the bot reports itself active.

5. **"Fallback rate high / data source down" detector.** Per-cycle counter of price-source misses and advisor fallbacks; alert when the rate crosses a threshold or a source returns 0 across a whole cycle.

6. **Heartbeat staleness watchdog.** Independent process (launchd timer) that reads the heartbeat file and alerts on age or runaway `backoff_s` — i.e. the daemon is wedged in its catch-all backoff loop.

7. **"Still paused" recurring reminder.** Periodic `warning` while `engine.paused` is true so an auto-pause can't sit unnoticed (critical once the webhook is live).

8. **Pending-order / reconciliation stall alert.** Alert when pending orders age past a window or the pending count grows without promotions.

9. **Startup config sanity gate.** On `daemon`/`run` start in live mode, refuse (or loudly warn) when `ALERT_WEBHOOK_URL` is empty, when 0 advisors are registered, or when the audit/metrics paths are unwritable — fail-loud at boot instead of silent at runtime.

10. **A real `/metrics` or status-rollup reader.** metrics.jsonl is currently write-only; a tail-and-aggregate step (feeding #2–#5) turns the existing sink into a detection layer with little new plumbing.
