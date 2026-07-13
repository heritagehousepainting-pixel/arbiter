"""Cockpit projection of the canonical robotics universe (DISPLAY-ONLY).

The data itself now lives in ``arbiter/arbiter/data/robotics_universe.py`` — the
single source of truth shared with the robotics early-insight signal.  This
module is a thin projection of it for the cockpit's ``GET /robotics-watchlist``.

Display-only invariant (enforced by ``test_robotics_watchlist.py::TestRosterPurity``):
this module imports ONLY the pure-data ``arbiter.data.robotics_universe`` module and
NOTHING that reaches ``sectors.py`` / ``_DEFAULT_WATCHLIST`` / the engine.  A symbol
appearing here can never become trade-eligible; only an explicit, separately-reviewed
edit to ``arbiter/arbiter/data/sectors.py`` does that
(see docs/specs/2026-07-13-robotics-watchlist-design.md §7).

The ``arbiter`` import is lazy (inside ``robotics_roster()``) with the same
``sys.path`` bootstrap ``ticker.py`` uses, so importing this module never requires
``arbiter`` to be on the path at cockpit start-up.
"""
from __future__ import annotations

import sys

from .db import DEFAULT_DB_PATH

# Display timestamp for the roster (kept here so it is import-safe; the canonical
# module owns the same value under GENERATED).
GENERATED = "2026-07-13"

_ARBITER_PKG_ROOT = DEFAULT_DB_PATH.parents[1]  # <repo>/arbiter


def robotics_roster() -> list[dict]:
    """Return the curated roster as a list of dicts (one per RoboticsRosterEntry).

    Projects the canonical ``arbiter.data.robotics_universe`` verbatim.
    """
    if str(_ARBITER_PKG_ROOT) not in sys.path:
        sys.path.insert(0, str(_ARBITER_PKG_ROOT))
    from arbiter.data.robotics_universe import robotics_universe  # noqa: PLC0415

    return robotics_universe()
