"""MiroFish adapter (A2) — main entry point for Lane 7.

``run(idea, as_of, ...)`` is the single public function.  It:

1. Computes an ``idea_fingerprint`` from the idea fields.
2. Checks the run cache (skip expensive MiroFish call if result is fresh).
3. If cache miss: calls MiroFish over local HTTP via ``MirofishHTTPClient``.
4. Materialises ``Opinion`` objects from the raw response.
5. Returns a list of Opinions (may be 2+ sharing a ``run_group_id``).

Shadow mode:
    A2 is always registered with ``shadow=True`` until it has enough live
    track record for trust promotion (Lane 11).  Shadow opinions are
    recorded in the DB but receive weight 0 in fusion (Lane 10).
    The weight flag is on the ``AdvisorWeight`` in the trust ledger, not here.

Fail-closed:
    Any unreachable / errored MiroFish call returns ``[]`` (abstain).
    The circuit breaker callback signals Lane 4 / the operator.

AGPL isolation:
    ``import mirofish`` is forbidden.  MiroFish is called over HTTP only.
    (INTERFACES.md §11.5, convention 5)

Advisor ID: ``"A2.mirofish"``
Hard weight cap: 0.35 (INTERFACES.md §5) — enforced in Lane 11.

No ``datetime.now()`` calls (INTERFACES.md §11.1).
``as_of`` is always the caller-supplied information timestamp.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import datetime

import structlog

from arbiter.adapters.mirofish.http_client import MirofishHTTPClient, MirofishUnavailable
from arbiter.adapters.mirofish import run_cache
from arbiter.contract.opinion import Opinion, validate_opinion
from arbiter.types import ConfidenceSource

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADVISOR_ID = "A2.mirofish"
HARD_WEIGHT_CAP = 0.35  # INTERFACES.md §5

# Soft cap on opinions materialised from one response — defends against a
# runaway / pathological body.  Localhost service is trusted but bounded.
MAX_OPINIONS_PER_RUN = 32

# Horizon days for SHORT and MEDIUM opinions MiroFish typically emits.
# MiroFish produces both a short-term and medium-term read of the same idea.
_SHORT_HORIZON_DAYS = 14   # 14 days → SHORT bucket
_MEDIUM_HORIZON_DAYS = 60  # 60 days → MEDIUM bucket


# ---------------------------------------------------------------------------
# Idea fingerprint
# ---------------------------------------------------------------------------


def _idea_fingerprint(idea: object) -> str:
    """Return a stable SHA-256 hex digest for deduplication / cache keying.

    Uses ``ticker``, ``thesis``, and ``horizon_days`` from the idea.  The
    fingerprint is the same for the same logical idea regardless of the
    ``idea_id`` ULID or ``state``.

    Args:
        idea: Any object with ``.ticker``, ``.thesis``, ``.horizon_days``.

    Returns:
        64-char lowercase hex digest.
    """
    raw = f"{idea.ticker}|{idea.thesis}|{idea.horizon_days}"  # type: ignore[attr-defined]
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Response → Opinion conversion
# ---------------------------------------------------------------------------


def _opinions_from_response(
    raw_opinions: list[dict],
    run_group_id: str,
    ticker: str,
    as_of: datetime,
    idea_fingerprint: str,
) -> list[Opinion]:
    """Convert raw MiroFish response dicts into validated Opinion objects.

    MiroFish returns a list of opinion dicts.  Each dict must contain:
    - ``stance_score`` (float, [-1, 1])
    - ``confidence``   (float, [0, 1])
    - ``horizon_days`` (int, > 0, ≤ 365)
    - ``rationale``    (str)
    - ``source_fingerprint`` (str, optional — falls back to idea_fingerprint)

    All resulting Opinions share the same ``run_group_id`` so fusion can
    recognise them as coming from the same run and dedup same-bucket opinions
    while treating different buckets independently.

    Args:
        raw_opinions:      List of raw dicts from MiroFish response.
        run_group_id:      Shared ULID/ID for this run.
        ticker:            Ticker symbol.
        as_of:             Information timestamp (tz-aware UTC).
        idea_fingerprint:  Used as fallback ``source_fingerprint``.

    Returns:
        List of validated Opinion objects (may be shorter than raw_opinions
        if any opinion fails validation — logged and skipped).
    """
    results: list[Opinion] = []
    for i, raw in enumerate(raw_opinions):
        try:
            op = Opinion(
                advisor_id=ADVISOR_ID,
                ticker=ticker,
                # NEGATIVE-STANCE PASSTHROUGH (load-bearing, do NOT clamp):
                # stance_score < 0 is a first-class SHORT/bearish signal and
                # must reach fusion unchanged.  validate_opinion accepts the
                # full [-1.0, 1.0] range; we never abs() or floor at 0.
                stance_score=float(raw["stance_score"]),
                confidence=float(raw["confidence"]),
                confidence_source=ConfidenceSource.MODELED,
                horizon_days=int(raw["horizon_days"]),
                as_of=as_of,
                rationale=str(raw.get("rationale", "")),
                source_fingerprint=str(
                    raw.get("source_fingerprint", idea_fingerprint)
                ),
                run_group_id=run_group_id,
            )
            validate_opinion(op)
            results.append(op)
        except (KeyError, ValueError, TypeError) as exc:
            log.warning(
                "mirofish.adapter.opinion_invalid",
                index=i,
                error=str(exc),
                run_group_id=run_group_id,
                ticker=ticker,
            )
    return results


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run(
    idea: object,
    as_of: datetime,
    *,
    conn: sqlite3.Connection | None = None,
    client: MirofishHTTPClient | None = None,
    breaker: Callable[[], None] | None = None,
    is_backtest: bool = False,
) -> list[Opinion]:
    """Run MiroFish analysis for *idea* and return a list of Opinions.

    This is the single public entry point for the MiroFish adapter.

    Args:
        idea:        Any object with ``.ticker``, ``.thesis``,
                     ``.horizon_days`` attributes (typically an ``Idea``
                     from Lane 13, but duck-typing is accepted for tests).
        as_of:       Information timestamp (tz-aware UTC, passed in by caller
                     — never ``datetime.now()`` inside this function).
        conn:        Optional SQLite connection for run-cache reads/writes.
                     If ``None``, caching is skipped (safe for tests without
                     a DB).
        client:      Optional pre-built ``MirofishHTTPClient`` (injection for
                     tests).  If ``None``, a new client is created using the
                     ``MIROFISH_ENDPOINT`` env var.
        breaker:     Callback to fire after ``DEFAULT_BREAKER_THRESHOLD``
                     consecutive failures.  Passed through to the HTTP client.
        is_backtest: If ``True``, cache reads are blocked (look-ahead guard).
                     Pass ``True`` in backtest harnesses.

    Returns:
        List of Opinion objects (0..N).  Empty list = abstain.
        Returns ``[]`` on any network error (fail-closed).

    Contract:
        - NEVER raises — always returns a list (``[]`` = abstain).  Network
          errors, malformed responses, and even a malformed ``idea`` all fail
          closed to ``[]`` (INTERFACES.md §11.7).
        - Opinions within a run share the same ``run_group_id``.
        - ``as_of`` is the caller-supplied timestamp; no ``datetime.now()``
          inside this function (INTERFACES.md §11.1).
    """
    try:
        return _run_impl(
            idea,
            as_of,
            conn=conn,
            client=client,
            breaker=breaker,
            is_backtest=is_backtest,
        )
    except Exception as exc:  # fail-closed boundary — never propagate
        log.warning(
            "mirofish.adapter.unexpected",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return []


def _run_impl(
    idea: object,
    as_of: datetime,
    *,
    conn: sqlite3.Connection | None = None,
    client: MirofishHTTPClient | None = None,
    breaker: Callable[[], None] | None = None,
    is_backtest: bool = False,
) -> list[Opinion]:
    """Core pipeline for :func:`run` (wrapped by a fail-closed boundary).

    Idea-attribute access happens here so that a malformed ``idea`` (missing
    ``.ticker`` / ``.thesis`` / ``.horizon_days``) is caught by ``run``'s outer
    try/except and degraded to ``[]`` rather than propagating.
    """
    ticker: str = idea.ticker  # type: ignore[attr-defined]
    fingerprint = _idea_fingerprint(idea)
    as_of_date_str = as_of.date().isoformat()

    log.debug(
        "mirofish.adapter.run.start",
        ticker=ticker,
        fingerprint=fingerprint,
        as_of=as_of.isoformat(),
    )

    # ------------------------------------------------------------------
    # 1. Cache lookup (skip if no DB connection or backtest)
    # ------------------------------------------------------------------
    if conn is not None:
        try:
            cached = run_cache.get(
                conn, fingerprint, as_of_date_str, is_backtest=is_backtest
            )
        except Exception as exc:
            log.warning(
                "mirofish.adapter.cache_read_error",
                ticker=ticker,
                error=str(exc),
            )
            cached = None

        if cached is not None:
            log.info(
                "mirofish.adapter.cache_hit",
                ticker=ticker,
                fingerprint=fingerprint,
                opinion_count=len(cached),
            )
            # Reconstruct opinions from cached raw dicts.
            # We need the run_group_id — it's stored in the first cached opinion.
            rg_id = cached[0].get("run_group_id", fingerprint) if cached else fingerprint
            return _opinions_from_response(cached, rg_id, ticker, as_of, fingerprint)
    else:
        cached = None

    # ------------------------------------------------------------------
    # 2. Fresh MiroFish call
    # ------------------------------------------------------------------
    if client is None:
        client = MirofishHTTPClient(breaker=breaker)

    try:
        response = client.analyze(
            ticker=ticker,
            as_of_iso=as_of.isoformat(),
            idea_fingerprint=fingerprint,
        )
    except MirofishUnavailable as exc:
        log.info(
            "mirofish.adapter.unavailable",
            ticker=ticker,
            reason=str(exc),
        )
        return []
    except Exception as exc:
        log.warning(
            "mirofish.adapter.call_failed",
            ticker=ticker,
            error=str(exc),
        )
        return []

    # ------------------------------------------------------------------
    # 2a. Top-level response-shape guard (fail-closed)
    # ------------------------------------------------------------------
    # MiroFish *should* return a dict, but a JSON list / string / null would
    # make ``response.get(...)`` raise AttributeError.  Guard the shape so
    # run() never crashes on a malformed top-level body.
    if not isinstance(response, dict):
        log.warning(
            "mirofish.adapter.bad_response_shape",
            ticker=ticker,
            response_type=type(response).__name__,
        )
        return []

    raw_opinions = response.get("opinions", [])
    if not isinstance(raw_opinions, list):
        log.warning(
            "mirofish.adapter.bad_response_shape",
            ticker=ticker,
            opinions_type=type(raw_opinions).__name__,
        )
        return []

    run_group_id: str = response.get("run_id", fingerprint)

    # Soft cap: truncate a runaway response (defends against pathology).
    if len(raw_opinions) > MAX_OPINIONS_PER_RUN:
        log.warning(
            "mirofish.adapter.opinions_truncated",
            ticker=ticker,
            received=len(raw_opinions),
            cap=MAX_OPINIONS_PER_RUN,
        )
        raw_opinions = raw_opinions[:MAX_OPINIONS_PER_RUN]

    # ------------------------------------------------------------------
    # 3. Cache the raw results (write-once)
    # ------------------------------------------------------------------
    if conn is not None and raw_opinions:
        # Stamp the run_group_id into each raw opinion dict so cache replay
        # can reconstruct it without calling back to MiroFish.
        cache_payload = [
            {**op, "run_group_id": run_group_id} for op in raw_opinions
        ]
        try:
            run_cache.put(
                conn,
                fingerprint,
                as_of_date_str,
                cache_payload,
                run_group_id,
                # Stamp the real information timestamp so created_at means
                # something (fixes the "NO_CLOCK" / migration-comment mismatch).
                created_at=as_of.isoformat(),
            )
        except Exception as exc:
            # Cache write failure is non-fatal — we still return opinions.
            log.warning(
                "mirofish.adapter.cache_write_error",
                ticker=ticker,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # 4. Convert to Opinion objects
    # ------------------------------------------------------------------
    opinions = _opinions_from_response(
        raw_opinions, run_group_id, ticker, as_of, fingerprint
    )

    log.info(
        "mirofish.adapter.run.complete",
        ticker=ticker,
        opinion_count=len(opinions),
        run_group_id=run_group_id,
    )
    return opinions
