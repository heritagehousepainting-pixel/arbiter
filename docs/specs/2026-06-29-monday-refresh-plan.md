# Monday Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an 08:00-Monday pre-market intelligence pass that scans market/position news + data-source staleness, pushes a digest to the user's phone, and feeds findings into the engine via a new probationary `A4.macro` advisor and source-targeted re-ingest.

**Architecture:** A new `arbiter/refresh/` package (orchestrator + four fail-closed scan units + an LLM seam mirroring MiroFish) invoked by a new `arbiter monday-refresh` CLI command, scheduled by a one-shot launchd plist. Macro findings are persisted to a `macro_findings` table; the existing daemon's cycle reads them via a new `_gather_a4_opinions()` so opinion injection stays in the engine (the single place that feeds fusion), exactly like `A3.news`.

**Tech Stack:** Python 3.12, Typer (CLI), SQLite (WAL), `anthropic` SDK 0.111 (already installed), Finnhub client (existing), structlog, pytest, launchd.

## Global Constraints

- Arbiter package is **nested**: source at `arbiter/` (within working dir `/Users/jonathanmorris/poly_bot/arbiter`), tests at `tests/`. All paths below are relative to that working dir.
- **Fail-closed everywhere**: no scan and no engine-feed call may raise out of the Monday refresh; catch, log a `structlog` warning, continue. Nothing here may abort/pause/block the trading daemon.
- **No `datetime.now()`** in library code — read time only via `engine.clock.now()` / a passed `as_of`. Passes `scripts/check_no_lookahead.sh`.
- **Hermetic tests**: no real network. Use `FakeLLM`, fake Finnhub source, and an in-memory/temp SQLite DB. Respects the conftest hermeticity guard — never patch global `httpx.post`.
- **Anthropic model:** default `claude-opus-4-8`; web search tool `{"type": "web_search_20260209", "name": "web_search"}`; `thinking={"type": "adaptive"}` (no `budget_tokens`); handle `stop_reason == "pause_turn"` with a `max_continuations=5` cap; do **not** also declare `code_execution`.
- **Reuse existing** `ANTHROPIC_API_KEY` (already in `.env`); never print it (add to `_SECRET_FIELDS`).
- Commit after each task with a `feat:`/`test:` message ending in the repo's `Co-Authored-By:` / `Claude-Session:` trailers.
- Run the full suite green before the final task: `pytest -q`.

---

### Task 1: Config additions (Anthropic key, refresh model, A4 knobs, redaction)

**Files:**
- Modify: `arbiter/config.py`
- Test: `tests/test_config_refresh_fields.py` (create)

**Interfaces:**
- Produces: `Config.anthropic_api_key: str`, `Config.refresh_model: str`, `Config.a4_min_stance: float`, `Config.a4_min_confidence: float`, `Config.a4_weight_multiplier: float`, `Config.a4_weight_cap: float`, `Config.a4_advisor_id: str`. `anthropic_api_key` is secret-redacted.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_refresh_fields.py
from arbiter.config import Config, load_config


def test_refresh_fields_default(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("REFRESH_MODEL", raising=False)
    cfg = load_config()
    assert cfg.anthropic_api_key == ""
    assert cfg.refresh_model == "claude-opus-4-8"
    assert cfg.a4_advisor_id == "A4.macro"
    assert 0.0 <= cfg.a4_weight_cap <= 1.0


def test_refresh_fields_from_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-XYZ")
    monkeypatch.setenv("REFRESH_MODEL", "claude-sonnet-4-6")
    cfg = load_config()
    assert cfg.anthropic_api_key == "sk-test-XYZ"
    assert cfg.refresh_model == "claude-sonnet-4-6"


def test_anthropic_key_redacted_in_repr(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-DONOTLEAK")
    cfg = load_config()
    assert "DONOTLEAK" not in repr(cfg)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_refresh_fields.py -v`
Expected: FAIL (`AttributeError: 'Config' object has no attribute 'anthropic_api_key'`).

- [ ] **Step 3: Add the fields**

In `arbiter/config.py`, add to the `Config` dataclass (near the `a3_*` fields, ~line 223):

```python
    # --- Monday Refresh / A4.macro -------------------------------------
    anthropic_api_key: str = ""
    refresh_model: str = "claude-opus-4-8"
    a4_min_stance: float = 0.25
    a4_min_confidence: float = 0.0
    a4_weight_multiplier: float = 2.0
    a4_weight_cap: float = 0.50
    a4_advisor_id: str = "A4.macro"
```

Add `"anthropic_api_key"` to `_SECRET_FIELDS` (~line 58):

```python
_SECRET_FIELDS = {"alpaca_api_key", "alpaca_secret_key", "kill_switch_url", "anthropic_api_key"}
```

In `load_config()` (where the `a3_*` env reads are, ~line 472), add:

```python
        anthropic_api_key=_env_str("ANTHROPIC_API_KEY", ""),
        refresh_model=_env_str("REFRESH_MODEL", "claude-opus-4-8"),
        a4_min_stance=_env_float("A4_MIN_STANCE", 0.25),
        a4_min_confidence=_env_float("A4_MIN_CONFIDENCE", 0.0),
        a4_weight_multiplier=_env_float("A4_WEIGHT_MULTIPLIER", 2.0),
        a4_weight_cap=_env_float("A4_WEIGHT_CAP", 0.50),
        a4_advisor_id=_env_str("A4_ADVISOR_ID", "A4.macro"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_refresh_fields.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add arbiter/config.py tests/test_config_refresh_fields.py
git commit -m "feat(config): anthropic key, refresh model, A4.macro knobs + redaction"
```

---

### Task 2: Refresh result types

**Files:**
- Create: `arbiter/refresh/__init__.py`
- Create: `arbiter/refresh/types.py`
- Test: `tests/refresh/test_types.py` (create; add `tests/refresh/__init__.py`)

**Interfaces:**
- Produces: `Severity` (Enum `LOW`/`MEDIUM`/`HIGH`), `PositionFinding`, `MacroFinding`, `StaleFlag`, `MacroResult`, `StaleSource`, `HealthResult`, `RefreshReport` (all frozen dataclasses). Field names below are consumed by every later task.

- [ ] **Step 1: Write the failing test**

```python
# tests/refresh/test_types.py
from datetime import datetime, timezone

from arbiter.refresh.types import (
    Severity, PositionFinding, MacroFinding, StaleFlag, MacroResult,
    StaleSource, HealthResult, RefreshReport,
)


def test_types_construct():
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    pf = PositionFinding(ticker="UBER", headlines=["x"], sentiment=-0.4,
                         severity=Severity.HIGH, available=True)
    mf = MacroFinding(summary="CPI Thu", severity=Severity.MEDIUM,
                      affected_tickers=["UBER"], sources=["reuters.com"])
    sf = StaleFlag(source="activist_filers", reason="Icahn wound down",
                   sources=["wsj.com"])
    macro = MacroResult(findings=[mf], stale_flags=[sf], available=True, note="")
    ss = StaleSource(source="fund_managers", reason="CIK 13F stale", confirmed=True)
    health = HealthResult(sources=[ss])
    report = RefreshReport(as_of=now, positions=[pf], macro=macro, health=health,
                           fed_tickers=["UBER"], reingested=["fund_managers"])
    assert report.positions[0].ticker == "UBER"
    assert macro.findings[0].affected_tickers == ["UBER"]
    assert health.confirmed_stale() == [ss]


def test_health_confirmed_stale_filters_unconfirmed():
    a = StaleSource(source="a", reason="r", confirmed=True)
    b = StaleSource(source="b", reason="r", confirmed=False)
    assert HealthResult(sources=[a, b]).confirmed_stale() == [a]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/refresh/test_types.py -v`
Expected: FAIL (`ModuleNotFoundError: arbiter.refresh`).

- [ ] **Step 3: Implement the types**

```python
# arbiter/refresh/__init__.py
"""Monday Refresh — weekly pre-market intelligence pass."""
```

```python
# arbiter/refresh/types.py
"""Frozen result types for the Monday Refresh scans."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class PositionFinding:
    ticker: str
    headlines: list[str]
    sentiment: float          # [-1, 1]; 0.0 when unavailable
    severity: Severity
    available: bool           # False => Finnhub unavailable for this ticker


@dataclass(frozen=True)
class MacroFinding:
    summary: str
    severity: Severity
    affected_tickers: list[str]
    sources: list[str]


@dataclass(frozen=True)
class StaleFlag:
    source: str               # e.g. "activist_filers"
    reason: str
    sources: list[str]


@dataclass(frozen=True)
class MacroResult:
    findings: list[MacroFinding]
    stale_flags: list[StaleFlag]
    available: bool           # False => Claude skipped/unavailable
    note: str                 # human-readable status when not available


@dataclass(frozen=True)
class StaleSource:
    source: str
    reason: str
    confirmed: bool           # deterministic confirmation (or matched news flag)


@dataclass(frozen=True)
class HealthResult:
    sources: list[StaleSource]

    def confirmed_stale(self) -> list[StaleSource]:
        return [s for s in self.sources if s.confirmed]


@dataclass(frozen=True)
class RefreshReport:
    as_of: datetime
    positions: list[PositionFinding]
    macro: MacroResult
    health: HealthResult
    fed_tickers: list[str] = field(default_factory=list)
    reingested: list[str] = field(default_factory=list)
```

Also create empty `tests/refresh/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/refresh/test_types.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add arbiter/refresh/__init__.py arbiter/refresh/types.py tests/refresh/__init__.py tests/refresh/test_types.py
git commit -m "feat(refresh): result types for the Monday refresh scans"
```

---

### Task 3: LLM seam (protocol + AnthropicLLM + FakeLLM)

**Files:**
- Create: `arbiter/refresh/llm.py`
- Test: `tests/refresh/test_llm.py` (create)

**Interfaces:**
- Produces: `LLM` Protocol with `create(*, model, max_tokens, thinking, tools, messages) -> Any`; `AnthropicLLM(api_key=None)` (lazy SDK import); `FakeLLM(canned_text: str)` returning an object whose `.content` is a list of blocks each with `.type`/`.text` and `.stop_reason == "end_turn"`, mirroring the SDK response shape. Consumed by Task 5.

- [ ] **Step 1: Write the failing test**

```python
# tests/refresh/test_llm.py
from arbiter.refresh.llm import LLM, FakeLLM


def test_fakellm_mirrors_sdk_shape():
    fake = FakeLLM("```json\n{\"market\": []}\n```")
    resp = fake.create(model="m", max_tokens=10, thinking={"type": "adaptive"},
                       tools=[], messages=[{"role": "user", "content": "hi"}])
    assert resp.stop_reason == "end_turn"
    text = "".join(b.text for b in resp.content if b.type == "text")
    assert "market" in text


def test_fakellm_satisfies_protocol():
    assert isinstance(FakeLLM("x"), LLM)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/refresh/test_llm.py -v`
Expected: FAIL (`ModuleNotFoundError: arbiter.refresh.llm`).

- [ ] **Step 3: Implement the seam** (mirrors `mirofish/llm.py`)

```python
# arbiter/refresh/llm.py
"""LLM seam for the Monday macro scan.

Mirrors mirofish/llm.py: a structural `LLM` Protocol, a real `AnthropicLLM`
wrapper that lazy-imports the SDK (so this module imports with no SDK/key), and
a `FakeLLM` whose response object mirrors the SDK shape for offline tests.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, Protocol


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/refresh/test_llm.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add arbiter/refresh/llm.py tests/refresh/test_llm.py
git commit -m "feat(refresh): LLM seam (Protocol + AnthropicLLM + FakeLLM)"
```

---

### Task 4: Position-news scan (Finnhub)

**Files:**
- Create: `arbiter/refresh/position_news.py`
- Test: `tests/refresh/test_position_news.py` (create)

**Interfaces:**
- Consumes: `arbiter.ingest.finnhub.client.FinnhubClient` (`get_company_news(ticker, from_date, to_date) -> list[dict]`, `get_news_sentiment(ticker) -> dict`); `PositionFinding`, `Severity`.
- Produces: `scan_position_news(tickers: list[str], as_of: datetime, client) -> list[PositionFinding]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/refresh/test_position_news.py
from datetime import datetime, timezone

from arbiter.refresh.position_news import scan_position_news
from arbiter.refresh.types import Severity


class _FakeClient:
    def __init__(self, news, sentiment, raises=False):
        self._news, self._sentiment, self._raises = news, sentiment, raises
    def get_company_news(self, ticker, from_date, to_date):
        if self._raises:
            raise RuntimeError("boom")
        return self._news
    def get_news_sentiment(self, ticker):
        return self._sentiment


def test_negative_sentiment_high_severity():
    c = _FakeClient(news=[{"headline": "DOJ probe"}],
                    sentiment={"sentiment_score": -0.6})
    [f] = scan_position_news(["UBER"], datetime(2026, 6, 29, tzinfo=timezone.utc), c)
    assert f.ticker == "UBER" and f.available is True
    assert f.severity == Severity.HIGH and f.headlines == ["DOJ probe"]


def test_client_error_is_unavailable_not_raised():
    c = _FakeClient(news=[], sentiment={}, raises=True)
    [f] = scan_position_news(["UBER"], datetime(2026, 6, 29, tzinfo=timezone.utc), c)
    assert f.available is False and f.severity == Severity.LOW
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/refresh/test_position_news.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# arbiter/refresh/position_news.py
"""Per-open-position news scan via the existing Finnhub client (fail-closed)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog

from arbiter.refresh.types import PositionFinding, Severity

log = structlog.get_logger(__name__)


def _severity(sentiment: float) -> Severity:
    mag = abs(sentiment)
    if mag >= 0.5:
        return Severity.HIGH
    if mag >= 0.2:
        return Severity.MEDIUM
    return Severity.LOW


def scan_position_news(tickers: list[str], as_of: datetime,
                       client: Any) -> list[PositionFinding]:
    out: list[PositionFinding] = []
    frm = (as_of - timedelta(days=7)).date().isoformat()
    to = as_of.date().isoformat()
    for ticker in tickers:
        try:
            articles = client.get_company_news(ticker, frm, to) or []
            sentiment = client.get_news_sentiment(ticker) or {}
            score = float(sentiment.get("sentiment_score", 0.0) or 0.0)
            headlines = [a.get("headline", "") for a in articles[:5] if a.get("headline")]
            out.append(PositionFinding(ticker=ticker, headlines=headlines,
                                       sentiment=score, severity=_severity(score),
                                       available=True))
        except Exception as exc:  # fail-closed per ticker
            log.warning("refresh.position_news.failed", ticker=ticker, error=str(exc))
            out.append(PositionFinding(ticker=ticker, headlines=[], sentiment=0.0,
                                       severity=Severity.LOW, available=False))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/refresh/test_position_news.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add arbiter/refresh/position_news.py tests/refresh/test_position_news.py
git commit -m "feat(refresh): per-position Finnhub news scan (fail-closed)"
```

---

### Task 5: Macro scan (Claude web search)

**Files:**
- Create: `arbiter/refresh/macro_scan.py`
- Test: `tests/refresh/test_macro_scan.py` (create)

**Interfaces:**
- Consumes: `LLM` (Task 3), `Config.refresh_model`, `Config.anthropic_api_key`; `MacroResult`, `MacroFinding`, `StaleFlag`, `Severity`.
- Produces: `scan_macro(tickers, as_of, config, *, llm=None) -> MacroResult` and `parse_macro_json(text) -> tuple[list[MacroFinding], list[StaleFlag]]`. Known source names for staleness: `KNOWN_SOURCES = {"fund_managers", "activist_filers", "watchlist", "sectors"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/refresh/test_macro_scan.py
from datetime import datetime, timezone

from arbiter.refresh.llm import FakeLLM
from arbiter.refresh.macro_scan import scan_macro, parse_macro_json
from arbiter.refresh.types import Severity


def _cfg(key="sk-x", model="claude-opus-4-8"):
    from types import SimpleNamespace
    return SimpleNamespace(anthropic_api_key=key, refresh_model=model)


CANNED = """Here is the analysis.
```json
{"market": [{"summary": "CPI print Thursday", "severity": "high",
             "affected_tickers": ["UBER"], "sources": ["reuters.com"]}],
 "stale_sources": [{"source": "activist_filers", "reason": "Icahn wound down",
                    "sources": ["wsj.com"]}]}
```
"""


def test_scan_parses_findings_and_flags():
    res = scan_macro(["UBER"], datetime(2026, 6, 29, tzinfo=timezone.utc),
                     _cfg(), llm=FakeLLM(CANNED))
    assert res.available is True
    assert res.findings[0].severity == Severity.HIGH
    assert res.findings[0].affected_tickers == ["UBER"]
    assert res.stale_flags[0].source == "activist_filers"


def test_no_key_is_unavailable():
    res = scan_macro(["UBER"], datetime(2026, 6, 29, tzinfo=timezone.utc),
                     _cfg(key=""), llm=None)
    assert res.available is False and res.findings == [] and res.stale_flags == []


def test_unparseable_is_unavailable_not_raised():
    res = scan_macro(["UBER"], datetime(2026, 6, 29, tzinfo=timezone.utc),
                     _cfg(), llm=FakeLLM("no json here"))
    assert res.available is False and res.findings == []


def test_parse_ignores_unknown_stale_source():
    findings, flags = parse_macro_json(
        '{"market": [], "stale_sources": [{"source": "nonsense", "reason": "x"}]}')
    assert flags == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/refresh/test_macro_scan.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# arbiter/refresh/macro_scan.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/refresh/test_macro_scan.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add arbiter/refresh/macro_scan.py tests/refresh/test_macro_scan.py
git commit -m "feat(refresh): macro web-search scan via Claude (fail-closed, opus-4-8)"
```

---

### Task 6: Source-health scan (deterministic staleness)

**Files:**
- Create: `arbiter/refresh/source_health.py`
- Test: `tests/refresh/test_source_health.py` (create)

**Interfaces:**
- Consumes: `arbiter.data.fund_managers.manager_ciks()`, an injected `now`, an injected `last_ingest_age_days(conn, source) -> int | None`; `StaleSource`, `HealthResult`, `StaleFlag`.
- Produces: `scan_source_health(conn, as_of, *, ingest_age_fn) -> HealthResult` and `merge_flags(health: HealthResult, flags: list[StaleFlag]) -> HealthResult` (adds confirmed `StaleSource` rows for news flags that match a known source, de-duped by `source`).

- [ ] **Step 1: Write the failing test**

```python
# tests/refresh/test_source_health.py
from datetime import datetime, timezone

from arbiter.refresh.source_health import scan_source_health, merge_flags
from arbiter.refresh.types import StaleFlag


def test_stale_when_ingest_age_exceeds_threshold():
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    # form13f ingested 200 days ago -> stale; congress 1 day ago -> fresh
    ages = {"form13f": 200, "congress": 1}
    res = scan_source_health(conn=None, as_of=now,
                             ingest_age_fn=lambda c, s: ages.get(s))
    stale = {s.source for s in res.confirmed_stale()}
    assert "form13f" in stale and "congress" not in stale


def test_merge_flags_adds_matching_news_flag():
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    res = scan_source_health(conn=None, as_of=now, ingest_age_fn=lambda c, s: 1)
    merged = merge_flags(res, [StaleFlag(source="activist_filers",
                                         reason="wound down", sources=[])])
    assert any(s.source == "activist_filers" and s.confirmed
               for s in merged.confirmed_stale())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/refresh/test_source_health.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# arbiter/refresh/source_health.py
"""Deterministic data-source staleness checks (fail-closed)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Callable

import structlog

from arbiter.refresh.types import HealthResult, StaleFlag, StaleSource

log = structlog.get_logger(__name__)

# Per-source max ingest age before we call it stale (calendar days).
_MAX_AGE_DAYS: dict[str, int] = {
    "form4": 14, "form13d": 30, "form13f": 100, "congress": 14,
}


def default_ingest_age_fn(conn: sqlite3.Connection, source: str) -> int | None:
    """Days since the newest row for `source` in the filings table, or None."""
    try:
        row = conn.execute(
            "SELECT MAX(ingested_at) FROM filings WHERE source = ?", (source,)
        ).fetchone()
        if not row or not row[0]:
            return None
        newest = datetime.fromisoformat(row[0])
        return (datetime.now(tz=newest.tzinfo) - newest).days
    except Exception:  # table/column shape differences -> unknown, never crash
        return None


def scan_source_health(conn: Any, as_of: datetime, *,
                       ingest_age_fn: Callable[[Any, str], int | None] | None = None
                       ) -> HealthResult:
    age_fn = ingest_age_fn or default_ingest_age_fn
    sources: list[StaleSource] = []
    for src, max_age in _MAX_AGE_DAYS.items():
        try:
            age = age_fn(conn, src)
        except Exception as exc:
            log.warning("refresh.health.age_failed", source=src, error=str(exc))
            age = None
        if age is None:
            sources.append(StaleSource(source=src, reason="ingest age unknown",
                                       confirmed=False))
        elif age > max_age:
            sources.append(StaleSource(source=src,
                                       reason=f"last ingest {age}d ago (>{max_age}d)",
                                       confirmed=True))
    return HealthResult(sources=sources)


def merge_flags(health: HealthResult, flags: list[StaleFlag]) -> HealthResult:
    seen = {s.source for s in health.sources}
    extra = [StaleSource(source=f.source, reason=f"news: {f.reason}", confirmed=True)
             for f in flags if f.source not in seen]
    return HealthResult(sources=[*health.sources, *extra])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/refresh/test_source_health.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add arbiter/refresh/source_health.py tests/refresh/test_source_health.py
git commit -m "feat(refresh): deterministic source-health staleness scan"
```

---

### Task 7: `macro_findings` table + persistence

**Files:**
- Create: `arbiter/db/migrations/0NN_macro_findings.sql` (use the next free migration number — list `arbiter/db/migrations/` and increment)
- Create: `arbiter/refresh/findings_store.py`
- Test: `tests/refresh/test_findings_store.py` (create)

**Interfaces:**
- Consumes: `MacroFinding`, `Severity`.
- Produces: `persist_findings(conn, findings, as_of, *, expiry_days=7) -> int` (rows written); `read_active_findings(conn, as_of) -> list[MacroFinding]` (unexpired only).

- [ ] **Step 1: Write the failing test**

```python
# tests/refresh/test_findings_store.py
import sqlite3
from datetime import datetime, timedelta, timezone

from arbiter.refresh.findings_store import (
    create_table, persist_findings, read_active_findings,
)
from arbiter.refresh.types import MacroFinding, Severity


def _conn():
    c = sqlite3.connect(":memory:")
    create_table(c)
    return c


def test_persist_and_read_active():
    c = _conn()
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    f = MacroFinding(summary="CPI", severity=Severity.HIGH,
                     affected_tickers=["UBER", "LYFT"], sources=["reuters.com"])
    assert persist_findings(c, [f], now) == 1
    active = read_active_findings(c, now)
    assert len(active) == 1
    assert active[0].affected_tickers == ["UBER", "LYFT"]
    assert active[0].severity == Severity.HIGH


def test_expired_findings_excluded():
    c = _conn()
    old = datetime(2026, 6, 1, tzinfo=timezone.utc)
    persist_findings(c, [MacroFinding("old", Severity.LOW, ["X"], [])], old)
    later = old + timedelta(days=30)
    assert read_active_findings(c, later) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/refresh/test_findings_store.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement migration + store**

```sql
-- arbiter/db/migrations/0NN_macro_findings.sql
CREATE TABLE IF NOT EXISTS macro_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    severity TEXT NOT NULL,
    affected_tickers TEXT NOT NULL,  -- comma-separated
    sources TEXT NOT NULL            -- comma-separated
);
CREATE INDEX IF NOT EXISTS idx_macro_findings_expires ON macro_findings (expires_at);
```

```python
# arbiter/refresh/findings_store.py
"""Persistence for macro findings consumed by the engine's A4.macro gather."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from arbiter.refresh.types import MacroFinding, Severity

_DDL = """
CREATE TABLE IF NOT EXISTS macro_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    severity TEXT NOT NULL,
    affected_tickers TEXT NOT NULL,
    sources TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_macro_findings_expires ON macro_findings (expires_at);
"""


def create_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()


def persist_findings(conn: sqlite3.Connection, findings: list[MacroFinding],
                     as_of: datetime, *, expiry_days: int = 7) -> int:
    expires = (as_of + timedelta(days=expiry_days)).isoformat()
    n = 0
    for f in findings:
        if not f.affected_tickers:
            continue
        conn.execute(
            "INSERT INTO macro_findings "
            "(as_of, expires_at, summary, severity, affected_tickers, sources) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (as_of.isoformat(), expires, f.summary, f.severity.value,
             ",".join(f.affected_tickers), ",".join(f.sources)),
        )
        n += 1
    conn.commit()
    return n


def read_active_findings(conn: sqlite3.Connection,
                         as_of: datetime) -> list[MacroFinding]:
    rows = conn.execute(
        "SELECT summary, severity, affected_tickers, sources "
        "FROM macro_findings WHERE expires_at > ?", (as_of.isoformat(),)
    ).fetchall()
    out: list[MacroFinding] = []
    for summary, sev, tickers, sources in rows:
        out.append(MacroFinding(
            summary=summary, severity=Severity(sev),
            affected_tickers=[t for t in tickers.split(",") if t],
            sources=[s for s in sources.split(",") if s]))
    return out
```

> The migration runner auto-applies `arbiter/db/migrations/*.sql` on connect; `create_table` is for tests using a bare `:memory:` connection. Confirm by reading the migration runner before numbering the file.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/refresh/test_findings_store.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add arbiter/db/migrations/0NN_macro_findings.sql arbiter/refresh/findings_store.py tests/refresh/test_findings_store.py
git commit -m "feat(refresh): macro_findings table + persistence store"
```

---

### Task 8: `A4.macro` advisor — engine gather + cycle wiring

**Files:**
- Create: `arbiter/adapters/a4/__init__.py`, `arbiter/adapters/a4/pipeline.py`
- Modify: `arbiter/engine/_engine.py` (add `_gather_a4_opinions`, call + idea/opinion append next to A3)
- Test: `tests/refresh/test_a4_macro.py` (create)

**Interfaces:**
- Consumes: `read_active_findings` (Task 7); `Opinion`, `default_registry`, `validate_opinion`, `ConfidenceSource`; `MacroFinding`, `Severity`.
- Produces: `arbiter.adapters.a4.gather_a4_opinions(conn, clock, config) -> list[Opinion]`, advisor id `A4.macro` registered at import. Engine spawns a SHORT-horizon idea per held-or-new affected ticker and appends the opinion (mirrors A3).

- [ ] **Step 1: Write the failing test**

```python
# tests/refresh/test_a4_macro.py
import sqlite3
from datetime import datetime, timezone

from arbiter.adapters.a4 import gather_a4_opinions, ADVISOR_ID
from arbiter.contract.opinion import default_registry, validate_opinion
from arbiter.refresh.findings_store import create_table, persist_findings
from arbiter.refresh.types import MacroFinding, Severity
from arbiter.data.clock import BacktestClock


class _LiveClock:
    def __init__(self, now): self._now = now
    def now(self): return self._now


def _cfg():
    from types import SimpleNamespace
    return SimpleNamespace(a4_advisor_id="A4.macro", a4_min_confidence=0.0)


def test_advisor_registered():
    assert ADVISOR_ID in default_registry.all_ids()  # or membership check the registry exposes


def test_findings_become_valid_opinions():
    c = sqlite3.connect(":memory:")
    create_table(c)
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    persist_findings(c, [MacroFinding("CPI risk", Severity.HIGH, ["UBER"],
                                      ["reuters.com"])], now)
    ops = gather_a4_opinions(c, _LiveClock(now), _cfg())
    assert len(ops) == 1
    op = ops[0]
    assert op.advisor_id == "A4.macro" and op.ticker == "UBER"
    assert -1.0 <= op.stance_score <= 1.0 and op.horizon_days == 7
    validate_opinion(op)  # must not raise


def test_inert_under_backtest_clock():
    c = sqlite3.connect(":memory:")
    create_table(c)
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    persist_findings(c, [MacroFinding("x", Severity.HIGH, ["UBER"], [])], now)
    assert gather_a4_opinions(c, BacktestClock(now), _cfg()) == []
```

> Adjust `test_advisor_registered` to whatever membership accessor `default_registry` exposes (read `arbiter/contract/opinion.py`); if none, assert that calling `default_registry.register("A4.macro")` twice is idempotent instead.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/refresh/test_a4_macro.py -v`
Expected: FAIL (`ModuleNotFoundError: arbiter.adapters.a4`).

- [ ] **Step 3: Implement the advisor**

```python
# arbiter/adapters/a4/__init__.py
from .pipeline import ADVISOR_ID, gather_a4_opinions

__all__ = ["ADVISOR_ID", "gather_a4_opinions"]
```

```python
# arbiter/adapters/a4/pipeline.py
"""A4.macro advisor — turns persisted macro findings into probationary opinions.

Mirrors A3.news: registered probationary at import (EQUAL_FLOOR until graduated),
SHORT horizon (7d), fail-closed, network-/look-ahead-gated under BacktestClock.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime

import structlog

from arbiter.contract.opinion import Opinion, default_registry, validate_opinion
from arbiter.data.clock import BacktestClock, Clock
from arbiter.db.helpers import generate_ulid
from arbiter.refresh.findings_store import read_active_findings
from arbiter.refresh.types import MacroFinding, Severity
from arbiter.types import ConfidenceSource

log = structlog.get_logger(__name__)

ADVISOR_ID = "A4.macro"
_HORIZON_DAYS = 7  # SHORT bucket
_SEV_STANCE = {Severity.HIGH: 0.5, Severity.MEDIUM: 0.3, Severity.LOW: 0.15}
_SEV_CONF = {Severity.HIGH: 0.45, Severity.MEDIUM: 0.30, Severity.LOW: 0.15}

default_registry.register(ADVISOR_ID)


def _stance(f: MacroFinding) -> float:
    # Macro risk reads bearish on the broad market by default; magnitude by severity.
    return -_SEV_STANCE.get(f.severity, 0.15)


def gather_a4_opinions(conn: sqlite3.Connection, clock: Clock,
                       config: object) -> list[Opinion]:
    try:
        if isinstance(clock, BacktestClock):
            return []
        as_of: datetime = clock.now()
        advisor_id = getattr(config, "a4_advisor_id", ADVISOR_ID)
        min_conf = float(getattr(config, "a4_min_confidence", 0.0))
        run_group = generate_ulid()
        out: list[Opinion] = []
        seen: set[str] = set()
        for f in read_active_findings(conn, as_of):
            conf = _SEV_CONF.get(f.severity, 0.15)
            if conf < min_conf:
                continue
            for ticker in f.affected_tickers:
                key = f"{ticker}:{f.summary}"
                if key in seen:
                    continue
                seen.add(key)
                fp = hashlib.sha256(key.encode()).hexdigest()
                op = Opinion(
                    advisor_id=advisor_id, ticker=ticker, stance_score=_stance(f),
                    confidence=conf, confidence_source=ConfidenceSource.MODELED,
                    horizon_days=_HORIZON_DAYS, as_of=as_of,
                    rationale=f"A4.macro {ticker}: {f.summary}"[:500],
                    source_fingerprint=fp, run_group_id=run_group)
                validate_opinion(op)
                out.append(op)
        log.info("a4.macro.complete", opinion_count=len(out))
        return out
    except Exception as exc:  # fail-closed
        log.warning("a4.macro.unexpected", error=str(exc))
        return []
```

- [ ] **Step 4: Wire into the engine** (`arbiter/engine/_engine.py`)

Add a method mirroring `_gather_a3_opinions` (near it, ~line 361):

```python
    def _gather_a4_opinions(self) -> list[Opinion]:
        """Gather A4.macro opinions from persisted findings (fail-closed)."""
        try:
            from arbiter.adapters.a4 import gather_a4_opinions  # noqa: PLC0415
            return gather_a4_opinions(self.conn, self.clock, self.config)
        except Exception as exc:
            log.warning("engine.a4.gather_failed", error=str(exc))
            return []
```

In `run_cycle`, right after `a3_opinions = self._gather_a3_opinions()` (~line 501):

```python
        a4_opinions = self._gather_a4_opinions()
```

Update the early-return guard (~line 507) to include A4:

```python
        if not signals and not a3_opinions and not a4_opinions:
```

After the A3 idea/opinion append loop (~line 544-556), add the identical loop for A4 (macro opinions spawn SHORT-horizon ideas and append, governed by the learning loop):

```python
        for op in a4_opinions:
            if op.ticker in held_tickers:
                # Still record the opinion against a held ticker's existing idea
                # path is out of scope; skip new-idea spawn to avoid double-buying.
                continue
            if op.ticker not in seen_tickers:
                seen_tickers.add(op.ticker)
                ideas.append(make_idea(
                    ticker=op.ticker,
                    thesis=f"macro on {op.ticker}",
                    horizon_days=op.horizon_days,
                    as_of=now,
                ))
            valid_opinions.append(op)
            live_advisor_count += 1
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/refresh/test_a4_macro.py -v && pytest tests/ -k engine -q`
Expected: PASS (3 new tests; engine suite still green).

- [ ] **Step 6: Commit**

```bash
git add arbiter/adapters/a4/ arbiter/engine/_engine.py tests/refresh/test_a4_macro.py
git commit -m "feat(engine): A4.macro probationary advisor fed by persisted findings"
```

---

### Task 9: `Alerting.notify()` info-tier phone push

**Files:**
- Modify: `arbiter/safety/alerting.py`
- Test: `tests/test_alerting_notify.py` (create)

**Interfaces:**
- Produces: `Alerting.notify(self, title: str, body: str, *, as_of: datetime) -> None` — writes an `alert.info` audit row and POSTs the webhook (reusing `_post_webhook` with `tier="info"`). Never raises on delivery failure.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_alerting_notify.py
from datetime import datetime, timezone

from arbiter.safety.alerting import Alerting


def test_notify_posts_webhook(monkeypatch, tmp_path):
    posted = {}

    cfg = type("C", (), {"alert_webhook_url": "https://example/ntfy"})()
    a = Alerting(config=cfg, audit_path=str(tmp_path / "audit.jsonl"))

    def fake_post(*, tier, message, ctx, ts):
        posted.update(tier=tier, message=message)
    monkeypatch.setattr(a, "_post_webhook", fake_post)

    a.notify("Monday Refresh", "7 positions; CPI Thursday",
             as_of=datetime(2026, 6, 29, tzinfo=timezone.utc))
    assert posted["tier"] == "info"
    assert "Monday Refresh" in posted["message"]
```

> Adjust the `Alerting(...)` constructor call to its real signature (read `arbiter/safety/alerting.py` — it takes `config` + `audit_path` or similar).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_alerting_notify.py -v`
Expected: FAIL (`AttributeError: 'Alerting' object has no attribute 'notify'`).

- [ ] **Step 3: Implement** — add to the `Alerting` class (after `alert`):

```python
    def notify(self, title: str, body: str, *, as_of: datetime) -> None:
        """Info-tier push: audit + always-POST the webhook (fire-and-forget)."""
        ts = as_of.isoformat()
        message = f"{title}\n{body}"
        _audit_write(
            "alert.info",
            {"message": message, "tier": "info", "ctx": {"title": title}},
            ts=ts,
            audit_path=self.audit_path,
        )
        self._post_webhook(tier="info", message=message,
                           ctx={"title": title}, ts=ts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_alerting_notify.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add arbiter/safety/alerting.py tests/test_alerting_notify.py
git commit -m "feat(alerting): info-tier notify() for non-critical phone pushes"
```

---

### Task 10: Digest builder + push

**Files:**
- Create: `arbiter/refresh/digest.py`
- Test: `tests/refresh/test_digest.py` (create)

**Interfaces:**
- Consumes: `RefreshReport`, `Alerting.notify`.
- Produces: `build_digest(report: RefreshReport) -> str` (markdown); `push_digest(report, *, alerting) -> None` (one-line title + short body via `alerting.notify`).

- [ ] **Step 1: Write the failing test**

```python
# tests/refresh/test_digest.py
from datetime import datetime, timezone

from arbiter.refresh.digest import build_digest, push_digest
from arbiter.refresh.types import (
    RefreshReport, PositionFinding, MacroResult, MacroFinding,
    HealthResult, StaleSource, Severity,
)


def _report():
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    return RefreshReport(
        as_of=now,
        positions=[PositionFinding("UBER", ["DOJ probe"], -0.6, Severity.HIGH, True)],
        macro=MacroResult([MacroFinding("CPI Thu", Severity.HIGH, ["UBER"],
                                        ["reuters.com"])], [], True, ""),
        health=HealthResult([StaleSource("form13f", "200d ago", True)]),
        fed_tickers=["UBER"], reingested=["form13f"])


def test_build_digest_has_sections():
    md = build_digest(_report())
    assert "MONDAY REFRESH" in md.upper()
    assert "UBER" in md and "CPI Thu" in md and "form13f" in md


def test_push_digest_calls_notify():
    calls = {}
    class _A:
        def notify(self, title, body, *, as_of):
            calls.update(title=title, body=body)
    push_digest(_report(), alerting=_A())
    assert "Monday Refresh" in calls["title"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/refresh/test_digest.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# arbiter/refresh/digest.py
"""Render + push the Monday Refresh digest."""
from __future__ import annotations

from typing import Any

from arbiter.refresh.types import RefreshReport


def build_digest(report: RefreshReport) -> str:
    d = report.as_of.date().isoformat()
    lines = [f"# 📋 MONDAY REFRESH — {d}", ""]

    lines.append("## MARKETS")
    if not report.macro.available:
        lines.append(f"- macro scan {report.macro.note}")
    elif not report.macro.findings:
        lines.append("- no market-moving items flagged")
    else:
        for f in report.macro.findings:
            tick = f", affects {', '.join(f.affected_tickers)}" if f.affected_tickers else ""
            lines.append(f"- [{f.severity.value}] {f.summary}{tick}")
    lines.append("")

    lines.append(f"## OPEN TRADES ({len(report.positions)})")
    for p in report.positions:
        if not p.available:
            lines.append(f"- {p.ticker}: news unavailable")
        elif p.headlines:
            mark = "⚠" if p.severity == p.severity.HIGH else "•"
            lines.append(f"- {mark} {p.ticker} ({p.sentiment:+.2f}): {p.headlines[0]}")
        else:
            lines.append(f"- ✓ {p.ticker}: nominal")
    lines.append("")

    lines.append("## DATA SOURCES")
    stale = report.health.confirmed_stale()
    if not stale:
        lines.append("- all sources nominal")
    for s in stale:
        lines.append(f"- ⚠ {s.source}: {s.reason}")
    lines.append("")

    lines.append("## ACTIONS")
    lines.append(f"- fed engine (A4.macro): {', '.join(report.fed_tickers) or 'none'}")
    lines.append(f"- re-ingested: {', '.join(report.reingested) or 'none'}")
    return "\n".join(lines)


def _headline(report: RefreshReport) -> str:
    n_macro = len(report.macro.findings)
    n_stale = len(report.health.confirmed_stale())
    return (f"{len(report.positions)} positions · {n_macro} macro item(s) · "
            f"{n_stale} stale source(s)")


def push_digest(report: RefreshReport, *, alerting: Any) -> None:
    alerting.notify("Monday Refresh", _headline(report), as_of=report.as_of)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/refresh/test_digest.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add arbiter/refresh/digest.py tests/refresh/test_digest.py
git commit -m "feat(refresh): digest markdown builder + phone push"
```

---

### Task 11: Orchestrator

**Files:**
- Create: `arbiter/refresh/orchestrator.py`
- Test: `tests/refresh/test_orchestrator.py` (create)

**Interfaces:**
- Consumes: every scan above, `persist_findings`, `merge_flags`, `run_ingest`, `build_digest`, `push_digest`, `Alerting`.
- Produces: `run_monday_refresh(engine, *, llm=None, finnhub=None, ingest_fn=None, alerting=None) -> RefreshReport`. Saves the markdown to `data/monday-refresh-YYYY-MM-DD.md`.

- [ ] **Step 1: Write the failing test**

```python
# tests/refresh/test_orchestrator.py
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

from arbiter.refresh.orchestrator import run_monday_refresh
from arbiter.refresh.findings_store import create_table
from arbiter.refresh.llm import FakeLLM


class _Exec:
    def get_positions(self): return {"UBER": object()}


class _Clock:
    def now(self): return datetime(2026, 6, 29, tzinfo=timezone.utc)


class _FakeFinnhub:
    def get_company_news(self, t, frm, to): return [{"headline": "h"}]
    def get_news_sentiment(self, t): return {"sentiment_score": -0.6}


CANNED = ('```json\n{"market": [{"summary": "CPI", "severity": "high", '
          '"affected_tickers": ["UBER"], "sources": []}], "stale_sources": []}\n```')


def test_orchestrator_runs_and_feeds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(":memory:")
    create_table(conn)
    engine = SimpleNamespace(
        conn=conn, clock=_Clock(), executor=_Exec(),
        config=SimpleNamespace(anthropic_api_key="k", refresh_model="claude-opus-4-8",
                               a4_advisor_id="A4.macro", a4_min_confidence=0.0,
                               edgar_user_agent="", audit_path=str(tmp_path/"a.jsonl")))
    ingested = []
    report = run_monday_refresh(
        engine, llm=FakeLLM(CANNED), finnhub=_FakeFinnhub(),
        ingest_fn=lambda **kw: ingested.append(kw.get("sources")),
        alerting=SimpleNamespace(notify=lambda *a, **k: None))
    assert report.positions[0].ticker == "UBER"
    assert "UBER" in report.fed_tickers
    assert (tmp_path / "data" / "monday-refresh-2026-06-29.md").exists()


def test_one_scan_failure_does_not_abort(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(":memory:")
    create_table(conn)
    class _BadFinnhub:
        def get_company_news(self, *a): raise RuntimeError("x")
        def get_news_sentiment(self, *a): raise RuntimeError("x")
    engine = SimpleNamespace(
        conn=conn, clock=_Clock(), executor=_Exec(),
        config=SimpleNamespace(anthropic_api_key="", refresh_model="claude-opus-4-8",
                               a4_advisor_id="A4.macro", a4_min_confidence=0.0,
                               edgar_user_agent="", audit_path=str(tmp_path/"a.jsonl")))
    report = run_monday_refresh(
        engine, llm=None, finnhub=_BadFinnhub(),
        ingest_fn=lambda **kw: None,
        alerting=SimpleNamespace(notify=lambda *a, **k: None))
    assert report.positions[0].available is False   # finnhub failed, still reported
    assert report.macro.available is False           # no key, skipped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/refresh/test_orchestrator.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# arbiter/refresh/orchestrator.py
"""Monday Refresh orchestrator — runs scans, feeds engine, pushes digest."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import structlog

from arbiter.refresh.digest import build_digest, push_digest
from arbiter.refresh.findings_store import persist_findings
from arbiter.refresh.macro_scan import scan_macro
from arbiter.refresh.position_news import scan_position_news
from arbiter.refresh.source_health import merge_flags, scan_source_health
from arbiter.refresh.types import RefreshReport

log = structlog.get_logger(__name__)


def _safe(label: str, fn: Callable[[], Any], default: Any) -> Any:
    try:
        return fn()
    except Exception as exc:  # belt-and-suspenders; scans are already fail-closed
        log.warning("refresh.step_failed", step=label, error=str(exc))
        return default


def run_monday_refresh(engine: Any, *, llm: Any = None, finnhub: Any = None,
                       ingest_fn: Callable[..., Any] | None = None,
                       alerting: Any = None) -> RefreshReport:
    as_of = engine.clock.now()
    tickers = sorted(engine.executor.get_positions().keys())

    if finnhub is None:
        from arbiter.ingest.finnhub.client import FinnhubClient  # noqa: PLC0415
        key = getattr(engine.config, "finnhub_api_key", "") or ""
        finnhub = FinnhubClient(key) if key else None

    positions = _safe("position_news",
                      lambda: scan_position_news(tickers, as_of, finnhub) if finnhub else [],
                      [])
    macro = _safe("macro", lambda: scan_macro(tickers, as_of, engine.config, llm=llm),
                  scan_macro([], as_of, engine.config, llm=None))
    health = _safe("health", lambda: scan_source_health(engine.conn, as_of), None)
    if health is None:
        from arbiter.refresh.types import HealthResult  # noqa: PLC0415
        health = HealthResult(sources=[])
    health = merge_flags(health, macro.stale_flags)

    # --- feed engine: persist macro findings (engine's A4 gather reads them) ---
    fed: list[str] = []
    def _persist() -> None:
        n = persist_findings(engine.conn, macro.findings, as_of)
        for f in macro.findings:
            fed.extend(f.affected_tickers)
        log.info("refresh.findings_persisted", rows=n)
    _safe("persist_findings", _persist, None)

    # --- feed engine: targeted re-ingest of confirmed-stale sources ---
    reingested: list[str] = []
    if ingest_fn is None:
        from arbiter.ingest import run_ingest  # noqa: PLC0415
        ingest_fn = lambda **kw: run_ingest(engine.config, conn=engine.conn,
                                            clock=lambda: as_of.isoformat(), **kw)
    for src in health.confirmed_stale():
        if src.source in {"form4", "form13d", "form13f", "congress"}:
            ok = _safe(f"reingest_{src.source}",
                       lambda s=src.source: ingest_fn(sources=[s]) or True, None)
            if ok:
                reingested.append(src.source)

    report = RefreshReport(as_of=as_of, positions=positions, macro=macro,
                           health=health, fed_tickers=sorted(set(fed)),
                           reingested=reingested)

    # --- digest: save + push (always) ---
    md = build_digest(report)
    out = Path("data") / f"monday-refresh-{as_of.date().isoformat()}.md"
    _safe("save_digest", lambda: out.write_text(md, encoding="utf-8"), None)
    if alerting is None:
        from arbiter.safety.alerting import Alerting  # noqa: PLC0415
        alerting = Alerting(config=engine.config,
                            audit_path=getattr(engine.config, "audit_path", "data/audit.jsonl"))
    _safe("push_digest", lambda: push_digest(report, alerting=alerting), None)
    return report
```

> Confirm the `Alerting(...)` constructor signature against `arbiter/safety/alerting.py` and the `run_ingest` import path before running.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/refresh/test_orchestrator.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add arbiter/refresh/orchestrator.py tests/refresh/test_orchestrator.py
git commit -m "feat(refresh): orchestrator wiring scans + engine-feed + digest"
```

---

### Task 12: CLI `monday-refresh` command

**Files:**
- Modify: `arbiter/cli.py`
- Test: `tests/test_cli_monday_refresh.py` (create)

**Interfaces:**
- Consumes: `build_engine`, `run_monday_refresh`.
- Produces: `arbiter monday-refresh` Typer command that builds the engine and runs the refresh, echoing a short summary.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_monday_refresh.py
from typer.testing import CliRunner

from arbiter.cli import app


def test_monday_refresh_command_registered():
    res = CliRunner().invoke(app, ["monday-refresh", "--help"])
    assert res.exit_code == 0
    assert "Monday" in res.output or "refresh" in res.output.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli_monday_refresh.py -v`
Expected: FAIL (no such command).

- [ ] **Step 3: Implement** — add to `arbiter/cli.py` (after the `daemon` command):

```python
@app.command("monday-refresh")
def monday_refresh() -> None:
    """Run the Monday pre-market intelligence pass (scan + digest + feed engine)."""
    from arbiter.engine import build_engine  # noqa: PLC0415
    from arbiter.refresh.orchestrator import run_monday_refresh  # noqa: PLC0415

    engine = build_engine()
    report = run_monday_refresh(engine)
    typer.echo("Monday refresh complete.")
    typer.echo(f"  positions scanned : {len(report.positions)}")
    typer.echo(f"  macro findings    : {len(report.macro.findings)} "
               f"(available={report.macro.available})")
    typer.echo(f"  stale sources     : {len(report.health.confirmed_stale())}")
    typer.echo(f"  fed (A4.macro)    : {', '.join(report.fed_tickers) or 'none'}")
    typer.echo(f"  re-ingested       : {', '.join(report.reingested) or 'none'}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli_monday_refresh.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add arbiter/cli.py tests/test_cli_monday_refresh.py
git commit -m "feat(cli): arbiter monday-refresh command"
```

---

### Task 13: launchd plist + scheduler install

**Files:**
- Create: `deploy/com.arbiter.monday.plist`
- Modify: `scripts/schedule.sh` (add an `install-monday` action mirroring `install-daemon`/`install-daily`)
- Test: `tests/test_monday_plist.py` (create)

**Interfaces:**
- Produces: a valid plist scheduling `arbiter monday-refresh` at 08:00 Monday; `scripts/schedule.sh install-monday` loads it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_monday_plist.py
import plistlib
from pathlib import Path


def test_monday_plist_valid_and_scheduled():
    p = Path("deploy/com.arbiter.monday.plist")
    data = plistlib.loads(p.read_bytes())
    assert data["Label"] == "com.arbiter.monday"
    assert data["RunAtLoad"] is False and data["KeepAlive"] is False
    cal = data["StartCalendarInterval"]
    assert cal["Weekday"] == 1 and cal["Hour"] == 8 and cal["Minute"] == 0
    assert data["ProgramArguments"][-1] == "monday-refresh"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_monday_plist.py -v`
Expected: FAIL (file missing).

- [ ] **Step 3: Create the plist** (mirror `deploy/com.arbiter.daily.plist`; use absolute paths)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.arbiter.monday</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/jonathanmorris/poly_bot/arbiter/.venv/bin/python</string>
        <string>-m</string>
        <string>arbiter.cli</string>
        <string>monday-refresh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/jonathanmorris/poly_bot/arbiter</string>
    <key>StandardOutPath</key>
    <string>/Users/jonathanmorris/poly_bot/arbiter/data/arbiter-monday.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/jonathanmorris/poly_bot/arbiter/data/arbiter-monday.stderr.log</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key><integer>1</integer>
        <key>Hour</key><integer>8</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
```

- [ ] **Step 4: Add the installer action** to `scripts/schedule.sh` (mirror the existing `install-daemon`/`install-daily` case — `mkdir -p data`, `cp`/`launchctl bootout` then `bootstrap`/`load` the plist). Read the existing script and follow its exact idiom.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_monday_plist.py -v`
Expected: PASS.

- [ ] **Step 6: Full suite + commit**

```bash
pytest -q
git add deploy/com.arbiter.monday.plist scripts/schedule.sh tests/test_monday_plist.py
git commit -m "feat(deploy): launchd plist + installer for 08:00 Monday refresh"
```

---

## Self-Review

**Spec coverage:**
- §1/§4.1 orchestration → Task 11. §4.2 position news → Task 4. §4.3 macro scan → Task 5. §4.4 source health → Task 6. §4.5 digest + push → Tasks 9, 10. §4.6 A4.macro advisor → Tasks 7 (findings table), 8 (advisor + engine wiring). §5 scheduling → Task 13. §6 config → Task 1. §7 failure model → covered by fail-closed wrappers in Tasks 4–6, 8, 11. §8 testing → every task is TDD with hermetic fakes. Decision #6 (reuse key, AnthropicLLM/FakeLLM) → Tasks 1, 3.
- LLM seam (`refresh/llm.py`) and `types.py` are prerequisites pulled out as Tasks 2–3. Covered.

**Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N". Three explicit *verification* notes (registry membership accessor in Task 8; `Alerting(...)` constructor in Tasks 9/11; migration runner numbering in Task 7) tell the implementer to confirm a real signature before running — these are grounding checks, not placeholders, because the surrounding code is complete.

**Type consistency:** `MacroFinding`/`PositionFinding`/`StaleSource`/`MacroResult`/`HealthResult`/`RefreshReport` field names defined in Task 2 are used verbatim in Tasks 5–11. `Opinion` constructed in Task 8 uses the real frozen-dataclass fields (`advisor_id, ticker, stance_score, confidence, confidence_source, horizon_days, as_of, rationale, source_fingerprint, run_group_id`) with `ConfidenceSource.MODELED`. `scan_*` function names match between definition and orchestrator call sites. `read_active_findings`/`persist_findings`/`create_table` consistent across Tasks 7, 8, 11.
