.PHONY: install test lint run run-cycle schedule unschedule schedule-status run-now

install:
	pip install -e ".[dev]"

test:
	cd /Users/jonathanmorris/poly_bot/arbiter && python -m pytest tests/ -q

lint:
	bash scripts/check_no_lookahead.sh

# ── One-shot manual run (full ingest + cycle, same as the scheduled job) ──

run:
	cd /Users/jonathanmorris/poly_bot/arbiter && .venv/bin/python -m arbiter.cli run

# ── Cycle-only smoke test (skips ingest, fastest check) ──────────────────────

run-cycle:
	cd /Users/jonathanmorris/poly_bot/arbiter && .venv/bin/python -m arbiter.cli run-cycle

# ── Scheduling ──────────────────────────────────────────────────────────────

schedule:
	bash scripts/schedule.sh install

unschedule:
	bash scripts/schedule.sh uninstall

schedule-status:
	bash scripts/schedule.sh status

run-now:
	bash scripts/schedule.sh run-now
