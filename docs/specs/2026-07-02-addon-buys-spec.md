# Add-on buys (pyramiding) — Tier-2 #5 spec (2026-07-02)

## Problem

Add-ons to held names are categorically impossible; three independent layers
block them, and a fourth (exit attribution) breaks if they ever happen:

1. **Engine gate** — `run_cycle` skips every signal/A3/A4 opinion whose ticker
   is already held (`engine.run_cycle.skip_held_ticker`).
2. **Broker dedup** — `ensure_not_duplicate` raises `DuplicateOrderError` for
   any non-exit order on a broker-held ticker (`idempotency.py::_check_broker`).
3. **Cap-blind sizing** — `compute_size`'s per-name cap doesn't know about
   already-held notional, so once unblocked an add-on could size to the FULL
   name cap a second time (2× concentration).
4. **Exit close-out** — a full-exit SELL resolves and closes ONE owning idea;
   any other MONITORED idea on the same ticker strands forever (pre-existing
   fragility, surfaced 2026-06-22 by the T churn).

Live evidence (2026-07-02 review): BAC holds a fresh +1.0 activist conviction
signal refreshed every cycle that cannot deploy; ~89% of equity idle.

## Design

An add-on is an ordinary new idea/order on a held ticker, allowed when ALL of:

- **Fresh signal this cycle** (same requirement as any idea).
- **Name-cap headroom**: `max_position_pct × equity − held_notional(ticker)`
  ≥ `_MIN_ADDON_NOTIONAL` ($25 — no dust adds).
- **Daily cooldown**: no FILLED/live BUY (non-exit) order row for the ticker
  dated today (one add per ticker per day, independent of advisor set).

Changes per layer:

1. **Engine** (`_engine.py`): the three held-ticker skips become
   `_addon_allowed(ticker)` checks (headroom + cooldown, computed from
   `executor.get_positions()` × avg_price and `account.equity`). Audit event
   `engine.run_cycle.addon_candidate` when a held ticker passes.
   `_bound_submit` passes `is_addon=order.ticker in held_tickers`.
2. **Sizing** (`sizing.py`): new `current_name_exposure: float = 0.0` kwarg
   (default keeps all callers unchanged). Step 2 becomes headroom:
   `min(size, name_cap − current_name_exposure)`. Step 5 (open-position count
   cap) is skipped when `current_name_exposure > 0` — an add-on does not open
   a NEW position. Threaded via `RiskBook.as_decide_kwargs` → `decide`.
3. **Idempotency** (`idempotency.py`): `is_addon: bool = False` skips ONLY the
   broker position-presence check (mirrors `is_exit`); the local-ledger
   dedup_hash check still applies (same-day same-advisor-set re-entry stays
   blocked). `submit_order` forwards the flag.
4. **Exit sweep** (`exit_monitor.py` + `engine/reconcile.py`): on a FULL exit
   fill, after closing the resolved owning idea, close EVERY other MONITORED
   idea for the same ticker with the same exit price/label (they rode the same
   position to the same exit). Fixes the pre-existing stranding fragility for
   add-ons AND the legacy T-churn case.

## Non-goals

- No averaging-down guard beyond the name cap (the council's fresh-signal
  requirement is the thesis check; paper mode, learning-loop-governed).
- No partial exits (full-exit semantics unchanged).
- SimExecutor unchanged (naked-SELL rejection etc. untouched).

## Rollout

Code + tests land together; live after the next daemon kickstart (batched with
Tier-2 #4 fractional shares). Kill switch: none needed — behavior only
activates when a held ticker has BOTH a fresh signal and cap headroom; setting
`ARBITER_MAX_POSITION_PCT` low effectively disables adds.
