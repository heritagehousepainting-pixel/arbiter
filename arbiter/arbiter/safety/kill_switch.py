"""Broker-side kill switch for Arbiter (Lane 4c).

Reads a remote OFF-BOX endpoint that declares halt-state.  The endpoint is
owned by infra — it keeps working even when the Python process is dead (because
it lives on a separate host/service).

Design decisions (INTERFACES.md §8, §11):
- **Fail-closed**: any network error or unexpected response → HALTED (True).
  This satisfies the "gate unreachable → no trade" convention (§11.4).
- Caches the last successful response for ``cache_ttl_seconds`` to avoid
  hammering the endpoint on every order check.  On cache miss or cache expiry
  the endpoint is queried fresh.  On failure the *stale* cached value is NOT
  used — we treat the failure as HALTED immediately.
- Does NOT auto-close existing positions (v1).  The kill switch blocks NEW
  orders only.  Position liquidation is an operational action.
- No ``datetime.now()`` — caller passes ``as_of`` (a tz-aware UTC datetime)
  for cache TTL evaluation.  This keeps backtest determinism intact.

Expected endpoint contract:
    GET <kill_switch_url>
    → 200 JSON  {"halted": true | false}

Any non-200 response, missing key, malformed JSON, or network exception
→ treat as halted=True.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from arbiter.config import Config

log = structlog.get_logger(__name__)

# Default TTL for caching a *successful* response.
_DEFAULT_TTL_SECONDS: float = 5.0


@dataclass
class _CacheEntry:
    halted: bool
    fetched_at: datetime  # tz-aware UTC of when this entry was populated


@dataclass
class KillSwitch:
    """Checks the remote kill-switch endpoint and returns halt state.

    Parameters
    ----------
    config:
        Frozen ``Config`` instance; ``config.kill_switch_url`` is the endpoint.
    cache_ttl_seconds:
        Seconds to cache a *successful* response.  Failures are never cached —
        they immediately return halted=True.
    http_timeout:
        Timeout in seconds for the HTTP request.

    Notes
    -----
    Thread safety: ``_cache`` is a simple Python attribute; callers should
    serialise concurrent access externally if needed (Wave-C concern).
    """

    config: Config
    cache_ttl_seconds: float = _DEFAULT_TTL_SECONDS
    http_timeout: float = 3.0
    _cache: _CacheEntry | None = field(default=None, init=False, repr=False)

    def is_halted(self, *, as_of: datetime) -> bool:
        """Return True if trading is halted (or if the endpoint is unreachable).

        Parameters
        ----------
        as_of:
            Current logical time (tz-aware UTC).  Used to evaluate cache TTL.
            Must **never** be ``datetime.now()`` at the call site — pass the
            Clock value instead (INTERFACES.md §11.1).

        Returns
        -------
        bool
            ``True``  → halt (block new orders).
            ``False`` → endpoint says trading is permitted.
        """
        if not self.config.kill_switch_url:
            # No URL configured: fail-closed (safe default).
            log.warning("kill_switch.no_url_configured", action="halting")
            return True

        # Check cache first.
        if self._cache is not None:
            age = (as_of - self._cache.fetched_at).total_seconds()
            if age < self.cache_ttl_seconds:
                log.debug(
                    "kill_switch.cache_hit",
                    halted=self._cache.halted,
                    age_seconds=round(age, 3),
                )
                return self._cache.halted

        # Cache miss or expired — fetch fresh.
        return self._fetch(as_of=as_of)

    def _fetch(self, *, as_of: datetime) -> bool:
        """Perform the HTTP GET.  On any error, return True (fail-closed)."""
        url = self.config.kill_switch_url
        try:
            response = httpx.get(url, timeout=self.http_timeout)
            response.raise_for_status()
            body: Any = response.json()
            halted: bool = bool(body.get("halted", True))
        except httpx.HTTPStatusError as exc:
            log.error(
                "kill_switch.http_error",
                status_code=exc.response.status_code,
                url=url,
                action="fail_closed",
            )
            return True
        except (httpx.RequestError, json.JSONDecodeError, ValueError, AttributeError) as exc:
            log.error(
                "kill_switch.fetch_failed",
                error=str(exc),
                url=url,
                action="fail_closed",
            )
            return True

        # Successful response — populate cache.
        self._cache = _CacheEntry(halted=halted, fetched_at=as_of)
        log.info("kill_switch.fetched", halted=halted, url=url)
        return halted
