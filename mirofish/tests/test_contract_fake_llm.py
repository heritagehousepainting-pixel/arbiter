"""Byte-for-byte contract test against the arbiter client's expectations.

SELF-CONTAINED: we do NOT `import arbiter` (isolation rule). Instead we
replicate inline exactly what `arbiter.adapters.mirofish.adapter`
(`_opinions_from_response` + `arbiter.contract.opinion.validate_opinion`)
requires of our `/analyze` response body, and prove a NEGATIVE stance survives
end-to-end unchanged (the client never abs()/floors stance_score).

Replicated arbiter contract (from adapter.py + opinion.validate_opinion):
  - each opinion MUST have `stance_score` (KeyError if missing)
  - each opinion MUST have `confidence` (KeyError if missing)
  - each opinion MUST have `horizon_days` (KeyError if missing)
  - `rationale` optional (defaults ""), `source_fingerprint` optional
  - stance_score in [-1.0, 1.0]; NEGATIVE passes through unchanged
  - confidence in [0.0, 1.0]
  - 0 < horizon_days <= 365
  - top-level `opinions` is a list; `run_id` present
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from mirofish.app import create_app
from mirofish.config import Config
from mirofish.llm import FakeLLM
from mirofish.types import Bar

AS_OF = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)


class StubAlpaca:
    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars

    def bars_as_of(self, ticker, as_of, *, lookback_days: int = 300):
        return list(self._bars)


class StubSec:
    def facts_as_of(self, ticker, as_of):
        return None  # no fundamentals -> SHORT opinion only (carries our negative)


def _bars(n: int = 220) -> list[Bar]:
    out: list[Bar] = []
    for i in range(n):
        t = AS_OF - timedelta(days=(n - 1 - i))
        c = 100.0 + i * 0.1
        out.append(Bar(t=t, o=c, h=c, l=c, c=c, v=1_000_000.0))
    return out


# Replicated arbiter-side validation (NO arbiter import). ------------------- #
def _validate_like_arbiter(opinion: dict) -> dict:
    """Mirror adapter._opinions_from_response + validate_opinion ranges.

    Raises KeyError/ValueError exactly where the arbiter client would skip an
    opinion. Returns the coerced values (stance preserved, never abs'd).
    """
    stance = float(opinion["stance_score"])   # REQUIRED — KeyError if missing
    confidence = float(opinion["confidence"])  # REQUIRED
    horizon = int(opinion["horizon_days"])     # REQUIRED
    rationale = str(opinion.get("rationale", ""))            # optional
    source_fp = str(opinion.get("source_fingerprint", ""))  # optional

    if stance < -1.0 or stance > 1.0:
        raise ValueError(f"stance_score out of range: {stance}")
    if confidence < 0.0 or confidence > 1.0:
        raise ValueError(f"confidence out of range: {confidence}")
    if horizon <= 0 or horizon > 365:
        raise ValueError(f"horizon_days out of range: {horizon}")

    return {
        "stance_score": stance,  # NOT abs'd / floored — passthrough
        "confidence": confidence,
        "horizon_days": horizon,
        "rationale": rationale,
        "source_fingerprint": source_fp,
    }


def test_response_satisfies_arbiter_contract_with_negative_stance() -> None:
    # FakeLLM emits an explicit NEGATIVE short stance.
    negative_payload = [
        {
            "stance_score": -0.73,  # bearish — must survive unchanged
            "confidence": 0.62,
            "horizon_days": 10,
            "rationale": "Overextended into resistance; bearish short read.",
        },
        {
            "stance_score": 0.1,
            "confidence": 0.5,
            "horizon_days": 60,
            "rationale": "Mild medium-term view.",
        },
    ]
    app = create_app(
        Config(fake_llm=True),
        alpaca=StubAlpaca(_bars()),
        sec=StubSec(),
        llm=FakeLLM(opinions=negative_payload),
    )
    client = TestClient(app)

    resp = client.post(
        "/analyze",
        json={"ticker": "AAPL", "as_of": AS_OF.isoformat(), "idea_fingerprint": "deadbeef"},
    )
    assert resp.status_code == 200
    body = resp.json()

    # Top-level shape the client reads.
    assert isinstance(body, dict)
    assert isinstance(body["opinions"], list)
    assert isinstance(body.get("run_id"), str) and body["run_id"]

    # No fundamentals -> exactly the SHORT opinion (carrying the negative stance).
    assert len(body["opinions"]) == 1

    coerced = [_validate_like_arbiter(o) for o in body["opinions"]]
    short = coerced[0]

    # THE load-bearing assertion: negative stance survived end-to-end unchanged.
    assert short["stance_score"] == -0.73
    assert short["stance_score"] < 0.0
    assert short["horizon_days"] == 10
    # source_fingerprint is OUR pack fingerprint (16 hex), not the idea fp.
    assert short["source_fingerprint"]
    assert short["source_fingerprint"] != "deadbeef"
    assert len(short["source_fingerprint"]) == 16


def test_two_opinion_body_all_keys_present_and_in_range() -> None:
    """With fundamentals present, both opinions must satisfy the client keys."""
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [
                    {"start": "2025-01-01", "end": "2025-12-31",
                     "val": 1_000_000_000, "filed": "2026-02-01"},
                ]}},
                "NetIncomeLoss": {"units": {"USD": [
                    {"start": "2025-01-01", "end": "2025-12-31",
                     "val": 200_000_000, "filed": "2026-02-01"},
                ]}},
            },
            "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                {"end": "2025-12-31", "val": 100_000_000, "filed": "2026-02-01"},
            ]}}},
        }
    }

    class StubSecWithFacts:
        def facts_as_of(self, ticker, as_of):
            return facts

    app = create_app(
        Config(fake_llm=True),
        alpaca=StubAlpaca(_bars()),
        sec=StubSecWithFacts(),
        llm=FakeLLM(opinions=[
            {"stance_score": -0.4, "confidence": 0.5, "horizon_days": 10, "rationale": "a"},
            {"stance_score": 0.3, "confidence": 0.5, "horizon_days": 60, "rationale": "b"},
        ]),
    )
    client = TestClient(app)
    resp = client.post(
        "/analyze",
        json={"ticker": "AAPL", "as_of": AS_OF.isoformat(), "idea_fingerprint": "fp"},
    )
    assert resp.status_code == 200
    opinions = resp.json()["opinions"]
    assert len(opinions) == 2
    coerced = [_validate_like_arbiter(o) for o in opinions]
    assert coerced[0]["stance_score"] == -0.4  # negative survives
    assert coerced[0]["horizon_days"] == 10
    assert coerced[1]["horizon_days"] == 60
