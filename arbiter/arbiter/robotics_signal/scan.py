"""Robotics-sector web-search scan via Claude (fail-closed).

Mirrors ``arbiter/arbiter/refresh/macro_scan.py``: one agentic ``web_search``
pass (pause_turn continuation loop), a single fenced ```json block parsed into
developments. Broad robotics developments each cycle, with explicit trigger-hit
flagging keyed to the universe's per-name "trigger to watch".
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import structlog

from arbiter.data.robotics_universe import early_insight_names, robotics_universe
from arbiter.refresh.llm import AnthropicLLM
from arbiter.robotics_signal.types import CATEGORIES, RoboticsDevelopment, RoboticsScanResult

log = structlog.get_logger(__name__)

_WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}
_MAX_CONTINUATIONS = 5
_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _category(raw: Any) -> str:
    c = str(raw or "other").lower()
    return c if c in CATEGORIES else "other"


def parse_robotics_json(text: str, valid_symbols: set[str]) -> list[RoboticsDevelopment]:
    """Extract developments from the model's text. Raises on no JSON.

    ``trigger_name`` is validated against ``valid_symbols`` — an unknown symbol
    demotes the row to ``trigger_hit=False`` (never trust a fabricated trigger).
    """
    m = _JSON_BLOCK.search(text)
    blob = m.group(1) if m else text[text.index("{"):text.rindex("}") + 1]
    data = json.loads(blob)
    out: list[RoboticsDevelopment] = []
    for it in data.get("developments", []):
        if not isinstance(it, dict):
            continue
        name = it.get("trigger_name")
        name = str(name) if name else None
        hit = bool(it.get("trigger_hit")) and name in valid_symbols
        out.append(
            RoboticsDevelopment(
                headline=str(it.get("headline", ""))[:200],
                summary=str(it.get("summary", ""))[:600],
                category=_category(it.get("category")),
                symbols=[str(s) for s in it.get("symbols", []) if s],
                trigger_hit=hit,
                trigger_name=name if hit else None,
                sources=[str(s) for s in it.get("sources", []) if s],
            )
        )
    return out


def _prompt(universe: list[dict], triggers: list[dict], as_of: datetime) -> str:
    roster = "; ".join(
        f"{r['symbol']} ({r['company']}, {r['layer']})" for r in universe
    )
    watch = "\n".join(
        f"  - {t['symbol']} ({t['company']}): {t.get('trigger', '')}" for t in triggers
    )
    return (
        f"Today is {as_of.date().isoformat()}. You are scanning the robotics sector "
        "for a twice-weekly early-insight digest. Use web search to identify the most "
        "notable robotics-sector developments since the last few days — funding rounds, "
        "orders, IPO filings, production-ramp news, partnerships, policy, demos.\n\n"
        "SECONDLY, flag any development that appears to satisfy one of these specific "
        "watch-triggers (name the matching symbol in trigger_name):\n"
        f"{watch}\n\n"
        f"Universe for reference (symbol → company, layer): {roster}\n\n"
        "Respond with a single fenced ```json block of the form:\n"
        '{"developments": [{"headline": str, "summary": str, '
        '"category": "compute|brain|components|integrator|deployment|other", '
        '"symbols": [str], "trigger_hit": bool, "trigger_name": str|null, '
        '"sources": [str]}]}\n'
        "Set trigger_hit=true and trigger_name to the exact universe symbol ONLY when a "
        "development genuinely matches that name's watch-trigger; otherwise trigger_hit=false. "
        "Prefer a few high-signal items over exhaustiveness. Ground every item in a source URL."
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


def scan_robotics(as_of: datetime, config: Any, *, llm: Any = None,
                  universe: list[dict] | None = None) -> RoboticsScanResult:
    """Run one robotics scan. Fail-closed: any error → ``available=False``."""
    key = getattr(config, "anthropic_api_key", "") or ""
    if llm is None and not key:
        return RoboticsScanResult(available=False, note="skipped (no ANTHROPIC_API_KEY)")
    uni = universe if universe is not None else robotics_universe()
    valid = {r["symbol"] for r in uni}
    model = getattr(config, "robotics_model", "") or getattr(
        config, "refresh_model", "claude-opus-4-8")
    try:
        client = llm if llm is not None else AnthropicLLM(api_key=key)
        text = _call(client, model, _prompt(uni, early_insight_names(), as_of))
        return RoboticsScanResult(developments=parse_robotics_json(text, valid),
                                  available=True, note="")
    except Exception as exc:  # fail-closed
        log.warning("robotics_signal.scan.failed", error=str(exc))
        return RoboticsScanResult(available=False, note=f"unavailable: {type(exc).__name__}")
