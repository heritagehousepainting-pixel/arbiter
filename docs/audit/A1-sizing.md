# A1 — Position Sizing & Kelly Correctness — Audit Findings

- **Lane:** A1 (position sizing & Kelly correctness)
- **Date:** 2026-06-19
- **Scope:** `arbiter/policy/sizing.py` (compute_size, quarter-Kelly, notional semantics); interaction with `arbiter/policy/decision.py` (qty assignment) and `arbiter/execution/submit.py` (notional→shares conversion + zero-share skip).
- **Health verdict:** **MOSTLY SOUND** — the cap pipeline, fail-closed paths, and notional→shares conversion are correct and conservative; the one structural weakness is the Kelly *fraction* being equated to raw conviction with no edge/odds model and no clamp, which is conservative-by-accident, not by-design.

---

## Findings

### [P1] — Kelly fraction equals raw conviction — there is no real Kelly model — `arbiter/policy/sizing.py:38-54`
The function is named `_quarter_kelly` and is documented as computing a Kelly fraction, but it does `kelly_fraction = abs(conviction)` then `0.25 * kelly_fraction * equity`. Real Kelly is `f* = edge/odds` (for even-money, `f* = 2p-1`); it requires both a win-probability and a payoff ratio. Here `conviction` is `signal_strength * diversity_factor - lone_bull_tax` (`arbiter/fusion/engine.py:141`) — a dimensionless directional score in roughly [-1, 1], **not** a probability and **not** an edge/odds ratio. So "quarter-Kelly" is really "25% of equity scaled linearly by a conviction score." 

**Why it matters:** The label implies a principled bet-sizing guarantee (growth-optimal, fractional for safety) that the code does not provide. A conviction of 1.0 maps to a full-Kelly fraction of 1.0 (bet 100% of equity), quarter-Kelly = 25% of equity, which is *enormous* for a single name and only survives because the 5% name cap (Finding context below) clips it. The conservatism is incidental to the caps, not to the Kelly math. If the name cap were ever loosened, sizing would become wildly aggressive.

**Recommended action:** Either (a) rename to reflect reality (e.g. `_conviction_scaled_size` / "conviction-linear sizing") and drop the Kelly framing, or (b) implement true fractional Kelly from a calibrated win-prob + payoff (conviction → p via a documented monotone map, then `f = (p·b - (1-p))/b`, then `0.25·f`). Document which. Until then the name oversells the guarantee.

### [P2] — No clamp/validation on conviction before scaling — `arbiter/policy/sizing.py:52`, `arbiter/fusion/engine.py:141`
`abs(conviction)` is used directly with no upper bound assertion. Conviction = `signal_strength·diversity_factor − tax`. `signal_strength` is a convex combination of stances in [-1,1] so it is bounded to [-1,1]; `diversity_factor ∈ [0,1]`; `tax` only *reduces* magnitude. So the realistic bound is |conviction| ≤ 1.0 and the math holds today. **But** this is an emergent property of three other modules, not enforced at the sizing boundary. A future calibrator that scales stances, or a weight bundle that doesn't sum to 1, could push |conviction| > 1 and silently produce a quarter-Kelly fraction > 0.25 (i.e. bet > 25% of equity pre-cap).

**Why it matters:** Defense-in-depth. Sizing is the last gate before dollars; it should not trust an unbounded upstream signal.

**Recommended action:** Clamp at the boundary: `kelly_fraction = min(1.0, abs(conviction))` with a structured-log warning when clamping fires, so an upstream regression is loud rather than silent over-sizing.

### [P2] — High-priced names silently round to zero shares; no minimum-position floor — `arbiter/execution/submit.py:255-275`, `arbiter/policy/sizing.py`
`shares = math.floor(notional / limit_price)` with a zero-share skip. On a $10k account with the default 5% name cap, max notional per name is **$500**. Verified numerically:
- $50 stock → 9 shares (OK)
- $100 stock → 4 shares (OK)
- $250 stock → 1 share (marginal)
- $600 stock → **0 shares → order skipped entirely**
- At the `_MIN_CONVICTION = 0.05` floor (`decision.py:42`), notional is only $125 (`0.25·0.05·10k`), so anything above ~$250/share rounds to 0.

**Why it matters:** On a small account a meaningful fraction of the high-conviction universe (any stock above a few hundred dollars) is silently un-tradeable. This is *safe* (no dollar leaks) but is a **coverage gap**, not a bug — and it's invisible except via the `zero_share_skip` audit event. The system will appear to "decline to trade" expensive names with no operator-facing signal that it's a sizing-resolution artifact, not a conviction decision.

**Recommended action:** (a) Emit a metric/alert when zero-share skips correlate with high price (operator visibility); (b) consider a documented minimum-position policy (e.g. round-up-to-1-share if notional ≥ ½·limit_price, gated by an explicit config flag and the name/gross caps) OR explicitly document that sub-1-share names are intentionally dropped. At minimum, document the $10k×5% = $500 floor consequence in the sizing module.

### [P2] — `size_multiplier == 0.0` fail-closed only catches *exact* zero; gate is trusted blindly otherwise — `arbiter/policy/sizing.py:106,135`
`if not gate_decision.allowed or gate_decision.size_multiplier == 0.0: return 0.0`. Step 6 then does `size *= gate_decision.size_multiplier` with no validation that the multiplier is in [0,1]. A gate returning a multiplier > 1.0 (bug or misconfig) would *amplify* the position beyond quarter-Kelly. The DEGRADED=0.25 / NORMAL=1.0 / HALTED=0.0 contract (INTERFACES.md §8) means values are expected in {0.0, 0.25, 1.0}, but sizing doesn't enforce the [0,1] envelope.

**Why it matters:** Same defense-in-depth concern as Finding 2 — sizing should not let an upstream multiplier scale a bet *up*.

**Recommended action:** `size *= min(1.0, max(0.0, gate_decision.size_multiplier))` (or assert/clamp with a warning).

### [P3] — Redundant zero-conviction guard vs. upstream `_MIN_CONVICTION` gate; thresholds not co-located — `arbiter/policy/sizing.py:110-111`, `arbiter/policy/decision.py:42-54`
`compute_size` returns 0 on `fusion.conviction == 0.0`, but `decide()` already filters anything with `|conviction| < 0.05` via `_conviction_to_side`. So `compute_size` is only ever called with |conviction| ≥ 0.05 in the normal path; its own `== 0.0` check is effectively dead for the production caller (it only guards direct/test callers). Not a correctness bug, but the two thresholds (0.0 in sizing, 0.05 in decision) live in different files and could drift.

**Why it matters:** Minor maintainability/clarity. A reader of `sizing.py` alone would think conviction can be 0 there.

**Recommended action:** Either move/document the `_MIN_CONVICTION` floor as the single source of "too small to size," or note in `compute_size` that its zero-guard is a belt-and-suspenders for non-`decide` callers.

### [P3] — Open-position cap (Step 5) is evaluated *after* steps 1–4 do wasted work, and uses pre-batch count in `decide_all` — `arbiter/policy/sizing.py:131-132`, `arbiter/policy/decision.py:264-265`
The `current_open_positions >= max_open_positions` check correctly returns 0 (fail-closed). `decide_all` increments `running_open` per *order*, not per ticker, and a multi-bucket ticker can emit multiple orders — so the open-position count is tracked at order granularity. This is consistent with how gross is rolled, and is conservative (counts each bucket order as a position), but it means a single ticker with 3 buckets consumes 3 of the 20 open-position slots. Worth confirming that's the intended semantics (position = order/bucket, not = ticker).

**Why it matters:** Definitional. If "position" is meant to be per-ticker, the cap is ~3× tighter than intended for multi-bucket names.

**Recommended action:** Confirm and document whether `max_open_positions` counts orders/buckets or distinct tickers; the current code counts orders.

---

## Verification summary (what is CORRECT / conservative)

- **NaN/None ADV fail-closed is genuinely correct** (`sizing.py:143-146`): the `math.isnan` guard is explicitly there to stop `min(x, nan) == x` from silently bypassing the ADV cap. Good catch by the original author; verified the comment matches behavior.
- **ADV cap is correctly the LAST transform** (`sizing.py:141-149`), matching INTERFACES.md §9 ("ADV cap is the LAST transform").
- **Headroom caps (sector/gross) are `max(0.0, cap − committed)`** (`sizing.py:122,127`) — cannot go negative, correctly clamp to non-negative headroom.
- **`max(0.0, size)` final return** (`sizing.py:151`) guarantees no negative notional escapes.
- **Notional→shares conversion is sound and on the correct side:** `floor(notional/limit_price)` never over-buys; uses the slippage-adjusted `limit_price` (BUY biased UP, so shares are if-anything slightly *under*-bought — conservative). Verified at $1–$600 prices.
- **No path submits a dollar notional as a share count:** `submit.py:251-277` converts notional→shares before building `OrderIntent`; the only direct-share path is `presized_shares` (exit SELLs), which is explicitly documented and bypasses the divide intentionally (B3).
- **Sign handling is correct:** sizing always works on `abs(conviction)` and returns a magnitude; direction (BUY/SELL) is decided separately in `decision.py:_conviction_to_side`. Notional is always ≥ 0; side carries the sign. Clean separation.
- **Zero-share skip is safe and audited** (`submit.py:256-275`): emits a `zero_share_skip` audit event and returns `SubmitResult(zero_share=True)` — no order placed, no dollar leak.
- **Cold-start 0.5× multiplier** (`sizing.py:30,138-139`) correctly halves size while the calibration prior dominates — conservative.

---

## OPPORTUNITIES TO ADD (make sizing best-in-class)

1. **Implement true fractional Kelly from calibrated probabilities (the headline upgrade).** Map conviction → win-probability `p` via the calibrator (which already exists as a seam), pair with an explicit payoff ratio `b`, and size `f = 0.25 · (p·b − (1−p))/b`. This turns the "quarter-Kelly" label into a real guarantee and naturally produces *smaller* bets near coin-flip edges (which the current linear map over-sizes). Today conviction=0.5 and conviction=0.1 differ only 5×; real Kelly near p≈0.5 would be far more humble.

2. **Volatility-/risk-parity-adjusted sizing.** Quarter-Kelly notional treats a 60%-vol name and a 15%-vol name identically. Scale the raw size by `target_vol / name_vol` (or by ATR) so equal *risk* is allocated, not equal *dollars*. ADV already gives a liquidity signal; vol would give a risk signal.

3. **Correlation-aware portfolio Kelly.** The fusion layer already computes a correlation matrix and `effective_n`; sizing ignores it. A simultaneous-Kelly allocation that down-weights bets correlated to existing exposure would beat the current independent-per-name caps.

4. **Minimum-position policy + price-aware universe filter.** Resolve Finding 3 proactively: either round-up-to-1-share within caps, or pre-filter the universe to names where `name_cap ≥ k · price` so the engine never spends conviction on a name it can't size. Surface a "skipped-for-resolution" count on the dashboard.

5. **Boundary clamps as first-class, logged invariants.** Wrap conviction, size_multiplier, and the final fraction in explicit `clamp(..., 0, 1)` calls that emit a structured warning on activation. Cheap, and converts three silent-over-sizing failure modes (Findings 1/2/4) into loud alerts.

6. **Fractional-share execution path (Alpaca supports notional/fractional market orders).** The whole notional→floor-shares dance and its zero-share gap exist because Alpaca rejects fractional *limit* orders. A notional market order (or fractional qty) for entries on a small paper account would eliminate the high-price coverage gap entirely; gate behind a config flag and keep the limit path for size discipline.
