# MiroFish A2 "Brain" — Design Spec

**Date:** 2026-06-21
**Status:** APPROVED (design); pending user spec-review → implementation plan.
**Goal:** Build *our own* A2 "second brain" — a standalone localhost inference service that
implements the **already-frozen** arbiter MiroFish contract, giving the decision engine an
independent, fundamentals-aware analyst that can disagree with A1 (including going **bearish**).

---

## 0. Why this exists (context)
Arbiter's A1 advisor family follows *disclosed* smart money (insiders, Congress, 13D/13G
activists) and only ever says "buy." A2 is the **counterweight**: an independent view formed
from price action + fundamentals that can say "the insider bought, but this is overextended /
richly valued → fade or low-confidence." Without a genuine second opinion, the fusion/learning
loop has nothing to weigh A1 against. The arbiter client (`arbiter/adapters/mirofish/`) is built
and inert; this service is what it talks to.

## 1. The frozen contract (do NOT change — the client already speaks it)
```
POST  http://<loopback>:<port>/analyze
req:  { "ticker": str, "as_of": ISO-8601-UTC, "idea_fingerprint": str }
resp: { "opinions": [ { "stance_score": float[-1,1],   # negative = bearish (REQUIRED capability)
                        "confidence":  float(0,1],
                        "horizon_days": int,
                        "rationale":   str,
                        "source_fingerprint": str } ],
        "run_id": str }
```
- **Loopback only.** The arbiter egress firewall rejects any non-localhost `MIROFISH_ENDPOINT`.
- **Forward-test-only.** The arbiter run-cache raises in backtests, so A2 never runs over history
  → an LLM in the loop is acceptable (no historical replay/determinism requirement on the LLM).
- The service receives **only `ticker` + `as_of`** (the `idea_fingerprint` is opaque, used as a
  fallback `source_fingerprint`). A2 forms its view independently — it never sees A1's thesis.

## 2. Decisions locked in brainstorming
1. **Brain = hybrid**: a deterministic feature/evidence layer → Claude judgment.
2. **Evidence = technical (Alpaca) + fundamentals (SEC `companyfacts` XBRL)**, strictly
   point-in-time. *(Updated 2026-06-21 after the research wave: Simfin's free tier is inadequate —
   ratios paywalled, bulk/credit-metered. SEC `companyfacts` is free, needs no new key (reuses the
   already-set `EDGAR_USER_AGENT`), and carries the true `filed` date for a robust `filed <= as_of`
   PIT filter. See `docs/specs/research/2026-06-21-simfin-fundamentals-feasibility.md`. Simfin kept
   only as an optional future paid cross-check.)*
3. **Output = two opinions** per call: a SHORT-horizon (~10d) technical-led opinion and a
   MEDIUM-horizon (~60d) fundamental-led opinion (distinct horizon buckets → they don't fight in
   fusion; each is calibrated separately by the learning loop).
4. **★ Model** = `claude-sonnet-4-6` default (configurable up to Opus). Volume is tiny + cached.
5. **★ Packaging** = a separate top-level `mirofish/` package (NOT under `arbiter/`), run as its
   own `uvicorn` process — keeps the "never import mirofish; HTTP-only" isolation honest.
6. **★ Degradation**: fundamentals unavailable → emit only the technical (short) opinion;
   Claude call fails → return `{opinions:[], run_id}` (the arbiter client already fails-closed on
   empty, so this is safe and surfaces as "A2 abstained").

## 3. Architecture (small, independently-testable units)
All under a new top-level `mirofish/` package. Each unit states: what it does / how you call it /
what it depends on.

### 3.1 `mirofish/evidence/technical.py`
- **Does:** turn Alpaca daily bars (≤ `as_of`) into a typed `TechnicalFeatures`: trend vs 50/200-day
  MA, 20d momentum, RSI-14 (overbought/oversold), annualized realized vol, distance from 52-week
  high/low, recent volume surge ratio.
- **Call:** `compute_technical(bars: list[Bar], as_of) -> TechnicalFeatures`.
- **Depends:** an Alpaca bars fetch (reuse the arbiter pattern; this service has its own thin client
  — no import of arbiter). Pure given bars → fully offline-unit-testable.
- **PIT:** caller passes only bars with timestamp ≤ `as_of`.

### 3.2 `mirofish/evidence/fundamentals.py`  (+ new SEC `companyfacts` client)
- **Does:** fetch fundamentals from **SEC `companyfacts`** (XBRL JSON) and compute a typed
  `FundamentalFeatures`: revenue growth YoY, gross margin, operating margin, P/E & P/S (derived via
  Alpaca price × shares-outstanding ÷ earnings/revenue), and a **valuation-vs-sector** z-score.
- **PIT discipline (load-bearing):** each XBRL fact carries a **`filed` date**; use only facts with
  `filed <= as_of`. This is the true public-disclosure date (strictly better than a fiscal-period
  end), so there is **no reporting-lag heuristic needed** (resolves §9.1). Enforced even though A2 is
  live-only, so a future backtest path can never leak.
- **Call:** `compute_fundamentals(ticker, as_of, *, client, sector_map) -> FundamentalFeatures | None`
  (`None` when SEC lacks coverage / the company doesn't report standard US-GAAP tags → triggers the
  degradation rule).
- **Depends:** a new `mirofish/clients/sec_facts.py` — `GET data.sec.gov/api/xbrl/companyfacts/
  CIK{10}.json`, ticker→CIK via `company_tickers.json`, `EDGAR_USER_AGENT` header (already set), 10
  req/s; reads US-GAAP tags (`RevenueFromContractWithCustomer*`/`Revenues`, `GrossProfit`,
  `OperatingIncomeLoss`, `NetIncomeLoss`, shares via `dei`/`CommonStockSharesOutstanding`). Plus the
  Alpaca price (for ratios) and a small vendored sector list (no arbiter import).

### 3.3 `mirofish/evidence/pack.py`
- **Does:** assemble `TechnicalFeatures` (+ optional `FundamentalFeatures`) into an `EvidencePack`
  and compute `source_fingerprint = sha256(canonical_json(evidence))[:16]` so identical evidence
  dedups and is auditable.
- **Call:** `build_pack(ticker, as_of, tech, fund|None) -> EvidencePack`.

### 3.4 `mirofish/judge.py`  (the Claude layer)
- **Does:** render the `EvidencePack` into a disciplined analyst prompt and call Claude via
  **structured tool-use** (a single `emit_opinions` tool whose schema is the two-opinion array), so
  output is reliably typed. Parse → validate (`stance ∈ [-1,1]`, `confidence ∈ (0,1]`, clamp; coerce
  horizons into the SHORT/MEDIUM buckets) → `list[Opinion]`.
- **Prompt rules:** the analyst is told it is an INDEPENDENT skeptic; it **may and should** return
  negative stances on overextended/richly-valued names; it must ground the rationale in the supplied
  evidence (no invented facts); short opinion = technical-led, medium = fundamental-led; if
  fundamentals are absent, emit only the short opinion.
- **Call:** `judge(pack: EvidencePack, *, model, llm=AnthropicClient|FakeLLM) -> list[Opinion]`.
- **Depends:** `ANTHROPIC_API_KEY`; the model id (`claude-sonnet-4-6` default). **Mockable** — a
  `FakeLLM` returns canned structured output for offline tests + `--fake-llm` mode.

### 3.5 `mirofish/app.py`  (FastAPI service)
- **Does:** `POST /analyze` → validate request (pydantic) → orchestrate
  `technical → fundamentals → pack → judge` → assemble `{opinions, run_id}`; `GET /health`.
- **Cache:** service-side cache keyed by `(ticker, as_of.date(), evidence_fingerprint)` so Claude is
  called **≤ once per ticker per day**; returns the cached opinions (with a fresh `run_id`) on hit.
- **Binding:** binds `127.0.0.1` only; refuses to start on a non-loopback host.
- **Errors:** any failure in evidence/judge degrades per §2.6; the endpoint always returns a
  schema-valid body (never a 500 that would just make the arbiter client fail-closed anyway, though
  the client handles that too).
- **Call:** `uvicorn mirofish.app:app --host 127.0.0.1 --port 8900` (configurable).

### 3.6 `mirofish/config.py`
- Loads `ANTHROPIC_API_KEY`, `EDGAR_USER_AGENT` (for SEC facts), `ALPACA_API_KEY`/`ALPACA_SECRET_KEY`
  (bars), `MIROFISH_MODEL` (default `claude-sonnet-4-6`), `MIROFISH_PORT` (8900), cache TTL, the two
  horizon constants (SHORT=10, MEDIUM=60), and a `MIROFISH_FAKE_LLM` flag. Secret-redacting repr.

## 4. Data flow
```
arbiter (live cycle) --POST /analyze {ticker, as_of}--> mirofish.app
   app: cache lookup (ticker, as_of.date, fp)
     miss → technical(Alpaca bars≤as_of) ─┐
            fundamentals(SEC companyfacts, filed≤as_of) ─┤→ EvidencePack(+source_fingerprint)
                                                 └→ judge(pack) --tool-use--> Claude
     → [short technical opinion, medium fundamental opinion]  (or [short] if no fundamentals)
   ← {opinions, run_id}
arbiter: A2.mirofish opinions enter fusion at hard_weight_cap 0.35, learning loop calibrates them
```

## 5. Independence & negative-stance (the whole point)
- A2 gets only the ticker — it cannot copy A1. Its bearish capability comes from (a) technical
  overextension (RSI, distance-from-high, momentum exhaustion) and (b) rich valuation-vs-sector.
- The judge prompt explicitly rewards disagreement and forbids defaulting to bullish. A
  characterization test asserts a deliberately overbought+rich evidence pack yields a **negative**
  short and/or medium stance via the `FakeLLM` contract.

## 6. Testing (offline-first, mirrors arbiter discipline)
- **Unit (offline, no network, no real clock):** technical features from fixture bars; fundamentals
  PIT filter (a figure reported *after* `as_of` is excluded); pack fingerprint stability; judge
  parsing/validation/clamping with `FakeLLM`; degradation (no-fundamentals → one opinion; LLM error
  → empty).
- **Contract test:** a response matches the arbiter client's expected schema exactly (reuse the
  shapes from `arbiter/adapters/mirofish/`), incl. a **negative stance** passing through.
- **Service test:** FastAPI `TestClient` over `/analyze` + `/health`, cache hit avoids a 2nd
  `FakeLLM` call, non-loopback bind refused.
- **One gated real-Claude smoke test:** skipped unless `ANTHROPIC_API_KEY` is set (no CI cost).

## 7. Setup the USER must provide (→ SETUP_NEEDED.md)
- `ANTHROPIC_API_KEY` — the brain. **The only new key.** Until set, run `--fake-llm` for wiring; no
  real opinions.
- Fundamentals need **no new key** — SEC `companyfacts` reuses the `EDGAR_USER_AGENT` already set
  this session. (Simfin dropped; `SIMFIN_API_KEY` only relevant if you later want a paid cross-check.)
- Reuses the existing `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` for bars (already set).
- After the service is running: set `MIROFISH_ENDPOINT=http://127.0.0.1:8900` in `arbiter/.env`
  to activate A2 (it stays a clean no-op until then).

## 8. Out of scope (YAGNI)
- No news/sentiment ingestion (that's arbiter's separate A3 phase).
- No historical/backtest support for A2 (forward-only by contract).
- No multi-model ensemble, no fine-tuning, no streaming.
- No changes to the arbiter side beyond eventually setting `MIROFISH_ENDPOINT` (already wired).

## 9. Research-wave outcomes (RESOLVED 2026-06-21)
Memos: `docs/specs/research/2026-06-21-simfin-fundamentals-feasibility.md` and
`docs/specs/research/2026-06-21-claude-tooluse-and-alpaca-client.md`.
1. **Fundamentals source — RESOLVED → SEC `companyfacts`, not Simfin.** Simfin free tier inadequate
   (ratios paywalled, credit-metered). SEC `companyfacts` is free, no new key (reuses
   `EDGAR_USER_AGENT`), 10 req/s, full history, and carries the true `filed` date → robust
   `filed <= as_of` PIT. **The ≥45-day reporting-lag fallback is NO LONGER NEEDED.**
2. **Claude structured output — RESOLVED.** Force the tool with
   `tool_choice={"type":"tool","name":"emit_opinions","disable_parallel_tool_use":true}` on
   `claude-sonnet-4-6`, `max_tokens≈1024`. One `emit_opinions` tool; payload `{opinions: array
   minItems=2 maxItems=2}`. JSON-Schema numeric/length bounds are **hints only** → the judge MUST
   re-validate + clamp in Python. Parse: `stop_reason=="tool_use"` → the `emit_opinions` block's
   `.input` dict → `opinions`; any miss/malformed/`max_tokens`/refusal → `[]` (abstain). Cost
   ~$0.015/call, cached ≤1×/ticker/day → trivial.
3. **Alpaca client — RESOLVED.** `GET /v2/stocks/{ticker}/bars?timeframe=1Day&adjustment=split&
   feed=iex&limit=10000` (auth `APCA-API-KEY-ID`/`APCA-API-SECRET-KEY`). **`feed=iex` mandatory**
   (else SIP→403). **Must ADD 429 backoff** — arbiter's own client has none (it leans on a Stooq
   fallback the service won't have): honor `Retry-After`, else exp-backoff+jitter (1s base, 60s cap,
   ~5 retries). Fetch `[as_of−~300d, as_of]`, vendor a local `Bar` dataclass — no arbiter import.

## 10. Build waves (after spec approval)
- **Research** (2 agents): ✅ DONE — §9 resolved (SEC `companyfacts` over Simfin; Claude tool schema;
  Alpaca client).
- **Plan** (1 agent): freeze the module contracts (`EvidencePack`, `Opinion`, the tool schema, the
  service routes) into an implementation plan.
- **Audit-plan** (1 agent).
- **Build** (3 disjoint agents): (A) `evidence/` + the SEC-facts/Alpaca clients; (B) `judge.py` +
  prompt + FakeLLM; (C) `app.py` + cache + config + service tests. Disjoint file ownership, offline
  tests, against the frozen `EvidencePack`/`Opinion`/tool contracts.
- Orchestrator gates the full `mirofish/` test suite after the wave, then an end-to-end `--fake-llm`
  run against the real arbiter client to prove the contract.
