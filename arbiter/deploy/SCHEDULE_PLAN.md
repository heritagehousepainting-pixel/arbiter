# Arbiter daily schedule — audit + build plan

**For the build agent**: do not deviate from the exact file contents and paths specified below.
This document is the authoritative spec; build exactly what is described.

---

## 1. Audit of existing artifacts

### 1.1 `deploy/com.arbiter.daily.plist`

| Item | Finding | Verdict |
|---|---|---|
| `Label` | `com.arbiter.daily` — matches filename | OK |
| `ProgramArguments` | Absolute venv path `.venv/bin/python -m arbiter.cli run` | OK |
| `WorkingDirectory` | `/Users/jonathanmorris/poly_bot/arbiter` (project root) | OK |
| `StandardOutPath` | `data/arbiter-daily.stdout.log` (absolute path in plist) | OK, **but data/ must exist** — plist does not create it |
| `StandardErrorPath` | `data/arbiter-daily.stderr.log` (absolute path in plist) | OK, same caveat |
| `KeepAlive` | `false` | OK |
| `RunAtLoad` | `false` | OK |
| `StartCalendarInterval` | 08:30 Mon–Fri local time | **BUG: wrong time for post-market analysis** — see §1.5 |
| `EnvironmentVariables` | Not present | **Intentionally omitted** — correct (see §1.3) |
| `PATH` | Not set | **Gap** — harmless because ProgramArguments uses absolute paths; no shell expansion needed |

### 1.2 `deploy/crontab.example`

| Item | Finding | Verdict |
|---|---|---|
| Schedule | `30 8 * * 1-5` — same 08:30 weekdays | **Same timing issue as plist** |
| Absolute venv path | Present | OK |
| `cd` before invocation | Present (`cd /path && python ...`) | OK — ensures relative paths in config/data work |
| Stdout/stderr redirection | `>> data/arbiter-daily.stdout.log 2>> data/arbiter-daily.stderr.log` | OK but **data/ must exist first**; cron will silently discard output if it does not |
| `.env` loading | Cron shells do not source user environment; but config.py loads `.env` itself | OK, same as plist — see §1.3 |

### 1.3 Environment / secrets approach

`arbiter/config.py` calls `_load_dotenv(root)` before reading env vars. That function reads
`root/.env` and calls `os.environ.setdefault()` for each key. This means secrets are loaded
by the Python process itself, not by launchd or cron. **This is the correct design** — neither
launchd's `EnvironmentVariables` key nor cron's sparse shell environment is needed.

However: see §1.4 below for a critical bug that affects `.env` discovery.

### 1.4 CRITICAL BUG — `config.py` root path resolution is off by one level

**File**: `arbiter/config.py` line 175

```python
root = Path(__file__).resolve().parents[2]
```

`__file__` resolves to `.../poly_bot/arbiter/arbiter/config.py`.

| Index | Path | Meaning |
|---|---|---|
| `parents[0]` | `.../poly_bot/arbiter/arbiter/` | Python package directory |
| `parents[1]` | `.../poly_bot/arbiter/` | **Project root (correct)** |
| `parents[2]` | `.../poly_bot/` | Parent of project root — **WRONG** |

**Consequence**: `_load_dotenv` looks for `.env` at `/Users/jonathanmorris/poly_bot/.env`
(does not exist), and `load_config` looks for `config/arbiter.toml` at
`/Users/jonathanmorris/poly_bot/config/arbiter.toml` (does not exist). Because both checks
use `if path.exists()`, they silently degrade to built-in defaults. The app currently runs
only because the defaults happen to be adequate and because `LIVE_TRADING` defaults to `false`.
**Secrets (ALPACA_API_KEY, etc.) from `.env` are NOT loaded by the scheduled process.**

**Fix** (one line — the build agent must apply this):

```python
# arbiter/config.py line 175 — change parents[2] to parents[1]
root = Path(__file__).resolve().parents[1]
```

### 1.5 Schedule time — wrong for the stated purpose

The plist and crontab both fire at **08:30 local time**. The machine timezone is
`America/New_York` (Eastern). US equity markets open at 09:30 ET. A run at 08:30 ET fires
**before** the previous day's closing data is fully available via Alpaca and EDGAR — the
ingest will pull stale or incomplete end-of-day prices and filings.

**Recommended schedule**: **18:30 ET Mon–Fri** (6:30 PM Eastern). This is:
- ~3 hours after the 15:30 ET close — enough time for EDGAR to publish after-market Form 4 filings
- Still within the same calendar day as the trading session being analyzed
- A natural "end of trading day" checkpoint

This requires updating both the plist and crontab.

### 1.6 `deploy/README.md`

| Item | Finding |
|---|---|
| Install steps | Correct for Monterey/Ventura+ dual-path (`launchctl load` vs `bootstrap`) |
| `launchctl start` for testing | Correct |
| Log paths | Consistent with plist |
| Missing: `data/` directory creation | Not mentioned — users will hit silent log-loss |
| Missing: Makefile targets | No mention of `make schedule` / `make unschedule` |
| Missing: TZ documentation | No statement of timezone assumption |
| Missing: `make run` one-shot | Not mentioned |

### 1.7 `Makefile`

The existing `run` target fires `python -m arbiter.cli run-cycle` (not `run`). This is
inconsistent — the schedule fires `arbiter run` (ingest + cycle), but `make run` fires only
the cycle. The build agent should add a `schedule`, `unschedule`, `schedule-status`, and
corrected `run` target.

### 1.8 Log file naming — task brief vs existing plist

The task brief mentioned `data/run.out.log` / `data/run.err.log`. The existing plist uses
`data/arbiter-daily.stdout.log` / `data/arbiter-daily.stderr.log`. The existing plist naming
is clearer. **Keep the plist names; do not rename.**

---

## 2. Build plan

### 2.1 Fix `arbiter/config.py` (prerequisite for everything else)

Change line 175 from `parents[2]` to `parents[1]`. This is a one-line fix that must be
applied first — without it, `.env` and `config/arbiter.toml` are never loaded under
launchd/cron.

### 2.2 Updated `deploy/com.arbiter.daily.plist`

Replace the plist entirely with the following. Changes vs. current:
- `StartCalendarInterval` changed from 08:30 to **18:30** on Mon–Fri (Weekday 1–5)
- Add `<!-- TZ note -->` comment block explaining local-time assumption

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- Label must match the filename (without .plist). -->
    <key>Label</key>
    <string>com.arbiter.daily</string>

    <!--
        ProgramArguments: absolute venv python + module invocation.
        No PATH or EnvironmentVariables key needed — config.py loads .env
        itself using setdefault semantics (real env vars always win).
    -->
    <key>ProgramArguments</key>
    <array>
        <string>/Users/jonathanmorris/poly_bot/arbiter/.venv/bin/python</string>
        <string>-m</string>
        <string>arbiter.cli</string>
        <string>run</string>
    </array>

    <!--
        Schedule: 18:30 local time, Monday–Friday (Weekday 1–5).
        launchd uses the MACHINE LOCAL TIMEZONE — this machine is America/New_York.
        18:30 ET = ~3 hours after market close; EDGAR Form 4 filings and
        Alpaca end-of-day prices are typically available by this time.

        To change the timezone assumption: adjust Hour/Minute to the equivalent
        UTC offset for your local close, or run the machine under a fixed TZ.

        This is a ONE-SHOT trigger — launchd starts the process, it runs
        ingest + one decision cycle, then exits.  KeepAlive is false.
        launchd will NOT fire a second instance if the previous one is still
        running (UserAgent queue serialization).
    -->
    <key>StartCalendarInterval</key>
    <array>
        <dict>
            <key>Weekday</key><integer>1</integer>
            <key>Hour</key><integer>18</integer>
            <key>Minute</key><integer>30</integer>
        </dict>
        <dict>
            <key>Weekday</key><integer>2</integer>
            <key>Hour</key><integer>18</integer>
            <key>Minute</key><integer>30</integer>
        </dict>
        <dict>
            <key>Weekday</key><integer>3</integer>
            <key>Hour</key><integer>18</integer>
            <key>Minute</key><integer>30</integer>
        </dict>
        <dict>
            <key>Weekday</key><integer>4</integer>
            <key>Hour</key><integer>18</integer>
            <key>Minute</key><integer>30</integer>
        </dict>
        <dict>
            <key>Weekday</key><integer>5</integer>
            <key>Hour</key><integer>18</integer>
            <key>Minute</key><integer>30</integer>
        </dict>
    </array>

    <!--
        Logs land in data/ next to the database.
        IMPORTANT: data/ must exist before loading this plist.
        launchd will NOT create missing directories.
        Run: mkdir -p /Users/jonathanmorris/poly_bot/arbiter/data
    -->
    <key>StandardOutPath</key>
    <string>/Users/jonathanmorris/poly_bot/arbiter/data/arbiter-daily.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/jonathanmorris/poly_bot/arbiter/data/arbiter-daily.stderr.log</string>

    <!-- WorkingDirectory ensures relative paths in config/ and data/ resolve. -->
    <key>WorkingDirectory</key>
    <string>/Users/jonathanmorris/poly_bot/arbiter</string>

    <!-- Not a daemon — fire once, then exit. -->
    <key>KeepAlive</key>
    <false/>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
```

### 2.3 Updated `deploy/crontab.example`

Update the schedule time to match 18:30 ET and add the TZ caveat:

```
# Arbiter daily scheduled run — cron alternative to com.arbiter.daily.plist
#
# Schedule: 18:30 local time Mon-Fri. Machine timezone is America/New_York.
# Adjust minute/hour if machine is UTC: 18:30 ET = 23:30 UTC (22:30 in EDT).
#
# Usage:
#   crontab -e
#   (paste the line below)
#
# IMPORTANT: ensure data/ exists before the first run.
#   mkdir -p /Users/jonathanmorris/poly_bot/arbiter/data
#
# Fields: minute hour dom month dow command
#
30 18 * * 1-5 cd /Users/jonathanmorris/poly_bot/arbiter && /Users/jonathanmorris/poly_bot/arbiter/.venv/bin/python -m arbiter.cli run >> data/arbiter-daily.stdout.log 2>> data/arbiter-daily.stderr.log
```

### 2.4 `scripts/schedule.sh`

Create a new file `scripts/schedule.sh`. This is what the Makefile targets delegate to.

```bash
#!/usr/bin/env bash
# scripts/schedule.sh — install / uninstall / status for the Arbiter launchd schedule.
#
# Usage:
#   scripts/schedule.sh install     — copy plist, create data/, load job
#   scripts/schedule.sh uninstall   — unload job, remove plist from LaunchAgents
#   scripts/schedule.sh status      — show launchd status + next fire info
#   scripts/schedule.sh run-now     — trigger an immediate one-shot run via launchctl
#
set -euo pipefail

PLIST_SRC="$(cd "$(dirname "$0")/.." && pwd)/deploy/com.arbiter.daily.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.arbiter.daily.plist"
LABEL="com.arbiter.daily"
DATA_DIR="$(cd "$(dirname "$0")/.." && pwd)/data"
GUI_UID="gui/$(id -u)"

_require_plist_src() {
    if [[ ! -f "$PLIST_SRC" ]]; then
        echo "ERROR: plist not found at $PLIST_SRC" >&2
        exit 1
    fi
}

cmd="${1:-}"

case "$cmd" in
install)
    _require_plist_src
    mkdir -p "$DATA_DIR"
    cp "$PLIST_SRC" "$PLIST_DST"
    # Use bootstrap (Ventura+) with load fallback for older macOS.
    if launchctl bootstrap "$GUI_UID" "$PLIST_DST" 2>/dev/null; then
        echo "Loaded via launchctl bootstrap."
    else
        launchctl load "$PLIST_DST"
        echo "Loaded via launchctl load (pre-Ventura fallback)."
    fi
    echo ""
    echo "Job installed. Verify with: scripts/schedule.sh status"
    ;;

uninstall)
    # Unload first (ignore errors if not loaded).
    if launchctl bootout "$GUI_UID" "$PLIST_DST" 2>/dev/null; then
        echo "Unloaded via launchctl bootout."
    elif launchctl unload "$PLIST_DST" 2>/dev/null; then
        echo "Unloaded via launchctl unload."
    else
        echo "Job was not loaded (or already unloaded)."
    fi
    if [[ -f "$PLIST_DST" ]]; then
        rm "$PLIST_DST"
        echo "Removed $PLIST_DST"
    fi
    echo "Uninstalled."
    ;;

status)
    echo "=== launchd job status ==="
    launchctl list | grep arbiter || echo "(not loaded)"
    echo ""
    echo "=== last 30 lines of stdout log ==="
    if [[ -f "$DATA_DIR/arbiter-daily.stdout.log" ]]; then
        tail -30 "$DATA_DIR/arbiter-daily.stdout.log"
    else
        echo "(no stdout log yet — job has not fired)"
    fi
    echo ""
    echo "=== last 30 lines of stderr log ==="
    if [[ -f "$DATA_DIR/arbiter-daily.stderr.log" ]]; then
        tail -30 "$DATA_DIR/arbiter-daily.stderr.log"
    else
        echo "(no stderr log yet)"
    fi
    echo ""
    echo "Next scheduled fire: launchd fires at 18:30 local time on the next weekday."
    echo "Machine timezone: $(readlink /etc/localtime | sed 's|.*/zoneinfo/||')"
    ;;

run-now)
    echo "Triggering immediate one-shot run via launchctl start ..."
    launchctl start "$LABEL"
    echo "Done. Check logs in $DATA_DIR/"
    ;;

*)
    echo "Usage: $0 {install|uninstall|status|run-now}" >&2
    exit 1
    ;;
esac
```

The script must be marked executable: `chmod +x scripts/schedule.sh`.

### 2.5 `Makefile` additions

Add these targets to the existing Makefile. **Replace the existing `run` target** (which
incorrectly calls `run-cycle`) with one that calls `arbiter run` (full ingest + cycle):

```makefile
# ── Scheduling ──────────────────────────────────────────────────────────────

schedule:
	bash scripts/schedule.sh install

unschedule:
	bash scripts/schedule.sh uninstall

schedule-status:
	bash scripts/schedule.sh status

# ── One-shot manual run (full ingest + cycle, same as the scheduled job) ──

run:
	cd /Users/jonathanmorris/poly_bot/arbiter && .venv/bin/python -m arbiter.cli run

# ── Dry-run / smoke test ─────────────────────────────────────────────────────

run-cycle:
	cd /Users/jonathanmorris/poly_bot/arbiter && .venv/bin/python -m arbiter.cli run-cycle
```

Note: the existing `.PHONY` line must be extended:
```makefile
.PHONY: install test lint run run-cycle schedule unschedule schedule-status
```

### 2.6 Updated `deploy/README.md`

The existing README is mostly correct but needs these additions:

1. **TZ section** — explicitly state machine timezone and the 18:30 ET rationale.
2. **`data/` creation prerequisite** — `mkdir -p data/` must run before `make schedule`.
3. **Makefile targets** — document `make schedule`, `make unschedule`, `make schedule-status`, `make run`.
4. **Log rotation note** — no rotation is set up; advise `newsyslog` or periodic manual clear.
5. **config.py fix** — note that the `parents[2]` bug fix is required for `.env` to load.

---

## 3. Observability and failure handling

### 3.1 Log paths

| Log | Path |
|---|---|
| Scheduled run stdout | `data/arbiter-daily.stdout.log` |
| Scheduled run stderr | `data/arbiter-daily.stderr.log` |
| Structured audit log (all events) | `data/audit.jsonl` |
| SQLite state | `data/arbiter.db` |
| metrics | `data/metrics.jsonl` (created on first run) |

### 3.2 Exit codes

`loop_runner.main()` propagates cycle exceptions — if the cycle raises, Python exits non-zero.
launchd records the exit code in `launchctl list | grep arbiter` output (the first column).
A non-zero exit code there means the last run failed.

### 3.3 Alert webhook

`arbiter/safety/alerting.py` POSTs JSON to `config.alert_webhook_url` (from `ALERT_WEBHOOK_URL`
in `.env`) on `critical`-tier alerts. Network failures are swallowed. This provides real-time
failure notification for circuit-breaker events during a cycle but does **not** cover the case
where the entire scheduled run fails before the engine starts (e.g., import error, `.env` not
found). For that, a wrapper in `schedule.sh` could check the exit code and POST separately —
but that is out of scope for this plan; log tailing is sufficient for the MVP.

### 3.4 Log rotation

No log rotation is configured. `data/arbiter-daily.stdout.log` and `data/arbiter-daily.stderr.log`
will grow unboundedly. Recommended approach (user must set up manually):

Add a `/etc/newsyslog.d/com.arbiter.daily.conf` entry (macOS):
```
/Users/jonathanmorris/poly_bot/arbiter/data/arbiter-daily.stdout.log  640  7  500  *  J
/Users/jonathanmorris/poly_bot/arbiter/data/arbiter-daily.stderr.log  640  7  500  *  J
```

This rotates when files exceed 500 KB, keeps 7 archives, compresses with bzip2 (`J`).
The user must create this file manually.

### 3.5 Idempotency and overlapping-run safety

`run_ingest` deduplicates filings by checking the DB before writing (the ingest runner uses
upsert semantics). `engine.run_cycle` is side-effect-isolated — if run twice in one day it will
re-emit signals but not double-submit orders (order idempotency depends on the executor, which
is currently simulated). Re-firing the daily job is safe.

**Overlapping runs**: launchd UserAgent jobs are serialized — a new fire will not launch while
the previous instance is still running. This eliminates the double-fire risk entirely under
launchd. Under cron, overlapping is possible; `flock` could be added to the crontab line but
is out of scope for this plan given the macOS-primary target.

### 3.6 Dry-run / smoke-test path

`arbiter run-cycle` (and `make run-cycle`) runs only the cycle step, skipping ingest. This is
the fastest smoke test. For a full dry run without writing orders, set `live_trading = false`
in `config/arbiter.toml` (the default) and run `make run`. The system will run through the
full flow in simulation mode.

---

## 4. Timezone assumption — explicit statement

**Machine timezone**: `America/New_York` (confirmed via `/etc/localtime` symlink).
**Scheduled time**: 18:30 ET Mon–Fri.
**Market close**: 15:30 ET (standard US equity session).
**Rationale**: 3-hour buffer gives EDGAR time to publish intraday and after-hours Form 4 filings
and Alpaca time to finalize end-of-day data. Adjust if the machine is not Eastern-timezone.

**If the machine is UTC** (e.g., a remote Linux server): use `23:30` in the crontab instead of
`18:30`. For launchd on macOS this is not relevant (macOS always uses local time).

---

## 5. File ownership for the build agent

The build agent should touch these files in order:

| File | Action | Notes |
|---|---|---|
| `arbiter/config.py` line 175 | Edit: `parents[2]` → `parents[1]` | One-line fix; highest priority |
| `deploy/com.arbiter.daily.plist` | Replace entirely | Use exact XML from §2.2 |
| `deploy/crontab.example` | Replace entirely | Use text from §2.3 |
| `scripts/schedule.sh` | Create new file | Use script from §2.4; `chmod +x` |
| `Makefile` | Edit: replace `run` target; add 3 new targets; extend `.PHONY` | See §2.5 |
| `deploy/README.md` | Edit: add TZ, data/ note, Makefile targets, log rotation, config fix note | See §2.6 |

---

## 6. What the user must do manually (not automated)

1. **Ensure `data/` exists** — `mkdir -p /Users/jonathanmorris/poly_bot/arbiter/data`
   (already exists on this machine, but `make schedule` / `scripts/schedule.sh install` will
   also call `mkdir -p` so it is safe to skip).

2. **Install the schedule** — `make schedule` (or `bash scripts/schedule.sh install`).
   This requires no elevated privileges — LaunchAgents is per-user.

3. **Verify it loaded** — `make schedule-status` or `launchctl list | grep arbiter`.

4. **Test a one-shot run** — `make run` (full ingest + cycle, no wait for scheduler) or
   `bash scripts/schedule.sh run-now` (fires via launchctl, same as the scheduled path).

5. **Set up log rotation** (optional but recommended for long-running installs) — create
   `/etc/newsyslog.d/com.arbiter.daily.conf` as specified in §3.4. This requires `sudo`.

6. **Confirm `.env` is populated** — after the `parents[2]→parents[1]` fix, secrets will be
   loaded from `.env`. The file already exists at the project root and contains the expected
   keys (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `EDGAR_USER_AGENT`, `ALERT_WEBHOOK_URL`, etc.).

7. **macOS Full Disk Access** — if the `data/` directory is under a protected location and
   launchd cannot write to it, grant Terminal (or `python`) Full Disk Access in
   System Settings → Privacy & Security → Full Disk Access. This machine's `data/` is under
   the user's home directory, so this is unlikely to be required, but note it as a fallback
   if logs don't appear after the first scheduled fire.
