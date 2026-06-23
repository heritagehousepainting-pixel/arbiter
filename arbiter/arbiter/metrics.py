"""Append-only metrics.jsonl writer.

Usage::

    from arbiter.metrics import MetricsWriter
    writer = MetricsWriter(path=Path("data/metrics.jsonl"))
    writer.record("cycle_complete", {"ideas": 3, "orders": 1})

Each line written is a JSON object with at minimum ``event`` and
``recorded_at`` fields (recorded_at is an ISO-8601 string provided by the
caller or injected as a placeholder; no ``datetime.now()`` here — per
INTERFACES.md §11 convention 1, wall-clock is owned by clock.py in lane 3).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class MetricsWriter:
    """Append-only writer to a ``metrics.jsonl`` file."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        recorded_at: str = "",
    ) -> None:
        """Append a single JSON line.

        ``recorded_at`` should be an ISO-8601 UTC string from the caller's
        ``Clock.now()`` (lane 3).  If omitted a sentinel ``"CLOCK_NOT_WIRED"``
        is written so the absence is visible and grep-able.
        """
        row: dict[str, Any] = {
            "event": event,
            "recorded_at": recorded_at or "CLOCK_NOT_WIRED",
        }
        row.update(payload)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
