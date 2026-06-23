# mirofish/judge.py
"""The MiroFish A2 judge: render an EvidencePack into an analyst brief, call the
LLM with the frozen `emit_opinions` tool, and parse/clamp the tool result into
well-formed `OpinionOut`s.

Frozen contracts: the tool schema (§2.1), the system prompt rules (§2.3), the
exact Anthropic call shape (§2.2), and the parse/abstain/clamp rules (§2.4).

ISOLATION: this module must not depend on the arbiter package.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from mirofish.types import (
    CONFIDENCE_MAX,
    CONFIDENCE_MIN,
    MEDIUM_DAYS,
    SHORT_DAYS,
    STANCE_MAX,
    STANCE_MIN,
    EvidencePack,
    FundamentalFeatures,
    OpinionOut,
    TechnicalFeatures,
)

if TYPE_CHECKING:
    from mirofish.llm import LLM


# --------------------------------------------------------------------------- #
# Frozen `emit_opinions` tool JSON-Schema (plan §2.1, verbatim).
# --------------------------------------------------------------------------- #
EMIT_OPINIONS_TOOL = {
    "name": "emit_opinions",
    "description": (
        "Emit exactly two independent analyst opinions on the ticker, grounded "
        "only in the supplied evidence. opinions[0] is the SHORT-horizon "
        "technical-led view (~10 trading days); opinions[1] is the MEDIUM-horizon "
        "fundamental-led view (~60 days). stance_score is signed: negative = "
        "bearish, positive = bullish, 0 = neutral. Do not invent facts not "
        "present in the evidence."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "opinions": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "stance_score": {
                            "type": "number", "minimum": -1.0, "maximum": 1.0,
                            "description": "Signed conviction. Negative = bearish, positive = bullish, 0 = neutral.",
                        },
                        "confidence": {
                            "type": "number", "exclusiveMinimum": 0.0, "maximum": 1.0,
                            "description": "Strength of conviction in (0, 1]. Never 0.",
                        },
                        "horizon_days": {
                            "type": "integer", "minimum": 1, "maximum": 365,
                            "description": "Holding horizon in trading days. ~10 short, ~60 medium.",
                        },
                        "rationale": {
                            "type": "string", "minLength": 1, "maxLength": 600,
                            "description": "One paragraph grounded ONLY in supplied evidence. No invented facts.",
                        },
                    },
                    "required": ["stance_score", "confidence", "horizon_days", "rationale"],
                },
            }
        },
        "required": ["opinions"],
    },
}


# --------------------------------------------------------------------------- #
# Frozen system prompt — encodes the 6 independent-skeptic rules (plan §2.3).
# --------------------------------------------------------------------------- #
ANALYST_SYSTEM_PROMPT = (
    "You are an INDEPENDENT, skeptical equity analyst. You did NOT originate "
    "this idea and you do not know why anyone else likes it. Judge the name "
    "purely on the evidence in front of you.\n"
    "\n"
    "You MAY and SHOULD return a NEGATIVE stance_score when the evidence shows "
    "the name is technically overextended (high RSI, far above its moving "
    "averages, exhausted momentum, or pressed right up under a recent 52-week "
    "high after a run) or richly valued versus its sector (positive valuation_z, "
    "elevated P/E).\n"
    "\n"
    "Do NOT default to bullish. A neutral stance (0) and a bearish stance (<0) "
    "are first-class, expected outcomes whenever the evidence warrants them.\n"
    "\n"
    "Ground every rationale ONLY in the supplied evidence. Never invent facts, "
    "news, prices, or figures that are not present in the evidence. If a field "
    "is shown as 'n/a', do not speculate about it.\n"
    "\n"
    "Emit EXACTLY two opinions via the emit_opinions tool. opinions[0] is the "
    "SHORT-horizon view (~10 trading days), led by the TECHNICAL evidence. "
    "opinions[1] is the MEDIUM-horizon view (~60 days), led by the FUNDAMENTAL "
    "evidence.\n"
    "\n"
    "If the evidence contains NO fundamentals (fundamentals shown as 'n/a'), "
    "still emit two opinions but base BOTH primarily on the technical evidence.\n"
)


# --------------------------------------------------------------------------- #
# Render an EvidencePack as a compact, labeled plain-text analyst brief.
# --------------------------------------------------------------------------- #
def _na(v: object) -> str:
    return "n/a" if v is None else str(v)


def _tech_lines(t: TechnicalFeatures) -> list[str]:
    return [
        f"  last_close: {_na(t.last_close)}",
        f"  ma_50: {_na(t.ma_50)}",
        f"  ma_200: {_na(t.ma_200)}",
        f"  pct_vs_ma_50: {_na(t.pct_vs_ma_50)}",
        f"  pct_vs_ma_200: {_na(t.pct_vs_ma_200)}",
        f"  momentum_20d: {_na(t.momentum_20d)}",
        f"  rsi_14: {_na(t.rsi_14)}",
        f"  realized_vol_annualized: {_na(t.realized_vol_annualized)}",
        f"  pct_from_52w_high: {_na(t.pct_from_52w_high)}",
        f"  pct_from_52w_low: {_na(t.pct_from_52w_low)}",
        f"  volume_surge_ratio: {_na(t.volume_surge_ratio)}",
        f"  n_bars: {_na(t.n_bars)}",
    ]


def _fund_lines(f: FundamentalFeatures) -> list[str]:
    return [
        f"  revenue_ttm: {_na(f.revenue_ttm)}",
        f"  revenue_growth_yoy: {_na(f.revenue_growth_yoy)}",
        f"  gross_margin: {_na(f.gross_margin)}",
        f"  operating_margin: {_na(f.operating_margin)}",
        f"  net_income_ttm: {_na(f.net_income_ttm)}",
        f"  shares_outstanding: {_na(f.shares_outstanding)}",
        f"  pe_ratio: {_na(f.pe_ratio)}",
        f"  ps_ratio: {_na(f.ps_ratio)}",
        f"  sector: {_na(f.sector)}",
        f"  valuation_z: {_na(f.valuation_z)}",
        f"  as_of_latest_filed: {_na(f.as_of_latest_filed)}",
    ]


def render_pack(pack: EvidencePack) -> str:
    """A compact, labeled plain-text brief (NOT a JSON dump). None -> 'n/a'."""
    lines: list[str] = []
    lines.append(f"Ticker: {pack.ticker.upper()}")
    lines.append(f"As-of (UTC): {pack.as_of.date().isoformat()}")
    lines.append("")
    lines.append("Technical evidence (price action <= as_of):")
    lines.extend(_tech_lines(pack.technical))
    lines.append("")
    if pack.fundamental is None:
        lines.append("Fundamental evidence: n/a (no point-in-time fundamentals available)")
    else:
        lines.append("Fundamental evidence (SEC facts filed <= as_of):")
        lines.extend(_fund_lines(pack.fundamental))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The judge.
# --------------------------------------------------------------------------- #
def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _parse_one(raw: object, *, index: int, fingerprint: str) -> OpinionOut:
    """Re-validate + clamp a single raw opinion dict. Raises on bad input
    (caller skips). horizon assigned BY INDEX/ROLE, not from the model."""
    if not isinstance(raw, dict):
        raise TypeError("opinion is not an object")

    stance = _clamp(float(raw["stance_score"]), STANCE_MIN, STANCE_MAX)
    confidence = _clamp(float(raw["confidence"]), CONFIDENCE_MIN, CONFIDENCE_MAX)
    horizon = SHORT_DAYS if index == 0 else MEDIUM_DAYS

    rationale = str(raw.get("rationale", "")).strip()
    if len(rationale) > 600:
        rationale = rationale[:600]
    if not rationale:
        rationale = "(no rationale)"

    return OpinionOut(
        stance_score=stance,
        confidence=confidence,
        horizon_days=horizon,
        rationale=rationale,
        source_fingerprint=fingerprint,
    )


def judge(pack: EvidencePack, *, model: str, llm: "LLM") -> list[OpinionOut]:
    """Call the LLM and parse its `emit_opinions` tool_use into OpinionOuts.

    NEVER raises: any error -> `[]` (abstain). Parse/clamp rules per plan §2.4.
    """
    try:
        evidence_text = render_pack(pack)

        resp = llm.create(
            model=model,
            max_tokens=1024,
            system=ANALYST_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": evidence_text}],
            tools=[EMIT_OPINIONS_TOOL],
            tool_choice={
                "type": "tool",
                "name": "emit_opinions",
                "disable_parallel_tool_use": True,
            },
        )

        # 1. Abstain unless the model actually used the tool.
        if getattr(resp, "stop_reason", None) != "tool_use":
            return []

        # 2. First emit_opinions tool_use block.
        block = None
        for b in getattr(resp, "content", None) or []:
            if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == "emit_opinions":
                block = b
                break
        if block is None:
            return []

        # 3. payload.opinions must be a non-empty list.
        payload = getattr(block, "input", None)
        if not isinstance(payload, dict):
            return []
        raw_opinions = payload.get("opinions")
        if not isinstance(raw_opinions, list) or not raw_opinions:
            return []

        # 4. Re-validate + clamp each; per-opinion exception -> skip.
        result: list[OpinionOut] = []
        for index, raw in enumerate(raw_opinions):
            try:
                result.append(
                    _parse_one(raw, index=index, fingerprint=pack.source_fingerprint)
                )
            except Exception:
                continue

        # 5. No fundamentals -> only the SHORT (technical) opinion.
        if pack.fundamental is None:
            return result[:1]
        return result[:2]
    except Exception:
        return []
