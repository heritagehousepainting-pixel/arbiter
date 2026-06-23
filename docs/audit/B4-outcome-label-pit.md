# B4 — Outcome-labeling correctness & PIT audit

**Auditor lane:** B4 (read-only)
**Date:** 2026-06-19
**Scope:** `arbiter/evaluation/outcome_labeler.py`, `arbiter/data/beta.py`, `arbiter/data/slippage.py` (oriented via `INTERFACES.md` §6)
**Verdict:** PASS WITH CONCERNS — alpha formula, beta-as-of-t0−1, slippage sign, the ±25bps band, and exit look-ahead guard are all correct per INTERFACES §6. One real correctness gap (P1: entry/beta reads are not bounded by `cutoff_as_of`) and one fragile-by-design coupling (P2: imputation flag detected via log-string sniffing). No P0.

---

## Findings

### [P1] — Entry OPEN and beta reads are not bounded by `cutoff_as_of` — outcome_labeler.py:151,169,174 — why — Only the EXIT timestamp is clamped via `effective_exit_as_of = min(exit_as_of, cutoff_as_of)` (line 162). The entry reads `_get_price_open(idea.ticker, t1_entry, pit)` (151) and `_get_price_open(_SPY, t1_entry, pit)` (169), and the beta read at `t0−1` (173-174), pass their raw timestamps straight to PIT with no comparison to `cutoff_as_of`. In normal flow `t1_entry = t0+1 trading day` and `t0−1` are both ≤ exit ≤ cutoff, so this is benign. But the guard is asymmetric: if a caller ever labels an idea whose `t1_entry` is itself > `cutoff_as_of` (e.g. an idea filed at/after the run's information cutoff, or a same-day re-label), the labeler will happily read a future OPEN, producing look-ahead alpha. The PIT layer is the last line of defense, but the labeler's own invariant ("everything ≤ cutoff_as_of") is only half-enforced. — recommended action: Add an explicit guard/assert that `t1_entry <= cutoff_as_of` (and `beta_as_of <= cutoff_as_of`) before the PIT reads; raise `LookupError`/`ValueError` rather than silently reading. At minimum, mirror the `min(..., cutoff_as_of)` discipline or document why entry is exempt.

### [P2] — Imputation flag detected by sniffing the log message string `"imputing 1.0"` — outcome_labeler.py:284-299 (`_get_beta_safe`) — why — `_FlagHandler.emit` sets `flagged=True` only when a log record's message contains the substring `"imputing 1.0"`. This couples the labeler's `beta_imputed` correctness to the exact wording of three separate `_logger.warning` calls in `beta.py` (lines 66, 78, 197 — all currently contain the substring, so it works today). It is brittle: (a) any reword of those warnings silently breaks imputation flagging; (b) if `arbiter.data.beta` logger level is raised above WARNING or `logging.disable(WARNING)` is in effect, `emit` is never called and imputation goes undetected → `beta_imputed=False` even when beta was imputed to 1.0, suppressing the operator warning at lines 176-182; (c) it mutates a shared module-level logger by add/removeHandler on every label call (not thread-safe under concurrent labeling). — recommended action: Have `beta_252d` return the imputation status directly (e.g. `(beta, imputed)` tuple or a sentinel) instead of inferring it from logs. If the signature must stay, at minimum force the beta logger to propagate at WARNING within the handler scope and add a unit test asserting the substring contract. Note this is a `beta.py`/seam change, flagged here for the owning lane.

### [P3] — `_next_trading_day` / `_on_or_next_trading_day` ignore the `tzinfo` and HH:MM of the datetime — outcome_labeler.py:213-245 — why — Both helpers add `timedelta(days=1)` to the datetime and test `_is_trading_day(candidate.date())`. They preserve the original time-of-day and tzinfo, which is correct for date arithmetic. However `idea.as_of` is documented as a `datetime` with no enforced tz; `replay_clock` uses `timezone.utc`. If `idea.as_of` is naive or in a non-UTC tz, `.date()` may land on the wrong calendar day near midnight, shifting the entry/exit by a full trading day. Currently benign if all `as_of` values are UTC-normalized upstream, but unenforced here. — recommended action: Document/assert that `idea.as_of` and `cutoff_as_of` are UTC; consider normalizing to UTC date before trading-day advancement.

### [P3] — `beta.py` window uses calendar-day probing with a 400-day buffer; thin/sparse PIT sources can silently yield <252 bars without imputing — beta.py:59,158 — why — `start = as_of - 400 days` and `_align_returns` caps at the last 253 common dates. With ~252 trading days in ~365 calendar days, a 400-day buffer is adequate for a dense source. But the probe is calendar-day-by-day (`cursor < end_exclusive`, line 125), and only dates present in BOTH ticker and SPY maps survive (`_align_returns` line 151). If a source has gaps, the function can return a beta computed from, say, 70 pairs (≥ `_MIN_PAIRS`=63) that is NOT a true 252-day beta, with no flag. The `_MIN_PAIRS=63` floor (one quarter) is far below the nominal 252-day window. — recommended action: Confirm 63 is the intended minimum (INTERFACES §6 says "252d rolling … impute 1.0+flag" — the spec implies the full window, not a quarter). If a partial-window beta is acceptable, document the 63 threshold; otherwise flag betas computed from materially fewer than 252 pairs.

---

## Verified correct (no action)

- **Alpha formula** (outcome_labeler.py:189-190) matches INTERFACES §6 line 178 exactly: `alpha_raw = r_i − beta_i * r_spy`, then `× 10_000` → bps. Continuous, drives trust. ✔
- **Entry = filing+1 OPEN net slippage** (150-152): `t1_entry = _next_trading_day(t0)` (strictly after t0, advances over weekend/holiday), reads `price_open`, then `model_slippage(entry_open, spread)`. ✔
- **Slippage sign for entry is correct** (slippage.py:65-67): entry is a BUY; `model_slippage` defaults to `OrderSide.BUY` → `price × (1 + 5bps) + 0.5×spread`, biasing the entry price UP (worse for buyer, conservative, raises cost basis → lowers `r_i`). Matches INTERFACES §3/§10b.3. The labeler does NOT apply slippage to the exit (raw `price_close`), which is the conservative choice for alpha. ✔
- **Exit = horizon CLOSE or override** (154-167): default `raw_exit = t0 + horizon_days`, advanced via `_on_or_next_trading_day`; explicit `exit_price` override path bypasses the PIT read for early_exit/corporate_event. ✔
- **`min(exit_as_of, cutoff_as_of)` look-ahead guard** (162): present and correct for the exit. ✔ (entry side gap captured in P1.)
- **Beta as of t0−1** (173): `beta_as_of = t0 - timedelta(days=1)`; `beta.py` window is `cursor < end_exclusive` where `end_exclusive = as_of` (beta.py:57,125), so the last bar probed is `as_of − 1 day` → effectively t0−2 relative to filing, never reading t0 or later. No look-ahead in beta. ✔
- **Beta imputation = 1.0 + flag** (beta.py:64-70, 75-84, 196-200): all three thin-data / no-data / zero-variance paths impute 1.0 and warn. Labeler re-warns at 176-182. ✔ (detection brittleness is P2.)
- **Binary ±25bps band → 0** (307-316): `> +25 → +1`, `< −25 → −1`, `|x| ≤ 25 → 0` ("no-call"). Matches INTERFACES §6 line 172. Boundary is exclusive (exactly ±25 → 0), consistent with the "band → 0" intent. ✔
- **LookupError handling** (252-269): `_get_price_open`/`_get_price_close` raise `LookupError` with ticker+ISO timestamp when PIT returns `None`. Documented in the `label` docstring (119-120). ✔
- **Weekend/holiday advancement** (213-245): `_next_trading_day` advances strictly past t0; `_on_or_next_trading_day` returns dt if already a trading day else advances. Both cap at 20 iterations (covers the longest holiday run). Uses NYSE `_is_trading_day`. ✔
- **No `datetime.now()` / wall-clock reads** in any of the three files. All time comes from `idea.as_of` / `cutoff_as_of` / explicit params. ✔
- **Abstained short-circuit** (128-140): returns `alpha_bps=0.0, binary=0` before any PIT read. ✔
- **`label_kind` validation** (122-125): raises `ValueError` for unknown kinds against `LABEL_KINDS`. ✔

---

## OPPORTUNITIES TO ADD

1. **Bound entry & beta reads by `cutoff_as_of` (closes P1):** make the look-ahead invariant total, not just exit-side. A one-line assert per read would make the labeler self-defending regardless of caller discipline.
2. **Return imputation status from `beta_252d` directly (closes P2):** kills the log-sniffing coupling and the per-call shared-logger mutation; enables thread-safe concurrent labeling.
3. **Exit-side slippage option:** alpha currently nets slippage on entry only. For round-trip realism (esp. early_exit/reversal where the override price is a market exit), consider `model_slippage_sell` on the exit so trust scores reflect both legs. Document the deliberate choice either way.
4. **Expose `beta_imputed` and `n_pairs` on `ResolvedOutcome`:** downstream calibration (L9/L11) cannot currently distinguish a true beta from an imputed 1.0 or a thin-window beta. Surfacing these would let consumers down-weight imputed labels.
5. **Tighten / document `_MIN_PAIRS`:** 63 vs the nominal 252 is a large gap; either justify a partial-window beta or raise the floor and flag partial windows distinctly from full imputation.
6. **UTC normalization assertion** on `idea.as_of` / `cutoff_as_of` at the `label` boundary to remove the tz ambiguity behind the P3 trading-day-advancement finding.
