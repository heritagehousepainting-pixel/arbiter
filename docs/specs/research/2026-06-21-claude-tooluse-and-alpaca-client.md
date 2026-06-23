# Research memo — Claude tool-use (judge.py) + standalone Alpaca client (mirofish/)

**Date:** 2026-06-21
**Scope:** Two implementation unknowns for the localhost A2-brain service (`mirofish/`).
Design spec: `docs/superpowers/specs/2026-06-21-mirofish-a2-brain-design.md` (§3.4 judge, §9.2/§9.3).
**Read-only research.** Snippets below are illustrative, not production code.

---

## Section A — Claude structured output via tool-use (the `judge.py` layer)

### Goal recap (from §3.4)
`judge(pack) -> list[Opinion]` must get back a RELIABLY-TYPED array of **exactly two** opinion
objects, each `{stance_score: float[-1,1], confidence: float(0,1], horizon_days: int, rationale: str}`.
We force structured output by giving Claude a single `emit_opinions` tool and **forcing** it via
`tool_choice`, so the model MUST emit the structured args rather than free prose.

### A.1 — The exact `client.messages.create(...)` call shape

Use the official `anthropic` Python SDK, model id **`claude-sonnet-4-6`**. Force the tool with
`tool_choice={"type": "tool", "name": "emit_opinions"}`.

```python
import anthropic

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

EMIT_OPINIONS_TOOL = { ... }  # see A.2

resp = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,                         # see A.4 — opinions are small; rationale-bounded
    system=ANALYST_SYSTEM_PROMPT,            # the "independent skeptic" rules (§3.4)
    messages=[{"role": "user", "content": EVIDENCE_PACK_RENDERED}],
    tools=[EMIT_OPINIONS_TOOL],
    tool_choice={
        "type": "tool",
        "name": "emit_opinions",
        "disable_parallel_tool_use": True,   # force exactly one tool call (see A.5)
    },
)
```

Notes that the official docs (docs.claude.com / platform.claude.com tool-use overview) confirm,
cross-checked against the bundled `claude-api` skill (2026 API):

- **`tool_choice` shapes** (tool-use concepts):
  `{"type": "auto"}` (default), `{"type": "any"}` (must use *some* tool),
  `{"type": "tool", "name": "..."}` (must use *this* tool), `{"type": "none"}`.
  For a single forced structured emitter, `{"type": "tool", "name": "emit_opinions"}` is correct.
- `disable_parallel_tool_use: true` may be added to ANY `tool_choice` value to cap the model at
  **one** tool call per response. With a single forced tool this guarantees exactly one `tool_use`
  block (belt-and-suspenders — see A.5).
- Model id is the bare string **`claude-sonnet-4-6`** — do NOT append a date suffix.
  (Catalog: 1M context, $3 in / $15 out per MTok.)
- **Do not** use `thinking` here. Sonnet 4.6 supports adaptive thinking, but the judge is a tiny
  forced-tool call — leave it off (omit the param). `budget_tokens` is deprecated on 4.6; never use it.
- **Do not** use `output_config.format` (JSON-schema structured outputs) here — the spec calls for
  **tool-use**, and `tool_choice`-forced tool-use is the right primitive for "emit this exact shape."
  (Both are valid in 2026; we pick tool-use per the design.)

### A.2 — The `input_schema` for `emit_opinions` (exact JSON)

A single tool whose payload is an **array of exactly 2** opinion objects. JSON-Schema constraints
(`minItems`/`maxItems`, `minimum`/`maximum`) are expressed; the judge still re-validates/clamps in
Python (the spec requires clamping regardless — the model can still drift).

```json
{
  "name": "emit_opinions",
  "description": "Emit exactly two independent analyst opinions on the ticker, grounded only in the supplied evidence. opinions[0] is the SHORT-horizon technical-led view (~10 trading days); opinions[1] is the MEDIUM-horizon fundamental-led view (~60 days). stance_score is signed: negative = bearish, positive = bullish, 0 = neutral. Do not invent facts not present in the evidence.",
  "input_schema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "opinions": {
        "type": "array",
        "minItems": 2,
        "maxItems": 2,
        "items": {
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "stance_score": {
              "type": "number",
              "minimum": -1.0,
              "maximum": 1.0,
              "description": "Signed directional conviction. Negative = bearish (overextended/richly valued), positive = bullish, 0 = neutral."
            },
            "confidence": {
              "type": "number",
              "exclusiveMinimum": 0.0,
              "maximum": 1.0,
              "description": "Strength of conviction in (0, 1]. Never 0."
            },
            "horizon_days": {
              "type": "integer",
              "minimum": 1,
              "maximum": 365,
              "description": "Holding-period horizon in trading days. ~10 for the short opinion, ~60 for the medium opinion."
            },
            "rationale": {
              "type": "string",
              "minLength": 1,
              "maxLength": 600,
              "description": "One-paragraph justification grounded ONLY in the supplied evidence. No invented facts."
            }
          },
          "required": ["stance_score", "confidence", "horizon_days", "rationale"]
        }
      }
    },
    "required": ["opinions"]
  }
}
```

**Caveat — partial JSON-Schema support (load-bearing).** The 2026 *structured-outputs / strict
tool-use* validator does NOT enforce every keyword: numeric bounds (`minimum`/`maximum`/
`exclusiveMinimum`/`multipleOf`) and string bounds (`minLength`/`maxLength`) and most array-length
constraints are **NOT** hard-enforced (they are documented as unsupported for strict validation; the
SDK strips/validates some client-side). Treat `minItems/maxItems`, `minimum/maximum`, and
`minLength/maxLength` as **hints to the model**, not guarantees. Therefore:
- Keep them in the schema (they steer the model and document intent), **and**
- Re-validate in `judge.py`: assert `len(opinions) == 2` (degrade gracefully if 1 — see degradation),
  clamp `stance ∈ [-1, 1]`, clamp `confidence ∈ (0, 1]` (e.g. `max(min(c, 1.0), 1e-6)`), coerce
  `horizon_days` into the SHORT(10)/MEDIUM(60) buckets per the spec.
- Do NOT rely on `strict: true` to enforce the numeric ranges. (You MAY add `strict: true` to get
  guaranteed *type/shape* validity — `additionalProperties:false` + `required` are honored — but the
  ranges still need Python clamping. If you add `strict`, every object needs `additionalProperties:
  false` + a complete `required`, which the schema above already has.)

### A.3 — Parsing the response

```python
def parse_emit_opinions(resp) -> list[dict]:
    # 1. Forced tool_choice => stop_reason should be "tool_use".
    if resp.stop_reason != "tool_use":
        # forced tool but no tool call: refusal / max_tokens / unexpected.
        # stop_reason "refusal" => check resp.stop_details; "max_tokens" => raise/bump budget.
        return []                      # judge maps empty -> A2 abstains (§2.6)

    # 2. Find the tool_use block whose .name == "emit_opinions".
    blocks = [b for b in resp.content
              if b.type == "tool_use" and b.name == "emit_opinions"]
    if not blocks:
        return []                      # malformed / missing tool_use -> abstain

    payload = blocks[0].input          # already a parsed dict (SDK gives dict, not a JSON string)
    opinions = payload.get("opinions")
    if not isinstance(opinions, list) or not opinions:
        return []
    return opinions                    # caller validates/clamps each (A.2)
```

Key facts (confirmed against the SDK docs):
- The response `content` is a **list of content blocks**; the structured args live on the
  `tool_use` block's **`.input`**, which the Python SDK exposes as an **already-parsed dict**
  (not a JSON string). No `json.loads` needed.
- **`stop_reason`** to handle: with forced `tool_choice` you normally get `"tool_use"`. Also
  guard `"max_tokens"` (output truncated — bump `max_tokens` and retry, or treat as abstain) and
  `"refusal"` (safety decline; `resp.stop_details.category` is populated only then — `stop_details`
  is `null` for every other stop reason, so guard before reading it).
- **Malformed / missing tool_use** → return `[]`. Per §2.6, the judge returns `{opinions: [], run_id}`
  on any failure and the arbiter client fails-closed on empty ("A2 abstained"). So "fail to empty"
  is the correct, safe behavior — never fabricate opinions.
- Because tool inputs may carry different Unicode/forward-slash escaping on 4.x models, ALWAYS read
  `.input` as the parsed dict the SDK gives you; never raw-string-match the serialized tool input.

### A.4 — Cost / token ballpark (`claude-sonnet-4-6`)

Pricing: **$3.00 / 1M input tokens, $15.00 / 1M output tokens** (catalog, cached 2026-06-04).

For a ~1–2k-token evidence prompt:
- Input: 2,000 tok × $3/1M ≈ **$0.006**.
- Output: two small opinion objects (two rationales ≤600 chars ≈ ~300–500 tok total + JSON
  scaffolding) → set `max_tokens=1024`; realistic output ~400–700 tok → 700 × $15/1M ≈ **$0.0105**.
- **Per call ≈ $0.015–$0.018.** Effectively free at A2's volume.

Volume is tiny and the service caches Claude to **≤ once per ticker per day** (§3.5 cache keyed by
`(ticker, as_of.date(), evidence_fingerprint)`), so daily Claude spend is bounded by the universe
size × one call. Even 100 tickers/day ≈ **$1.50/day**.

**Does `tool_choice` forcing add tokens?** Marginally. Tool *definitions* (the `emit_opinions`
schema) are rendered into the request as input tokens — the schema above is small (~250–400 tokens).
Forcing the tool (`tool_choice`) itself adds negligible overhead and, if anything, *reduces* output
tokens (the model emits only the structured args, no prose preamble). Set a small `max_tokens` (1024)
since the structured payload is bounded.

### A.5 — 2026 gotchas

1. **`tool_choice` forcing.** Use `{"type": "tool", "name": "emit_opinions"}`. Add
   `disable_parallel_tool_use: True` to guarantee exactly one tool call (parallel tool use is ON by
   default in 2026; with a forced single emitter you want it OFF so you never get two `tool_use`
   blocks to reconcile).
2. **JSON-Schema constraints are not all enforced** (see A.2). Numeric/length bounds are hints →
   clamp in Python. This is the single biggest correctness trap.
3. **`max_tokens` guidance.** Don't lowball — a truncated tool_use (`stop_reason == "max_tokens"`)
   yields partial/invalid JSON. 1024 is comfortable for two bounded opinions; treat `max_tokens`
   stop as abstain-or-retry.
4. **Thinking off.** Omit `thinking`. `budget_tokens` is deprecated on Sonnet 4.6 (use adaptive if
   ever needed) — but for the judge, no thinking is correct and cheapest.
5. **`.input` is a dict.** No `json.loads`. Parse defensively; on any shape mismatch, abstain (`[]`).
6. **FakeLLM contract.** Per §3.4/§6, the `judge(pack, *, llm=AnthropicClient|FakeLLM)` seam must let
   a `FakeLLM` return a canned response whose shape mirrors a real `messages.create` result — i.e. an
   object with `.stop_reason == "tool_use"` and `.content == [tool_use_block(name="emit_opinions",
   input={"opinions": [...]})]`. Mirror the real block shape so the same parse path runs offline.
7. **Stay on the non-beta path.** Forced tool-use needs no beta header; use plain
   `client.messages.create(...)` (not `client.beta.messages.*`).

### A.6 — Citations
- Tool-use overview: `https://docs.claude.com/en/docs/agents-and-tools/tool-use/overview`
  (equivalently `https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview.md`)
- Implement tool use (tool_choice, forcing, parsing tool_use blocks):
  `https://docs.claude.com/en/docs/agents-and-tools/tool-use/implement-tool-use`
- Structured outputs / strict tool use + JSON-Schema limitations:
  `https://platform.claude.com/docs/en/build-with-claude/structured-outputs.md`
- Models & pricing (model id `claude-sonnet-4-6`, $3/$15 per MTok):
  `https://platform.claude.com/docs/en/about-claude/models/overview.md`
  Verified against the local `claude-api` skill (model catalog cached 2026-06-04) — model id, pricing,
  tool_choice shapes, and the parsed-`.input` dict all match.

### A.7 — For the planner (Claude tool-use decisions)
- **Call:** `client.messages.create(model="claude-sonnet-4-6", max_tokens=1024, system=..., messages=[{"role":"user","content":pack_text}], tools=[EMIT_OPINIONS_TOOL], tool_choice={"type":"tool","name":"emit_opinions","disable_parallel_tool_use":True})`. Non-beta path. No `thinking`.
- **Tool:** one tool `emit_opinions`; `input_schema` = `{opinions: array(minItems=2,maxItems=2) of {stance_score[-1,1], confidence(0,1], horizon_days:int, rationale:str}}` (exact JSON in A.2). Optionally add `strict: true` for guaranteed shape — but it does NOT enforce numeric ranges.
- **Parse:** check `stop_reason == "tool_use"` → take the `tool_use` block named `emit_opinions` → its `.input` is an already-parsed dict → `payload["opinions"]`. On any miss/malformed/refusal/max_tokens → return `[]` (A2 abstains; arbiter fails-closed). Then re-validate in Python: assert len==2 (degrade to 1 allowed per §2.6 degradation), clamp stance∈[-1,1], confidence∈(0,1], bucket horizons to SHORT=10/MEDIUM=60.
- **Cost:** ~$0.015–0.018/call on `claude-sonnet-4-6` ($3 in/$15 out per MTok); cached ≤1×/ticker/day → trivially cheap. Tool schema adds ~250–400 input tokens; forcing reduces output tokens.
- **Gotchas:** JSON-Schema numeric/length bounds are NOT hard-enforced → clamp in code; disable parallel tool use; `.input` is a dict (no json.loads); guard `max_tokens`/`refusal` stop reasons; FakeLLM must mimic the `.stop_reason`/`.content[tool_use].input` shape.

---

## Section B — Standalone Alpaca daily-bars client (`mirofish/clients/alpaca.py`)

The service **must not import arbiter**. This section mirrors the hard-won correctness of
`arbiter/arbiter/data/sources/alpaca.py` + `_gateway.py` as a spec for a fresh thin client.

### B.1 — Endpoint + params (daily bars)

From `arbiter/.../alpaca.py`:
- **Base URL:** `https://data.alpaca.markets` (arbiter config default `alpaca_data_base_url`).
- **Path:** `/v2/stocks/{ticker}/bars`.
- **Params:**
  - `start` — ISO-8601 UTC, format `"%Y-%m-%dT%H:%M:%SZ"` (e.g. `2026-01-15T00:00:00Z`).
  - `end` — same format. Exclusive end; arbiter additionally drops bars with `timestamp >= end`.
  - `timeframe` = `"1Day"`.
  - `adjustment` = `"split"` (split-adjusted; NOT total-return — matches arbiter).
  - `limit` = `10000`.
  - **`feed` = `"iex"`** ← the free-tier requirement (see B.2).
  - `page_token` — set on subsequent pages from the previous response's `next_page_token`.

### B.2 — `feed=iex` free-tier requirement (load-bearing)

Direct quote from the arbiter source (lines 103–106):
> "Data feed: the FREE Alpaca plan only allows `iex`; omitting it defaults to `sip` (paid) and
> returns **403 Forbidden**. Override via `ALPACA_DATA_FEED` (e.g. `sip`) if you have a paid
> market-data subscription."

So: **always send `feed=iex`** on the free tier. Omitting `feed` → server defaults to `sip` → 403.
Mirror the env override: `feed = os.getenv("ALPACA_DATA_FEED", "iex")`. (IEX is a single-exchange
feed; coverage/volume differ from SIP but it's sufficient for the A2 technical features.)

### B.3 — 429 backoff (this session hit Alpaca 429s repeatedly)

**Gap in the arbiter source:** arbiter's `bars()` does NOT special-case 429 — it calls
`resp.raise_for_status()`, which raises `httpx.HTTPStatusError` on 429 and lets it propagate
(the `_FallbackPriceAdapter` then falls back to Stooq). It handles only 404/422 (→ empty) and
network `RequestError` (→ empty). There is **no built-in retry/backoff for 429** in arbiter.

Because mirofish has no Stooq fallback and this session hit 429s repeatedly, the standalone client
**must add explicit 429 backoff** that arbiter lacks. Spec:
- On `resp.status_code == 429`: read the **`Retry-After`** response header (seconds) if present;
  otherwise use exponential backoff with jitter: `delay = min(base * 2**attempt + rand(0,1), cap)`
  (e.g. `base=1.0`, `cap=60.0`, `max_retries=5`).
- Sleep, then retry the **same** request (same params incl. `page_token`).
- After `max_retries` exhausted → give up (return `[]` / raise — caller degrades; per §2.6 the
  service degrades, and `compute_technical` simply gets no bars → no technical opinion).
- Keep the existing soft-fail behavior for 404/422 (delisted/unknown → `[]`) and network errors.
- Optionally also back off on `5x` and `529` (Alpaca/transient).

(For reference, the arbiter SDK-style pattern elsewhere uses exp backoff with `Retry-After`; mirror
that here since we can't lean on the SDK's auto-retry — this is a raw `httpx` client.)

### B.4 — Auth headers

From arbiter (lines 97–101): Alpaca v2 uses **two custom headers**, not Bearer:
- `APCA-API-KEY-ID`: the API key  → from **`ALPACA_API_KEY`** env.
- `APCA-API-SECRET-KEY`: the secret → from **`ALPACA_SECRET_KEY`** env.
- `Accept: application/json`.

mirofish should read `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` straight from env (the same names arbiter
uses) via `mirofish/config.py` — no arbiter import.

### B.5 — Response shape + parsing

- JSON body: `{"bars": [ {bar}, ... ], "next_page_token": str|null}`.
  `data.get("bars") or []` (key may be absent/empty).
- Each bar dict keys (Alpaca v2): **`t`** (RFC-3339 timestamp, e.g. `2026-01-15T00:00:00Z`),
  **`o`** open, **`h`** high, **`l`** low, **`c`** close, **`v`** volume.
- Timestamp parse: `datetime.fromisoformat(t.replace("Z", "+00:00"))` (py3.11+); fallback
  `strptime(t.rstrip("Z"), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=utc)`.
- Pagination: loop while `next_page_token` is truthy, setting `params["page_token"]` each iteration.
- **Look-ahead guard:** arbiter appends a bar only if `bar.timestamp < end_utc`. mirofish needs a
  PIT guard too (B.6).
- Sort ascending by timestamp before returning.

### B.6 — What the minimal `mirofish/clients/alpaca.py` needs (spec, not code)

Per §3.1, `compute_technical(bars, as_of)` needs daily OHLCV bars **≤ as_of**, with enough history
for the longest feature (200-day MA → fetch ≥ ~300 calendar days of lookback to be safe).

Minimal surface:
- `class AlpacaBarsClient` (or a single function `daily_bars(ticker, start, end, *, feed, retries)`).
  - Construct with `api_key`, `secret_key`, `timeout`, `feed` (default `iex`), backoff params —
    all from `mirofish/config.py` (env: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_DATA_FEED`).
  - `bars(ticker, start, end) -> list[Bar]`:
    - Hit `/v2/stocks/{ticker}/bars` with the params in B.1, `feed` in B.2.
    - **429 backoff** per B.3 (the piece arbiter is missing).
    - 404/422 → `[]`; network error → `[]`.
    - Paginate; parse per B.5; **drop bars with `timestamp >= end`** (strict `<` guard).
    - Sort ascending; return.
  - A `bars_as_of(ticker, as_of, lookback_days=~300) -> list[Bar]`:
    - `start = as_of - lookback`, `end = as_of + 1 day` (so a bar timestamped at `as_of` 00:00:00Z is
      included by the strict `< end` guard — mirror arbiter's `as_of + timedelta(days=1)` trick;
      do NOT use `+1 second`).
    - Then filter `eligible = [b for b in fetched if b.timestamp <= as_of]` — explicit PIT guard so
      `compute_technical` only ever sees bars ≤ as_of (§3.1 PIT discipline).
- A tiny local `Bar` dataclass (`ticker, timestamp, open, high, low, close, volume`) — **vendored**
  into mirofish, NOT imported from `arbiter.data.pit`. (Same fields; independent definition keeps the
  HTTP-only isolation honest.)
- **No** `get_pit`/`PITGateway` adapter, **no** Stooq fallback, **no** ADV path — those are arbiter
  concerns. mirofish only needs the raw daily-bar fetch for the technical-feature layer.

### B.7 — For the planner (Alpaca client decisions)
- **Endpoint:** `GET https://data.alpaca.markets/v2/stocks/{ticker}/bars` with
  `timeframe=1Day, adjustment=split, limit=10000, feed=iex, start/end` (ISO `%Y-%m-%dT%H:%M:%SZ`),
  paginate via `page_token` ← `next_page_token`.
- **`feed=iex` is mandatory on the free tier** — omitting it → SIP → **403**. Env override
  `ALPACA_DATA_FEED` (default `iex`).
- **Add 429 backoff (arbiter lacks it).** Honor `Retry-After`; else exp-backoff+jitter
  (base 1s, cap 60s, ~5 retries) and retry the same paged request. Keep 404/422→`[]` and
  network-error→`[]` soft-fails. No Stooq fallback in mirofish.
- **Auth:** headers `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` (+ `Accept: application/json`), from
  env `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`.
- **Response:** `{bars:[{t,o,h,l,c,v}], next_page_token}`; parse `t` via `fromisoformat(Z→+00:00)`.
- **PIT:** fetch `[as_of - ~300d, as_of + 1 day)`, drop `timestamp >= end`, then filter
  `timestamp <= as_of`. Vendor a local `Bar` dataclass — do NOT import arbiter.
- **Surface:** thin `AlpacaBarsClient.bars()` + `bars_as_of()`; no PITGateway, no ADV, no Stooq.
