"""adapter.run() contract tests — the frozen public entry point.

run(idea, as_of, *, conn, client, breaker, is_backtest) -> list[Opinion]
must NEVER raise and must return a list (``[]`` = abstain).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from arbiter.adapters.mirofish.adapter import (
    ADVISOR_ID,
    _idea_fingerprint,
    run,
)
from arbiter.adapters.mirofish.http_client import MirofishHTTPClient
from arbiter.contract.opinion import validate_opinion
from arbiter.types import ConfidenceSource, HorizonBucket

from .conftest import (
    AS_OF,
    FakeIdea,
    httpx_connection_error,
    make_memory_db,
    mirofish_response,
)


# ===========================================================================
# Happy path
# ===========================================================================


def test_run_returns_valid_opinions_with_shared_run_group_id() -> None:
    idea = FakeIdea()
    run_id = "RUN_SHARED_01"
    mock_client = MagicMock(spec=MirofishHTTPClient)
    mock_client.analyze.return_value = mirofish_response(run_id=run_id)

    opinions = run(idea, AS_OF, client=mock_client)

    assert len(opinions) == 2
    assert {op.run_group_id for op in opinions} == {run_id}
    assert all(op.advisor_id == ADVISOR_ID for op in opinions)
    buckets = {op.horizon_bucket for op in opinions}
    assert HorizonBucket.SHORT in buckets
    assert HorizonBucket.MEDIUM in buckets
    for op in opinions:
        validate_opinion(op)
    assert all(op.as_of == AS_OF for op in opinions)
    assert all(op.ticker == "AAPL" for op in opinions)


def test_run_opinions_have_modeled_confidence_source() -> None:
    mock_client = MagicMock(spec=MirofishHTTPClient)
    mock_client.analyze.return_value = mirofish_response()
    opinions = run(FakeIdea(), AS_OF, client=mock_client)
    assert all(
        op.confidence_source == ConfidenceSource.MODELED for op in opinions
    )


# ===========================================================================
# Fingerprint stability
# ===========================================================================


def test_idea_fingerprint_is_stable() -> None:
    idea = FakeIdea(ticker="MSFT", thesis="Buybacks", horizon_days=30)
    fp1, fp2 = _idea_fingerprint(idea), _idea_fingerprint(idea)
    assert fp1 == fp2
    assert len(fp1) == 64
    assert all(c in "0123456789abcdef" for c in fp1)


def test_idea_fingerprint_differs_across_ideas() -> None:
    assert _idea_fingerprint(FakeIdea(ticker="AAPL")) != _idea_fingerprint(
        FakeIdea(ticker="MSFT")
    )


# ===========================================================================
# NEGATIVE stance passthrough (load-bearing) — do NOT clamp
# ===========================================================================


def test_negative_stance_passes_through_unclamped() -> None:
    """stance_score = -0.7 must reach the Opinion as -0.7 (bearish/SHORT),
    not clamped to 0, not abs'd."""
    mock_client = MagicMock(spec=MirofishHTTPClient)
    mock_client.analyze.return_value = {
        "run_id": "NEG_RUN",
        "opinions": [
            {
                "stance_score": -0.7,
                "confidence": 0.8,
                "horizon_days": 14,
                "rationale": "Bearish: deteriorating fundamentals",
                "source_fingerprint": "fp_neg",
            }
        ],
    }
    opinions = run(FakeIdea(), AS_OF, client=mock_client)
    assert len(opinions) == 1
    assert opinions[0].stance_score == -0.7
    validate_opinion(opinions[0])


def test_negative_stance_boundary_minus_one_passes() -> None:
    mock_client = MagicMock(spec=MirofishHTTPClient)
    mock_client.analyze.return_value = {
        "run_id": "NEG1",
        "opinions": [
            {"stance_score": -1.0, "confidence": 0.5, "horizon_days": 14}
        ],
    }
    opinions = run(FakeIdea(), AS_OF, client=mock_client)
    assert len(opinions) == 1
    assert opinions[0].stance_score == -1.0


def test_stance_below_minus_one_is_skipped() -> None:
    """-1.0001 is out of range → skipped (validate_opinion rejects)."""
    mock_client = MagicMock(spec=MirofishHTTPClient)
    mock_client.analyze.return_value = {
        "run_id": "NEGBAD",
        "opinions": [
            {"stance_score": -1.0001, "confidence": 0.5, "horizon_days": 14}
        ],
    }
    assert run(FakeIdea(), AS_OF, client=mock_client) == []


# ===========================================================================
# Malformed top-level response — must NOT raise, returns []
# ===========================================================================


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"opinions": None},
        {"opinions": "nope"},
        ["list", "not", "dict"],
        None,
        "raw string",
    ],
)
def test_malformed_response_returns_empty_and_never_raises(body: object) -> None:
    mock_client = MagicMock(spec=MirofishHTTPClient)
    mock_client.analyze.return_value = body
    result = run(FakeIdea(), AS_OF, client=mock_client)
    assert result == []


def test_per_opinion_missing_key_is_skipped() -> None:
    mock_client = MagicMock(spec=MirofishHTTPClient)
    mock_client.analyze.return_value = {
        "run_id": "MISS",
        "opinions": [{"confidence": 0.5}],  # missing stance_score
    }
    assert run(FakeIdea(), AS_OF, client=mock_client) == []


def test_run_with_empty_opinions_response() -> None:
    mock_client = MagicMock(spec=MirofishHTTPClient)
    mock_client.analyze.return_value = {"run_id": "EMPTY", "opinions": []}
    assert run(FakeIdea(), AS_OF, client=mock_client) == []


def test_run_skips_invalid_opinion_but_returns_valid_ones() -> None:
    mock_client = MagicMock(spec=MirofishHTTPClient)
    mock_client.analyze.return_value = {
        "run_id": "PARTIAL",
        "opinions": [
            {"stance_score": 5.0, "confidence": 0.8, "horizon_days": 14},
            {"stance_score": 0.4, "confidence": 0.6, "horizon_days": 60},
        ],
    }
    result = run(FakeIdea(), AS_OF, client=mock_client)
    assert len(result) == 1
    assert result[0].stance_score == 0.4


def test_malformed_idea_fails_closed() -> None:
    """An idea missing required attributes must degrade to [] (not raise)."""

    class _Bad:
        pass

    assert run(_Bad(), AS_OF, client=MagicMock(spec=MirofishHTTPClient)) == []


def test_soft_opinion_cap_truncates() -> None:
    from arbiter.adapters.mirofish.adapter import MAX_OPINIONS_PER_RUN

    opinions_raw = [
        {"stance_score": 0.1, "confidence": 0.5, "horizon_days": 30}
        for _ in range(MAX_OPINIONS_PER_RUN + 10)
    ]
    mock_client = MagicMock(spec=MirofishHTTPClient)
    mock_client.analyze.return_value = {"run_id": "BIG", "opinions": opinions_raw}
    result = run(FakeIdea(), AS_OF, client=mock_client)
    assert len(result) == MAX_OPINIONS_PER_RUN


# ===========================================================================
# Cache behavior (through run)
# ===========================================================================


def test_cache_hit_avoids_second_call() -> None:
    conn = make_memory_db()
    run_id = "RUN_CACHED_02"
    mock_client = MagicMock(spec=MirofishHTTPClient)
    mock_client.analyze.return_value = mirofish_response(run_id=run_id)

    first = run(FakeIdea(), AS_OF, conn=conn, client=mock_client)
    assert mock_client.analyze.call_count == 1
    second = run(FakeIdea(), AS_OF, conn=conn, client=mock_client)
    assert mock_client.analyze.call_count == 1
    assert {op.run_group_id for op in first} == {
        op.run_group_id for op in second
    } == {run_id}


def test_cache_miss_on_different_date() -> None:
    from datetime import datetime, timezone

    conn = make_memory_db()
    mock_client = MagicMock(spec=MirofishHTTPClient)
    mock_client.analyze.return_value = mirofish_response(run_id="RUN_DAY1")

    run(FakeIdea(), AS_OF, conn=conn, client=mock_client)
    assert mock_client.analyze.call_count == 1

    day2 = datetime(2025, 6, 16, 9, 30, 0, tzinfo=timezone.utc)
    mock_client.analyze.return_value = mirofish_response(run_id="RUN_DAY2")
    run(FakeIdea(), day2, conn=conn, client=mock_client)
    assert mock_client.analyze.call_count == 2


def test_created_at_is_the_information_timestamp() -> None:
    """After a cache write, created_at == as_of.isoformat() (not NO_CLOCK)."""
    conn = make_memory_db()
    mock_client = MagicMock(spec=MirofishHTTPClient)
    mock_client.analyze.return_value = mirofish_response()
    run(FakeIdea(), AS_OF, conn=conn, client=mock_client)

    row = conn.execute(
        "SELECT created_at FROM mirofish_run_cache LIMIT 1"
    ).fetchone()
    assert row["created_at"] == AS_OF.isoformat()
    assert row["created_at"] != "NO_CLOCK"


# ===========================================================================
# Network failures through run() — fail-closed
# ===========================================================================


def test_unreachable_returns_empty_no_endpoint() -> None:
    client = MirofishHTTPClient(endpoint=None)
    assert run(FakeIdea(), AS_OF, client=client) == []


def test_network_error_returns_empty() -> None:
    client = MirofishHTTPClient(endpoint="http://localhost:8765")
    with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
        assert run(FakeIdea(), AS_OF, client=client) == []


def test_timeout_returns_empty_and_advances_breaker_no_retry() -> None:
    """Timeout via run(): [] returned, breaker counter advanced, call_count==1
    (the 20-min run must not be silently re-launched)."""
    client = MirofishHTTPClient(endpoint="http://localhost:8765")
    with patch(
        "httpx.post", side_effect=httpx.TimeoutException("read timeout")
    ) as mock_post:
        assert run(FakeIdea(), AS_OF, client=client) == []
    assert mock_post.call_count == 1
    assert client.consecutive_failures == 1


def test_connect_retry_recovers_through_run() -> None:
    """Two ConnectErrors then success → run() returns opinions (retry worked)."""
    client = MirofishHTTPClient(endpoint="http://localhost:8765")

    ok = MagicMock(spec=httpx.Response)
    ok.raise_for_status.return_value = None
    ok.json.return_value = mirofish_response(run_id="RETRY_OK")

    with patch(
        "httpx.post",
        side_effect=[
            httpx.ConnectError("cold"),
            httpx.ConnectError("cold"),
            ok,
        ],
    ) as mock_post:
        result = run(FakeIdea(), AS_OF, client=client)
    assert mock_post.call_count == 3
    assert len(result) == 2
    assert client.consecutive_failures == 0


def test_nonlocal_endpoint_swallowed_to_empty() -> None:
    """A non-loopback MIROFISH_ENDPOINT raises EgressViolation inside analyze;
    run() swallows it to [] (fail-closed)."""
    client = MirofishHTTPClient(endpoint="https://data.sec.gov")
    with patch("httpx.post") as mock_post:
        assert run(FakeIdea(), AS_OF, client=client) == []
    mock_post.assert_not_called()


# ===========================================================================
# Breaker through run()
# ===========================================================================


def test_breaker_fires_after_threshold_through_run() -> None:
    fired: list[int] = []
    client = MirofishHTTPClient(
        endpoint="http://localhost:8765",
        breaker=lambda: fired.append(1),
        breaker_threshold=3,
    )
    with patch("httpx.post", side_effect=httpx_connection_error()):
        for _ in range(3):
            assert run(FakeIdea(), AS_OF, client=client) == []
    assert len(fired) == 1
    assert client.consecutive_failures == 3


def test_breaker_does_not_fire_before_threshold_through_run() -> None:
    fired: list[int] = []
    client = MirofishHTTPClient(
        endpoint="http://localhost:8765",
        breaker=lambda: fired.append(1),
        breaker_threshold=3,
    )
    with patch("httpx.post", side_effect=httpx_connection_error()):
        for _ in range(2):
            run(FakeIdea(), AS_OF, client=client)
    assert len(fired) == 0


# ===========================================================================
# Bad-body-is-not-an-outage through run()
# ===========================================================================


def test_bad_body_returns_empty_without_advancing_breaker() -> None:
    """A reachable 200 with a non-JSON body → run() returns [] AND the breaker
    counter is unchanged (malformed-but-up != down)."""
    client = MirofishHTTPClient(endpoint="http://localhost:8765")
    resp = MagicMock(spec=httpx.Response)
    resp.raise_for_status.return_value = None
    resp.json.side_effect = ValueError("not json")
    with patch("httpx.post", return_value=resp):
        assert run(FakeIdea(), AS_OF, client=client) == []
    assert client.consecutive_failures == 0


# ===========================================================================
# Disabled-noop: endpoint unset → [], no network, breaker stays 0
# ===========================================================================


def test_disabled_noop_returns_empty_and_never_touches_httpx() -> None:
    """With MIROFISH_ENDPOINT unset (autouse fixture clears it), a client with
    no endpoint yields [] from run() and never calls httpx.post."""
    client = MirofishHTTPClient(endpoint=None)
    assert client.endpoint is None
    with patch("httpx.post") as mock_post:
        assert run(FakeIdea(), AS_OF, client=client) == []
    mock_post.assert_not_called()
    assert client.consecutive_failures == 0


# ===========================================================================
# Backtest cache guard through run()
# ===========================================================================


def test_run_backtest_does_not_replay_cache() -> None:
    """In backtest mode, run() must not serve cached forward results — a
    primed cache is bypassed (BacktestCacheError → degrade to fresh call)."""
    conn = make_memory_db()
    mock_client = MagicMock(spec=MirofishHTTPClient)
    mock_client.analyze.return_value = mirofish_response(run_id="FWD")

    # Prime the cache via a forward run.
    run(FakeIdea(), AS_OF, conn=conn, client=mock_client)
    assert mock_client.analyze.call_count == 1

    # Backtest run: cache read raises BacktestCacheError, caught → fresh call.
    run(FakeIdea(), AS_OF, conn=conn, client=mock_client, is_backtest=True)
    assert mock_client.analyze.call_count == 2


# ===========================================================================
# No datetime.now() in adapter source (INTERFACES.md §11.1)
# ===========================================================================


def test_adapter_has_no_datetime_now_calls() -> None:
    import ast
    from pathlib import Path

    import arbiter.adapters.mirofish.adapter as adapter_mod

    source = Path(adapter_mod.__file__).read_text()
    tree = ast.parse(source)

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            f = node.func
            if f.attr in ("now", "utcnow") and isinstance(f.value, ast.Name):
                if f.value.id in ("datetime", "dt"):
                    violations.append(f"Line {node.lineno}: {ast.unparse(node)!r}")
    assert not violations, "adapter.py must not call datetime.now()/utcnow()"
