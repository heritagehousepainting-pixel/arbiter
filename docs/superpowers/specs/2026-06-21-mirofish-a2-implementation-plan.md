# MiroFish A2 "Brain" — Implementation Plan (FROZEN CONTRACTS)

**Date:** 2026-06-21
**Status:** PLAN (freezes interfaces for the 3-agent parallel build).
**Reads from (authoritative):**
- Design spec `docs/superpowers/specs/2026-06-21-mirofish-a2-brain-design.md`
- Research `docs/specs/research/2026-06-21-simfin-fundamentals-feasibility.md`
- Research `docs/specs/research/2026-06-21-claude-tooluse-and-alpaca-client.md`
- Client contract `arbiter/arbiter/adapters/mirofish/{adapter.py,http_client.py,egress.py}`

This document FREEZES every shared type, every tool/LLM call, every function signature at a
seam, the cache key, the 3 disjoint build lanes, the foundation step, the per-module offline test
plan, and the PIT/isolation invariants. A build agent owning one lane can implement it with **zero
further design decisions**.

> **Isolation rule (non-negotiable, repeated everywhere):** nothing under `mirofish/` may
> `import arbiter` (or any `arbiter.*` submodule). The service is reached by the arbiter only over
> loopback HTTP. The types below are **vendored**, deliberately independent copies — they are NOT
> imported from arbiter even where the fields coincide.

---

## 0. Contract reconciliation with the arbiter client (read before coding)

The arbiter client (`adapter.py`) sends and expects, **byte-for-byte**:

**Request body** (`http_client.MirofishHTTPClient.analyze` builds it):
```json
{ "ticker": "AAPL", "as_of": "<ISO-8601 UTC>", "idea_fingerprint": "<sha256 hex>" }
```

**Response body** the client parses (`adapter._opinions_from_response` + `_run_impl`):
```json
{
  "opinions": [
    { "stance_score": <float>,            // REQUIRED — KeyError if missing
      "confidence":   <float>,            // REQUIRED — KeyError if missing
      "horizon_days": <int>,              // REQUIRED — KeyError if missing
      "rationale":    "<str>",            // optional on client (defaults "") — WE always send it
      "source_fingerprint": "<str>" },    // optional on client (falls back to idea_fingerprint) — WE always send it
    ...
  ],
  "run_id": "<str>"                        // optional on client (falls back to fingerprint) — WE always send it
}
```

Client-side validation facts the response MUST satisfy (`arbiter/contract/opinion.validate_opinion`):
- `stance_score` ∈ **[-1.0, 1.0]** — and **negative stance passes through unchanged** (the client
  comment is explicit: "we never abs() or floor at 0"). Our judge MUST be able to emit negatives.
- `confidence` ∈ **(0, 1]** (client docstring says [0,1]; `validate_opinion` is the real gate — we
  treat confidence as strictly > 0 to be safe, never emit 0).
- `horizon_days` int, **> 0 and ≤ 365**.
- Each opinion that fails client validation is *skipped* (logged), the rest pass. We never want a
  skip → emit only well-formed opinions.
- The client soft-caps at 32 opinions/run. We emit ≤ 2. Fine.
- On `{ "opinions": [], "run_id": ... }` the client returns `[]` = **A2 abstains** (fail-closed).
  This is our safe degradation target whenever the LLM or evidence path fails.

**⚠ AMBIGUITY FLAGGED — horizon-day constants.** The design spec §3 fixes **SHORT_DAYS = 10**,
MEDIUM_DAYS = 60. The *arbiter client* file hard-codes `_SHORT_HORIZON_DAYS = 14`,
`_MEDIUM_HORIZON_DAYS = 60` (but those constants are **unused** in `adapter.py` — opinions take
`horizon_days` straight from our response). The client only requires `0 < horizon_days ≤ 365`, and
arbiter's fusion buckets by horizon *range*, not exact value. **Decision: follow the design spec —
`SHORT_DAYS = 10`, `MEDIUM_DAYS = 60`.** Both are valid under the client's `≤365` check, and 10 vs
14 lands in the same SHORT bucket. The unused `14` in the client is dead and does not bind us. If a
future audit wants exact parity, change one constant in `mirofish/types.py` and nothing else.

---

## 1. FROZEN shared contracts — this is the literal `mirofish/types.py`

Created in the **foundation step** (before the 3 build agents run). Drop in verbatim. All three
build lanes import from here; none of them edits it.

```python
# mirofish/types.py
"""Frozen shared contracts for the MiroFish A2 brain service.

This module is the single source of truth for every type that crosses a seam
between the evidence layer, the judge, and the FastAPI service. It is created
in the foundation step and is NOT owned/edited by any build lane.

ISOLATION: this module (and the whole `mirofish` package) must never
`import arbiter`. These dataclasses are vendored copies, intentionally
independent of arbiter's own types.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Horizon constants (design spec §2.3, §3.4). SHORT = technical-led opinion,
# MEDIUM = fundamental-led opinion. Both satisfy the client's 0 < h <= 365.
# --------------------------------------------------------------------------- #
SHORT_DAYS: int = 10
MEDIUM_DAYS: int = 60

# Clamp ranges the judge re-applies in Python (JSON-Schema bounds are HINTS).
STANCE_MIN: float = -1.0
STANCE_MAX: float = 1.0
CONFIDENCE_MIN: float = 1e-6   # strictly > 0 (client gate is (0, 1])
CONFIDENCE_MAX: float = 1.0


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Bar:
    """One daily OHLCV bar. Vendored — NOT arbiter.data.pit.Bar.

    `t` is a tz-aware UTC datetime (the bar's timestamp). Field names o/h/l/c/v
    mirror Alpaca's wire keys for clarity at the parse seam.
    """
    t: datetime          # tz-aware UTC
    o: float             # open
    h: float             # high
    l: float             # low   (noqa: E741 — matches Alpaca wire key)
    c: float             # close
    v: float             # volume (shares)


# --------------------------------------------------------------------------- #
# Evidence features (all fields are plain floats/ints/None — JSON-serializable,
# so they fold cleanly into the source_fingerprint canonical JSON).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TechnicalFeatures:
    """Deterministic price-action features computed from bars <= as_of.

    Units are documented per-field. `None` where insufficient history exists
    (e.g. ma_200 needs >=200 bars); the judge prompt renders None as "n/a".
    """
    last_close: float                 # USD, most recent close <= as_of
    ma_50: float | None               # USD, 50-day simple MA of closes
    ma_200: float | None              # USD, 200-day simple MA of closes
    pct_vs_ma_50: float | None        # fraction, (last_close/ma_50 - 1); +0.05 = 5% above
    pct_vs_ma_200: float | None       # fraction
    momentum_20d: float | None        # fraction, 20-trading-day return (close_t/close_t-20 - 1)
    rsi_14: float | None              # 0..100, Wilder RSI over 14 periods
    realized_vol_annualized: float | None  # fraction, stdev(daily log-returns, 20d) * sqrt(252)
    pct_from_52w_high: float | None   # fraction <= 0, (last_close/52w_high - 1); -0.20 = 20% below high
    pct_from_52w_low: float | None    # fraction >= 0, (last_close/52w_low - 1)
    volume_surge_ratio: float | None  # ratio, last_volume / avg(volume, trailing 20d); 2.0 = double
    n_bars: int                       # number of eligible bars used (audit)


@dataclass(frozen=True)
class FundamentalFeatures:
    """Point-in-time fundamentals from SEC companyfacts (facts with filed <= as_of).

    Ratios (pe, ps) are derived with the Alpaca last_close price * shares.
    `valuation_z` is this name's P/E vs its sector peers (z-score); positive =
    richer than sector (a bearish-leaning input). All None-able where the tag /
    peer set is unavailable.
    """
    revenue_ttm: float | None             # USD, trailing reported revenue (latest filed)
    revenue_growth_yoy: float | None      # fraction, (rev_latest/rev_year_ago - 1)
    gross_margin: float | None            # fraction, gross_profit / revenue
    operating_margin: float | None        # fraction, operating_income / revenue
    net_income_ttm: float | None          # USD
    shares_outstanding: float | None      # shares (dei / weighted diluted)
    pe_ratio: float | None                # price*shares / net_income (None if earnings<=0)
    ps_ratio: float | None                # price*shares / revenue
    sector: str | None                    # vendored sector label, or None
    valuation_z: float | None             # z-score of pe_ratio vs sector peers (+ = richer)
    as_of_latest_filed: str | None        # ISO date of the newest fact used (audit; <= as_of)


# --------------------------------------------------------------------------- #
# Evidence pack + fingerprint
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EvidencePack:
    """Everything the judge sees. `source_fingerprint` is the audit/dedup key.

    The fingerprint is computed over technical + fundamental only (NOT over
    as_of's time-of-day, NOT over the ticker casing) so identical evidence on
    the same logical name dedups. ticker/as_of are carried for prompt context.
    """
    ticker: str
    as_of: datetime                       # tz-aware UTC (the information timestamp)
    technical: TechnicalFeatures
    fundamental: FundamentalFeatures | None
    source_fingerprint: str = field(default="")  # filled by build_pack via compute_fingerprint


def _canonical_json(obj: object) -> str:
    """Deterministic JSON: sorted keys, no whitespace, ASCII, stable floats."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def compute_fingerprint(ticker: str, technical: TechnicalFeatures,
                        fundamental: FundamentalFeatures | None) -> str:
    """sha256(canonical_json(evidence))[:16]. Stable across runs/processes.

    Excludes as_of so the same evidence on the same day dedups; the service
    cache key separately carries as_of.date(). ticker is upper-cased for
    stability. Returns a 16-char lowercase hex string.
    """
    payload = {
        "ticker": ticker.upper(),
        "technical": asdict(technical),
        "fundamental": asdict(fundamental) if fundamental is not None else None,
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return digest[:16]


@dataclass(frozen=True)
class OpinionOut:
    """One opinion as it appears in the /analyze response `opinions` array.

    Field names + value ranges match the arbiter client's expectations exactly
    (adapter._opinions_from_response). stance_score may be negative.
    """
    stance_score: float          # [-1, 1]; negative = bearish (passthrough)
    confidence: float            # (0, 1]
    horizon_days: int            # SHORT_DAYS or MEDIUM_DAYS (0 < h <= 365)
    rationale: str               # grounded in evidence, <= 600 chars
    source_fingerprint: str      # the pack's fingerprint (NOT the idea fingerprint)


# --------------------------------------------------------------------------- #
# Pydantic request / response models for FastAPI (/analyze).
# These match the arbiter client wire contract byte-for-byte.
# --------------------------------------------------------------------------- #
class AnalyzeRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=16)
    as_of: datetime                      # pydantic parses ISO-8601; must be tz-aware UTC
    idea_fingerprint: str = Field(default="", max_length=128)


class OpinionModel(BaseModel):
    stance_score: float
    confidence: float
    horizon_days: int
    rationale: str
    source_fingerprint: str


class AnalyzeResponse(BaseModel):
    opinions: list[OpinionModel]
    run_id: str


def opinion_to_model(op: OpinionOut) -> OpinionModel:
    return OpinionModel(
        stance_score=op.stance_score,
        confidence=op.confidence,
        horizon_days=op.horizon_days,
        rationale=op.rationale,
        source_fingerprint=op.source_fingerprint,
    )
```

Notes for builders:
- `AnalyzeRequest.as_of` — accept tz-aware ISO. If naive, the route normalizes to UTC (see §5.2).
- `Bar.l` triggers the E741 lint (ambiguous name `l`). Add `# noqa: E741` inline (already shown).
  Matching the Alpaca wire key is the intentional trade-off; flagged for the audit.
- All feature dataclasses are `frozen=True` and contain only JSON-native scalars so
  `asdict()` → canonical JSON is total. No nested dataclasses inside features.

---

## 2. FROZEN judge contract — tool schema, LLM call, parse/clamp rules

### 2.1 The `emit_opinions` tool JSON-Schema (frozen; verbatim from research §A.2)

Lives in `mirofish/judge.py` as a module constant `EMIT_OPINIONS_TOOL`.

```python
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
```

**Decision:** keep the JSON-Schema numeric/length bounds (they steer the model) but treat them as
**hints, not guarantees** (research §A.2 caveat — the 2026 validator does NOT hard-enforce
numeric/length/array bounds). The judge **re-validates and clamps in Python** (§2.4). Do **not** add
`strict: true` (optional; shape is re-validated in Python anyway, and avoiding `strict` keeps the
schema flexible). The arbiter side never sees the schema — only our re-validated `OpinionOut`s.

### 2.2 The exact `client.messages.create(...)` call (frozen)

In `mirofish/llm.py`, the real wrapper `AnthropicLLM.create(...)` issues exactly:

```python
resp = self._client.messages.create(
    model=model,                              # config MIROFISH_MODEL, default "claude-sonnet-4-6"
    max_tokens=1024,
    system=ANALYST_SYSTEM_PROMPT,             # the independent-skeptic rules (§2.3)
    messages=[{"role": "user", "content": evidence_text}],
    tools=[EMIT_OPINIONS_TOOL],
    tool_choice={
        "type": "tool",
        "name": "emit_opinions",
        "disable_parallel_tool_use": True,    # exactly one tool_use block
    },
)
```

Rules (research §A): non-beta path (`client.messages.create`, not `client.beta.*`); **no
`thinking`** param; model id is the bare string (no date suffix); `max_tokens=1024`.

### 2.3 The analyst system prompt (frozen content; Build B owns the exact string)

Build B writes `ANALYST_SYSTEM_PROMPT` to encode these REQUIRED rules (the wording is B's, the
rules are frozen):
1. "You are an INDEPENDENT, skeptical equity analyst. You did NOT originate this idea and you do
   not know why anyone else likes it."
2. "You MAY and SHOULD return a **negative** stance_score when the evidence shows the name is
   technically overextended (high RSI, far above moving averages, exhausted momentum, far below a
   recent high after a run) or richly valued vs its sector (positive valuation_z, high P/E)."
3. "Do NOT default to bullish. Neutral (0) and bearish (<0) are first-class outcomes."
4. "Ground every rationale ONLY in the supplied evidence. Never invent facts, news, or figures not
   in the evidence. If a field is 'n/a', do not speculate about it."
5. "opinions[0] = SHORT horizon (~10 trading days), technical-led. opinions[1] = MEDIUM horizon
   (~60 days), fundamental-led."
6. "If the evidence contains NO fundamentals (fundamentals: n/a), still emit two opinions but base
   BOTH primarily on the technical evidence." *(See §2.4 note: the judge collapses to one opinion
   when `fundamental is None`; the prompt instruction is belt-and-suspenders so a 2-opinion model
   response is still well-formed.)*

The user message `evidence_text` is rendered by `render_pack(pack) -> str` (Build B) — a compact,
labeled plain-text block listing every technical field and (if present) every fundamental field,
with `None → "n/a"`, the ticker, and the as_of date. No JSON dump; a readable analyst brief.

### 2.4 Parse / abstain / clamp rules (frozen)

`judge.py` parsing follows research §A.3 exactly:

1. If `resp.stop_reason != "tool_use"` → return `[]` (abstain). Guard `"max_tokens"` and
   `"refusal"` the same way (abstain). **Never** read `resp.stop_details` unless
   `stop_reason == "refusal"` (it is `None` otherwise).
2. Take the first content block with `b.type == "tool_use" and b.name == "emit_opinions"`. If none
   → `[]`.
3. `payload = block.input` (already a parsed dict; no `json.loads`). `raw = payload.get("opinions")`.
   If not a non-empty list → `[]`.
4. **Re-validate + clamp each raw opinion in Python** (this is load-bearing — schema bounds are
   hints):
   - `stance_score`: `float(x)`, then `max(STANCE_MIN, min(STANCE_MAX, x))` → **negative preserved**.
   - `confidence`: `float(x)`, then `max(CONFIDENCE_MIN, min(CONFIDENCE_MAX, x))` (never 0, never >1).
   - `horizon_days`: ignore the model's number; **assign by index/role** — opinion[0] → `SHORT_DAYS`,
     opinion[1] → `MEDIUM_DAYS` (coerce into the two buckets per spec §3.4). (If only one opinion
     survives, it is the SHORT one.)
   - `rationale`: `str(x)`, strip, truncate to 600 chars; if empty → `"(no rationale)"`.
   - `source_fingerprint`: set to `pack.source_fingerprint` (NOT model-supplied).
   - Any per-opinion exception → skip that opinion (do not abort the whole list).
5. **Bucket assignment when fundamentals absent:** if `pack.fundamental is None`, the judge returns
   **only the SHORT (technical) opinion** (design §2.6, §3.4) — even if the model emitted two. Take
   `result[:1]`. If fundamentals present, keep up to 2 (index 0 = SHORT, index 1 = MEDIUM).
6. Return `list[OpinionOut]` (length 0, 1, or 2). Length 0 → service emits `{opinions: [], ...}`.

### 2.5 The `FakeLLM` contract (frozen — enables offline + `--fake-llm`)

`mirofish/llm.py` exposes a `FakeLLM` whose `.create(...)` returns an object that **mirrors the real
SDK response shape** so the SAME parse path runs:
- `.stop_reason` (str, default `"tool_use"`).
- `.stop_details` (`None` unless simulating a refusal).
- `.content` = list of block objects; one block has `.type == "tool_use"`, `.name ==
  "emit_opinions"`, `.input == {"opinions": [...]}`.

`FakeLLM` is constructed with a canned `opinions` list (and optional `stop_reason` override). Build
B provides tiny `_FakeBlock`/`_FakeResp` namespaces (e.g. `types.SimpleNamespace`) so no real SDK or
key is needed. The default canned response (used by `--fake-llm` mode) returns a deterministic
2-opinion payload derived from the pack (e.g. short stance keyed off RSI/`pct_from_52w_high`, medium
off `valuation_z`) so the service end-to-end test can assert a **negative stance** for an
overbought+rich pack.

### 2.6 Judge seam signature (frozen)

```python
def judge(pack: EvidencePack, *, model: str, llm: "LLM") -> list[OpinionOut]: ...
```
- `llm` is any object with `.create(*, model, max_tokens, system, messages, tools, tool_choice) ->
  resp`. Both `AnthropicLLM` and `FakeLLM` satisfy it (structural typing; define a `typing.Protocol`
  named `LLM` in `llm.py`).
- `judge` NEVER raises: wrap the whole body in try/except → `[]` on any error (matches the
  service's never-500 rule). Log and abstain.

---

## 3. FROZEN function signatures at every seam

Build agents implement against exactly these. (Imports from `mirofish.types` assumed.)

### 3.1 Build A — clients + evidence

```python
# mirofish/clients/alpaca.py
class AlpacaBarsClient:
    def __init__(self, *, api_key: str, secret_key: str, feed: str = "iex",
                 base_url: str = "https://data.alpaca.markets",
                 timeout: float = 30.0, max_retries: int = 5,
                 backoff_base: float = 1.0, backoff_cap: float = 60.0) -> None: ...

    def bars(self, ticker: str, start: datetime, end: datetime) -> list[Bar]:
        """GET /v2/stocks/{ticker}/bars (timeframe=1Day, adjustment=split,
        limit=10000, feed=self.feed). Paginate via page_token<-next_page_token.
        429 -> honor Retry-After else exp-backoff+jitter (base..cap, max_retries),
        retry SAME paged request. 404/422 -> []. network error -> []. Drop bars
        with t >= end (strict). Sort ascending. NEVER raises."""

    def bars_as_of(self, ticker: str, as_of: datetime, *, lookback_days: int = 300) -> list[Bar]:
        """start = as_of - lookback_days; end = as_of + 1 day. Fetch via bars(),
        then PIT filter: return [b for b in fetched if b.t <= as_of]. NEVER raises."""
```

```python
# mirofish/clients/sec_facts.py
class SecFactsClient:
    def __init__(self, *, user_agent: str, base_url: str = "https://data.sec.gov",
                 tickers_url: str = "https://www.sec.gov/files/company_tickers.json",
                 timeout: float = 30.0, max_retries: int = 5,
                 backoff_base: float = 0.2, backoff_cap: float = 10.0) -> None: ...

    def cik_for_ticker(self, ticker: str) -> str | None:
        """Resolve ticker -> 10-digit zero-padded CIK via company_tickers.json
        (fetched once, cached on the instance). None if unknown. NEVER raises -> None."""

    def company_facts(self, cik: str) -> dict | None:
        """GET /api/xbrl/companyfacts/CIK{cik}.json with User-Agent header.
        Returns the parsed dict, or None on 404/network/parse error. NEVER raises.
        Respects ~10 req/s (sleep between calls) + 429 backoff."""

    def facts_as_of(self, ticker: str, as_of: datetime) -> dict | None:
        """Convenience: cik_for_ticker -> company_facts -> return the raw facts
        dict (caller applies the filed<=as_of filter in fundamentals.py).
        None if no CIK / no facts. NEVER raises."""
```
> Build A may keep the `filed <= as_of` PIT filtering inside `fundamentals.py` (preferred — keeps
> the client a thin fetch) OR expose a helper; either way the **filter lives in Build A's code** and
> is unit-tested there. Recommended: a module-level pure helper
> `pit_concept_values(facts: dict, gaap_tag: str, as_of: datetime, *, unit="USD") -> list[dict]` in
> `fundamentals.py` that returns the facts with `filed <= as_of`, newest first.

```python
# mirofish/evidence/technical.py
def compute_technical(bars: list[Bar], as_of: datetime) -> TechnicalFeatures:
    """Pure. Assumes bars already filtered to <= as_of and sorted ascending.
    Computes every TechnicalFeatures field; None where history is insufficient.
    Raises ValueError ONLY if bars is empty (caller treats empty -> no technical
    opinion / degrade). n_bars = len(bars)."""
```

```python
# mirofish/evidence/fundamentals.py
def compute_fundamentals(ticker: str, as_of: datetime, *,
                         client: "SecFactsClient", last_close: float | None,
                         sector_map: dict[str, str]) -> FundamentalFeatures | None:
    """Fetch SEC facts via client, apply filed<=as_of PIT filter, derive the
    FundamentalFeatures. last_close (from Alpaca) powers pe/ps. sector_map maps
    TICKER->sector for valuation_z (peer P/E set may be vendored/static; if a
    peer set is unavailable, valuation_z=None). Returns None when SEC lacks
    coverage / no US-GAAP revenue tag with filed<=as_of. NEVER raises -> None."""
```
> **AMBIGUITY FLAGGED — `valuation_z` peer set.** The spec wants "valuation-vs-sector z-score" but
> A2 analyzes one ticker per call and has no live peer-P/E feed (egress is SEC + Alpaca only; no
> bulk ratios). **Decision for the build:** Build A ships a small **vendored static table** in
> `mirofish/data/sector_valuation.py` mapping sector → `(median_pe, stdev_pe)` (rough, documented as
> a heuristic baseline), and `valuation_z = (pe_ratio - median_pe) / stdev_pe` when both the name's
> P/E and its sector baseline exist, else `None`. This keeps the egress contract intact and is
> honestly labeled an approximation. `sector_map` (ticker→sector) is the same small vendored table's
> companion. Flag in the README that this is a coarse static baseline, not a live cross-section.

```python
# mirofish/evidence/pack.py
def build_pack(ticker: str, as_of: datetime, tech: TechnicalFeatures,
               fund: FundamentalFeatures | None) -> EvidencePack:
    """Assemble + compute source_fingerprint via types.compute_fingerprint.
    Pure. Returns a frozen EvidencePack with source_fingerprint filled."""
```

### 3.2 Build B — judge + llm
```python
# mirofish/llm.py
class LLM(Protocol):
    def create(self, *, model, max_tokens, system, messages, tools, tool_choice): ...
class AnthropicLLM:  # real wrapper, reads ANTHROPIC_API_KEY
    def __init__(self, *, api_key: str | None = None) -> None: ...
    def create(self, **kwargs): ...           # -> anthropic Message
class FakeLLM:
    def __init__(self, opinions: list[dict] | None = None,
                 stop_reason: str = "tool_use") -> None: ...
    def create(self, **kwargs): ...           # -> SimpleNamespace mirroring the SDK shape

# mirofish/judge.py
EMIT_OPINIONS_TOOL: dict
ANALYST_SYSTEM_PROMPT: str
def render_pack(pack: EvidencePack) -> str: ...
def judge(pack: EvidencePack, *, model: str, llm: LLM) -> list[OpinionOut]: ...
```

### 3.3 Build C — service + cache + config
```python
# mirofish/config.py
class Config:
    anthropic_api_key: str | None
    edgar_user_agent: str | None
    alpaca_api_key: str | None
    alpaca_secret_key: str | None
    alpaca_data_feed: str          # default "iex"
    model: str                     # MIROFISH_MODEL, default "claude-sonnet-4-6"
    host: str                      # MIROFISH_HOST, default "127.0.0.1"
    port: int                      # MIROFISH_PORT, default 8900
    cache_ttl_seconds: int         # default 86400
    fake_llm: bool                 # MIROFISH_FAKE_LLM
    short_days: int                # = types.SHORT_DAYS
    medium_days: int               # = types.MEDIUM_DAYS
    @classmethod
    def from_env(cls) -> "Config": ...
    def __repr__(self) -> str: ...   # SECRET-REDACTING (keys -> "***")

# mirofish/cache.py
def cache_key(ticker: str, as_of: datetime, fingerprint: str) -> str:
    """f'{ticker.upper()}|{as_of.date().isoformat()}|{fingerprint}'"""
class OpinionCache:                 # in-memory TTL dict; thread-safe (Lock)
    def __init__(self, ttl_seconds: int) -> None: ...
    def get(self, key: str) -> list[OpinionOut] | None: ...
    def put(self, key: str, opinions: list[OpinionOut]) -> None: ...

# mirofish/app.py
def create_app(config: Config | None = None) -> FastAPI: ...
# routes: POST /analyze (AnalyzeRequest -> AnalyzeResponse), GET /health -> {"status":"ok", ...}
def build_evidence(req: AnalyzeRequest, config, *, alpaca, sec) -> EvidencePack | None: ...
def analyze(req: AnalyzeRequest) -> AnalyzeResponse: ...   # the route handler body
```

**Route orchestration (frozen flow)** in `analyze`:
1. Normalize `as_of` to tz-aware UTC.
2. Fetch bars `alpaca.bars_as_of(ticker, as_of)`. If empty → no technical → **abstain** `{opinions:
   [], run_id}`.
3. `tech = compute_technical(bars, as_of)`; `last_close = tech.last_close`.
4. `fund = compute_fundamentals(ticker, as_of, client=sec, last_close=last_close,
   sector_map=...)` (may be `None`).
5. `pack = build_pack(ticker, as_of, tech, fund)`.
6. `key = cache_key(ticker, as_of, pack.source_fingerprint)`; on cache hit → return cached opinions
   with a **fresh `run_id`** (do not re-call the LLM).
7. Miss → `opinions = judge(pack, model=config.model, llm=llm)`; `cache.put(key, opinions)` (only if
   non-empty, mirroring the arbiter "write-once when results exist" rule).
8. Build `AnalyzeResponse(opinions=[opinion_to_model(o) for o in opinions], run_id=new_run_id())`.
   `run_id` = a fresh ULID/uuid4 hex per call.
9. ANY exception anywhere → `AnalyzeResponse(opinions=[], run_id=new_run_id())` (never a 500).

**Run-id:** `new_run_id() -> str` = `uuid.uuid4().hex` (no arbiter ULID import). Opinions in one
response implicitly share the run via the top-level `run_id`; arbiter stamps its own `run_group_id`
from it.

---

## 4. The 3 DISJOINT build lanes (no shared files)

Every lane imports the frozen `mirofish/types.py` (foundation) and `mirofish/config.py`'s public
names only where noted. **No file is edited by two lanes.**

### Build A — evidence + clients
Owns (and ONLY these):
- `mirofish/clients/__init__.py`
- `mirofish/clients/alpaca.py`
- `mirofish/clients/sec_facts.py`
- `mirofish/evidence/__init__.py`
- `mirofish/evidence/technical.py`
- `mirofish/evidence/fundamentals.py`
- `mirofish/evidence/pack.py`
- `mirofish/data/__init__.py`, `mirofish/data/sector_valuation.py` (vendored sector + valuation table)
- Tests: `mirofish/tests/test_alpaca.py`, `test_sec_facts.py`, `test_technical.py`,
  `test_fundamentals.py`, `test_pack.py`
Depends on: `types.Bar/TechnicalFeatures/FundamentalFeatures/EvidencePack/compute_fingerprint`.
Reads config values via parameters (constructor args), not by importing `config` — keeps A
independent of C. (The service in C wires env→client constructors.)

### Build B — judge
Owns:
- `mirofish/judge.py`
- `mirofish/llm.py` (AnthropicLLM + FakeLLM + LLM Protocol)
- Tests: `mirofish/tests/test_judge.py`, `mirofish/tests/test_llm_fake.py`
Depends on: `types.EvidencePack/OpinionOut/SHORT_DAYS/MEDIUM_DAYS/clamp consts`.
Does NOT import evidence or app. Tests build `EvidencePack`s by hand (or via a tiny fixture factory
in `conftest.py` — foundation owns conftest).

### Build C — service
Owns:
- `mirofish/app.py`
- `mirofish/cache.py`
- `mirofish/config.py`
- `mirofish/__main__.py` (optional `python -m mirofish` → uvicorn launcher with loopback guard)
- `pyproject.toml` / `requirements.txt`, `README.md`, `SETUP_NEEDED.md` additions
- Tests: `mirofish/tests/test_config.py`, `test_cache.py`, `test_app.py`,
  `test_contract_fake_llm.py` (end-to-end `--fake-llm` against the arbiter client's expected schema)
Depends on: `types.*`, plus it IMPORTS Build A's `build_evidence` pieces
(`alpaca`, `sec_facts`, `evidence.*`) and Build B's `judge`/`llm` **by their frozen signatures**.
This is the integration lane; it does not edit A's or B's files, only calls them.

**Cross-cutting files → assigned to the FOUNDATION step (NOT to any lane):**
- `mirofish/__init__.py`
- `mirofish/types.py` (the frozen contracts)
- `mirofish/tests/__init__.py` and `mirofish/conftest.py` (shared fixtures: a sample `EvidencePack`
  factory, sample `Bar` lists, a fake SEC companyfacts JSON fixture, env-var clearing).
- The directory skeleton + empty `__init__.py`s for `clients/`, `evidence/`, `data/`, `tests/`.

> If a lane needs a brand-new shared helper mid-build, it goes in that lane's own module and is NOT
> back-ported into `types.py` (which is frozen). Escalate to the orchestrator instead of editing a
> foundation file.

---

## 5. Foundation step (orchestrator runs this BEFORE the 3 builds)

### 5.1 Package skeleton
```
mirofish/
  __init__.py                # version string; NO heavy imports
  types.py                   # §1 verbatim (frozen)
  config.py                  # STUB created by foundation? NO -> owned by Build C.
  clients/__init__.py        # empty (foundation)
  evidence/__init__.py       # empty (foundation)
  data/__init__.py           # empty (foundation)
  tests/__init__.py          # empty (foundation)
  conftest.py                # shared fixtures (foundation)
  pyproject.toml             # owned by Build C (foundation creates only the dir)
```
Foundation creates: `__init__.py`, `types.py`, the empty package `__init__.py`s, `tests/__init__.py`,
`conftest.py`. Foundation does NOT create `config.py`/`app.py`/lane modules (those are lane-owned) —
it only creates empty `__init__.py` package markers so imports resolve.

### 5.2 `conftest.py` shared fixtures (foundation)
- `sample_bars()` → a deterministic ascending `list[Bar]` (≥220 bars so MA-200 is computable).
- `overbought_rich_pack()` → an `EvidencePack` engineered to be technically overextended (high RSI,
  near 52w high, hot momentum) AND richly valued (`valuation_z` large positive) — used by the
  negative-stance characterization test.
- `no_fundamentals_pack()` → an `EvidencePack` with `fundamental=None`.
- `fake_companyfacts()` → a small SEC companyfacts dict with two revenue facts, one `filed` BEFORE a
  reference as_of and one `filed` AFTER (drives the PIT exclusion test).
- `clear_env` autouse fixture → unset `ANTHROPIC_API_KEY`/`ALPACA_*`/`EDGAR_USER_AGENT`/
  `MIROFISH_*` so no test accidentally hits the network or reads a real key.
- `as_of_utc()` → a fixed tz-aware UTC datetime.

`as_of` normalization helper (used by both `app.py` and tests; define in `types.py` so it's shared,
foundation-owned): `def ensure_utc(dt: datetime) -> datetime` (naive → assume UTC; aware → convert).

### 5.3 Dependencies (pin nothing exotic)
`mirofish/requirements.txt` (Build C writes; foundation lists here for reference):
```
fastapi
uvicorn[standard]
httpx
anthropic
pydantic>=2
```
Dev/test: `pytest`, `pytest-asyncio` (only if any async test), `respx` or `httpx`'s
`MockTransport` for mocking Alpaca/SEC HTTP (prefer `httpx.MockTransport` — no extra dep). The
FastAPI `TestClient` (via `starlette`) covers the service test. Anthropic is imported lazily inside
`AnthropicLLM.__init__` so the package imports without the SDK present in `--fake-llm`/offline tests.

### 5.4 Venv / how tests run (RECOMMENDATION)
**Recommendation: reuse the existing arbiter `.venv`** at `/Users/jonathanmorris/poly_bot/arbiter/.venv`
for running mirofish tests, but DO NOT install mirofish into arbiter's package, and DO NOT let
mirofish import arbiter. Rationale:
- The design spec §2.5 says the service "must not import mirofish [from arbiter]; HTTP-only" — that's
  an *import* isolation rule, not a *venv* isolation rule. Sharing a venv is explicitly allowed by
  the task ("the service must not import arbiter but MAY share a venv").
- arbiter's `.venv` already has `httpx`, `pydantic`, `pytest`, `anthropic` likely present; add
  `fastapi`/`uvicorn` if missing. One `pip install -r mirofish/requirements.txt` into the existing
  `.venv`.
- Run tests from the repo root with `arbiter/.venv/bin/python -m pytest mirofish/tests -q`.
- An **import-isolation test** (`mirofish/tests/test_isolation.py`, foundation or Build C) asserts no
  `mirofish` module imports `arbiter` — e.g. walk `mirofish/` source files and assert no
  `import arbiter` / `from arbiter` substring (a cheap AST/grep test), guaranteeing the shared venv
  can't hide a sneaky import.

Alternative (if the user prefers hard separation): a dedicated `mirofish/.venv`. Recommended only if
arbiter's venv proves to have conflicting pins; otherwise the shared venv is simpler. **Flag for the
orchestrator to confirm.**

---

## 6. Per-module offline test plan (every external call mocked)

All tests are offline: Alpaca + SEC via `httpx.MockTransport`; Anthropic via `FakeLLM`. No real
clock dependence (as_of is always passed in). Each lane's tests live under `mirofish/tests/`.

**Build A**
- `test_alpaca.py`: MockTransport returns a 2-page `{bars, next_page_token}` body → pagination
  concatenates + sorts; `t >= end` bars dropped; 404 → `[]`; 429 once then 200 → retried (assert no
  raise, monkeypatch `time.sleep`); `bars_as_of` filters `t <= as_of`.
- `test_sec_facts.py`: MockTransport for `company_tickers.json` → CIK resolved + zero-padded;
  companyfacts 200 → dict; 404 → None; unknown ticker → None.
- `test_technical.py`: from `sample_bars()` assert RSI/MA/momentum/vol/52w/volume-surge against
  hand-computed expected values; <200 bars → ma_200 None but others set; empty bars → ValueError.
- `test_fundamentals.py`: **PIT TEST (load-bearing)** — feed `fake_companyfacts()` with one revenue
  fact `filed` after as_of and one before; assert the post-as_of fact is **excluded** and revenue
  comes from the pre-as_of fact. Also: missing revenue tag → `None`; pe None when net_income ≤ 0;
  `valuation_z` from the vendored sector table.
- `test_pack.py`: fingerprint is stable across two builds of identical evidence; differs when a
  feature changes; `fundamental=None` packs fingerprint without error; fingerprint is 16 hex chars.

**Build B**
- `test_judge.py`:
  - happy path: `FakeLLM` returns 2 valid opinions → `judge` returns 2 `OpinionOut`,
    horizons coerced to `[SHORT_DAYS, MEDIUM_DAYS]`, source_fingerprint == pack's.
  - **clamp test:** FakeLLM returns `stance_score=2.5, confidence=0.0` → clamped to `1.0` /
    `CONFIDENCE_MIN`.
  - **NEGATIVE-STANCE PASSTHROUGH TEST:** `overbought_rich_pack()` + a FakeLLM returning a negative
    short stance → `judge` returns it **unchanged negative** (no abs/floor).
  - abstain: `stop_reason="max_tokens"` → `[]`; `stop_reason="refusal"` → `[]`; missing tool_use
    block → `[]`; `opinions=[]` → `[]`.
  - degradation: `no_fundamentals_pack()` → exactly **one** (SHORT) opinion even if FakeLLM emits two.
- `test_llm_fake.py`: `FakeLLM.create(...)` returns an object whose `.content[0].input["opinions"]`
  round-trips through the real parse path; default canned response is deterministic.

**Build C**
- `test_config.py`: `from_env` reads all vars + defaults (model=`claude-sonnet-4-6`, port 8900,
  host 127.0.0.1); `repr` REDACTS `anthropic_api_key`/`alpaca_secret_key` (assert the secret string
  is NOT in `repr`).
- `test_cache.py`: `cache_key` format; put→get hit; TTL expiry (monkeypatch the clock) → miss;
  thread-safety smoke.
- `test_app.py` (FastAPI `TestClient`, `--fake-llm` / `FakeLLM` injected, Alpaca+SEC mocked):
  - `POST /analyze` happy path → 200, body validates against `AnalyzeResponse`, 2 opinions, valid
    ranges, `run_id` present.
  - **cache hit avoids a 2nd LLM call** — a counting FakeLLM asserts `.create` called once across two
    identical requests; second response has a **fresh `run_id`** but same opinions.
  - no-fundamentals (SEC mock → no facts) → 1 opinion.
  - empty bars (Alpaca mock → `[]`) → `{opinions: [], run_id}` (abstain), still 200.
  - judge raises (FakeLLM forced to throw) → `{opinions: [], run_id}`, still 200 (never 500).
  - **non-loopback bind refused:** `create_app`/launcher with `host="0.0.0.0"` → raises/refuses at
    startup (assert the guard).
  - `GET /health` → 200 `{"status": "ok"}`.
- `test_contract_fake_llm.py` (**the byte-for-byte contract test**): start the app with `FakeLLM`,
  drive it through the **real arbiter client** `MirofishHTTPClient` is heavy (20-min timeout, egress
  loopback gate) — instead assert the response JSON satisfies what
  `arbiter.adapters.mirofish.adapter._opinions_from_response` needs by **replicating the required
  keys/ranges inline** (do NOT import arbiter into the test if it would violate isolation; importing
  arbiter *in a test that lives under mirofish/* is the one gray area — **see flag below**). Include a
  **negative stance** in the FakeLLM payload and assert it survives end-to-end in the response body.
- `test_isolation.py`: assert no `mirofish/**.py` contains `import arbiter` / `from arbiter`.

> **AMBIGUITY FLAGGED — may a mirofish *test* import arbiter for the contract check?** The isolation
> rule forbids *production* `mirofish` code importing arbiter. A test that imports arbiter's client
> to prove byte-compatibility is arguably fine (tests aren't shipped, and it's the strongest
> contract proof). **Decision: keep the contract test self-contained — replicate the arbiter
> client's required-keys/ranges assertions inline in the mirofish test (no `import arbiter`).** Then,
> separately, the **orchestrator's post-wave end-to-end step** (NOT a mirofish unit test) runs the
> launched `--fake-llm` service against the actual arbiter `MirofishHTTPClient` from arbiter's side
> (set `MIROFISH_ENDPOINT=http://127.0.0.1:8900`), which is where the real cross-package contract is
> exercised. This keeps `mirofish/` import-clean while still proving the contract. Flagged for
> orchestrator confirmation.

---

## 7. PIT + isolation invariants — checklist for build AND audit

Every item is testable; the audit verifies each.

**Isolation**
- [ ] No file under `mirofish/` contains `import arbiter` or `from arbiter` (test_isolation).
- [ ] `mirofish` package imports with NO env vars set and NO `anthropic` SDK installed (lazy import
      in `AnthropicLLM`).
- [ ] Egress is implicitly SEC + Alpaca + loopback only — mirofish has its own clients; it does not
      reuse arbiter's egress module, but its host list is the same (SEC, Alpaca data, localhost).

**Point-in-time correctness**
- [ ] Alpaca: fetch `[as_of-300d, as_of+1d)`, drop `t >= end`, then keep `t <= as_of`
      (`bars_as_of`). `compute_technical` only ever sees bars ≤ as_of.
- [ ] SEC: every fact used satisfies `filed <= as_of`; a fact filed AFTER as_of is excluded
      (the PIT test asserts this). No reporting-lag heuristic (the true `filed` date is exact).
- [ ] `as_of` is always caller-supplied; **no `datetime.now()`** anywhere in request handling.
      (A grep for `datetime.now`/`time.time` in non-cache code is part of the audit; the cache TTL
      clock is the only allowed wall-clock and is injectable for tests.)

**Bind / safety**
- [ ] Service binds `127.0.0.1` only; a non-loopback host is refused at startup (test_app).
- [ ] `run_id` is fresh per call (uuid4 hex); cache hit reuses opinions but NOT the run_id.

**Never-raise / degrade**
- [ ] `alpaca.bars*`, `sec_facts.*`, `compute_fundamentals`, `judge`, and the `/analyze` handler
      NEVER raise out — they degrade to `[]`/`None` and a schema-valid `{opinions:[], run_id}`.
- [ ] No-fundamentals → exactly one (SHORT) opinion. LLM failure/abstain → empty opinions.
- [ ] `/analyze` always returns a schema-valid body; never a 500 (the arbiter client fails-closed on
      empty anyway, but we don't rely on that).

**Output correctness**
- [ ] `stance_score` clamped to [-1,1] with **negative preserved** (no abs, no floor at 0).
- [ ] `confidence` clamped to (0,1] (never 0, never >1).
- [ ] `horizon_days` ∈ {SHORT_DAYS=10, MEDIUM_DAYS=60} by opinion role; both ≤ 365.
- [ ] `source_fingerprint` = the pack fingerprint (16 hex), NOT the model-supplied or idea
      fingerprint.

**Secret hygiene**
- [ ] `Config.__repr__` redacts `ANTHROPIC_API_KEY` and `ALPACA_SECRET_KEY` (test asserts the secret
      is absent from repr). Logs never print keys.

---

## 8. Build order & gating (for the orchestrator)
1. **Foundation** — skeleton + frozen `types.py` + `conftest.py` + venv (`pip install -r
   mirofish/requirements.txt` into arbiter's `.venv`). Verify `mirofish/types.py` imports clean.
2. **Builds A, B, C in parallel** (disjoint files; all green against the frozen types).
3. **Gate:** `arbiter/.venv/bin/python -m pytest mirofish/tests -q` all green; both linters clean
   (ruff/whatever arbiter uses — but NOT arbiter's insert-only/no-lookahead linters, which don't
   apply; mirofish uses plain ruff). Confirm `test_isolation` passes.
4. **End-to-end:** launch `uvicorn mirofish.app:create_app --factory --host 127.0.0.1 --port 8900`
   in `--fake-llm` mode (`MIROFISH_FAKE_LLM=1`); from arbiter's side set
   `MIROFISH_ENDPOINT=http://127.0.0.1:8900` and call `arbiter.adapters.mirofish.adapter.run(...)`
   with a duck-typed idea → assert real opinions (incl. a negative stance) flow through. This is the
   only step where both packages run together (over HTTP, as designed).

---

## 9. Ambiguities / judgment calls flagged (honest list)
1. **Horizon = 10 vs 14** — followed the design spec (SHORT=10); the arbiter client's `14` constant
   is dead/unused and the client only checks `≤365`. (§0)
2. **`valuation_z` peer set** — no live cross-section is reachable under the egress contract;
   shipped as a vendored static sector P/E baseline, honestly labeled a heuristic. (§3.1)
3. **Contract test importing arbiter** — kept the mirofish unit test self-contained (no arbiter
   import); the true cross-package contract is exercised by the orchestrator's end-to-end step. (§6)
4. **Shared vs dedicated venv** — recommended sharing arbiter's `.venv` (import-isolation enforced by
   a test, not by venv separation); orchestrator to confirm. (§5.4)
5. **`Bar.l` lint** — kept the single-letter field to mirror Alpaca's wire key; `# noqa: E741`. (§1)
6. **`run_id` vs ULID** — used `uuid4().hex` to avoid any arbiter ULID import; arbiter stamps its own
   `run_group_id` from our `run_id`, so format is free. (§3.3)
7. **`disable_parallel_tool_use`** kept `True` per research; with a single forced tool this is
   belt-and-suspenders, harmless.
```
