"""Async/sync HTTP client for the self-hosted MiroFish inference endpoint.

MiroFish is a self-hosted quantitative model that runs entirely over local
HTTP — it is NEVER imported directly (AGPL, INTERFACES.md §11.5).

Characteristics:
- Runs are on-demand and expensive: 15–20 min per ticker analysis.
- Timeout is set generously (default 1200 s = 20 min) but configurable.
- A lightweight circuit breaker tracks consecutive failures; after
  ``breaker_threshold`` consecutive errors the ``breaker`` callback fires.
  The breaker does NOT retry automatically — callers decide what to do.
- Endpoint URL is read from environment variable ``MIROFISH_ENDPOINT``
  (no default — omit → unreachable; adapter returns [] per fail-closed rule).

Network calls:
    POST /analyze  — body: {ticker, as_of, idea_fingerprint}
                    response: {opinions: [...]}

Egress:
    The ``/analyze`` inference URL is validated through
    ``egress.check_inference_egress`` (loopback-only) before dispatch.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from typing import Any

import httpx
import structlog

from arbiter.adapters.mirofish.egress import check_inference_egress

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADVISOR_ID = "A2.mirofish"
HARD_WEIGHT_CAP = 0.35  # INTERFACES.md §5

# Default *read* timeout: 20 minutes (MiroFish runs are expensive and slow).
# Connect is kept short (below) so an *absent* service fails fast while a
# *running* job still gets its full read budget.
DEFAULT_TIMEOUT_S: float = 1200.0
CONNECT_TIMEOUT_S: float = 5.0
WRITE_TIMEOUT_S: float = 10.0
POOL_TIMEOUT_S: float = 5.0

# Circuit-breaker default threshold.
DEFAULT_BREAKER_THRESHOLD: int = 3

# Bounded retry for cold-socket connect errors ONLY.  A dropped connection at
# establishment is transient and safe to retry; a TimeoutException means an
# in-flight 20-min run is already underway and must NOT be silently re-launched,
# and HTTP status errors are deterministic — neither is retried.
CONNECT_RETRIES: int = 2
_CONNECT_BACKOFF_S: tuple[float, ...] = (0.2, 0.4)


# ---------------------------------------------------------------------------
# Exceptions (declared before the client that references them)
# ---------------------------------------------------------------------------


class MirofishUnavailable(RuntimeError):
    """Raised when the MiroFish endpoint is not configured or unreachable.

    Callers should treat this as an abstain signal (return []).
    """


class MirofishBadResponse(MirofishUnavailable):
    """Raised when MiroFish is *reachable* but returns an unparseable body.

    Distinct from a network outage: the service answered, so this must NOT
    advance the circuit breaker's consecutive-failure counter (the breaker is
    for outages, not malformed payloads).  Still a subclass of
    ``MirofishUnavailable`` so the adapter's existing abstain handling treats
    it as a clean ``[]``.
    """


def _get_endpoint() -> str | None:
    """Return the MiroFish endpoint URL from env, or None if unset."""
    val = os.environ.get("MIROFISH_ENDPOINT", "").strip()
    return val if val else None


# ---------------------------------------------------------------------------
# Synchronous client (used by the adapter's run() for simplicity)
# ---------------------------------------------------------------------------


class MirofishHTTPClient:
    """Thin synchronous wrapper around ``httpx`` for the MiroFish endpoint.

    Args:
        endpoint:           Base URL of the self-hosted MiroFish service,
                            e.g. ``"http://localhost:8765"``.  If ``None``
                            the client reads ``MIROFISH_ENDPOINT`` from env.
        timeout:            HTTP timeout in seconds (default 1200 s / 20 min).
        breaker_threshold:  Number of consecutive failures before the
                            ``breaker`` callback is invoked.
        breaker:            Optional callable with no arguments.  Called once
                            when ``consecutive_failures >= breaker_threshold``.
                            Idempotent — not called again until reset.
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        breaker_threshold: int = DEFAULT_BREAKER_THRESHOLD,
        breaker: Callable[[], None] | None = None,
    ) -> None:
        self._endpoint = endpoint or _get_endpoint()
        self._timeout = timeout
        self._breaker_threshold = breaker_threshold
        self._breaker: Callable[[], None] | None = breaker

        # Consecutive failure counter; reset on any success.
        self._consecutive_failures: int = 0
        # Flag so breaker fires exactly once per failure streak.
        self._breaker_tripped: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def endpoint(self) -> str | None:
        """Configured endpoint URL, or None if env var is absent."""
        return self._endpoint

    @property
    def consecutive_failures(self) -> int:
        """Current consecutive failure count (read-only)."""
        return self._consecutive_failures

    def analyze(
        self,
        ticker: str,
        as_of_iso: str,
        idea_fingerprint: str,
    ) -> dict[str, Any]:
        """POST /analyze and return the parsed JSON response body.

        Args:
            ticker:            Ticker symbol, e.g. ``"AAPL"``.
            as_of_iso:         Information timestamp as ISO string (UTC).
            idea_fingerprint:  Stable hash identifying the idea, used by the
                               run cache to avoid duplicate calls.

        Returns:
            Parsed JSON dict from MiroFish, expected shape::

                {
                    "opinions": [
                        {
                            "stance_score": float,
                            "confidence": float,
                            "horizon_days": int,
                            "rationale": str,
                            "source_fingerprint": str,
                        },
                        ...
                    ],
                    "run_id": str,  # shared across all opinions in this run
                }

        Raises:
            MirofishUnavailable: If ``endpoint`` is not configured, or is not
                a ``http(s)://`` URL (fail-closed → treated as disabled).
            MirofishBadResponse: If the service is reachable but returns a
                non-JSON / non-dict body (does NOT advance the breaker).
            httpx.HTTPError:     On network or HTTP-level errors.
            EgressViolation:     If the endpoint URL is not loopback / on the
                allowlist.
        """
        if not self._endpoint:
            raise MirofishUnavailable(
                "MIROFISH_ENDPOINT is not set; MiroFish is unreachable."
            )

        # Scheme guard: a scheme-less base (e.g. "localhost:8765") makes
        # urlparse mis-read the host as the scheme.  Fail closed (disabled)
        # rather than let egress raise on a malformed parse.
        if not self._endpoint.startswith(("http://", "https://")):
            raise MirofishUnavailable(
                f"MIROFISH_ENDPOINT must be an http(s):// URL; got "
                f"{self._endpoint!r}. Treating MiroFish as disabled."
            )

        url = f"{self._endpoint.rstrip('/')}/analyze"
        # Localhost-only firewall gate for the inference endpoint, before I/O.
        check_inference_egress(url)

        payload = {
            "ticker": ticker,
            "as_of": as_of_iso,
            "idea_fingerprint": idea_fingerprint,
        }

        log.debug(
            "mirofish.analyze.request",
            ticker=ticker,
            as_of=as_of_iso,
            fingerprint=idea_fingerprint,
        )

        timeout = httpx.Timeout(
            connect=CONNECT_TIMEOUT_S,
            read=self._timeout,
            write=WRITE_TIMEOUT_S,
            pool=POOL_TIMEOUT_S,
        )

        resp = self._post_with_connect_retry(url, payload, timeout, ticker)

        # At this point the service answered.  A non-JSON / non-dict body is a
        # *malformed-but-reachable* response: abstain WITHOUT advancing the
        # breaker (reset the streak, since the socket round-trip succeeded).
        try:
            data: Any = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            self._record_success()  # reachable: do not arm the breaker
            log.warning(
                "mirofish.analyze.bad_body",
                ticker=ticker,
                error=str(exc),
            )
            raise MirofishBadResponse(
                f"MiroFish returned a non-JSON body: {exc}"
            ) from exc

        if not isinstance(data, dict):
            self._record_success()  # reachable: do not arm the breaker
            log.warning(
                "mirofish.analyze.bad_body",
                ticker=ticker,
                error=f"expected JSON object, got {type(data).__name__}",
            )
            raise MirofishBadResponse(
                f"MiroFish returned a non-dict body of type "
                f"{type(data).__name__}."
            )

        self._record_success()
        log.debug(
            "mirofish.analyze.response",
            ticker=ticker,
            opinion_count=len(data.get("opinions", [])),
        )
        return data

    def _post_with_connect_retry(
        self,
        url: str,
        payload: dict[str, Any],
        timeout: httpx.Timeout,
        ticker: str,
    ) -> httpx.Response:
        """POST with a bounded retry for cold-socket ``ConnectError`` only.

        Retries ``httpx.ConnectError`` up to ``CONNECT_RETRIES`` times with a
        short backoff.  ``TimeoutException`` (an in-flight run is underway) and
        HTTP status errors are never retried.  Any genuine failure advances the
        breaker via ``_record_failure`` and is re-raised; the adapter swallows
        it to ``[]`` (fail-closed).
        """
        last_exc: Exception | None = None
        for attempt in range(CONNECT_RETRIES + 1):
            try:
                resp = httpx.post(url, json=payload, timeout=timeout)
                resp.raise_for_status()
                return resp
            except httpx.ConnectError as exc:
                last_exc = exc
                if attempt < CONNECT_RETRIES:
                    backoff = _CONNECT_BACKOFF_S[
                        min(attempt, len(_CONNECT_BACKOFF_S) - 1)
                    ]
                    log.debug(
                        "mirofish.analyze.connect_retry",
                        ticker=ticker,
                        attempt=attempt + 1,
                        backoff_s=backoff,
                    )
                    time.sleep(backoff)
                    continue
                # Retries exhausted → genuine outage.
                break
            except Exception as exc:  # timeouts, HTTP status, etc.
                self._record_failure()
                log.warning(
                    "mirofish.analyze.error",
                    ticker=ticker,
                    error=str(exc),
                    consecutive_failures=self._consecutive_failures,
                )
                raise

        # ConnectError retries exhausted.
        self._record_failure()
        log.warning(
            "mirofish.analyze.error",
            ticker=ticker,
            error=str(last_exc),
            consecutive_failures=self._consecutive_failures,
        )
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------
    # Circuit-breaker bookkeeping
    # ------------------------------------------------------------------

    def _record_failure(self) -> None:
        """Increment failure counter and fire the breaker if threshold hit."""
        self._consecutive_failures += 1
        if (
            self._consecutive_failures >= self._breaker_threshold
            and not self._breaker_tripped
            and self._breaker is not None
        ):
            self._breaker_tripped = True
            log.error(
                "mirofish.breaker.tripped",
                consecutive_failures=self._consecutive_failures,
                threshold=self._breaker_threshold,
            )
            self._breaker()

    def _record_success(self) -> None:
        """Reset failure counter and breaker state on success."""
        self._consecutive_failures = 0
        self._breaker_tripped = False

    def reset_breaker(self) -> None:
        """Manually reset the breaker (e.g. after service recovery)."""
        self._consecutive_failures = 0
        self._breaker_tripped = False
