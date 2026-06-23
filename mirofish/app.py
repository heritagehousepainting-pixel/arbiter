"""FastAPI service for the MiroFish A2 brain.

`create_app()` wires the evidence clients (Build A), the judge + LLM (Build B),
the TTL cache, and the frozen `/analyze` route flow into a FastAPI app. Clients
and the LLM are built from `Config` when not injected; tests inject stubs /
`FakeLLM` so the whole suite runs offline.

Hard guarantees (plan §3.3, §7):
  - `/analyze` NEVER 500s — any exception anywhere degrades to a schema-valid
    `{opinions: [], run_id}` (abstain), matching the arbiter client's
    fail-closed contract.
  - The service binds loopback only; a non-loopback host is refused at startup.
  - `run_id` is a fresh `uuid4().hex` per call (cache hits reuse opinions but
    NOT the run_id).
  - No `datetime.now()` in the request path; `as_of` is always caller-supplied.

ISOLATION: never imports arbiter. The arbiter reaches this service over
loopback HTTP only.
"""
from __future__ import annotations

import uuid

from fastapi import FastAPI

from mirofish.cache import OpinionCache, cache_key
from mirofish.clients.alpaca import AlpacaBarsClient
from mirofish.clients.sec_facts import SecFactsClient
from mirofish.config import LOOPBACK_HOSTS, Config
from mirofish.data.sector_valuation import SECTOR_MAP
from mirofish.evidence.fundamentals import compute_fundamentals
from mirofish.evidence.pack import build_pack
from mirofish.evidence.technical import compute_technical
from mirofish.judge import judge
from mirofish.llm import AnthropicLLM, FakeLLM
from mirofish.types import (
    AnalyzeRequest,
    AnalyzeResponse,
    ensure_utc,
    opinion_to_model,
)


def new_run_id() -> str:
    """Fresh per-call run id. uuid4 hex (no arbiter ULID import)."""
    return uuid.uuid4().hex


def _abstain() -> AnalyzeResponse:
    """Schema-valid empty response (the fail-closed degradation target)."""
    return AnalyzeResponse(opinions=[], run_id=new_run_id())


def create_app(
    config: Config | None = None,
    *,
    alpaca: AlpacaBarsClient | None = None,
    sec: SecFactsClient | None = None,
    llm: object | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    Real clients / LLM are constructed from `config` when not injected
    (`FakeLLM` when `config.fake_llm`, else `AnthropicLLM`). Tests inject stub
    clients (or a client built on the `transport=` MockTransport seam) and a
    `FakeLLM`.

    Refuses to start if `config.host` is not a loopback address.
    """
    config = config or Config.from_env()

    if config.host not in LOOPBACK_HOSTS:
        raise RuntimeError(
            f"refusing non-loopback host {config.host!r}: MiroFish A2 must bind "
            f"loopback only (one of {sorted(LOOPBACK_HOSTS)})"
        )

    # ---- wire clients / LLM (inject for tests, else build from config) ----
    if alpaca is None:
        alpaca = AlpacaBarsClient(
            api_key=config.alpaca_api_key or "",
            secret_key=config.alpaca_secret_key or "",
            feed=config.alpaca_data_feed,
        )
    if sec is None:
        sec = SecFactsClient(user_agent=config.edgar_user_agent or "")
    if llm is None:
        llm = (
            FakeLLM()
            if config.fake_llm
            else AnthropicLLM(api_key=config.anthropic_api_key)
        )

    cache = OpinionCache(config.cache_ttl_seconds)

    app = FastAPI(title="MiroFish A2 Brain", version="0.1.0")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/analyze", response_model=AnalyzeResponse)
    def analyze(req: AnalyzeRequest) -> AnalyzeResponse:
        try:
            # 1. Normalize as_of -> tz-aware UTC.
            as_of = ensure_utc(req.as_of)
            ticker = req.ticker

            # 2. Bars (PIT). Empty -> no technical evidence -> abstain.
            bars = alpaca.bars_as_of(ticker, as_of)
            if not bars:
                return _abstain()

            # 3. Technical features.
            tech = compute_technical(bars, as_of)
            last_close = tech.last_close

            # 4. Fundamentals (may be None).
            fund = compute_fundamentals(
                ticker,
                as_of,
                client=sec,
                last_close=last_close,
                sector_map=SECTOR_MAP,
            )

            # 5. Evidence pack (fills source_fingerprint).
            pack = build_pack(ticker, as_of, tech, fund)

            # 6. Cache lookup. Hit -> reuse opinions with a FRESH run_id,
            #    no LLM call.
            key = cache_key(ticker, as_of, pack.source_fingerprint)
            cached = cache.get(key)
            if cached is not None:
                return AnalyzeResponse(
                    opinions=[opinion_to_model(o) for o in cached],
                    run_id=new_run_id(),
                )

            # 7. Miss -> judge. Bind the pack so `--fake-llm` is deterministic.
            if isinstance(llm, FakeLLM):
                llm.bind_pack(pack)
            opinions = judge(pack, model=config.model, llm=llm)

            # Write-once only when results exist (mirrors arbiter cache rule).
            if opinions:
                cache.put(key, opinions)

            # 8. Build response.
            return AnalyzeResponse(
                opinions=[opinion_to_model(o) for o in opinions],
                run_id=new_run_id(),
            )
        except Exception:
            # 9. ANY exception anywhere -> abstain, never a 500.
            return _abstain()

    return app
