"""Tests for arbiter.trust.weight_resolver — bootstrap floor + suppression (#4).

Covers D1/D3/D6:
  - T1 bootstrap: dormant/None ledger → both advisors floored, shadow=False,
    non-empty bundle (no deadlock).
  - graduation: graduated advisor's learned weight replaces the floor.
  - T5 negative-skill: cap_reason="negative_skill" → weight 0, shadow=True
    (suppressed) while a cold sibling stays floored.
"""
from __future__ import annotations

from arbiter.contract.seams import AdvisorWeight, WeightBundle
from arbiter.trust.weight_resolver import (
    EQUAL_FLOOR,
    EQUAL_FLOOR_GRADUATED,
    resolve_weight_bundle,
)

LIVE = ["A1.insider", "A1.congress"]


def test_bootstrap_dormant_ledger_floors_all_live_advisors():
    """T1: None ledger → every live advisor floored, non-shadow, non-empty."""
    rb = resolve_weight_bundle(None, LIVE, equal_floor=EQUAL_FLOOR)
    assert set(rb.weights.keys()) == set(LIVE)
    for aw in rb.weights.values():
        assert aw.shadow is False
        assert aw.weight == EQUAL_FLOOR
    assert rb.correlation_matrix == {}


def test_shadow_ledger_advisor_still_floors():
    """A ledger shadow advisor (onboarding) trades at the floor (shadow=False)."""
    led = WeightBundle(
        weights={
            "A1.insider": AdvisorWeight("A1.insider", 0.0, 0.0, 0.0, shadow=True),
            "A1.congress": AdvisorWeight("A1.congress", 0.0, 0.0, 0.0, shadow=True),
        },
        correlation_matrix={},
    )
    rb = resolve_weight_bundle(led, LIVE, equal_floor=0.5)
    for aw in rb.weights.values():
        assert aw.shadow is False
        assert aw.weight == 0.5


def test_graduated_advisor_uses_learned_weight():
    """Graduated (shadow=False, weight>0) → learned weight, not floor."""
    led = WeightBundle(
        weights={
            "A1.insider": AdvisorWeight("A1.insider", 0.42, 0.34, 0.50, shadow=False),
            "A1.congress": AdvisorWeight("A1.congress", 0.0, 0.0, 0.0, shadow=True),
        },
        correlation_matrix={},
    )
    rb = resolve_weight_bundle(led, LIVE, equal_floor=0.5)
    assert rb.weights["A1.insider"].weight == 0.42
    assert rb.weights["A1.insider"].shadow is False
    # cold sibling floored
    assert rb.weights["A1.congress"].weight == 0.5


def test_graduated_tiny_weight_floored_to_graduated_floor():
    led = WeightBundle(
        weights={"A1.insider": AdvisorWeight("A1.insider", 0.001, 0.0, 0.0, shadow=False)},
        correlation_matrix={},
    )
    rb = resolve_weight_bundle(led, ["A1.insider"], equal_floor=0.5)
    assert rb.weights["A1.insider"].weight == EQUAL_FLOOR_GRADUATED


def test_negative_skill_suppressed_not_floored():
    """T5: cap_reason negative_skill → weight 0, shadow=True (muted)."""
    led = WeightBundle(
        weights={
            "A1.insider": AdvisorWeight("A1.insider", 0.0, 0.0, 0.0, shadow=True),
            "A1.congress": AdvisorWeight("A1.congress", 0.0, 0.0, 0.0, shadow=True),
        },
        correlation_matrix={},
    )
    rb = resolve_weight_bundle(
        led, LIVE, equal_floor=0.5,
        cap_reasons={"A1.insider": "negative_skill", "A1.congress": None},
    )
    # negative-skill advisor suppressed
    assert rb.weights["A1.insider"].weight == 0.0
    assert rb.weights["A1.insider"].shadow is True
    # cold sibling still floored → bucket still trades (no deadlock)
    assert rb.weights["A1.congress"].weight == 0.5
    assert rb.weights["A1.congress"].shadow is False


def test_default_floor_strictly_below_graduated_ceiling():
    """P1-a / D3: the DEFAULT cold floor (0.25) is strictly LESS than the
    ledger's graduated weight CEILING (0.50), so a cold/unproven advisor gets a
    strictly smaller pool share than a fully-graduated max-trust advisor — never
    parity.  Uses the module default ``EQUAL_FLOOR`` (no explicit override)."""
    from arbiter.trust.ledger import CEILING

    # Module default must be the post-fix value and strictly below the ceiling.
    assert EQUAL_FLOOR == 0.25
    assert EQUAL_FLOOR < CEILING

    led = WeightBundle(
        weights={
            # Fully graduated at the ledger ceiling (best-proven advisor).
            "A1.insider": AdvisorWeight("A1.insider", CEILING, 0.4, 0.5, shadow=False),
            # Cold/onboarding sibling.
            "A1.congress": AdvisorWeight("A1.congress", 0.0, 0.0, 0.0, shadow=True),
        },
        correlation_matrix={},
    )
    # Resolve with the MODULE DEFAULT floor (do not pass equal_floor).
    rb = resolve_weight_bundle(led, LIVE)

    proven = rb.weights["A1.insider"].weight
    cold = rb.weights["A1.congress"].weight

    # Cold advisor floored at the default; proven keeps the ceiling weight.
    assert cold == EQUAL_FLOOR
    assert proven == CEILING
    # Strictly less RAW weight → strictly less normalised (simplex) pool share.
    assert cold < proven
    cold_share = cold / (cold + proven)
    proven_share = proven / (cold + proven)
    assert cold_share < proven_share
    # And two cold advisors could not outvote the one proven advisor:
    # 2 * floor (0.50) does not EXCEED the ceiling (0.50) — was a tie at 0.5+0.5
    # under the old 0.5 floor (1.0 > 0.5 → cold pair dominated).
    assert 2 * EQUAL_FLOOR <= CEILING


def test_correlation_matrix_passed_through():
    led = WeightBundle(
        weights={"A1.insider": AdvisorWeight("A1.insider", 0.3, 0.2, 0.4, shadow=False)},
        correlation_matrix={("A1.insider", "A1.congress"): 0.7},
    )
    rb = resolve_weight_bundle(led, ["A1.insider"], equal_floor=0.5)
    assert rb.correlation_matrix == {("A1.insider", "A1.congress"): 0.7}
