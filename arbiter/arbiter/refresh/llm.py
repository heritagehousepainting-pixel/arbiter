"""LLM seam for the Monday macro scan.

Mirrors mirofish/llm.py: a structural `LLM` Protocol, a real `AnthropicLLM`
wrapper that lazy-imports the SDK (so this module imports with no SDK/key), and
a `FakeLLM` whose response object mirrors the SDK shape for offline tests.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLM(Protocol):
    def create(self, *, model: Any, max_tokens: Any, thinking: Any,
               tools: Any, messages: Any) -> Any: ...


class AnthropicLLM:
    """Thin wrapper over `anthropic.Anthropic().messages.create` (lazy import)."""

    def __init__(self, *, api_key: str | None = None) -> None:
        import anthropic  # lazy — keeps module import SDK/key-free
        key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
        self._client = anthropic.Anthropic(api_key=key)

    def create(self, **kwargs: Any) -> Any:
        return self._client.messages.create(**kwargs)


class FakeLLM:
    """Returns a single end_turn text block carrying `canned_text`."""

    def __init__(self, canned_text: str) -> None:
        self._text = canned_text

    def create(self, **_kwargs: Any) -> Any:
        block = SimpleNamespace(type="text", text=self._text)
        return SimpleNamespace(content=[block], stop_reason="end_turn")
