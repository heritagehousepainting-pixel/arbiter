"""Identity resolution sub-package (Lane 5c).

Public API
----------
- ``resolve_person(name, source, hints, conn, clock)`` — stable dedup of
  insiders / members across filings; returns a canonical person_id (ULID).
"""
from __future__ import annotations

from arbiter.ingest.identity.resolver import resolve_person

__all__ = ["resolve_person"]
