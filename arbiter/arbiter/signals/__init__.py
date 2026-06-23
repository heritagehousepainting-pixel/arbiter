"""A1 signal detection, scoring, leaderboard, and Opinion emission (Lane 6).

Public surface
--------------
- :mod:`arbiter.signals.detection`  — detect opportunistic cluster buys and
  single-insider / congress signals from ``filings`` rows.
- :mod:`arbiter.signals.scoring`    — score signal-types AND people side-by-side.
- :mod:`arbiter.signals.emit`       — build a valid :class:`~arbiter.contract.opinion.Opinion`
  from a detected signal (or return ``None`` to abstain).
- :mod:`arbiter.signals.leaderboard` — CLI table of signal-types and people
  with accuracy/sample columns; gate-failing rows grayed.

Design rules (INTERFACES.md §11)
---------------------------------
- No ``datetime.now()``; callers pass ``as_of``.
- Abstain = ``None``; never ``stance_score = 0.0``.
- Imports from frozen interfaces only: ``arbiter.contract.opinion``,
  ``arbiter.types``, ``arbiter.db.connection``, ``arbiter.config``.
- Never imports the ingest lane or another in-progress lane.
"""
from __future__ import annotations

from arbiter.signals.detection import Signal, SignalType, detect_signals
from arbiter.signals.scoring import ScoreBundle, score_signal_type, score_person
from arbiter.signals.emit import emit_opinion
from arbiter.signals.leaderboard import render_leaderboard

__all__ = [
    # detection
    "Signal",
    "SignalType",
    "detect_signals",
    # scoring
    "ScoreBundle",
    "score_signal_type",
    "score_person",
    # emit
    "emit_opinion",
    # leaderboard
    "render_leaderboard",
]
