# Robotics Early-Insight Signal — Design

*Date: 2026-07-13 · Status: DRAFT (approved verbally; building) · Sub-project #3 of the robotics thematic-intelligence module*

## Context

Piece #3 of the robotics module (after #1 the map, #2 the display-only board). Goal: a
twice-weekly scan that surfaces robotics-sector developments **before mainstream** and flags
when one hits a curated "trigger to watch", delivered to phone + cockpit, and — as a deliberate,
probationary step — able to nudge the live paper-trader.

Decisions taken during brainstorming (2026-07-13):
- **Signal model:** broad robotics-news scan each cycle, **plus** explicit trigger-hit flagging.
- **Cadence:** twice a week (Mon + Thu pre-market).
- **Delivery:** phone digest **and** a cockpit signals feed.
- **Trading posture:** **also feed a probationary advisor** (not phone-only) — built with the
  same safety properties as `A4.macro`.

## Template

The `arbiter/arbiter/refresh/` package (Monday Refresh) is the blueprint and is cloned closely:
orchestrator + `_safe`-wrapped scans → digest → `Alerting.notify` phone push → persist findings
→ probationary advisor reads the findings table. Key files to mirror: `refresh/orchestrator.py`,
`refresh/llm.py` (`AnthropicLLM` / `LLM` protocol / `FakeLLM`), `refresh/macro_scan.py` (the
`web_search` tool loop + json-fence parsing), `refresh/digest.py`, `safety/alerting.py::Alerting.notify`,
`refresh/findings_store.py` + `adapters/a4/pipeline.py` (persist→advisor seam),
`runtime/daemon.py::_maybe_monday_refresh` (schedule guard).

## Architecture

**Data foundation (single source of truth).** New pure-data module
`arbiter/arbiter/data/robotics_universe.py`, following the `data/fund_managers.py` /
`data/activist_filers.py` / `data/sectors.py` convention (module-level tuple of rows +
thin accessors, `from __future__ import annotations`, no I/O, no network). Rows carry
`symbol, company, layer, longevity, priceable, early_insight, trigger, region, note`.
Accessors: `robotics_universe() -> list[dict]`, `early_insight_names() -> list[dict]`.
The cockpit roster (`cockpit/api/robotics_roster.py`, #2) is refactored to **project from this
canonical module** instead of holding its own copy.

**Scanner.** New `arbiter/arbiter/robotics_signal/` package cloned from `refresh/`:
`orchestrator.py::run_robotics_scan(engine, *, llm=None, alerting=None) -> RoboticsReport`,
a `scan.py` doing one Claude `web_search` pass over the universe (reusing `refresh/llm.py`),
prompted to return a json block of `developments` (broad) each tagged with an optional
`trigger_hit` referencing a universe symbol, and a `digest.py`. Every step `_safe`-wrapped.

**Delivery — phone + cockpit.**
- Phone: `Alerting.notify("Robotics Signal", headline, as_of=...)` (existing webhook).
- Cockpit: persist findings to a new `robotics_signals` table (new migration); a read-only
  cockpit endpoint `GET /robotics-signals` feeds a "recent signals" view in `RoboticsPanel`.

**Trading influence — probationary advisor (mirrors A4.macro exactly).** A new advisor
(e.g. `A5.robotics`) reads trigger-hits from the findings table and emits `Opinion`s, with the
A4 safety properties, all preserved verbatim in intent:
- **Live-only:** returns `[]` under `BacktestClock` (no look-ahead), like `adapters/a4/pipeline.py:39`.
- **Significance-gated + capped:** min-confidence / min-stance thresholds and a small weight cap
  (new config knobs mirroring `a4_min_*` / `a4_weight_cap`).
- **Config kill-switch:** a `robotics_advisor_enabled` flag (default OFF until watched), so the
  seam exists but is dormant unless explicitly turned on.
- Findings carry a 7-day expiry (like `findings_store.py`).

**Scheduling.** A `_maybe_robotics_scan(engine, now, state)` daemon guard mirroring
`_maybe_monday_refresh` (`daemon.py:365`): fires on ET weekday ∈ {Mon, Thu} inside the
08:00–09:30 pre-market window, once-per-ET-date dedup via a new `DaemonState` field, fail-safe
(exceptions logged/alerted, never crash the loop). Plus a `robotics-scan` CLI command beside
`cli.py:179` for manual/launchd runs.

**Config.** Reuse `anthropic_api_key`, `alert_webhook_url`, `audit_path`. New keys (env +
TOML, secret-redaction where needed): `robotics_model` (default = same as `refresh_model`),
`robotics_advisor_enabled` (default False), advisor thresholds. **Before writing the LLM call,
verify the current Claude model id + `web_search` tool version via the `claude-api` skill** —
do not copy `claude-opus-4-8` / `web_search_20260209` from memory.

## Goals / non-goals

- **Goal:** twice-weekly robotics scan → phone + cockpit → optional probationary trading nudge.
- **Non-goal (this build):** turning the advisor ON by default; changing Monday Refresh; any
  change to `sectors.py` / `_DEFAULT_WATCHLIST` (the roster's presence never makes a symbol
  trade-eligible on its own).

## Safety invariants

- The scanner + universe module import nothing that reaches `sectors.py` / `_DEFAULT_WATCHLIST`.
- The advisor is dormant (`robotics_advisor_enabled=False`) until the creator flips it after
  watching the signal; even enabled it is probationary, live-only, significance-gated, weight-capped.
- All LLM/network steps fail-closed; tests are hermetic (`FakeLLM`, no real webhook/LLM).

## Increment sequence (each independently shippable + tested)

1. **3a — Canonical universe + cockpit projection.** `data/robotics_universe.py` (ported from
   the #2 roster) + refactor `cockpit/api/robotics_roster.py` to project from it + refine the
   cockpit guardrail test to "imports no trade-eligibility seam" (not "nothing from arbiter").
2. **3b — Scanner + phone digest.** `robotics_signal/` package (orchestrator, scan, digest) +
   `Alerting.notify` push + CLI command + daemon twice-weekly guard. *Milestone: phone digests.*
3. **3c — Persist + cockpit signals feed.** `robotics_signals` migration + persist step +
   `GET /robotics-signals` endpoint + RoboticsPanel recent-signals view. *Milestone: cockpit shows signals.*
4. **3d — Probationary robotics advisor.** `A5.robotics` advisor (A4-mirrored safety) + engine
   wiring + config kill-switch (default OFF). *Milestone: a fired trigger can nudge the live engine.*

## Testing

Hermetic throughout (conftest guard already blocks real alerts): `FakeLLM` for scans; the
`robotics_universe` module gets data-hygiene tests (enums, unique symbols, every early-insight
row has a trigger); the advisor gets look-ahead (`BacktestClock` → `[]`), gating, and
kill-switch tests; the cockpit projection keeps the display-only guardrail (refined) green.
