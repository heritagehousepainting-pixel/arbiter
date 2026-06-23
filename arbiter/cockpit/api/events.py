"""Live event stream (SSE) — OWNED BY LANE 2.

Tails the arbiter audit log (``data/audit.jsonl``) and maps raw
audit-log lines to typed ``Event`` frames that the web client subscribes to
via ``GET /events`` (Server-Sent-Events).

Key design decisions
--------------------
* **Strictly read-only** — we open the file in read mode only; never write.
* **Seek to END on start** — only *new* lines appended after the stream
  opens are forwarded.  Existing history is not replayed on connection.
* **Non-blocking** — file reads are brief and performed inside ``asyncio.sleep``
  gaps; we poll every ~0.5 s, reading synchronously but cheaply.
* **Resilient** — file missing → emit heartbeats until it appears; file
  truncated/rotated (size shrinks below last offset) → reopen and seek to 0;
  partial last line (no trailing newline yet) → buffer and wait.
* **Heartbeat cadence** — one ``heartbeat`` event every ~10 s when idle
  (no new lines arrived in that window).
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .contract import Event
from .db import DEFAULT_DB_PATH

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------
_DEFAULT_AUDIT_PATH: Path = DEFAULT_DB_PATH.parent / "audit.jsonl"


def _audit_path() -> Path:
    override = os.environ.get("COCKPIT_AUDIT_PATH")
    return Path(override) if override else _DEFAULT_AUDIT_PATH


# ---------------------------------------------------------------------------
# SSE framing
# ---------------------------------------------------------------------------

def _sse(event: Event) -> str:
    return f"data: {json.dumps(event.model_dump())}\n\n"


# ---------------------------------------------------------------------------
# Audit-event → Contract-Event mapping
# ---------------------------------------------------------------------------

_VALID_ADVISORS = frozenset(
    {"A1.insider", "A1.congress", "A1.activist", "A2.mirofish"}
)


def _safe_advisor(raw: Optional[str]) -> Optional[str]:
    """Return advisor_id only if it is a known graph node id."""
    if raw and raw in _VALID_ADVISORS:
        return raw
    return None


def _map_audit_line(raw: str) -> Optional[Event]:
    """Parse one JSON audit-log line and return a typed Event, or None."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        return None

    event_name: str = record.get("event", "")
    payload: dict = record.get("payload", {})
    ts: str = record.get("ts") or datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # persist_opinion  →  "opinion"
    # node_ids: [advisor_id, "idea.<idea_id>"]  (or "trade.<TICKER>" if no idea)
    # ------------------------------------------------------------------
    if event_name == "persist_opinion":
        advisor = _safe_advisor(payload.get("advisor_id"))
        idea_id = payload.get("idea_id")
        ticker = payload.get("ticker", "")
        node_ids: list[str] = []
        if advisor:
            node_ids.append(advisor)
        if idea_id:
            node_ids.append(f"idea.{idea_id}")
        elif ticker:
            node_ids.append(f"trade.{ticker.upper()}")
        if not node_ids:
            return None
        return Event(ts=ts, kind="opinion", node_ids=node_ids, payload=payload)

    # ------------------------------------------------------------------
    # order.submitted / order.reconciled_fill  →  "fill"
    # node_ids: ["exec.adapter", "trade.<TICKER>"]
    # ------------------------------------------------------------------
    if event_name in ("order.submitted", "order.reconciled_fill"):
        ticker = payload.get("ticker", "")
        if not ticker:
            return None
        return Event(
            ts=ts,
            kind="fill",
            node_ids=["exec.adapter", f"trade.{ticker.upper()}"],
            payload=payload,
        )

    # ------------------------------------------------------------------
    # exit_monitor.trigger  →  "cover"
    # node_ids: ["exec.exit_monitor", "trade.<TICKER>"]
    # ------------------------------------------------------------------
    if event_name == "exit_monitor.trigger":
        ticker = payload.get("ticker", "")
        if not ticker:
            return None
        return Event(
            ts=ts,
            kind="cover",
            node_ids=["exec.exit_monitor", f"trade.{ticker.upper()}"],
            payload=payload,
        )

    # ------------------------------------------------------------------
    # exit_monitor.closed  →  "outcome"
    # node_ids: ["idea.<idea_id>"]  (+ advisor if present in payload)
    # ------------------------------------------------------------------
    if event_name == "exit_monitor.closed":
        idea_id = payload.get("idea_id")
        if not idea_id:
            return None
        node_ids = [f"idea.{idea_id}"]
        advisor = _safe_advisor(payload.get("advisor_id"))
        if advisor:
            node_ids.append(advisor)
        return Event(ts=ts, kind="outcome", node_ids=node_ids, payload=payload)

    # ------------------------------------------------------------------
    # idea_state_transition  →  "idea_new" (if GATHERING) or "idea_transition"
    # node_ids: ["idea.<idea_id>"]
    # ------------------------------------------------------------------
    if event_name == "idea_state_transition":
        idea_id = payload.get("idea_id")
        new_state = payload.get("new_state", "")
        if not idea_id:
            return None
        kind = "idea_new" if new_state == "GATHERING" else "idea_transition"
        return Event(
            ts=ts,
            kind=kind,
            node_ids=[f"idea.{idea_id}"],
            payload=payload,
        )

    # ------------------------------------------------------------------
    # breaker_trip / breaker_reset / engine.auto_paused  →  "breaker"
    # node_ids: ["core.safety", "infra.daemon"]
    # ------------------------------------------------------------------
    if event_name in ("breaker_trip", "breaker_reset", "engine.auto_paused"):
        return Event(
            ts=ts,
            kind="breaker",
            node_ids=["core.safety", "infra.daemon"],
            payload=payload,
        )

    # ------------------------------------------------------------------
    # alert.* (alert.critical / alert.warning / alert.fired)  →  "alert"
    # node_ids: ["infra.alerting"]
    # ------------------------------------------------------------------
    if event_name.startswith("alert."):
        return Event(
            ts=ts,
            kind="alert",
            node_ids=["infra.alerting"],
            payload=payload,
        )

    # Ignore everything else (safety_gate_decision, reconciler.pass, etc.)
    return None


# ---------------------------------------------------------------------------
# Heartbeat helper
# ---------------------------------------------------------------------------

def _make_heartbeat(ts: Optional[str] = None) -> Event:
    return Event(
        ts=ts or datetime.now(timezone.utc).isoformat(),
        kind="heartbeat",
        node_ids=["infra.daemon"],
        payload={},
    )


# ---------------------------------------------------------------------------
# Sync tail helpers (called from asyncio.to_thread to avoid blocking)
# ---------------------------------------------------------------------------

def _file_size(path: Path) -> int:
    """Return file size in bytes, or -1 if the file doesn't exist."""
    try:
        return path.stat().st_size
    except (FileNotFoundError, PermissionError):
        return -1


def _read_new_bytes(path: Path, offset: int) -> tuple[str, int, int]:
    """Read new content from *path* starting at *offset*.

    Returns ``(text, new_offset, current_size)``.  If the file doesn't exist
    returns ``("", offset, -1)``.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            text = f.read()
            new_offset = f.tell()
            f.seek(0, 2)   # SEEK_END
            size = f.tell()
        return text, new_offset, size
    except (FileNotFoundError, PermissionError):
        return "", offset, -1


def _get_eof_offset(path: Path) -> tuple[int, int]:
    """Return ``(eof_offset, size)`` by seeking to end, or ``(-1, -1)`` on error."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            pos = f.tell()
        return pos, pos
    except (FileNotFoundError, PermissionError):
        return -1, -1


# ---------------------------------------------------------------------------
# Public async generator
# ---------------------------------------------------------------------------

#: Poll interval in seconds (how often we check for new data).
_POLL_INTERVAL_S: float = 0.5

#: Emit a heartbeat if no events have been yielded for this many seconds.
_HEARTBEAT_EVERY_S: float = 10.0


async def event_stream() -> AsyncIterator[str]:
    """Async generator yielding SSE-framed Event JSON strings.

    Seeks to the end of the audit log on start so only *new* events are
    forwarded.  Emits a heartbeat every ~10 s when idle.  Never raises.
    """
    audit_path = _audit_path()
    offset: int = -1          # -1 = not yet initialised
    last_known_size: int = -1
    buf: str = ""
    last_event_at: float = 0.0

    # Seek to end on startup (non-blocking)
    try:
        eof, size = await asyncio.to_thread(_get_eof_offset, audit_path)
        if eof >= 0:
            offset = eof
            last_known_size = size
    except Exception:
        pass

    last_event_at = asyncio.get_event_loop().time()

    while True:
        now = asyncio.get_event_loop().time()

        # ----- File not yet found: just heartbeat -----
        if offset < 0:
            if now - last_event_at >= _HEARTBEAT_EVERY_S:
                yield _sse(_make_heartbeat())
                last_event_at = asyncio.get_event_loop().time()
            await asyncio.sleep(_POLL_INTERVAL_S)
            # Retry opening
            try:
                eof, size = await asyncio.to_thread(_get_eof_offset, audit_path)
                if eof >= 0:
                    offset = eof
                    last_known_size = size
            except Exception:
                pass
            continue

        # ----- Read new data -----
        try:
            chunk, new_offset, current_size = await asyncio.to_thread(
                _read_new_bytes, audit_path, offset
            )
        except Exception:
            await asyncio.sleep(_POLL_INTERVAL_S)
            continue

        # ----- Detect rotation / truncation -----
        if current_size >= 0 and last_known_size >= 0 and current_size < last_known_size:
            # File shrank → reset to beginning
            offset = 0
            last_known_size = 0
            buf = ""
            await asyncio.sleep(_POLL_INTERVAL_S)
            continue

        if current_size >= 0:
            last_known_size = current_size
        offset = new_offset

        # ----- Process complete lines from the buffer -----
        if chunk:
            buf += chunk
            lines = buf.split("\n")
            # The last element is either "" (complete) or a partial fragment.
            buf = lines[-1]
            for line in lines[:-1]:
                ev = _map_audit_line(line)
                if ev is not None:
                    yield _sse(ev)
                    last_event_at = asyncio.get_event_loop().time()

        # ----- Heartbeat when idle -----
        now = asyncio.get_event_loop().time()
        if now - last_event_at >= _HEARTBEAT_EVERY_S:
            yield _sse(_make_heartbeat())
            last_event_at = asyncio.get_event_loop().time()

        await asyncio.sleep(_POLL_INTERVAL_S)
