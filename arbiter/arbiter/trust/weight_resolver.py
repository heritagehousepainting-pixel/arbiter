"""Bootstrap weight resolver — Learning loop (sub-project #4), decisions D1/D3/D6.

Translates the trust ledger's learned ``WeightBundle`` into the bundle actually
handed to ``fuse`` for the *live* advisor roster, applying the "keep trading while
cold" policy that lives in the ENGINE/FUSION layer (never in the ledger — the
ledger stays PIT-pure and advisor-agnostic so A2 reuses it verbatim).

Per-advisor rule (D1/D3/D6)
---------------------------
For each live advisor id:

  * cold / shadow / in-ramp / no ledger row  (aw is None, or aw.shadow,
    or aw.weight <= 0 with cap_reason != "negative_skill")
        → ``weight = EQUAL_FLOOR`` (a probationary FRACTION, D3),
          ``shadow = False`` so ``pool.py`` INCLUDES it → the bucket always has
          ≥1 participant → no deadlock.  This is the bootstrap.

  * graduated  (aw.shadow == False and aw.weight > 0)
        → use the ledger's LEARNED weight, floored at ``EQUAL_FLOOR_GRADUATED``
          (0.02) so a graduated weight is never clamped to 0 by the resolver.

  * negative-skill  (cap_reason == "negative_skill")
        → ``weight = 0.0, shadow = True`` (genuinely suppressed; ``pool.py``
          excludes it).  A demonstrably-harmful advisor is muted, NOT floored
          back to full participation.

Correlation matrix (D4, v1)
---------------------------
Pass the ledger's ``correlation_matrix`` through UNCHANGED.  When the ledger is
dormant/None the matrix is empty → ``fusion/correlation.py`` defaults every
off-diagonal pair to ρ=0.0 → ``effective_n ≈ N`` (no deflation), the documented
Phase-1-safe default.

``EQUAL_FLOOR`` (D3)
--------------------
A probationary FRACTION (default 0.25), NOT full 1.0 parity and strictly BELOW
the ledger's graduated weight CEILING (0.50).  Rationale: emitting 1.0 (or even
the 0.5 ceiling) for every cold advisor lets an unproven (possibly garbage)
advisor sit at parity with a fully-graduated proven advisor, and it gets worse
with ≥3 advisors (two cold advisors outvote one proven).  0.25 sits strictly
below the 0.50 composite-weight ceiling so a COLD advisor always gets a smaller
pool share than a fully-graduated max-trust advisor, while staying positive +
non-shadow so the bucket still trades.  Config-exposed via
``Config.trust_equal_floor``.

No ``datetime.now()`` anywhere (PIT lint clean).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from arbiter.contract.seams import AdvisorWeight, WeightBundle

# Default probationary floor (D3).  A fraction, not 1.0 — see module docstring.
# 0.25 (NOT 0.5): the ledger's graduated weight CEILING is 0.50, so a floor of
# 0.5 would let a COLD/unproven advisor pool at PARITY with a fully-graduated
# max-trust advisor (and ≥2 cold advisors would outvote one proven one).  0.25
# keeps a cold advisor strictly BELOW the best-proven ceiling while staying
# positive + non-shadow so the bucket still trades.
EQUAL_FLOOR: float = 0.25

# Floor re-asserted for a GRADUATED advisor so the resolver never clamps a
# learned weight to 0 (matches the ledger's own thin-sample floor of 0.02).
EQUAL_FLOOR_GRADUATED: float = 0.02

# cap_reason value the ledger persists for sub-chance (negative-skill) advisors.
NEGATIVE_SKILL_REASON: str = "negative_skill"


def resolve_weight_bundle(
    ledger_output: WeightBundle | None,
    live_ids: Iterable[str],
    *,
    equal_floor: float = EQUAL_FLOOR,
    cap_reasons: Mapping[str, str | None] | None = None,
) -> WeightBundle:
    """Build the live ``WeightBundle`` handed to ``fuse``.

    Parameters
    ----------
    ledger_output:
        The latest ledger/persisted bundle, or ``None`` when the ledger is
        dormant / has produced no bundle yet.
    live_ids:
        Advisor ids that are live this cycle (``engine.advisor_map`` keys).
    equal_floor:
        Probationary floor weight for cold/shadow advisors (D3).  Config-exposed.
    cap_reasons:
        Optional ``{advisor_id: cap_reason}`` from the ledger / persisted rows.
        ``"negative_skill"`` flips an advisor to genuine suppression (0/shadow)
        instead of the floor.  Absent / None → onboarding (floored).

    Returns
    -------
    WeightBundle
        Keyed by every live advisor id.  Never empty when ``live_ids`` is
        non-empty, and never all-shadow unless every live advisor is
        negative-skill — so the deadlock is structurally impossible during
        bootstrap.
    """
    cap_reasons = cap_reasons or {}
    ledger_weights = ledger_output.weights if ledger_output is not None else {}

    resolved: dict[str, AdvisorWeight] = {}
    for advisor_id in live_ids:
        aw = ledger_weights.get(advisor_id)
        reason = cap_reasons.get(advisor_id)

        if reason == NEGATIVE_SKILL_REASON:
            # Genuinely suppressed — mute it (pool.py excludes shadow/zero).
            resolved[advisor_id] = AdvisorWeight(
                advisor_id=advisor_id,
                weight=0.0,
                ci_low=0.0,
                ci_high=0.0,
                shadow=True,
            )
            continue

        if aw is None or aw.shadow or aw.weight <= 0.0:
            # Cold / shadow / in-ramp / dormant ledger → trade at the floor.
            resolved[advisor_id] = AdvisorWeight(
                advisor_id=advisor_id,
                weight=equal_floor,
                ci_low=equal_floor,
                ci_high=equal_floor,
                shadow=False,
            )
            continue

        # Graduated: use the learned weight, never clamped below the graduated floor.
        effective = max(aw.weight, EQUAL_FLOOR_GRADUATED)
        resolved[advisor_id] = AdvisorWeight(
            advisor_id=advisor_id,
            weight=effective,
            ci_low=aw.ci_low,
            ci_high=aw.ci_high,
            shadow=False,
        )

    corr = dict(ledger_output.correlation_matrix) if ledger_output is not None else {}
    return WeightBundle(weights=resolved, correlation_matrix=corr)
