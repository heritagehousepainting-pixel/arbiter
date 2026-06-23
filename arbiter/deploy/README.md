# Arbiter scheduled-run deployment

Arbiter runs as a **scheduled trigger, not a resident daemon**.  Each
invocation runs ingest then one decision cycle, then exits cleanly.
Nothing persists between runs except the SQLite database and audit log.

---

## Timezone assumption

**Machine timezone**: `America/New_York` (Eastern).

**Scheduled time**: **18:30 ET, Monday–Friday** (~3 hours after the 15:30 ET
market close).  This buffer allows EDGAR to publish intraday and after-hours
Form 4 filings and Alpaca to finalize end-of-day price data before the cycle
runs.

`launchd` fires at machine local time — no TZ key is needed in the plist.
If you move the machine to a different timezone (or run on a UTC server under
cron), adjust the `Hour`/`Minute` in the plist or `crontab.example` to the
equivalent local time:

| Timezone | Time to use |
|---|---|
| America/New_York (ET) | 18:30 |
| America/Chicago (CT) | 17:30 |
| America/Denver (MT) | 16:30 |
| America/Los_Angeles (PT) | 15:30 |
| UTC | 23:30 (22:30 during EDT) |

---

## Prerequisites

### 1. `data/` directory must exist

`launchd` will **not** create missing directories.  If the log paths in the
plist point to a non-existent directory the scheduled job will run silently
with no output.

```bash
mkdir -p /Users/jonathanmorris/poly_bot/arbiter/data
```

`make schedule` (and `scripts/schedule.sh install`) calls `mkdir -p data/`
automatically, so this is only a concern if you load the plist by hand.

### 2. `.env` must be populated

`arbiter/config.py` loads `<project_root>/.env` at startup.  Secrets
(`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `EDGAR_USER_AGENT`, etc.) must be
present there.  The scheduled job inherits no shell environment from launchd —
Python reads the file directly.

> **Note**: a `parents[2] → parents[1]` bug in `config.py` was fixed — without
> this fix the `.env` file was not found and secrets silently fell back to empty
> defaults.  Ensure you are running the fixed version (line 175 reads
> `parents[1]`).

---

## Quick start (Makefile)

```bash
# One-time setup
mkdir -p data/

# Install and activate the launchd schedule
make schedule

# Verify it loaded
make schedule-status

# Run a one-shot test immediately (same as the scheduled path)
make run

# Remove the schedule
make unschedule
```

### All relevant Makefile targets

| Target | What it does |
|---|---|
| `make run` | Full ingest + cycle (identical to what the schedule runs) |
| `make run-cycle` | Cycle only (skips ingest — fastest smoke test) |
| `make schedule` | Install plist to `~/Library/LaunchAgents/` and load it |
| `make unschedule` | Unload and remove the plist |
| `make schedule-status` | Show launchd status + last 30 lines of each log |
| `make run-now` | Trigger an immediate one-shot run via `launchctl start` |

---

## launchd (macOS — recommended)

### Manual install steps (if not using `make schedule`)

#### 1. Ensure `data/` exists

```bash
mkdir -p /Users/jonathanmorris/poly_bot/arbiter/data
```

#### 2. Copy the plist to the LaunchAgents directory

```bash
cp deploy/com.arbiter.daily.plist ~/Library/LaunchAgents/
```

#### 3. Load the job

```bash
# macOS Ventura+ (recommended):
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.arbiter.daily.plist

# Older macOS fallback:
launchctl load ~/Library/LaunchAgents/com.arbiter.daily.plist
```

#### 4. Verify it is loaded

```bash
launchctl list | grep arbiter
# Expected: a line with "com.arbiter.daily"
# The first column is the exit code of the last run (- = not yet run, 0 = success).
```

#### 5. Run immediately for testing

```bash
launchctl start com.arbiter.daily
# Then check logs:
tail -f data/arbiter-daily.stdout.log
```

#### 6. Unload / remove

```bash
# Ventura+:
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.arbiter.daily.plist
# Older macOS:
launchctl unload ~/Library/LaunchAgents/com.arbiter.daily.plist

rm ~/Library/LaunchAgents/com.arbiter.daily.plist
```

---

## cron (alternative)

See `deploy/crontab.example` for the equivalent cron line.

```bash
crontab -e
# paste the line from crontab.example
```

---

## Logs

| Log | Path |
|---|---|
| Scheduled run stdout | `data/arbiter-daily.stdout.log` |
| Scheduled run stderr | `data/arbiter-daily.stderr.log` |
| Structured audit log | `data/audit.jsonl` |
| SQLite state | `data/arbiter.db` |
| Metrics | `data/metrics.jsonl` |

Paths are relative to `WorkingDirectory` (`/Users/jonathanmorris/poly_bot/arbiter`).

### Log rotation

No rotation is configured by default.  `arbiter-daily.stdout.log` and
`arbiter-daily.stderr.log` will grow unboundedly.

**Recommended**: add a `newsyslog` config (macOS):

```
# /etc/newsyslog.d/com.arbiter.daily.conf
# Rotate at 500 KB, keep 7 compressed archives (requires sudo to create)
/Users/jonathanmorris/poly_bot/arbiter/data/arbiter-daily.stdout.log  640  7  500  *  J
/Users/jonathanmorris/poly_bot/arbiter/data/arbiter-daily.stderr.log  640  7  500  *  J
```

**Quick manual clear** (safe because the file is appended, not held open between runs):

```bash
> data/arbiter-daily.stdout.log
> data/arbiter-daily.stderr.log
```

---

## macOS Full Disk Access

If logs do not appear after the first scheduled fire, grant `python` (or
Terminal) Full Disk Access in **System Settings → Privacy & Security → Full
Disk Access**.  This is typically not needed when `data/` is inside the user's
home directory, but is a common cause of silent log-loss on newer macOS.

---

## What the job does

```
arbiter run
  -> loop_runner.main()
       1. build_engine(config)            # wires all lanes
       2. run_ingest(config, conn, clock)  # pull new Form 4 / Congress filings
          (fault-isolated: if ingest fails, step 3 still runs on stored data)
       3. engine.run_cycle(as_of)         # fuse opinions -> decide -> execute
       4. exits
```

This is intentionally **not** a daemon.  The Python process lives only for
the duration of one ingest + cycle pass.  There is no socket, no listener,
no background thread that persists.  The launchd scheduler is the only thing
that keeps the cadence.

---

## Triggering manually

```bash
# Via Makefile (recommended):
make run

# Direct invocation:
cd /Users/jonathanmorris/poly_bot/arbiter
.venv/bin/python -m arbiter.cli run

# If installed in editable mode:
arbiter run
```
