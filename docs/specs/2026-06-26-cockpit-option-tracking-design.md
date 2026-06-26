# Cockpit — click-to-expand option tracking (design)

**Date:** 2026-06-26 · **Status:** APPROVED (design) → build directly

## Goal
Give open option positions the same "track it" affordance trades have: in the
OPTIONS panel, click an option's contract → the row expands to show the
**underlying's** live tracking (company name, today's price + day %, 1-month
return — same as a trade) **plus** option-specific "since you opened" context.

## Constraint
No live option premium/P&L — Alpaca's options data needs an OPRA agreement we
don't have, so `current_mid`/`unrealized_pl` are `None` (the P&L column shows
"—"). The tracking is therefore about the **underlying** + the **contract
terms**, not the option's live mark.

## Hard constraints
- **READ-ONLY**, **additive** (existing option-table columns/rows unchanged),
  **frozen contract** (`contract.py` ↔ `contract.ts`).

## UX
- The **contract cell** is the click target; clicking toggles an inline
  accordion expansion (chevron `▾`/`▸`). **One** option open at a time. Same
  pattern/behavior as the Open Positions trade rows.
- Detail content (the stock + the contract; the row's own columns already show
  Side/Δ/Qty/Entry/DTE/P&L):
  - **Underlying** — full company name.
  - **Today** — underlying current price + **day %** (▲ green / ▼ red).
  - **1-Month** — underlying price return (▲/▼).
  - **Since you opened** — `underlying_open_price → current` (% move, ▲/▼);
    strike with **ITM/OTM by $X** (call: ITM if current > strike; put: inverse);
    Δ; entry premium; conviction (`original_conviction`).
  - Header line: expiry (`fmtExpiry`) + DTE.
- **States:** loading → "loading…"; any field Alpaca can't supply → "—".

## Data
- From `/options` (already polled): `underlying`, `strike`, `expiry`, `dte`,
  `delta_at_open`, `entry_premium`, `underlying_open_price`, `original_conviction`,
  `side`, `contracts_qty`.
- From `/ticker/{underlying}` (lazy on expand, cached per session): `name`,
  `current_price`, `day_change_pct`, `month_return_pct`.
- Derived in the panel: since-open % = `(current_price − underlying_open_price)/
  underlying_open_price`; ITM/OTM distance from `current_price` vs `strike`.

## Backend
- **Extend `TickerDetail`** (contract.py + .ts) with
  `day_change_pct: float | None` (a fraction). Compute in `ticker.py` from the
  daily bars already fetched: `(bars[-1].c − bars[-2].c)/bars[-2].c`; `None` when
  `< 2` bars or `prev == 0`. No new endpoint — `/ticker/{symbol}` becomes the
  self-contained "ticker tracking" source for trades AND options.

## Frontend
- **`OptionsPanel.tsx`:** the open-position `Row` becomes expandable — contract
  cell is a focusable button (`aria-expanded`); panel holds one `openOptionId`
  state + a session cache (`useRef<Map>`). On expand, `fetchTickerDetail(underlying)`
  once; render the detail sub-row combining the position fields + the ticker
  detail. Reuse the existing `fmtExpiry`, `pct()`, and color helpers. Existing
  columns untouched.

## Testing
- **Backend** (`test_ticker.py`): `day_change_pct` happy path (bars → fraction)
  and degrade (`< 2` bars / `prev == 0` → `None`); surfaced in `/ticker/{symbol}`.
- **Web** (`OptionsPanel` test): clicking a contract toggles expansion +
  `aria-expanded`; detail renders underlying name / today / month / since-open
  (open→now %, ITM/OTM); loading and "—" states; one open at a time.

## Success criteria
- Click any open option → row expands with the underlying's company name, today's
  price + day %, 1-month return, and the since-open contract context; click again
  collapses; one at a time. No live-premium claims. Read-only, additive, existing
  option UI unchanged. tsc clean; backend + web tests green.
