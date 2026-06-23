# Spec — Blanket weight boost for the A3 news advisor

- **Date:** 2026-06-23
- **Status:** Approved design, implementing directly (small, contained)
- **Motivation:** User wants the system to "trust news more" so it pivots on breaking news
  instead of waiting for the slow learning-loop graduation. Scope (confirmed): **weight only**
  — not intraday speed, not macro/market-wide signal, not new feeds.

## Decision (from brainstorm)
- **Blanket** boost (not conviction-scaled, not defensive-only): A3 always carries more weight
  than other ungraduated advisors.
- **~2× (strong lead)**, capped so a strong multi-advisor consensus can still out-total it
  ("lead, not dictator").
- **Safety preserved:** the boost is a strong PRIOR, not an immortal override — if the learning
  loop ever flags A3 `negative_skill`, it is still suppressed to 0/shadow *before* any boost.

## Mechanism
In `arbiter/trust/weight_resolver.py::resolve_weight_bundle`, after each advisor's weight is
resolved, apply a per-advisor boost to the **news** advisor only:

```
if advisor_id == news_advisor_id and reason != NEGATIVE_SKILL_REASON:
    weight = min(weight * news_multiplier, news_cap)   # shadow stays False
```

- The negative-skill branch (`reason == "negative_skill"` → 0.0/shadow) is evaluated FIRST and
  is untouched, so suppression always wins over the boost.
- Applies to BOTH the cold-floor path (0.25 → 0.50) and the graduated path (learned × mult,
  capped) → "blanket, always heavier."
- All non-news advisors unchanged.
- `fuse` normalizes weights to a simplex per horizon bucket, so 2× → ~2× share but a consensus
  of other advisors still out-totals it (e.g. 3 × 0.25 = 0.75 > 0.50). A3 lives in the SHORT
  (7d) bucket, so in practice it most directly out-weighs MiroFish on news-driven ideas.

## Config (env-overridable, mirroring `A3_MIN_STANCE`)
| key | default | meaning |
|-----|---------|---------|
| `A3_WEIGHT_MULTIPLIER` | `2.0` | multiplier on the news advisor's resolved weight |
| `A3_WEIGHT_CAP` | `0.50` | absolute ceiling on the boosted news weight |
| `A3_ADVISOR_ID` | `A3.news` | which advisor the boost targets |

`resolve_weight_bundle` gains optional params `news_advisor_id=None, news_multiplier=1.0,
news_cap=1.0` (defaults = NO-OP so existing callers/tests are unaffected). The live call site
(`engine/learning.py::_build_learning_inputs` → `resolve_weight_bundle(...)`) passes the
config-derived values.

## Out of scope / unchanged
- Position caps, gross/sector limits, ADV liquidity, and A3's `|stance|≥0.25` gate all still
  bind — this changes only A3's *share of the fused stance*, not the guardrails.
- No change to cadence (news still acts at full-cycle slots), sources, or macro awareness.

## Testing
- A3 cold (no ledger) → weight 0.50 (2× the 0.25 floor), shadow False.
- A3 graduated (learned weight, e.g. 0.30) → min(0.30×2, 0.50) = 0.50; (e.g. 0.10) → 0.20.
- A3 `negative_skill` → 0.0/shadow (boost does NOT rescue it).
- Non-news advisors unchanged at floor.
- Cap respected (boosted weight never exceeds `A3_WEIGHT_CAP`).
- Default no-op: `resolve_weight_bundle` without news params behaves exactly as before.
- A consensus of 3 floor advisors out-weights boosted A3 after fusion normalization.
- Full suite + `check_no_lookahead.sh` + `check_insert_only.sh` stay green.
