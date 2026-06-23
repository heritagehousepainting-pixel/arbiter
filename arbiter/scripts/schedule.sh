#!/usr/bin/env bash
# scripts/schedule.sh — install / uninstall / status for the Arbiter launchd schedule.
#
# Usage:
#   scripts/schedule.sh install     — copy plist, create data/, load job
#   scripts/schedule.sh uninstall   — unload job, remove plist from LaunchAgents
#   scripts/schedule.sh status      — show launchd status + last log lines
#   scripts/schedule.sh run-now     — trigger an immediate one-shot run via launchctl
#
#   scripts/schedule.sh install-daemon   — install the market-hours runtime daemon
#                                           (KeepAlive=true; sub-project #3)
#   scripts/schedule.sh uninstall-daemon — unload + remove the daemon
#   scripts/schedule.sh daemon-status    — daemon launchd status + last log lines
#
# C6: the daily 18:30 one-shot (com.arbiter.daily) is a flock-guarded "daemon was
# down" fallback.  When the daemon holds data/arbiter-daemon.pid, the one-shot
# `arbiter run` no-ops cleanly (see orchestrator/loop_runner.main).  Running both
# is safe — every mutate path is idempotent + durable and the flock prevents
# concurrent engine mutation against the same SQLite DB.
#
set -euo pipefail

PLIST_SRC="$(cd "$(dirname "$0")/.." && pwd)/deploy/com.arbiter.daily.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.arbiter.daily.plist"
LABEL="com.arbiter.daily"
DAEMON_PLIST_SRC="$(cd "$(dirname "$0")/.." && pwd)/deploy/com.arbiter.daemon.plist"
DAEMON_PLIST_DST="$HOME/Library/LaunchAgents/com.arbiter.daemon.plist"
DAEMON_LABEL="com.arbiter.daemon"
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
    # Idempotency guard: don't try to load an already-loaded job (avoids the
    # confusing "false success" path where launchctl load no-ops + prints noise).
    if launchctl list 2>/dev/null | grep -q "$LABEL"; then
        echo "Job already loaded. Run 'scripts/schedule.sh uninstall' first to reinstall."
        exit 0
    fi
    mkdir -p "$DATA_DIR"
    cp "$PLIST_SRC" "$PLIST_DST"
    # Use bootstrap (Ventura+) with load fallback for older macOS.
    if launchctl bootstrap "$GUI_UID" "$PLIST_DST" 2>/dev/null; then
        echo "Loaded via launchctl bootstrap."
    elif launchctl load "$PLIST_DST" 2>/dev/null; then
        echo "Loaded via launchctl load (pre-Ventura fallback)."
    else
        echo "ERROR: launchctl could not load the job. Check 'launchctl list' and the plist." >&2
        exit 1
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
    # Pre-check: launchctl start only works on a loaded job; fail clearly otherwise.
    if ! launchctl list 2>/dev/null | grep -q "$LABEL"; then
        echo "ERROR: job not loaded — run 'scripts/schedule.sh install' (or 'make schedule') first." >&2
        echo "       (For a one-shot WITHOUT installing the schedule, use: make run)" >&2
        exit 1
    fi
    echo "Triggering immediate one-shot run via launchctl start ..."
    launchctl start "$LABEL"
    echo "Done. Check logs in $DATA_DIR/"
    ;;

install-daemon)
    if [[ ! -f "$DAEMON_PLIST_SRC" ]]; then
        echo "ERROR: daemon plist not found at $DAEMON_PLIST_SRC" >&2
        exit 1
    fi
    if launchctl list 2>/dev/null | grep -q "$DAEMON_LABEL"; then
        echo "Daemon already loaded. Run 'scripts/schedule.sh uninstall-daemon' first."
        exit 0
    fi
    mkdir -p "$DATA_DIR"   # C7b: launchd will NOT create data/ — create it here.
    cp "$DAEMON_PLIST_SRC" "$DAEMON_PLIST_DST"
    if launchctl bootstrap "$GUI_UID" "$DAEMON_PLIST_DST" 2>/dev/null; then
        echo "Daemon loaded via launchctl bootstrap (RunAtLoad=true → starts now)."
    elif launchctl load "$DAEMON_PLIST_DST" 2>/dev/null; then
        echo "Daemon loaded via launchctl load (pre-Ventura fallback)."
    else
        echo "ERROR: launchctl could not load the daemon. Check 'launchctl list'." >&2
        exit 1
    fi
    echo ""
    echo "Daemon installed. Verify with: scripts/schedule.sh daemon-status"
    echo "Keep the daily one-shot installed as the post-close 'daemon was down' backstop."
    ;;

uninstall-daemon)
    if launchctl bootout "$GUI_UID" "$DAEMON_PLIST_DST" 2>/dev/null; then
        echo "Daemon unloaded via launchctl bootout."
    elif launchctl unload "$DAEMON_PLIST_DST" 2>/dev/null; then
        echo "Daemon unloaded via launchctl unload."
    else
        echo "Daemon was not loaded (or already unloaded)."
    fi
    if [[ -f "$DAEMON_PLIST_DST" ]]; then
        rm "$DAEMON_PLIST_DST"
        echo "Removed $DAEMON_PLIST_DST"
    fi
    echo "Daemon uninstalled."
    ;;

daemon-status)
    echo "=== daemon launchd status ==="
    launchctl list | grep "$DAEMON_LABEL" || echo "(daemon not loaded)"
    echo ""
    echo "=== heartbeat ==="
    if [[ -f "$DATA_DIR/arbiter-daemon.heartbeat" ]]; then
        cat "$DATA_DIR/arbiter-daemon.heartbeat"; echo ""
    else
        echo "(no heartbeat file yet)"
    fi
    echo ""
    echo "=== last 30 lines of daemon stdout log ==="
    if [[ -f "$DATA_DIR/arbiter-daemon.stdout.log" ]]; then
        tail -30 "$DATA_DIR/arbiter-daemon.stdout.log"
    else
        echo "(no daemon stdout log yet)"
    fi
    echo ""
    echo "=== last 30 lines of daemon stderr log ==="
    if [[ -f "$DATA_DIR/arbiter-daemon.stderr.log" ]]; then
        tail -30 "$DATA_DIR/arbiter-daemon.stderr.log"
    else
        echo "(no daemon stderr log yet)"
    fi
    ;;

*)
    echo "Usage: $0 {install|uninstall|status|run-now|install-daemon|uninstall-daemon|daemon-status}" >&2
    exit 1
    ;;
esac
