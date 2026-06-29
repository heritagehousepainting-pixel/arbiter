"""Macro market-news + staleness reasoning via Claude web search (fail-closed)."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import structlog

from arbiter.refresh.llm import AnthropicLLM
from arbiter.refresh.types import MacroFinding, MacroResult, Severity, StaleFlag

log = structlog.get_logger(__name__)

KNOWN_SOURCES = {"fund_managers", "activist_filers", "watchlist", "sectors"}
_WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}
_MAX_CONTINUATIONS = 5
_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _severity(raw: str) -> Severity:
    try:
        return Severity(str(raw).lower())
    except ValueError:
        return Severity.LOW


def parse_macro_json(text: str) -> tuple[list[MacroFinding], list[StaleFlag]]:
    """Extract findings + stale flags from the model's text. Raises on no JSON."""
    m = _JSON_BLOCK.search(text)
    blob = m.group(1) if m else text[text.index("{"):text.rindex("}") + 1]
    data = json.loads(blob)
    findings = [
        MacroFinding(
            summary=str(it.get("summary", ""))[:500],
            severity=_severity(it.get("severity", "low")),
            affected_tickers=[str(t).upper() for t in it.get("affected_tickers", [])],
            sources=[str(s) for s in it.get("sources", [])],
        )
        for it in data.get("market", []) if isinstance(it, dict)
    ]
    flags = [
        StaleFlag(source=str(it.get("source", "")),
                  reason=str(it.get("reason", ""))[:300],
                  sources=[str(s) for s in it.get("sources", [])])
        for it in data.get("stale_sources", [])
        if isinstance(it, dict) and str(it.get("source", "")) in KNOWN_SOURCES
    ]
    return findings, flags


def _prompt(tickers: list[str], as_of: datetime) -> str:
    held = ", ".join(tickers) if tickers else "(none)"
    return (
        f"Today is {as_of.date().isoformat()} (Monday pre-market). Use web search to "
        "identify (1) news that moved or is likely to move the broad US equity market "
        "this week, and which of these held tickers it could affect; and (2) whether "
        "any of these arbiter data sources look stale due to a real-world event: "
        f"{sorted(KNOWN_SOURCES)} (e.g. an activist fund wound down, a tracked manager "
        f"left, a watchlist ticker was acquired/delisted). Held tickers: {held}.\n\n"
        "Respond with a single fenced ```json block of the form:\n"
        '{"market": [{"summary": str, "severity": "low|medium|high", '
        '"affected_tickers": [str], "sources": [str]}], '
        '"stale_sources": [{"source": str, "reason": str, "sources": [str]}]}\n'
        "Only use these exact source names for stale_sources: "
        f"{sorted(KNOWN_SOURCES)}."
    )


def _call(llm: Any, model: str, prompt: str) -> str:
    messages = [{"role": "user", "content": prompt}]
    for _ in range(_MAX_CONTINUATIONS + 1):
        resp = llm.create(model=model, max_tokens=8000,
                          thinking={"type": "adaptive"},
                          tools=[_WEB_SEARCH_TOOL], messages=messages)
        if getattr(resp, "stop_reason", "end_turn") == "pause_turn":
            messages = [messages[0], {"role": "assistant", "content": resp.content}]
            continue
        return "".join(getattr(b, "text", "") for b in resp.content
                       if getattr(b, "type", None) == "text")
    return ""


def scan_macro(tickers: list[str], as_of: datetime, config: Any,
               *, llm: Any = None) -> MacroResult:
    key = getattr(config, "anthropic_api_key", "") or ""
    if llm is None and not key:
        return MacroResult(findings=[], stale_flags=[], available=False,
                           note="skipped (no ANTHROPIC_API_KEY)")
    try:
        client = llm if llm is not None else AnthropicLLM(api_key=key)
        text = _call(client, getattr(config, "refresh_model", "claude-opus-4-8"),
                     _prompt(tickers, as_of))
        findings, flags = parse_macro_json(text)
        return MacroResult(findings=findings, stale_flags=flags, available=True, note="")
    except Exception as exc:  # fail-closed
        log.warning("refresh.macro_scan.failed", error=str(exc))
        return MacroResult(findings=[], stale_flags=[], available=False,
                           note=f"unavailable: {type(exc).__name__}")
