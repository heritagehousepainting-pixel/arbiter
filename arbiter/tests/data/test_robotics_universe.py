"""Data-hygiene tests for the canonical robotics universe (source of truth for
the cockpit board #2 and the robotics signal #3)."""
from __future__ import annotations

from arbiter.data.robotics_universe import (
    LAYERS,
    early_insight_names,
    robotics_universe,
    universe_by_symbol,
)

_LAYERS = {"compute", "brain", "components", "integrator", "deployment"}
_LONGEVITY = {"chokepoint", "durable", "commodity", "hype-risk", "unclear"}
_REQUIRED = {"symbol", "company", "layer", "longevity", "priceable"}


def test_nonempty():
    assert len(robotics_universe()) >= 25


def test_required_fields_and_enums():
    for r in robotics_universe():
        assert _REQUIRED <= set(r), f"{r.get('symbol')} missing required fields"
        assert r["layer"] in _LAYERS, f"{r['symbol']} bad layer {r['layer']}"
        assert r["longevity"] in _LONGEVITY, f"{r['symbol']} bad longevity {r['longevity']}"
        assert isinstance(r["priceable"], bool)


def test_no_duplicate_symbols():
    syms = [r["symbol"] for r in robotics_universe()]
    assert len(syms) == len(set(syms)), "duplicate symbols in universe"


def test_every_layer_represented():
    assert {r["layer"] for r in robotics_universe()} == _LAYERS
    assert set(LAYERS) == _LAYERS


def test_early_insight_rows_have_trigger():
    for r in robotics_universe():
        if r.get("early_insight"):
            assert r.get("trigger"), f"{r['symbol']} early_insight without trigger"


def test_has_both_priceable_and_reference():
    rows = robotics_universe()
    assert any(r["priceable"] for r in rows)
    assert any(not r["priceable"] for r in rows)


def test_early_insight_accessor():
    early = early_insight_names()
    assert len(early) >= 8
    assert all(r.get("early_insight") and r.get("trigger") for r in early)
    # accessor is a strict subset of the full universe
    all_syms = {r["symbol"] for r in robotics_universe()}
    assert {r["symbol"] for r in early} <= all_syms


def test_universe_by_symbol():
    by = universe_by_symbol()
    assert by["NVDA"]["company"] == "Nvidia"
    subset = universe_by_symbol({"NVDA", "SKILD"})
    assert set(subset) == {"NVDA", "SKILD"}


def test_returned_dicts_are_copies():
    """Mutating a returned row must not corrupt the module-level data."""
    rows = robotics_universe()
    rows[0]["company"] = "MUTATED"
    assert robotics_universe()[0]["company"] != "MUTATED"
