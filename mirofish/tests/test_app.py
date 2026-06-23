"""FastAPI service tests — fully offline (FakeLLM + stub clients).

Covers the frozen route flow (plan §3.3, §6 Build C): happy path, cache-hit
skips the 2nd LLM call, no-fundamentals -> 1 opinion, empty-bars -> abstain,
judge-raises -> empty-but-200, non-loopback refused, /health.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from mirofish.app import create_app
from mirofish.config import Config
from mirofish.llm import FakeLLM
from mirofish.types import (
    AnalyzeResponse,
    Bar,
    FundamentalFeatures,
)

AS_OF = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Stub clients (duck-typed to Build A's frozen signatures).
# --------------------------------------------------------------------------- #
def _ascending_bars(n: int = 220, *, end: datetime = AS_OF) -> list[Bar]:
    """A deterministic ascending OHLCV series ending at `end`, so MA-200 is
    computable. Includes a late upswing so the default FakeLLM canned opinions
    (RSI/52w-high keyed) produce a negative short stance for the rich pack."""
    bars: list[Bar] = []
    for i in range(n):
        t = end - timedelta(days=(n - 1 - i))
        # Mostly flat then a sharp run into the high near the end.
        base = 100.0 + i * 0.05
        if i >= n - 15:
            base += (i - (n - 16)) * 4.0  # push RSI high + near 52w high
        bars.append(Bar(t=t, o=base, h=base * 1.01, l=base * 0.99, c=base, v=1_000_000.0))
    return bars


class StubAlpaca:
    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars

    def bars_as_of(self, ticker: str, as_of: datetime, *, lookback_days: int = 300):
        return list(self._bars)


class StubSec:
    """Returns a canned fundamentals object directly is not how it's wired —
    the route calls compute_fundamentals(client=...). So this stub mimics
    SecFactsClient.facts_as_of returning either a facts dict or None."""

    def __init__(self, facts: dict | None) -> None:
        self._facts = facts

    def facts_as_of(self, ticker: str, as_of: datetime):
        return self._facts


def _rich_companyfacts() -> dict:
    """Minimal SEC companyfacts with revenue + net income + shares, all filed
    before AS_OF, so compute_fundamentals yields a real FundamentalFeatures."""
    return {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {"start": "2025-01-01", "end": "2025-12-31",
                             "val": 400_000_000_000,
                             "filed": "2026-02-01", "form": "10-K"},
                            {"start": "2024-01-01", "end": "2024-12-31",
                             "val": 380_000_000_000,
                             "filed": "2025-02-01", "form": "10-K"},
                        ]
                    }
                },
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            {"start": "2025-01-01", "end": "2025-12-31",
                             "val": 100_000_000_000,
                             "filed": "2026-02-01", "form": "10-K"},
                        ]
                    }
                },
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {"end": "2025-12-31", "val": 15_000_000_000,
                             "filed": "2026-02-01", "form": "10-K"},
                        ]
                    }
                }
            },
        }
    }


def _make_client(*, bars, facts, llm=None, fake_llm=True) -> TestClient:
    cfg = Config(fake_llm=fake_llm)
    app = create_app(
        cfg,
        alpaca=StubAlpaca(bars),
        sec=StubSec(facts),
        llm=llm if llm is not None else FakeLLM(),
    )
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_health() -> None:
    client = _make_client(bars=_ascending_bars(), facts=_rich_companyfacts())
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_analyze_happy_path_two_opinions() -> None:
    client = _make_client(bars=_ascending_bars(), facts=_rich_companyfacts())
    r = client.post(
        "/analyze",
        json={"ticker": "AAPL", "as_of": AS_OF.isoformat(), "idea_fingerprint": "x"},
    )
    assert r.status_code == 200
    body = r.json()
    parsed = AnalyzeResponse.model_validate(body)  # schema-valid
    assert len(parsed.opinions) == 2
    assert parsed.run_id
    for op in parsed.opinions:
        assert -1.0 <= op.stance_score <= 1.0
        assert 0.0 < op.confidence <= 1.0
        assert 0 < op.horizon_days <= 365
        assert op.source_fingerprint
    # Short opinion is SHORT_DAYS, medium is MEDIUM_DAYS.
    assert parsed.opinions[0].horizon_days == 10
    assert parsed.opinions[1].horizon_days == 60


def test_cache_hit_avoids_second_llm_call() -> None:
    fake = FakeLLM()  # counts .create calls
    client = _make_client(
        bars=_ascending_bars(), facts=_rich_companyfacts(), llm=fake
    )
    payload = {"ticker": "AAPL", "as_of": AS_OF.isoformat(), "idea_fingerprint": "x"}

    r1 = client.post("/analyze", json=payload)
    r2 = client.post("/analyze", json=payload)
    assert r1.status_code == r2.status_code == 200

    # Exactly one LLM call across two identical requests (2nd was a cache hit).
    assert fake.create_calls == 1

    b1, b2 = r1.json(), r2.json()
    # Same opinions, but a FRESH run_id.
    assert b1["opinions"] == b2["opinions"]
    assert b1["run_id"] != b2["run_id"]


def test_no_fundamentals_yields_one_opinion() -> None:
    # SEC returns no facts -> compute_fundamentals None -> judge returns SHORT only.
    client = _make_client(bars=_ascending_bars(), facts=None)
    r = client.post(
        "/analyze",
        json={"ticker": "ZZZZ", "as_of": AS_OF.isoformat()},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["opinions"]) == 1
    assert body["opinions"][0]["horizon_days"] == 10


def test_empty_bars_abstains_still_200() -> None:
    client = _make_client(bars=[], facts=_rich_companyfacts())
    r = client.post(
        "/analyze",
        json={"ticker": "AAPL", "as_of": AS_OF.isoformat()},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["opinions"] == []
    assert body["run_id"]


def test_judge_raises_returns_empty_but_200() -> None:
    class ExplodingLLM:
        def create(self, **kwargs):
            raise RuntimeError("boom")

    # judge() swallows the error -> [] ; route returns abstain, never 500.
    client = _make_client(
        bars=_ascending_bars(), facts=_rich_companyfacts(), llm=ExplodingLLM()
    )
    r = client.post(
        "/analyze",
        json={"ticker": "AAPL", "as_of": AS_OF.isoformat()},
    )
    assert r.status_code == 200
    assert r.json()["opinions"] == []


def test_route_never_500_on_internal_exception() -> None:
    """A client that raises (not just judge) inside the flow still -> abstain 200."""
    class ExplodingAlpaca:
        def bars_as_of(self, ticker, as_of, *, lookback_days=300):
            raise RuntimeError("alpaca down hard")

    cfg = Config(fake_llm=True)
    app = create_app(
        cfg, alpaca=ExplodingAlpaca(), sec=StubSec(None), llm=FakeLLM()
    )
    client = TestClient(app)
    r = client.post("/analyze", json={"ticker": "AAPL", "as_of": AS_OF.isoformat()})
    assert r.status_code == 200
    assert r.json()["opinions"] == []


def test_non_loopback_host_refused() -> None:
    with pytest.raises(RuntimeError, match="loopback"):
        create_app(Config(host="0.0.0.0", fake_llm=True))


def test_loopback_hosts_allowed() -> None:
    for host in ("127.0.0.1", "::1", "localhost"):
        app = create_app(
            Config(host=host, fake_llm=True),
            alpaca=StubAlpaca(_ascending_bars()),
            sec=StubSec(None),
            llm=FakeLLM(),
        )
        assert app is not None


def test_fundamental_features_shape_smoke() -> None:
    """Sanity: the rich companyfacts actually produce a FundamentalFeatures so
    the 2-opinion path is genuinely the fundamentals-present branch."""
    from mirofish.evidence.fundamentals import compute_fundamentals

    fund = compute_fundamentals(
        "AAPL", AS_OF, client=StubSec(_rich_companyfacts()),
        last_close=200.0, sector_map={"AAPL": "Technology"},
    )
    assert isinstance(fund, FundamentalFeatures)
    assert fund.revenue_ttm == 400_000_000_000
