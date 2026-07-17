"""Tests for arbiter.trust.weight_resolver — bootstrap floor + suppression (#4).

Covers D1/D3/D6:
  - T1 bootstrap: dormant/None ledger → both advisors floored, shadow=False,
    non-empty bundle (no deadlock).
  - graduation: graduated advisor's learned weight replaces the floor.
  - T5 negative-skill: cap_reason="negative_skill" → weight 0, shadow=True
    (suppressed) while a cold sibling stays floored.
"""
from __future__ import annotations

import pytest

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


# ---------------------------------------------------------------------------
# News-advisor blanket weight boost (2026-06-23 spec:
# docs/superpowers/specs/2026-06-23-news-weight-boost-design.md)
# ---------------------------------------------------------------------------

NEWS = "A3.news"
LIVE_NEWS = ["A1.insider", NEWS]


def test_news_boost_is_noop_by_default():
    """No news params → A3 floored normally (back-compat for existing callers)."""
    rb = resolve_weight_bundle(None, LIVE_NEWS, equal_floor=EQUAL_FLOOR)
    assert rb.weights[NEWS].weight == EQUAL_FLOOR
    assert rb.weights["A1.insider"].weight == EQUAL_FLOOR


def test_news_advisor_cold_weight_is_boosted():
    """Cold A3 → floor * multiplier, capped; non-news sibling untouched."""
    rb = resolve_weight_bundle(
        None, LIVE_NEWS, equal_floor=0.25,
        news_advisor_id=NEWS, news_multiplier=2.0, news_cap=0.50,
    )
    assert rb.weights[NEWS].weight == 0.50          # 0.25 * 2, under cap
    assert rb.weights[NEWS].shadow is False
    assert rb.weights["A1.insider"].weight == 0.25  # unchanged


def test_news_boost_respects_cap():
    """Boost never exceeds news_cap."""
    rb = resolve_weight_bundle(
        None, LIVE_NEWS, equal_floor=0.40,
        news_advisor_id=NEWS, news_multiplier=2.0, news_cap=0.50,
    )
    assert rb.weights[NEWS].weight == 0.50  # min(0.40*2=0.80, 0.50)


def test_graduated_news_advisor_boosted_and_capped():
    """Graduated A3 learned weight is boosted then capped."""
    led = WeightBundle(
        weights={
            "A1.insider": AdvisorWeight("A1.insider", 0.0, 0.0, 0.0, shadow=True),
            NEWS: AdvisorWeight(NEWS, 0.10, 0.05, 0.18, shadow=False),
        },
        correlation_matrix={},
    )
    rb = resolve_weight_bundle(
        led, LIVE_NEWS, equal_floor=0.25,
        news_advisor_id=NEWS, news_multiplier=2.0, news_cap=0.50,
    )
    assert rb.weights[NEWS].weight == 0.20  # min(0.10*2=0.20, 0.50)


def test_news_negative_skill_not_rescued_by_boost():
    """A3 flagged negative_skill stays 0/shadow — the boost must NOT rescue it."""
    rb = resolve_weight_bundle(
        None, LIVE_NEWS, equal_floor=0.25,
        cap_reasons={NEWS: "negative_skill"},
        news_advisor_id=NEWS, news_multiplier=2.0, news_cap=0.50,
    )
    assert rb.weights[NEWS].weight == 0.0
    assert rb.weights[NEWS].shadow is True


# ---------------------------------------------------------------------------
# Parole (unfreeze Stage 2)
# ---------------------------------------------------------------------------

def test_parole_advisor_floored_at_reduced_fraction():
    """cap_reason='parole' → equal_floor × parole_fraction, NON-shadow: the
    advisor keeps trading small instead of being benched."""
    rb = resolve_weight_bundle(
        None, ["A1.thin"], equal_floor=0.25,
        cap_reasons={"A1.thin": "parole"},
        parole_fraction=0.5,
    )
    assert rb.weights["A1.thin"].weight == pytest.approx(0.125)
    assert rb.weights["A1.thin"].shadow is False


def test_parole_default_fraction_is_half():
    rb = resolve_weight_bundle(
        None, ["A1.thin"], equal_floor=0.25,
        cap_reasons={"A1.thin": "parole"},
    )
    assert rb.weights["A1.thin"].weight == pytest.approx(0.125)


def test_negative_skill_still_hard_muted_not_parole():
    rb = resolve_weight_bundle(
        None, ["A1.bad"], equal_floor=0.25,
        cap_reasons={"A1.bad": "negative_skill"},
        parole_fraction=0.5,
    )
    assert rb.weights["A1.bad"].weight == 0.0
    assert rb.weights["A1.bad"].shadow is True
