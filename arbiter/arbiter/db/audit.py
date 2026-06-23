"""Append-only audit log for Arbiter (Lane 2).

Each call to ``audit()`` appends one JSON line to the file at
``Config.audit_path``.  The audit log is **authoritative** on divergence with
the DB (INTERFACES.md §10).

Design constraints:
    - No ``datetime.now()`` calls — the clock is owned by Lane 3.  The
      timestamp must be passed in by the caller.  If omitted the sentinel
      ``"NO_CLOCK"`` is written (same pattern as MetricsWriter in Lane 1).
    - File is append-only; never truncated or re-written by this module.

Public API:
    audit(event, payload, *, ts=None, audit_path=None) -> None
    read_audit(audit_path=None) -> list[dict]
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# Sentinel used when no timestamp is supplied — mirrors the scaffold convention
# used by MetricsWriter (INTERFACES.md §10b.4).
_NO_CLOCK_SENTINEL = "NO_CLOCK"


# ---------------------------------------------------------------------------
# Module-level injectable for tests / Lane 3 wiring.
# Lane 3 may replace this with a real Clock callable:
#   import arbiter.db.audit as _audit_mod
#   _audit_mod._clock = lambda: clock.now().isoformat()
# ---------------------------------------------------------------------------
_clock: Any = None  # None -> use sentinel


def _resolve_ts(ts: str | None) -> str:
    """Return a timestamp string, falling back to the module clock or sentinel."""
    if ts is not None:
        return ts
    if _clock is not None:
        return _clock()
    return _NO_CLOCK_SENTINEL


def _resolve_audit_path(audit_path: str | Path | None) -> Path:
    """Return the audit file path, defaulting to Config.audit_path."""
    if audit_path is not None:
        return Path(audit_path)
    from arbiter.config import load_config
    cfg = load_config()
    return Path(cfg.audit_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def audit(
    event: str,
    payload: dict,
    *,
    ts: str | None = None,
    audit_path: str | Path | None = None,
) -> None:
    """Append one JSON line to the audit log.

    Args:
        event:      Short event name, e.g. ``"insert_opinion"``.
        payload:    Arbitrary dict — will be serialised to JSON.
        ts:         ISO timestamp string.  If ``None``, falls back to the
                    module-level ``_clock`` callable, then to the sentinel
                    ``"NO_CLOCK"``.
        audit_path: Override the audit file path (useful in tests).
    """
    path = _resolve_audit_path(audit_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "ts": _resolve_ts(ts),
        "event": event,
        "payload": payload,
    }
    line = json.dumps(record, default=str)

    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_audit(audit_path: str | Path | None = None) -> list[dict]:
    """Read all lines from the audit log and return as a list of dicts.

    Returns an empty list if the file does not exist yet.  Intended for tests
    and diagnostics only — not a production query path.
    """
    path = _resolve_audit_path(audit_path)
    if not path.exists():
        return []

    records: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records
