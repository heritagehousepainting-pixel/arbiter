# Two Independent Working Books (2026-07-20)

## Mandate (creator, verbatim intent)

The money in the account is WORKING money, not capital with a reserve buffer:
"put the money I put into you as working — don't let it sit." Target posture:

- **Stock book**: up to 100% of current account equity deployed in equities,
  compounding as the account grows.
- **Options book**: a SEPARATE budget — premium at risk up to 100% of current
  equity ("another $10k"), enforced by the options sleeve, never taxing the
  stock book.

## Problem being fixed

2026-07-20 live `cycle_funnel`: 174 ideas, 173 dead at `caps_exhausted`.
Three open option positions carry $10.7k delta-adjusted notional which
`seed_risk_book` (Tier-2 #7) folds into the equity gross cap ($8,032 =
0.80 × equity) → gross headroom negative for every candidate → the stock book
is structurally frozen while LEAPS are open. Exactly the coupling the mandate
forbids.

## Changes

1. **Decouple** (`arbiter/engine/safety_ops.py::seed_risk_book`): option
   delta-notional no longer folds into GROSS exposure. It still folds into the
   PER-NAME exposure for its underlying (cross-book anti-doubling guard: no
   equity adds on a name already expressed via options). Implementation: the
   fold populates a separate `option_name_exposure` map consumed by
   `RiskBook.name_exposure_for` only — `gross_exposure()` and
   `sector_exposure_for` become equity-only.
2. **Idle threshold knob** (`arbiter/runtime/daemon.py`):
   `_IDLE_DEPLOYMENT_THRESHOLD` becomes config-driven
   (`ARBITER_IDLE_DEPLOYMENT_THRESHOLD`, default **0.75**, config field
   `idle_deployment_threshold`). Alert message unchanged (3 consecutive
   closed sessions below threshold → warning ntfy with funnel counts).
3. **Posture knobs** (`.env`, no code):
   - `ARBITER_MAX_GROSS_PCT=1.00` (was 0.80)
   - `OPTIONS_SLEEVE_PCT=1.00` (was 0.35)
   - `ARBITER_MAX_POSITION_PCT=0.10` (was 0.05)
   - `ARBITER_MAX_SECTOR_PCT=0.30` (was 0.20)
   - `ARBITER_MAX_OPEN_POSITIONS=20` (unchanged)

## Non-goals

- No separate broker sub-accounts or transfer ledger (YAGNI — one paper
  account, two logical budgets).
- No change to fusion, trust, floors, tracing, revisit sweep.
- Deployment metric stays `1 − cash/equity` (cash spent on option premium
  counts as deployed — consistent with "money at work").

## Testing

TDD: RiskBook/seed tests — option position contributes to
`name_exposure_for(underlying)` but NOT `gross_exposure()` nor
`sector_exposure_for`; config env round-trip for the new knob; daemon idle
test updated for threshold 0.75 via fake config. Full suite + both repo
linters green before merge.
