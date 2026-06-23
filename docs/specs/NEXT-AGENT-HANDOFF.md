# Arbiter — Next-Session Handoff (paste the PROMPT block below)

Status date: **2026-06-19**. Everything under the prompt is reference detail.

---

## PROMPT (paste this into a fresh Claude session)

You are continuing work on **arbiter**, a local-first Python "smart-money decision engine" at
`/Users/jonathanmorris/poly_bot/arbiter/`. It is an autonomous, self-learning PAPER trading system. The MVP,
five sub-projects, a full audit, AND an audit-driven upgrade pass are all DONE — **do not rebuild them.**

First, orient yourself:
- Read `/Users/jonathanmorris/poly_bot/docs/specs/NEXT-AGENT-HANDOFF.md` (this file) end to end.
- Skim `arbiter/INTERFACES.md` (the contract bible — recently reconciled), `docs/audit/00-INDEX.md` (the 36-lane
  audit synthesis), and `docs/specs/UPGRADES-PLAN.md` (what was just built + what's deferred).
- Your memory files `arbiter-project`, `user-workflow-parallel-agents`, and the MEMORY.md audit/upgrade bullets
  load automatically.

**Always use `.venv/bin/python`** (system `python3.14` has no pytest). Verify health before/after any change:
`cd /Users/jonathanmorris/poly_bot/arbiter && .venv/bin/python -m pytest tests/ -q` (expect **~2088 passing,
~30s**), plus `bash scripts/check_no_lookahead.sh` AND `bash scripts/check_insert_only.sh` (both must stay clean).
Tests MUST stay OFFLINE (no network, no real sleeps); the `arbiter` CLI (`run`, `ingest`, `daemon`, `backfill`)
DOES hit the real network.

**Current state (all green, still on paper-sim):**
- Sub-projects DONE: #1 real Alpaca paper execution, #2 exit/sell monitor, #3 market-hours daemon
  (`arbiter daemon`), #4 learning loop, #5a real outcome attribution. The learning loop genuinely learns now
  (a confidently-wrong advisor gets suppressed — verified end-to-end).
- A 36-lane read-only audit (`docs/audit/`) found ~11 P0s; an upgrade pass then fixed **10 of 11** + added the
  historical outcome-backfill harness. Risk caps now BIND, ADV is dollar-volume, reconciler is wired, the
  calendar is correct, the learning signal's label-leaks are closed, graduation is significance-gated, and there
  is a real `/health` + silent-failure watchdogs. See `docs/specs/UPGRADES-PLAN.md` for the full list + the
  remaining minor P2s.
- **`EXECUTOR_BACKEND=sim` (NOT live).** A dedicated **$10k Alpaca PAPER account** is verified and its keys are
  staged in `arbiter/.env` (with `ARBITER_MAX_OPEN_POSITIONS=8`, `ARBITER_MAX_GROSS_PCT=0.50`). Flipping to
  `EXECUTOR_BACKEND=alpaca_paper` is the go-live switch — the user said **NOT to go live until the building is
  finished**, so confirm before flipping.

**What's left / the real options (ask the user which, don't assume):**
1. **Form-4 insider ingest** — discovery is broken AND needs the user to set `EDGAR_USER_AGENT="Name email@host"`.
   Fixing+enabling it adds the second (better-evidenced) A1 advisor. NEEDS-USER.
2. **MiroFish A2 (#5b)** — the second "brain"; BLOCKED on the user standing up a self-hosted inference service at
   `MIROFISH_ENDPOINT` (contract: `POST /analyze {ticker,as_of,idea_fingerprint}` -> `{opinions:[{stance_score,
   confidence,horizon_days,rationale,source_fingerprint}],run_id}`, localhost-only egress, must emit NEGATIVE
   stances). The `adapters/mirofish/` client exists; inert without the endpoint.
3. **Run the backfill harness** (`arbiter backfill`) + verify the learning loop on accumulated data.
4. **`engine.py` refactor** (it's a ~1400-line god-object — H1), 13D/13G advisor, deep stats research, the minor
   P2 refinements (wider sector map, etc.).
5. **Go live**: flip to `alpaca_paper` + a supervised first run, then unattended (first set `KILL_SWITCH_URL` +
   `ALERT_WEBHOOK_URL`, both currently empty).

**How the user likes to work (honor this):** they spawn **batches of parallel Sonnet agents** and say things like
"spawn N agents to plan, audit the plan, build, audit, in a loop." Run them as **plan -> audit-plan -> build ->
audit waves with strictly DISJOINT file ownership** against frozen contracts (the architecture is a dependency
DAG; the repo is NOT git-tracked so there are no worktrees — parallel agents share one tree, so keep ownership
disjoint and have each agent run only ITS targeted tests; you run the full suite after each wave). For a big find
like a P0, fold a BINDING-AMENDMENTS section into the spec before building. **Push back on wasteful agent counts**
(e.g. "75 agents on a loop") — one agent per genuinely-independent domain, not redundant clones. Keep tests
offline + the suite green, save plans to `docs/specs/`, log audits to `docs/audit/`, and **verify on LIVE data
where possible** (live runs catch what offline tests miss). Stay in planning mode if told.

Start by confirming you've read the materials, summarizing the current state in your own words, and asking which
of the options above to pursue (or whatever the user directs).

---

## Reference — verification & key paths
- Health: `cd /Users/jonathanmorris/poly_bot/arbiter && .venv/bin/python -m pytest tests/ -q` (~2088),
  `bash scripts/check_no_lookahead.sh`, `bash scripts/check_insert_only.sh`.
- Plans/specs: `docs/specs/*.md` (esp. `UPGRADES-PLAN.md` + the five `2026-06-19-*-design.md` sub-project specs).
- Audit: `docs/audit/00-INDEX.md` + 36 lane files. Research memos: `docs/specs/research/`.
- Config/secrets: `arbiter/.env` (gitignored; repo not git-tracked). `EXECUTOR_BACKEND`, `EDGAR_USER_AGENT`,
  `KILL_SWITCH_URL`, `ALERT_WEBHOOK_URL`, `MIROFISH_ENDPOINT` are the levers.

## Carry-forward gotchas
- `EDGAR_USER_AGENT` empty -> Form-4 skipped every run (congress-only signal today, the weaker one).
- `KILL_SWITCH_URL` + `ALERT_WEBHOOK_URL` empty -> no remote stop / no alert delivery; set both before any
  UNATTENDED run.
- Learning is plumbed + correct but data-starved until trades accumulate (or `arbiter backfill` is run).
- Deferred minor P2s are listed in `docs/audit/00-INDEX.md` + `UPGRADES-PLAN.md` — none block.
