# Handoff prompt — complete robotics increment 3d (probationary A5.robotics advisor)

Paste the block below into a fresh Claude Code session in `poly_bot`. It is
self-contained. Optionally prefix with `/loop ` to have the agent self-pace the
build loop.

---

You are taking over an in-progress build in the `poly_bot` monorepo. Your job: complete increment **3d** of the robotics early-insight signal — the probationary **A5.robotics advisor** — working in a TDD loop (write failing test → implement → green → commit → next), one task at a time, until 3d is fully built, tested, and verified. **Read your memory first** (`MEMORY.md` → `robotics-sector-map.md`); it has the full backstory and operational details. Before writing any code, read the design spec and the A4.macro templates named below.

## Where the work lives
- Git worktree: `/Users/jonathanmorris/poly_bot/.worktrees/robotics-watchlist`, branch `robotics-watchlist` (12 commits, NOT pushed/merged). Do ALL work here.
- Do NOT touch the main checkout (`/Users/jonathanmorris/poly_bot`, on branch `arbiter-equity-unfreeze` with unrelated uncommitted WIP).
- Design spec (read first): `docs/specs/2026-07-13-robotics-signal-design.md` (in the worktree).
- Package layout: arbiter is nested at `arbiter/arbiter/`. Canonical cockpit is repo-root `cockpit/` — there is a STALE duplicate `arbiter/cockpit/`; ignore it, and when importing put repo-root paths first.

## Already done — do NOT redo
- #1 map, #2 cockpit board (verified live), 3a canonical universe (`arbiter/arbiter/data/robotics_universe.py`), 3b scanner (scheduled Mon+Thu via daemon guard, phone digest), 3c persistence + cockpit feed (`robotics_signals` table via migration `035_robotics_signals.sql`, `GET /robotics-signals`, RoboticsPanel "RECENT SIGNALS" feed).
- The scan writes developments/trigger-hits to the `robotics_signals` table via `arbiter/arbiter/robotics_signal/store.py` (`persist_signals`, `read_recent_signals`). Row fields: `as_of, headline, summary, category, symbols` (csv), `trigger_hit` (0/1), `trigger_name` (a universe symbol), `sources` (csv).

## Your task — 3d: probationary A5.robotics advisor
Mirror the **A4.macro** pattern exactly. Read these templates first:
- `arbiter/arbiter/adapters/a4/pipeline.py` — `gather_a4_opinions(conn, clock, config) -> list[Opinion]`: advisor id `"A4.macro"`, reads `macro_findings`, maps to `Opinion`, gated by `a4_min_confidence`/`a4_min_stance`, **returns `[]` under `BacktestClock`** (live-only, look-ahead-safe).
- `arbiter/arbiter/engine/_engine.py:398` — `_gather_a4_opinions()` wiring; consumed in the decision cycle at `_engine.py:588`.
- `arbiter/arbiter/config.py` — the `a4_*` knobs (`a4_min_stance`, `a4_min_confidence`, `a4_weight_cap`, `a4_advisor_id`) + their env overrides in `load_config`; plus the `robotics_model` key already added.
- `arbiter/arbiter/robotics_signal/store.py` — `read_recent_signals`.

Build, in order:
1. **Config kill-switch + knobs**: add `robotics_advisor_enabled: bool = False` (env `ROBOTICS_ADVISOR_ENABLED`, **DEFAULT OFF**) plus `a5_min_stance` / `a5_min_confidence` / `a5_weight_cap` / `a5_advisor_id = "A5.robotics"`, mirroring the `a4_*` knobs, and wire them into `load_config`.
2. **Advisor pipeline** (new module, e.g. `arbiter/arbiter/adapters/a5/pipeline.py`): `gather_a5_opinions(conn, clock, config) -> list[Opinion]` that MUST:
   - return `[]` when `config.robotics_advisor_enabled` is False (kill-switch, default OFF);
   - return `[]` under `BacktestClock` (mirror `a4/pipeline.py:39` — look-ahead-safe);
   - read recent **trigger-hits** from `robotics_signals` (windowed, e.g. last ~7 days), map each to an `Opinion` for its `trigger_name` symbol, but ONLY for symbols that are actually tradeable (US-listed — cross-check `robotics_universe` `priceable=True`, and never a symbol absent from the real tradeable universe);
   - be significance-gated (`a5_min_stance`/`a5_min_confidence`) and weight-capped (`a5_weight_cap`, small).
3. **Engine wiring**: add `_gather_a5_opinions()` in `_engine.py` mirroring `_gather_a4_opinions`, call it in the decision cycle alongside A4, consume the opinions. It must be inert (no opinions) when disabled.
4. **Tests** (hermetic): look-ahead (`BacktestClock` → `[]`); kill-switch (disabled → `[]`); gating (below threshold filtered); weight cap; happy path (enabled + a trigger-hit → an `A5.robotics` Opinion for that symbol); and an engine-cycle test that A5 opinions flow when enabled. Find the A4 tests to mirror: `grep -rl "A4.macro" arbiter/tests`.

## Safety invariants (non-negotiable)
- `robotics_advisor_enabled` DEFAULT FALSE — dormant until the creator explicitly flips it after watching the signal. Do NOT flip it to true.
- Live-only (BacktestClock → []), significance-gated, weight-capped, findings expire — it can only NUDGE, never dominate.
- Do NOT modify `arbiter/arbiter/data/sectors.py` or `_DEFAULT_WATCHLIST` — a robotics symbol being in the universe must never become trade-eligible on its own.

## How to run tests (worktree)
- arbiter Python: `cd /Users/jonathanmorris/poly_bot/.worktrees/robotics-watchlist/arbiter && /Users/jonathanmorris/poly_bot/arbiter/.venv/bin/python -m pytest tests/<path> -q` (reuse the main checkout's venv).
- cockpit API: from the worktree root, `/Users/jonathanmorris/poly_bot/arbiter/.venv/bin/python -m pytest cockpit/api -q`.
- frontend: `cd cockpit/web && npm test` + `npx tsc -b` (node_modules is symlinked).
- Known pre-existing UNRELATED failures (NOT yours, do not chase): `test_state_figure_nodes_lit`, `test_opt_layer_summary_values`.

## Loop discipline
Work the TDD loop one task at a time; commit after each green task. End every commit message with:
```
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: <your current session's claude.ai/code URL>
```
When 3d is fully built, all its tests are green, and there are no NEW regressions (the two pre-existing failures may remain), update memory (`robotics-sector-map.md`: mark 3d done) and report a summary. Do NOT push or merge unless the user asks.

Begin by reading `docs/specs/2026-07-13-robotics-signal-design.md` and the A4.macro templates, then start the loop.
