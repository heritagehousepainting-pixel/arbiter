"""Stable identity resolution for insiders and Congress members (Lane 5c).

``resolve_person`` maps a raw name + source pair to a canonical person_id
(ULID).  The mapping is stored in the ``people`` table (migration 008_identity).
Once a name is resolved it is never re-assigned a different ULID; the table is
insert-only.

Normalisation rules (order matters)
-------------------------------------
1. Strip leading/trailing whitespace.
2. Collapse internal runs of whitespace to a single space.
3. Fold to upper-case (ASCII-safe; names are Latin-script in practice).
4. Remove punctuation characters that vary across filings: commas, periods,
   hyphens that connect name tokens (but keep intra-word hyphens e.g. "JEAN-
   LUC" is left as-is when the hyphen has letters on both sides).
   Trailing commas and periods are always dropped.
5. Strip the suffix tokens JR / SR / II / III / IV / V that appear at the end
   of the name — these are inconsistently present across Form 4 / Congress
   disclosures for the same person.

Design contract (INTERFACES.md §10, §11)
-----------------------------------------
- Insert-only store.  No UPDATE on the people table.
- ``conn`` must already have the 008_identity migration applied.
- ``clock`` is a callable returning an ISO timestamp string — never
  ``datetime.now()`` (INTERFACES.md §11.1).
- ``hints`` is an optional dict reserved for future hint fields (e.g. CIK,
  bioguide_id) that can short-circuit normalisation.  Currently only
  ``hints["person_id"]`` is honoured: if supplied and already in the DB the
  stored entry takes precedence; if not in the DB it is used as the ULID.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Callable

from arbiter.db.helpers import generate_ulid


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

_SUFFIX_RE = re.compile(
    r"\s+(?:JR|SR|II|III|IV|V)\.?$",
    flags=re.IGNORECASE,
)

_TRAILING_PUNCT_RE = re.compile(r"[,\.]+$")

_INTERNAL_SPACE_RE = re.compile(r"\s+")

# Middle-initial period: a single letter followed by a period and a space (or
# a single letter followed by a period at end-of-name after upper-casing).
# E.g. "R." in "ELON R. MUSK" -> "ELON R MUSK".
_MIDDLE_INITIAL_PERIOD_RE = re.compile(r"(?<=\b[A-Z])\.")


def _normalize_name(raw: str) -> str:
    """Return a canonical form of *raw* suitable for deduplication.

    Steps:
      1. Strip outer whitespace.
      2. Collapse internal whitespace runs to a single space.
      3. Upper-case.
      4. Remove trailing punctuation (commas, periods).
      5. Strip trailing suffix tokens (JR/SR/II/III/IV/V).
    """
    name = raw.strip()
    name = _INTERNAL_SPACE_RE.sub(" ", name)
    name = name.upper()
    # Remove periods after single-letter middle initials (e.g. "R." -> "R").
    name = _MIDDLE_INITIAL_PERIOD_RE.sub("", name)
    name = _TRAILING_PUNCT_RE.sub("", name)
    name = _SUFFIX_RE.sub("", name).rstrip()
    return name


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_person(
    name: str,
    source: str,
    hints: dict | None,
    conn: sqlite3.Connection,
    clock: Callable[[], str],
) -> str:
    """Return the canonical person_id (ULID) for *name* in *source*.

    If the normalised (name, source) pair already exists in the ``people``
    table the existing person_id is returned without any write.  Otherwise a
    new row is inserted and its person_id returned.

    Args:
        name:   Raw display name as it appears in the filing.
        source: ``"form4"`` or ``"congress"``.
        hints:  Optional dict.  Recognised keys:
                    ``person_id`` — if supplied and not yet in the DB it is
                    used as the ULID rather than generating a fresh one.
        conn:   Open SQLite connection with 008_identity migration applied.
        clock:  Callable returning an ISO timestamp string (no datetime.now).

    Returns:
        A stable ULID string for this person within *source*.
    """
    hints = hints or {}
    canonical = _normalize_name(name)

    # --- Fast path: already resolved ---
    row = conn.execute(
        "SELECT person_id FROM people WHERE canonical_name = ? AND source = ?",
        (canonical, source),
    ).fetchone()
    if row is not None:
        return str(row[0])

    # --- Hint path: caller supplied a person_id ---
    # If the hint person_id exists in the table (different canonical name but
    # same ULID), we honour the existing ULID.  If it's not in the table at
    # all, we use it as the PK for the new row.
    hinted_id: str | None = hints.get("person_id")
    if hinted_id:
        existing = conn.execute(
            "SELECT person_id FROM people WHERE person_id = ?",
            (hinted_id,),
        ).fetchone()
        if existing is not None:
            # The hinted ULID is already mapped to a different canonical name —
            # insert the new alias row using the same person_id only if the
            # UNIQUE constraint allows it (i.e. the (canonical, source) pair
            # is new).  Use INSERT OR IGNORE to stay idempotent.
            conn.execute(
                "INSERT OR IGNORE INTO people "
                "(person_id, canonical_name, source, created_at) VALUES (?,?,?,?)",
                (hinted_id, canonical, source, clock()),
            )
            conn.commit()
            return hinted_id

    # --- Insert new person ---
    person_id = hinted_id if hinted_id else generate_ulid()
    conn.execute(
        "INSERT OR IGNORE INTO people "
        "(person_id, canonical_name, source, created_at) VALUES (?,?,?,?)",
        (person_id, canonical, source, clock()),
    )
    conn.commit()

    # In the unlikely race where INSERT OR IGNORE was silently skipped (two
    # concurrent writers), re-read to return the winner's person_id.
    row = conn.execute(
        "SELECT person_id FROM people WHERE canonical_name = ? AND source = ?",
        (canonical, source),
    ).fetchone()
    return str(row[0])
