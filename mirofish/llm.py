# mirofish/llm.py
"""LLM seam for the MiroFish A2 judge.

Defines the structural `LLM` Protocol that `judge()` depends on, the real
`AnthropicLLM` wrapper (lazy-imports the `anthropic` SDK so this module imports
with no SDK and no API key), and a `FakeLLM` that mirrors the SDK response shape
so the SAME parse path runs fully offline (and powers `--fake-llm`).

ISOLATION: this module must not depend on the arbiter package.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, Protocol

from mirofish.types import EvidencePack


# --------------------------------------------------------------------------- #
# Structural protocol (judge depends only on this shape).
# --------------------------------------------------------------------------- #
class LLM(Protocol):
    """Anything with a `.create(...)` mirroring `messages.create`."""

    def create(
        self,
        *,
        model: Any,
        max_tokens: Any,
        system: Any,
        messages: Any,
        tools: Any,
        tool_choice: Any,
    ) -> Any: ...


# --------------------------------------------------------------------------- #
# Real wrapper — lazy SDK import so the package imports without anthropic/key.
# --------------------------------------------------------------------------- #
class AnthropicLLM:
    """Thin wrapper over `anthropic.Anthropic().messages.create`.

    The `anthropic` SDK is imported INSIDE `__init__` so that merely importing
    this module (e.g. in `--fake-llm`/offline tests) does not require the SDK or
    the `ANTHROPIC_API_KEY`.
    """

    def __init__(self, *, api_key: str | None = None) -> None:
        import anthropic  # lazy — keeps module import SDK/key-free

        key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
        self._client = anthropic.Anthropic(api_key=key)

    def create(self, **kwargs: Any) -> Any:
        # Exact frozen call shape (plan §2.2) is constructed by the caller
        # (judge) and passed through verbatim as kwargs.
        return self._client.messages.create(**kwargs)


# --------------------------------------------------------------------------- #
# Fake LLM — mirrors the SDK response shape; no SDK / key / network needed.
# --------------------------------------------------------------------------- #
def _default_canned_opinions(pack: EvidencePack) -> list[dict]:
    """Deterministic 2-opinion payload derived from the pack.

    The short stance keys off RSI / distance-from-52w-high (overbought -> bearish);
    the medium stance keys off valuation_z (richer than sector -> bearish). This
    makes `--fake-llm` deterministic AND yields a NEGATIVE short stance for an
    overbought + richly-valued pack, which the end-to-end test asserts.
    """
    tech = pack.technical
    fund = pack.fundamental

    # --- short (technical) stance -------------------------------------------
    short = 0.0
    rsi = tech.rsi_14
    if rsi is not None:
        if rsi >= 70.0:
            short -= 0.5
        elif rsi <= 30.0:
            short += 0.5
    pct_high = tech.pct_from_52w_high
    if pct_high is not None and pct_high >= -0.02:
        # within 2% of the 52w high after a run -> overextended
        short -= 0.3
    mom = tech.momentum_20d
    if mom is not None and mom >= 0.15:
        short -= 0.2  # exhausted hot momentum
    short = max(-1.0, min(1.0, short))

    # --- medium (fundamental) stance ----------------------------------------
    medium = 0.0
    if fund is not None and fund.valuation_z is not None:
        # positive valuation_z = richer than sector = bearish-leaning
        medium = max(-1.0, min(1.0, -0.25 * fund.valuation_z))

    return [
        {
            "stance_score": short,
            "confidence": 0.6,
            "horizon_days": 10,
            "rationale": (
                "Technical read on supplied evidence: RSI/52w-high/momentum "
                "indicate the name is extended into resistance."
            ),
        },
        {
            "stance_score": medium,
            "confidence": 0.55,
            "horizon_days": 60,
            "rationale": (
                "Fundamental read: valuation relative to its sector baseline "
                "drives the medium-horizon view."
            ),
        },
    ]


class FakeLLM:
    """Offline stand-in. `.create()` returns a SimpleNamespace mirroring the SDK.

    Constructed with a canned `opinions` list (and optional `stop_reason`
    override). If `opinions` is None the canned default is derived from the pack
    rendered in the user message at call time, so `--fake-llm` is deterministic.
    """

    def __init__(
        self,
        opinions: list[dict] | None = None,
        stop_reason: str = "tool_use",
    ) -> None:
        self._opinions = opinions
        self._stop_reason = stop_reason
        self.create_calls = 0  # observability for cache-hit tests
        self._pack: EvidencePack | None = None

    def bind_pack(self, pack: EvidencePack) -> None:
        """Optionally supply the pack so the default canned response is derived
        from it (used by the service in `--fake-llm` mode). No-op for tests that
        pass an explicit `opinions` list."""
        self._pack = pack

    def create(self, **kwargs: Any) -> Any:
        self.create_calls += 1

        opinions = self._opinions
        if opinions is None:
            if self._pack is not None:
                opinions = _default_canned_opinions(self._pack)
            else:
                opinions = [
                    {
                        "stance_score": 0.0,
                        "confidence": 0.5,
                        "horizon_days": 10,
                        "rationale": "(default fake opinion)",
                    },
                    {
                        "stance_score": 0.0,
                        "confidence": 0.5,
                        "horizon_days": 60,
                        "rationale": "(default fake opinion)",
                    },
                ]

        block = SimpleNamespace(
            type="tool_use",
            name="emit_opinions",
            input={"opinions": opinions},
        )
        return SimpleNamespace(
            stop_reason=self._stop_reason,
            stop_details=None,
            content=[block],
        )
