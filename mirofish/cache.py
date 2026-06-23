"""In-memory TTL cache for MiroFish A2 opinions.

`cache_key(ticker, as_of, fingerprint)` builds the dedup key; `OpinionCache`
is a thread-safe TTL dict keyed by it. The wall-clock used for TTL is the ONLY
clock in the service (the request path is point-in-time and clock-free) and is
injectable so TTL expiry can be tested deterministically.

ISOLATION: pure stdlib + mirofish.types. Never imports arbiter.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime

from mirofish.types import OpinionOut, ensure_utc


def cache_key(ticker: str, as_of: datetime, fingerprint: str) -> str:
    """`f"{TICKER}|{as_of.date()}|{fingerprint}"` — the dedup key.

    Ticker is upper-cased and as_of is normalized to UTC before taking its
    date so the key is stable across casing / timezone.
    """
    as_of_utc = ensure_utc(as_of)
    return f"{ticker.upper()}|{as_of_utc.date().isoformat()}|{fingerprint}"


class OpinionCache:
    """Thread-safe in-memory TTL store of `list[OpinionOut]`.

    `clock` returns monotonic-ish seconds (default `time.monotonic`); inject a
    fake clock in tests to drive TTL expiry without sleeping.
    """

    def __init__(
        self,
        ttl_seconds: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # key -> (expires_at_seconds, opinions)
        self._store: dict[str, tuple[float, list[OpinionOut]]] = {}

    def get(self, key: str) -> list[OpinionOut] | None:
        """Return cached opinions if present and unexpired, else None."""
        now = self._clock()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, opinions = entry
            if now >= expires_at:
                # Lazily evict the expired entry.
                self._store.pop(key, None)
                return None
            # Return a shallow copy so callers can't mutate the cached list.
            return list(opinions)

    def put(self, key: str, opinions: list[OpinionOut]) -> None:
        """Store opinions under `key` with the configured TTL."""
        expires_at = self._clock() + self._ttl
        with self._lock:
            self._store[key] = (expires_at, list(opinions))
