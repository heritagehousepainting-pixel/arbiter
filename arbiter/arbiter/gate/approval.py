"""Paper→live gate approval — Lane 12c (approval.py).

Manual ``--approve-live`` approval gate.  Approvals expire every 30 days.
Default: LIVE off.  Fail-closed: no approval / expired → live disabled.

Design constraints:
    - No ``datetime.now()`` — ``as_of`` always injected by caller.
    - Insert-only (corrections = new rows with ``supersedes_id``).
    - ``from __future__ import annotations`` (py3.11+).

Public API
----------
record_approval(conn, *, approved_by, as_of, criteria_hash, note=None) -> str
    Write a new approval row. Returns the row ULID.

current_approval(conn, *, as_of) -> sqlite3.Row | None
    Return the most-recent non-superseded, non-expired approval row, or None.

is_approved(conn, *, as_of) -> bool
    True only when a valid, unexpired approval exists.

APPROVAL_EXPIRY_DAYS : int
    30 — exported so tests and callers don't hard-code the constant.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from arbiter.db.helpers import generate_ulid
from arbiter.gate.criteria import CRITERIA_HASH


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APPROVAL_EXPIRY_DAYS: int = 30


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record_approval(
    conn: sqlite3.Connection,
    *,
    approved_by: str,
    as_of: datetime,
    criteria_hash: str = CRITERIA_HASH,
    note: Optional[str] = None,
    supersedes_id: Optional[str] = None,
) -> str:
    """Insert a new approval record and return its ULID.

    Parameters
    ----------
    conn:
        Active SQLite connection (must have ``gate_approvals`` table).
    approved_by:
        Human identifier for the approver (e.g. CLI username or ``--approve-live`` flag).
    as_of:
        Approval timestamp (tz-aware UTC).  Expiry is computed as ``as_of + 30 days``.
    criteria_hash:
        Hash of the criteria set at approval time.  Defaults to the live ``CRITERIA_HASH``.
    note:
        Optional free-text note.
    supersedes_id:
        ULID of the previous approval row this corrects (insert-only convention).

    Returns
    -------
    str
        ULID of the newly created approval row.
    """
    row_id = generate_ulid()
    expires_at = as_of + timedelta(days=APPROVAL_EXPIRY_DAYS)

    conn.execute(
        """
        INSERT INTO gate_approvals
            (id, approved_by, approved_at, expires_at, criteria_hash, note, supersedes_id, is_superseded)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            row_id,
            approved_by,
            as_of.isoformat(),
            expires_at.isoformat(),
            criteria_hash,
            note,
            supersedes_id,
        ),
    )
    conn.commit()
    return row_id


def current_approval(
    conn: sqlite3.Connection,
    *,
    as_of: datetime,
) -> Optional[sqlite3.Row]:
    """Return the most-recent valid, unexpired, non-superseded approval row.

    An approval is valid when:
        - ``is_superseded = 0``
        - ``expires_at > as_of``

    Returns ``None`` if no such approval exists (fail-closed).
    """
    row = conn.execute(
        """
        SELECT *
        FROM   gate_approvals
        WHERE  is_superseded = 0
          AND  expires_at > ?
        ORDER  BY approved_at DESC
        LIMIT  1
        """,
        (as_of.isoformat(),),
    ).fetchone()
    return row


def is_approved(
    conn: sqlite3.Connection,
    *,
    as_of: datetime,
) -> bool:
    """Return True only when a valid, unexpired approval exists.

    Fail-closed: returns False on any exception or when no approval row found.
    """
    try:
        return current_approval(conn, as_of=as_of) is not None
    except Exception:  # noqa: BLE001
        return False
