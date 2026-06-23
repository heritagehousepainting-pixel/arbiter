"""Offline tests for the FakeLLM stand-in (mirrors the SDK response shape)."""
from __future__ import annotations

from datetime import datetime, timezone

from mirofish.judge import judge
from mirofish.llm import AnthropicLLM, FakeLLM, _default_canned_opinions
from mirofish.types import (
    EvidencePack,
    FundamentalFeatures,
    TechnicalFeatures,
    compute_fingerprint,
)

_AS_OF = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _tech(**over) -> TechnicalFeatures:
    base = dict(
        last_close=100.0, ma_50=90.0, ma_200=80.0,
        pct_vs_ma_50=0.11, pct_vs_ma_200=0.25, momentum_20d=0.18,
        rsi_14=78.0, realized_vol_annualized=0.3,
        pct_from_52w_high=-0.01, pct_from_52w_low=0.5,
        volume_surge_ratio=1.5, n_bars=250,
    )
    base.update(over)
    return TechnicalFeatures(**base)


def _fund(**over) -> FundamentalFeatures:
    base = dict(
        revenue_ttm=1e9, revenue_growth_yoy=0.1, gross_margin=0.5,
        operating_margin=0.2, net_income_ttm=1e8, shares_outstanding=1e7,
        pe_ratio=40.0, ps_ratio=10.0, sector="Tech",
        valuation_z=2.0, as_of_latest_filed="2026-03-31",
    )
    base.update(over)
    return FundamentalFeatures(**base)


def _pack(tech=None, fund=None) -> EvidencePack:
    tech = tech if tech is not None else _tech()
    fp = compute_fingerprint("AAPL", tech, fund)
    return EvidencePack(
        ticker="AAPL", as_of=_AS_OF, technical=tech, fundamental=fund,
        source_fingerprint=fp,
    )


def test_create_mirrors_sdk_shape():
    fake = FakeLLM(opinions=[
        {"stance_score": 0.2, "confidence": 0.5, "horizon_days": 10, "rationale": "a"},
        {"stance_score": -0.1, "confidence": 0.4, "horizon_days": 60, "rationale": "b"},
    ])
    resp = fake.create(
        model="m", max_tokens=1024, system="s",
        messages=[], tools=[], tool_choice={},
    )
    assert resp.stop_reason == "tool_use"
    assert resp.stop_details is None
    block = resp.content[0]
    assert block.type == "tool_use"
    assert block.name == "emit_opinions"
    assert block.input["opinions"][0]["stance_score"] == 0.2


def test_canned_payload_round_trips_through_judge():
    fake = FakeLLM(opinions=[
        {"stance_score": 0.3, "confidence": 0.7, "horizon_days": 10, "rationale": "tech"},
        {"stance_score": 0.1, "confidence": 0.6, "horizon_days": 60, "rationale": "fund"},
    ])
    pack = _pack(fund=_fund())
    out = judge(pack, model="m", llm=fake)
    assert len(out) == 2
    assert out[0].stance_score == 0.3


def test_default_canned_is_deterministic_and_negative_for_overbought_rich():
    pack = _pack(fund=_fund(valuation_z=2.0))
    a = _default_canned_opinions(pack)
    b = _default_canned_opinions(pack)
    assert a == b
    # overbought (rsi 78, near high, hot momentum) -> negative short
    assert a[0]["stance_score"] < 0
    # rich (valuation_z +2) -> negative medium
    assert a[1]["stance_score"] < 0


def test_default_canned_via_bind_pack():
    pack = _pack(fund=_fund(valuation_z=2.0))
    fake = FakeLLM()  # no explicit opinions
    fake.bind_pack(pack)
    out = judge(pack, model="m", llm=fake)
    assert len(out) == 2
    assert out[0].stance_score < 0


def test_stop_reason_override():
    fake = FakeLLM(opinions=[], stop_reason="max_tokens")
    resp = fake.create(model="m", max_tokens=1, system="s",
                       messages=[], tools=[], tool_choice={})
    assert resp.stop_reason == "max_tokens"


def test_create_call_counter():
    fake = FakeLLM(opinions=[
        {"stance_score": 0.0, "confidence": 0.5, "horizon_days": 10, "rationale": "a"},
        {"stance_score": 0.0, "confidence": 0.5, "horizon_days": 60, "rationale": "b"},
    ])
    assert fake.create_calls == 0
    fake.create(model="m", max_tokens=1, system="s", messages=[], tools=[], tool_choice={})
    fake.create(model="m", max_tokens=1, system="s", messages=[], tools=[], tool_choice={})
    assert fake.create_calls == 2


def test_anthropic_llm_class_present_but_not_constructed():
    # Lazy import means class is importable with no key/SDK call. We do NOT
    # construct it here (that would require the SDK + would try to read a key).
    assert callable(AnthropicLLM)
