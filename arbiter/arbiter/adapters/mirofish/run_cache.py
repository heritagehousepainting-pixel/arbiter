"""Run cache for MiroFish (A2) — Lane 7.

MiroFish runs are:
  - Non-deterministic (LLM/quant model internals vary per call).
  - Expensive (~15–20 min per ticker).
  - Forward-test-only: results must NEVER be used for backtesting, because
    the model's output is influenced by information available at call time,
    not the information set at ``as_of``.  Using cached results in a
    historical simulation would introduce look-ahead bias.

Cache design:
  - Key: ``(idea_fingerprint, as_of_date_str)`` — same idea, same day →
    reuse the cached opinions.  The ``as_of_date_str`` component ensures
    that runs on different days are NOT reused, even for the same idea,
    because intraday data may have advanced the information set.
  - Storage: SQLite table ``mirofish_run_cache`` (migration 007).
  - Cache entries are write-once (insert-only, INTERFACES.md §10).
  - Expiry: none in v1 — stale results are prevented by keying on
    ``as_of_date_str`` (a cache miss occurs naturally when the date rolls).
  - Forward-test guard: ``is_forward_test_only`` column is always written
    as ``1``; any attempt to read cache entries for a backtest will raise
    ``BacktestCacheError``.

Public API:
    get(conn, idea_fingerprint, as_of_date_str) -> list[dict] | None
    put(conn, idea_fingerprint, as_of_date_str, raw_opinions, run_id) -> str
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


class BacktestCacheError(RuntimeError):
    """Raised if the cache is about to be used in a backtest context.

    MiroFish run results are forward-test-only.  They must never be replayed
    in historical simulations to avoid look-ahead bias.
    """


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get(
    conn: sqlite3.Connection,
    idea_fingerprint: str,
    as_of_date_str: str,
    *,
    is_backtest: bool = False,
) -> list[dict] | None:
    """Return cached opinions for this fingerprint+date, or None on miss.

    Precondition:
        ``conn.row_factory`` MUST be ``sqlite3.Row`` — this function reads the
        result column by name (``row["raw_opinions_json"]``).  Production
        ``db/connection.py`` sets this; the test helper sets it.  A tuple-factory
        connection raises ``TypeError`` here, which ``adapter.run`` catches and
        degrades to a cache miss.

    Args:
        conn:              Active SQLite connection (WAL mode, row_factory=Row).
        idea_fingerprint:  Stable hash of the idea being analyzed.
        as_of_date_str:    ISO date string for the information date
                           (``as_of.date().isoformat()``).
        is_backtest:       If ``True``, raises ``BacktestCacheError``
                           before any DB access.  Pass ``True`` in
                           historical simulation contexts.

    Returns:
        List of raw opinion dicts (as stored) on cache hit, or ``None``
        on cache miss.

    Raises:
        BacktestCacheError: If ``is_backtest=True``.
    """
    if is_backtest:
        raise BacktestCacheError(
            "MiroFish run cache must not be used in backtest mode. "
            "MiroFish outputs are forward-test-only (look-ahead bias risk). "
            "Run a fresh MiroFish call with the target as_of, or abstain."
        )

    row = conn.execute(
        """
        SELECT raw_opinions_json, run_id
        FROM mirofish_run_cache
        WHERE idea_fingerprint = ?
          AND as_of_date       = ?
        LIMIT 1
        """,
        (idea_fingerprint, as_of_date_str),
    ).fetchone()

    if row is None:
        return None

    return json.loads(row["raw_opinions_json"])


def put(
    conn: sqlite3.Connection,
    idea_fingerprint: str,
    as_of_date_str: str,
    raw_opinions: list[dict],
    run_id: str,
    created_at: str | None = None,
) -> str:
    """Insert a new cache entry and return the cache row id (ULID).

    Cache entries are write-once (INTERFACES.md §10 — insert-only).
    Duplicate calls for the same ``(fingerprint, date)`` will raise
    ``sqlite3.IntegrityError`` (the table has a UNIQUE constraint on
    that pair).  Callers should call ``get()`` first (check-then-act
    is safe because only one process writes cache entries per run).

    Args:
        conn:              Active SQLite connection (WAL mode).
        idea_fingerprint:  Stable hash of the idea being analyzed.
        as_of_date_str:    ISO date string.
        raw_opinions:      List of raw opinion dicts from MiroFish.
        run_id:            Shared run group ID (ULID or MiroFish-assigned).
        created_at:        Information timestamp (tz-aware UTC ISO string).
                           ``adapter.run`` threads ``as_of.isoformat()`` here so
                           the column carries the real information timestamp;
                           falls back to the ``"NO_CLOCK"`` sentinel when the
                           caller omits it (never reads the wall clock here).

    Note:
        This calls ``conn.commit()`` internally — it commits the caller's
        transaction.  Fine for the standalone forward path; a future batched
        caller should be aware of the implicit commit.

    Returns:
        The ULID primary key of the newly inserted cache row.
    """
    from ulid import ULID

    row_id = str(ULID())
    # Write-time bookkeeping timestamp. Supplied by the caller's clock
    # (Lane 3) when available; falls back to the scaffold's "NO_CLOCK"
    # sentinel rather than calling datetime.now() here (convention §11.1).
    created_at = created_at if created_at is not None else "NO_CLOCK"

    conn.execute(
        """
        INSERT INTO mirofish_run_cache
            (id, idea_fingerprint, as_of_date, run_id,
             raw_opinions_json, is_forward_test_only, created_at)
        VALUES (?, ?, ?, ?, ?, 1, ?)
        """,
        (
            row_id,
            idea_fingerprint,
            as_of_date_str,
            run_id,
            json.dumps(raw_opinions),
            created_at,
        ),
    )
    conn.commit()
    return row_id
