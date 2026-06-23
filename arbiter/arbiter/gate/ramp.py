"""Paper→live staged ramp — Lane 12c (ramp.py).

Staged live-trading ramp: 10 % → 25 % → 50 % → 100 %.
Step-ups are MANUAL only; no automatic advancement.
Current stage is persisted in ``gate_ramp`` (insert-only).

Design constraints:
    - No ``datetime.now()`` — ``as_of`` always injected by caller.
    - Insert-only; the latest row (by ``advanced_at``) is authoritative.
    - ``from __future__ import annotations`` (py3.11+).

Public API
----------
STAGE_ORDER : tuple[int, ...]
    Canonical sequence of allowed stage percentages: (10, 25, 50, 100).

current_stage(conn) -> int | None
    Return the current stage percentage from DB, or None if never set.

advance_stage(conn, *, advanced_by, as_of, note=None) -> int
    Advance to the NEXT stage in STAGE_ORDER.  Returns the new stage pct.
    Raises ``StageLimitError`` if already at 100 %.
    Raises ``NoStageSetError`` if no stage has been initialised yet
    (must call ``init_ramp`` first or pass ``force_pct``).

init_ramp(conn, *, advanced_by, as_of, note=None) -> int
    Set the initial stage to 10 %.  Raises ``RampAlreadyInitialised`` if a
    stage row already exists.

stage_multiplier(conn) -> float
    Return the fractional position multiplier for the current stage
    (e.g. stage 10 → 0.10).  Returns 0.0 if no stage is set (fail-closed).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STAGE_ORDER: tuple[int, ...] = (10, 25, 50, 100)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class StageLimitError(Exception):
    """Raised when ``advance_stage`` is called at the maximum stage (100 %)."""


class NoStageSetError(Exception):
    """Raised when ``advance_stage`` is called before ``init_ramp``."""


class RampAlreadyInitialised(Exception):
    """Raised when ``init_ramp`` is called but a stage row already exists."""


# ---------------------------------------------------------------------------
# ULID-compatible ID generator
# ---------------------------------------------------------------------------

def _new_ulid() -> str:
    import uuid
    return str(uuid.uuid4()).replace("-", "").upper()[:26]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def current_stage(conn: sqlite3.Connection) -> Optional[int]:
    """Return the most-recent stage percentage from ``gate_ramp``, or None.

    "Most-recent" is determined by ``advanced_at`` (DESC), then ``rowid``.
    """
    row = conn.execute(
        """
        SELECT stage_pct
        FROM   gate_ramp
        ORDER  BY advanced_at DESC, rowid DESC
        LIMIT  1
        """
    ).fetchone()
    if row is None:
        return None
    return int(row[0])


def _insert_stage(
    conn: sqlite3.Connection,
    stage_pct: int,
    *,
    advanced_by: str,
    as_of: datetime,
    note: Optional[str],
) -> int:
    """Insert a new stage row and return the stage_pct."""
    row_id = _new_ulid()
    conn.execute(
        """
        INSERT INTO gate_ramp (id, stage_pct, advanced_by, advanced_at, note)
        VALUES (?, ?, ?, ?, ?)
        """,
        (row_id, stage_pct, advanced_by, as_of.isoformat(), note),
    )
    conn.commit()
    return stage_pct


def init_ramp(
    conn: sqlite3.Connection,
    *,
    advanced_by: str,
    as_of: datetime,
    note: Optional[str] = None,
) -> int:
    """Initialise the ramp at the first stage (10 %).

    Parameters
    ----------
    conn:
        Active SQLite connection with ``gate_ramp`` table.
    advanced_by:
        Human identifier for whoever initialised the ramp.
    as_of:
        Timestamp (tz-aware UTC).
    note:
        Optional free-text note.

    Returns
    -------
    int
        The initial stage percentage (always 10).

    Raises
    ------
    RampAlreadyInitialised
        If a stage row already exists.
    """
    if current_stage(conn) is not None:
        raise RampAlreadyInitialised(
            "Ramp is already initialised. Use advance_stage() to move to the next step."
        )
    return _insert_stage(conn, STAGE_ORDER[0], advanced_by=advanced_by, as_of=as_of, note=note)


def advance_stage(
    conn: sqlite3.Connection,
    *,
    advanced_by: str,
    as_of: datetime,
    note: Optional[str] = None,
) -> int:
    """Advance to the next stage in STAGE_ORDER and persist it.

    Manual only — no automatic advancement.  Steps exactly one stage at a time.

    Parameters
    ----------
    conn:
        Active SQLite connection with ``gate_ramp`` table.
    advanced_by:
        Human identifier for the operator performing the step-up.
    as_of:
        Timestamp (tz-aware UTC).
    note:
        Optional free-text note.

    Returns
    -------
    int
        The new stage percentage (25, 50, or 100).

    Raises
    ------
    NoStageSetError
        If ``init_ramp`` has never been called.
    StageLimitError
        If already at the maximum stage (100 %).
    """
    stage = current_stage(conn)
    if stage is None:
        raise NoStageSetError(
            "No ramp stage has been set. Call init_ramp() first."
        )

    try:
        idx = STAGE_ORDER.index(stage)
    except ValueError:
        # Defensive: persisted value is not in the canonical sequence.
        raise ValueError(
            f"Persisted stage_pct={stage} is not in STAGE_ORDER={STAGE_ORDER}."
        )

    if idx == len(STAGE_ORDER) - 1:
        raise StageLimitError(
            f"Already at maximum stage ({STAGE_ORDER[-1]} %). Cannot advance further."
        )

    next_stage = STAGE_ORDER[idx + 1]
    return _insert_stage(conn, next_stage, advanced_by=advanced_by, as_of=as_of, note=note)


def stage_multiplier(conn: sqlite3.Connection) -> float:
    """Return the fractional position-size multiplier for the current stage.

    Returns 0.0 when no stage is set (fail-closed).

    Examples
    --------
    stage 10  → 0.10
    stage 25  → 0.25
    stage 50  → 0.50
    stage 100 → 1.00
    None      → 0.00
    """
    stage = current_stage(conn)
    if stage is None:
        return 0.0
    return stage / 100.0
