"""cache_key format, put/get, TTL expiry (injected clock), thread-safety."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from mirofish.cache import OpinionCache, cache_key
from mirofish.types import OpinionOut


def _op(stance: float = 0.5) -> OpinionOut:
    return OpinionOut(
        stance_score=stance,
        confidence=0.6,
        horizon_days=10,
        rationale="r",
        source_fingerprint="fp16chars0000000",
    )


def test_cache_key_format() -> None:
    as_of = datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc)
    assert cache_key("aapl", as_of, "abc123") == "AAPL|2026-06-01|abc123"


def test_cache_key_normalizes_naive_to_utc() -> None:
    naive = datetime(2026, 6, 1, 23, 0)  # naive -> assume UTC
    assert cache_key("MSFT", naive, "fp") == "MSFT|2026-06-01|fp"


def test_put_then_get_hit() -> None:
    cache = OpinionCache(ttl_seconds=100, clock=lambda: 0.0)
    cache.put("k", [_op()])
    got = cache.get("k")
    assert got is not None
    assert len(got) == 1
    assert got[0].stance_score == 0.5


def test_miss_returns_none() -> None:
    cache = OpinionCache(ttl_seconds=100, clock=lambda: 0.0)
    assert cache.get("absent") is None


def test_ttl_expiry_with_injected_clock() -> None:
    now = {"t": 0.0}
    cache = OpinionCache(ttl_seconds=100, clock=lambda: now["t"])
    cache.put("k", [_op()])
    now["t"] = 99.0
    assert cache.get("k") is not None  # still fresh
    now["t"] = 100.0
    assert cache.get("k") is None  # expired at exactly ttl
    now["t"] = 500.0
    assert cache.get("k") is None


def test_get_returns_copy_not_internal_list() -> None:
    cache = OpinionCache(ttl_seconds=100, clock=lambda: 0.0)
    cache.put("k", [_op()])
    first = cache.get("k")
    assert first is not None
    first.append(_op(stance=-1.0))
    second = cache.get("k")
    assert second is not None
    assert len(second) == 1  # mutation of the returned list didn't leak


def test_thread_safety_smoke() -> None:
    cache = OpinionCache(ttl_seconds=1000)
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            for i in range(200):
                key = f"k{n}-{i % 10}"
                cache.put(key, [_op(stance=float(i % 3) - 1.0)])
                cache.get(key)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
