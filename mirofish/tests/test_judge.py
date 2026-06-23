"""Offline tests for the judge: parse, clamp, abstain, degradation."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from mirofish.judge import (
    ANALYST_SYSTEM_PROMPT,
    EMIT_OPINIONS_TOOL,
    judge,
    render_pack,
)
from mirofish.llm import FakeLLM
from mirofish.types import (
    CONFIDENCE_MIN,
    MEDIUM_DAYS,
    SHORT_DAYS,
    EvidencePack,
    FundamentalFeatures,
    TechnicalFeatures,
    compute_fingerprint,
)

_AS_OF = datetime(2026, 6, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Fixtures built by hand (Build B owns its own test fixtures).
# --------------------------------------------------------------------------- #
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


def _pack(tech=None, fund=None, ticker="AAPL") -> EvidencePack:
    tech = tech if tech is not None else _tech()
    fp = compute_fingerprint(ticker, tech, fund)
    return EvidencePack(
        ticker=ticker, as_of=_AS_OF, technical=tech, fundamental=fund,
        source_fingerprint=fp,
    )


def overbought_rich_pack() -> EvidencePack:
    tech = _tech(rsi_14=82.0, pct_from_52w_high=-0.005, momentum_20d=0.25)
    fund = _fund(valuation_z=2.5, pe_ratio=55.0)
    return _pack(tech=tech, fund=fund)


def no_fundamentals_pack() -> EvidencePack:
    return _pack(fund=None)


def _two_opinions(s0=0.3, c0=0.7, s1=0.1, c1=0.6):
    return [
        {"stance_score": s0, "confidence": c0, "horizon_days": 10, "rationale": "tech view"},
        {"stance_score": s1, "confidence": c1, "horizon_days": 60, "rationale": "fund view"},
    ]


# --------------------------------------------------------------------------- #
# Frozen constants present and correct.
# --------------------------------------------------------------------------- #
def test_tool_schema_frozen_shape():
    assert EMIT_OPINIONS_TOOL["name"] == "emit_opinions"
    items = EMIT_OPINIONS_TOOL["input_schema"]["properties"]["opinions"]
    assert items["minItems"] == 2 and items["maxItems"] == 2
    props = items["items"]["properties"]
    assert props["stance_score"]["minimum"] == -1.0
    assert props["confidence"]["exclusiveMinimum"] == 0.0
    assert "strict" not in EMIT_OPINIONS_TOOL


def test_system_prompt_encodes_negative_rule():
    p = ANALYST_SYSTEM_PROMPT.lower()
    assert "independent" in p and "skeptic" in p
    assert "negative" in p
    assert "do not default to bullish" in p
    assert "do not invent" in p or "never invent" in p


# --------------------------------------------------------------------------- #
# render_pack
# --------------------------------------------------------------------------- #
def test_render_pack_labeled_text_not_json():
    out = render_pack(_pack(fund=_fund()))
    assert "Ticker: AAPL" in out
    assert "rsi_14: 78.0" in out
    assert "valuation_z: 2.0" in out
    assert not out.lstrip().startswith("{")


def test_render_pack_none_becomes_na():
    tech = _tech(ma_200=None, rsi_14=None)
    out = render_pack(_pack(tech=tech, fund=None))
    assert "ma_200: n/a" in out
    assert "Fundamental evidence: n/a" in out


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_happy_path_two_opinions_horizons_and_fingerprint():
    pack = _pack(fund=_fund())
    fake = FakeLLM(opinions=_two_opinions())
    out = judge(pack, model="m", llm=fake)
    assert len(out) == 2
    assert out[0].horizon_days == SHORT_DAYS == 10
    assert out[1].horizon_days == MEDIUM_DAYS == 60
    assert all(o.source_fingerprint == pack.source_fingerprint for o in out)
    # horizon is assigned by role even if the model said something else
    fake2 = FakeLLM(opinions=[
        {"stance_score": 0.1, "confidence": 0.5, "horizon_days": 999, "rationale": "x"},
        {"stance_score": 0.1, "confidence": 0.5, "horizon_days": 1, "rationale": "y"},
    ])
    out2 = judge(pack, model="m", llm=fake2)
    assert [o.horizon_days for o in out2] == [SHORT_DAYS, MEDIUM_DAYS]


# --------------------------------------------------------------------------- #
# Clamp (load-bearing)
# --------------------------------------------------------------------------- #
def test_clamp_stance_over_one_and_confidence_zero():
    pack = _pack(fund=_fund())
    fake = FakeLLM(opinions=[
        {"stance_score": 2.5, "confidence": 0.0, "horizon_days": 10, "rationale": "a"},
        {"stance_score": -9.0, "confidence": 5.0, "horizon_days": 60, "rationale": "b"},
    ])
    out = judge(pack, model="m", llm=fake)
    assert out[0].stance_score == 1.0
    assert out[0].confidence == CONFIDENCE_MIN
    assert out[1].stance_score == -1.0  # clamped to floor, still negative
    assert out[1].confidence == 1.0


# --------------------------------------------------------------------------- #
# Negative-stance passthrough (load-bearing)
# --------------------------------------------------------------------------- #
def test_negative_stance_passes_through_unclamped():
    pack = overbought_rich_pack()
    fake = FakeLLM(opinions=[
        {"stance_score": -0.65, "confidence": 0.8, "horizon_days": 10, "rationale": "overbought"},
        {"stance_score": -0.4, "confidence": 0.7, "horizon_days": 60, "rationale": "rich"},
    ])
    out = judge(pack, model="m", llm=fake)
    assert out[0].stance_score == -0.65  # no abs, no floor at 0
    assert out[1].stance_score == -0.4


# --------------------------------------------------------------------------- #
# Abstain
# --------------------------------------------------------------------------- #
def test_abstain_on_max_tokens():
    pack = _pack(fund=_fund())
    fake = FakeLLM(opinions=_two_opinions(), stop_reason="max_tokens")
    assert judge(pack, model="m", llm=fake) == []


def test_abstain_on_refusal():
    pack = _pack(fund=_fund())
    fake = FakeLLM(opinions=_two_opinions(), stop_reason="refusal")
    assert judge(pack, model="m", llm=fake) == []


def test_abstain_on_missing_tool_use_block():
    pack = _pack(fund=_fund())

    class NoToolLLM:
        def create(self, **kwargs):
            text_block = SimpleNamespace(type="text", text="hello")
            return SimpleNamespace(stop_reason="tool_use", stop_details=None,
                                   content=[text_block])

    assert judge(pack, model="m", llm=NoToolLLM()) == []


def test_abstain_on_empty_opinions():
    pack = _pack(fund=_fund())
    fake = FakeLLM(opinions=[])
    assert judge(pack, model="m", llm=fake) == []


# --------------------------------------------------------------------------- #
# Degradation: no fundamentals -> exactly one SHORT opinion.
# --------------------------------------------------------------------------- #
def test_no_fundamentals_yields_single_short_opinion():
    pack = no_fundamentals_pack()
    fake = FakeLLM(opinions=_two_opinions())  # model emits two
    out = judge(pack, model="m", llm=fake)
    assert len(out) == 1
    assert out[0].horizon_days == SHORT_DAYS


# --------------------------------------------------------------------------- #
# Never raises.
# --------------------------------------------------------------------------- #
def test_judge_never_raises_when_llm_throws():
    pack = _pack(fund=_fund())

    class BoomLLM:
        def create(self, **kwargs):
            raise RuntimeError("boom")

    assert judge(pack, model="m", llm=BoomLLM()) == []


def test_judge_never_raises_on_garbage_response():
    pack = _pack(fund=_fund())

    class GarbageLLM:
        def create(self, **kwargs):
            return SimpleNamespace(stop_reason="tool_use", stop_details=None,
                                   content="not-a-list")

    assert judge(pack, model="m", llm=GarbageLLM()) == []


def test_per_opinion_exception_skips_only_bad_one():
    pack = _pack(fund=_fund())
    fake = FakeLLM(opinions=[
        {"stance_score": "not-a-number", "confidence": 0.5, "horizon_days": 10, "rationale": "bad"},
        {"stance_score": 0.2, "confidence": 0.5, "horizon_days": 60, "rationale": "good"},
    ])
    out = judge(pack, model="m", llm=fake)
    # first (index 0) is bad and skipped; the surviving one keeps its own index
    # role -> MEDIUM. Length is 1, and with fundamentals present we keep up to 2.
    assert len(out) == 1
    assert out[0].stance_score == 0.2
    assert out[0].horizon_days == MEDIUM_DAYS


def test_rationale_truncated_and_empty_defaulted():
    pack = _pack(fund=_fund())
    long_r = "x" * 800
    fake = FakeLLM(opinions=[
        {"stance_score": 0.1, "confidence": 0.5, "horizon_days": 10, "rationale": long_r},
        {"stance_score": 0.1, "confidence": 0.5, "horizon_days": 60, "rationale": "   "},
    ])
    out = judge(pack, model="m", llm=fake)
    assert len(out[0].rationale) == 600
    assert out[1].rationale == "(no rationale)"


def test_package_imports_without_anthropic_key(monkeypatch):
    # No ANTHROPIC_API_KEY in env (clear_env autouse), yet importing llm works.
    import importlib

    import mirofish.llm as llm_mod
    importlib.reload(llm_mod)
    assert hasattr(llm_mod, "AnthropicLLM")
