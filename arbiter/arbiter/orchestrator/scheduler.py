"""Advisor scheduler — Lane 13.

Runs advisor callables in a bounded thread pool with fault isolation:
- A timed-out or crashed advisor yields None (null opinion) and the cycle
  continues without that advisor's input.
- Advisors are NOT imported here — they are injected as callables.

This module is a **scheduled loop helper**, NOT a daemon.  The caller
(cycle.py or a CLI cron) decides when to invoke ``run_advisors_parallel``.
"""
from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Sequence

from arbiter.contract.opinion import Opinion

logger = logging.getLogger(__name__)

# Default per-advisor wall-clock timeout (seconds).
DEFAULT_ADVISOR_TIMEOUT: float = 30.0

# Default thread pool size.  Large enough to overlap I/O-bound advisors.
DEFAULT_MAX_WORKERS: int = 8


def run_advisors_parallel(
    advisors: Sequence[Callable[[], Opinion | None]],
    *,
    timeout_seconds: float = DEFAULT_ADVISOR_TIMEOUT,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> list[Opinion | None]:
    """Invoke *advisors* in parallel; return one slot per advisor (None on fault).

    Each advisor callable is called with no arguments and must return either
    an ``Opinion`` or ``None`` (abstain).  If the callable raises an exception
    or exceeds *timeout_seconds*, that slot becomes ``None`` and the cycle
    continues — the fault is logged at WARNING level.

    Parameters
    ----------
    advisors:
        Sequence of zero-argument callables.  Each callable represents one
        advisor run for the current cycle.  The list preserves ordering so
        callers can zip against advisor IDs.
    timeout_seconds:
        Per-advisor timeout.  Advisors that exceed this are cancelled and
        their slot becomes None.
    max_workers:
        Maximum threads in the pool.

    Returns
    -------
    list[Opinion | None]
        One entry per input advisor, in the same order.  ``None`` means the
        advisor abstained OR faulted (log distinguishes the two).
    """
    if not advisors:
        return []

    results: list[Opinion | None] = [None] * len(advisors)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures: list[tuple[int, Future]] = [
            (idx, pool.submit(fn))
            for idx, fn in enumerate(advisors)
        ]

        for idx, future in futures:
            try:
                results[idx] = future.result(timeout=timeout_seconds)
            except FuturesTimeoutError:
                logger.warning(
                    "Advisor at index %d timed out after %.1fs — yielding null opinion",
                    idx,
                    timeout_seconds,
                )
                future.cancel()
                results[idx] = None
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Advisor at index %d raised %s: %s — yielding null opinion",
                    idx,
                    type(exc).__name__,
                    exc,
                )
                results[idx] = None

    return results


def run_named_advisors_parallel(
    advisor_map: dict[str, Callable[[], Opinion | None]],
    *,
    timeout_seconds: float = DEFAULT_ADVISOR_TIMEOUT,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict[str, Opinion | None]:
    """Like ``run_advisors_parallel`` but keyed by advisor_id string.

    This is the preferred API for ``cycle.py`` — it preserves the advisor_id
    association so the caller doesn't need to track index order.

    Parameters
    ----------
    advisor_map:
        Dict mapping advisor_id → zero-argument callable.
    timeout_seconds:
        Per-advisor timeout.
    max_workers:
        Maximum pool threads.

    Returns
    -------
    dict[str, Opinion | None]
        One entry per advisor_id.  None = abstained or faulted.
    """
    if not advisor_map:
        return {}

    ids = list(advisor_map.keys())
    callables = [advisor_map[aid] for aid in ids]

    raw = run_advisors_parallel(
        callables,
        timeout_seconds=timeout_seconds,
        max_workers=max_workers,
    )

    return dict(zip(ids, raw))
