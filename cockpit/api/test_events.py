"""Tests for the SSE event stream (Lane 2).

Strategy
--------
* No network, no real DB.
* We point ``COCKPIT_AUDIT_PATH`` at a temporary file we control.
* We run ``event_stream()`` in a background asyncio Task and collect SSE
  frames via an asyncio Queue with a timeout.  This avoids the Python 3.14
  issue where ``asyncio.wait_for(anext(gen), ...)`` cancels the generator's
  internal ``asyncio.sleep`` when the timeout fires.
* We test all audit-event → Event-kind mappings, rotation/truncation
  resilience, missing-file → heartbeat, and partial-line buffering.

pytest-asyncio ``asyncio_mode = "auto"`` is set via the ini marker below.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# pytest-asyncio: auto mode
# ---------------------------------------------------------------------------
pytest_plugins = ("pytest_asyncio",)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _jl(ts: str, event: str, payload: dict) -> str:
    """Build one audit JSONL line."""
    return json.dumps({"ts": ts, "event": event, "payload": payload}) + "\n"


TS = "2026-06-22T12:00:00+00:00"


def _decode_sse(frame: str) -> dict:
    """Decode a raw SSE frame string into the JSON payload dict."""
    for part in frame.split("\n"):
        part = part.strip()
        if part.startswith("data:"):
            return json.loads(part[len("data:"):].strip())
    raise ValueError(f"no data: line in SSE frame: {frame!r}")


async def _drain(
    queue: asyncio.Queue,
    n: int,
    timeout: float = 5.0,
) -> list[dict]:
    """Pull up to *n* items from *queue* with per-item *timeout*."""
    results: list[dict] = []
    for _ in range(n):
        item = await asyncio.wait_for(queue.get(), timeout=timeout)
        results.append(_decode_sse(item))
    return results


class _EventHarness:
    """Runs event_stream() in a background task; frames go to a queue."""

    def __init__(self, ev_mod, queue: asyncio.Queue):
        self._ev_mod = ev_mod
        self._queue = queue
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        async def _pump():
            try:
                async for frame in self._ev_mod.event_stream():
                    await self._queue.put(frame)
            except asyncio.CancelledError:
                pass

        self._task = asyncio.create_task(_pump())
        # Give the generator time to initialise (seek to EOF, etc.)
        await asyncio.sleep(0.2)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# Fixture: isolated audit path per test
# ---------------------------------------------------------------------------

@pytest.fixture
def audit_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a fresh temp file wired as COCKPIT_AUDIT_PATH."""
    p = tmp_path / "audit.jsonl"
    p.touch()
    monkeypatch.setenv("COCKPIT_AUDIT_PATH", str(p))
    return p


def _load_ev_mod(monkeypatch, heartbeat_s: float = 30.0, poll_s: float = 0.05):
    """Reload events module and patch timing constants."""
    import importlib
    import cockpit.api.events as ev_mod
    importlib.reload(ev_mod)
    monkeypatch.setattr(ev_mod, "_HEARTBEAT_EVERY_S", heartbeat_s)
    monkeypatch.setattr(ev_mod, "_POLL_INTERVAL_S", poll_s)
    return ev_mod


# ===========================================================================
# 1. Missing file → heartbeat
# ===========================================================================

@pytest.mark.asyncio
async def test_missing_file_yields_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the audit file doesn't exist, stream must emit heartbeats."""
    missing = tmp_path / "nonexistent.jsonl"
    monkeypatch.setenv("COCKPIT_AUDIT_PATH", str(missing))
    ev_mod = _load_ev_mod(monkeypatch, heartbeat_s=0.05, poll_s=0.05)

    queue: asyncio.Queue = asyncio.Queue()
    harness = _EventHarness(ev_mod, queue)
    await harness.start()
    try:
        events = await _drain(queue, 1, timeout=5.0)
    finally:
        await harness.stop()

    assert events[0]["kind"] == "heartbeat"
    assert "infra.daemon" in events[0]["node_ids"]


# ===========================================================================
# 2. Idle heartbeat when no new lines appear
# ===========================================================================

@pytest.mark.asyncio
async def test_idle_heartbeat(audit_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty (but present) file must emit heartbeats."""
    ev_mod = _load_ev_mod(monkeypatch, heartbeat_s=0.05, poll_s=0.05)

    queue: asyncio.Queue = asyncio.Queue()
    harness = _EventHarness(ev_mod, queue)
    await harness.start()
    try:
        events = await _drain(queue, 1, timeout=5.0)
    finally:
        await harness.stop()

    assert events[0]["kind"] == "heartbeat"
    assert events[0]["node_ids"] == ["infra.daemon"]


# ===========================================================================
# 3. persist_opinion  →  "opinion"
# ===========================================================================

@pytest.mark.asyncio
async def test_persist_opinion_with_idea(
    audit_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ev_mod = _load_ev_mod(monkeypatch)

    queue: asyncio.Queue = asyncio.Queue()
    harness = _EventHarness(ev_mod, queue)
    await harness.start()
    try:
        audit_file.write_text(
            _jl(TS, "persist_opinion", {
                "id": "OP1", "advisor_id": "A1.insider", "ticker": "AAPL",
                "idea_id": "IDEA-42", "stance_score": 0.7, "confidence": 0.8,
            })
        )
        events = await _drain(queue, 1, timeout=5.0)
    finally:
        await harness.stop()

    assert events[0]["kind"] == "opinion"
    assert "A1.insider" in events[0]["node_ids"]
    assert "idea.IDEA-42" in events[0]["node_ids"]


@pytest.mark.asyncio
async def test_persist_opinion_without_idea_uses_ticker(
    audit_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ev_mod = _load_ev_mod(monkeypatch)

    queue: asyncio.Queue = asyncio.Queue()
    harness = _EventHarness(ev_mod, queue)
    await harness.start()
    try:
        audit_file.write_text(
            _jl(TS, "persist_opinion", {
                "id": "OP2", "advisor_id": "A2.mirofish", "ticker": "TSLA",
                "idea_id": None, "stance_score": 0.5, "confidence": 0.6,
            })
        )
        events = await _drain(queue, 1, timeout=5.0)
    finally:
        await harness.stop()

    assert events[0]["kind"] == "opinion"
    assert "A2.mirofish" in events[0]["node_ids"]
    assert "trade.TSLA" in events[0]["node_ids"]


# ===========================================================================
# 4. order.submitted  →  "fill"
# ===========================================================================

@pytest.mark.asyncio
async def test_order_submitted(
    audit_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ev_mod = _load_ev_mod(monkeypatch)

    queue: asyncio.Queue = asyncio.Queue()
    harness = _EventHarness(ev_mod, queue)
    await harness.start()
    try:
        audit_file.write_text(
            _jl(TS, "order.submitted", {
                "order_id": "ORD1", "ticker": "AMZN", "side": "BUY",
                "qty": 2.0, "status": "filled",
            })
        )
        events = await _drain(queue, 1, timeout=5.0)
    finally:
        await harness.stop()

    assert events[0]["kind"] == "fill"
    assert "exec.adapter" in events[0]["node_ids"]
    assert "trade.AMZN" in events[0]["node_ids"]


# ===========================================================================
# 5. order.reconciled_fill  →  "fill"
# ===========================================================================

@pytest.mark.asyncio
async def test_order_reconciled_fill(
    audit_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ev_mod = _load_ev_mod(monkeypatch)

    queue: asyncio.Queue = asyncio.Queue()
    harness = _EventHarness(ev_mod, queue)
    await harness.start()
    try:
        audit_file.write_text(
            _jl(TS, "order.reconciled_fill", {
                "order_id": "ORD2", "ticker": "UBER", "side": "BUY",
                "new_status": "filled", "filled_qty": 1.0, "avg_fill_price": 75.0,
            })
        )
        events = await _drain(queue, 1, timeout=5.0)
    finally:
        await harness.stop()

    assert events[0]["kind"] == "fill"
    assert "exec.adapter" in events[0]["node_ids"]
    assert "trade.UBER" in events[0]["node_ids"]


# ===========================================================================
# 6. exit_monitor.trigger  →  "cover"
# ===========================================================================

@pytest.mark.asyncio
async def test_exit_monitor_trigger(
    audit_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ev_mod = _load_ev_mod(monkeypatch)

    queue: asyncio.Queue = asyncio.Queue()
    harness = _EventHarness(ev_mod, queue)
    await harness.start()
    try:
        audit_file.write_text(
            _jl(TS, "exit_monitor.trigger", {
                "ticker": "T", "reason": "stop_loss", "shares": 50,
            })
        )
        events = await _drain(queue, 1, timeout=5.0)
    finally:
        await harness.stop()

    assert events[0]["kind"] == "cover"
    assert "exec.exit_monitor" in events[0]["node_ids"]
    assert "trade.T" in events[0]["node_ids"]


# ===========================================================================
# 7. exit_monitor.closed  →  "outcome"
# ===========================================================================

@pytest.mark.asyncio
async def test_exit_monitor_closed(
    audit_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ev_mod = _load_ev_mod(monkeypatch)

    queue: asyncio.Queue = asyncio.Queue()
    harness = _EventHarness(ev_mod, queue)
    await harness.start()
    try:
        audit_file.write_text(
            _jl(TS, "exit_monitor.closed", {
                "idea_id": "01XYZ", "ticker": "MSFT",
                "label_kind": "normal", "exit_price": 210.0, "outcome_id": "OUT1",
            })
        )
        events = await _drain(queue, 1, timeout=5.0)
    finally:
        await harness.stop()

    assert events[0]["kind"] == "outcome"
    assert "idea.01XYZ" in events[0]["node_ids"]


# ===========================================================================
# 8. idea_state_transition: GATHERING  →  "idea_new"
# ===========================================================================

@pytest.mark.asyncio
async def test_idea_state_transition_gathering(
    audit_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ev_mod = _load_ev_mod(monkeypatch)

    queue: asyncio.Queue = asyncio.Queue()
    harness = _EventHarness(ev_mod, queue)
    await harness.start()
    try:
        audit_file.write_text(
            _jl(TS, "idea_state_transition", {
                "idea_id": "IDEA-NEW", "new_state": "GATHERING",
            })
        )
        events = await _drain(queue, 1, timeout=5.0)
    finally:
        await harness.stop()

    assert events[0]["kind"] == "idea_new"
    assert "idea.IDEA-NEW" in events[0]["node_ids"]


# ===========================================================================
# 9. idea_state_transition: other state  →  "idea_transition"
# ===========================================================================

@pytest.mark.asyncio
async def test_idea_state_transition_other(
    audit_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ev_mod = _load_ev_mod(monkeypatch)

    queue: asyncio.Queue = asyncio.Queue()
    harness = _EventHarness(ev_mod, queue)
    await harness.start()
    try:
        audit_file.write_text(
            _jl(TS, "idea_state_transition", {
                "idea_id": "IDEA-OLD", "new_state": "MONITORED",
            })
        )
        events = await _drain(queue, 1, timeout=5.0)
    finally:
        await harness.stop()

    assert events[0]["kind"] == "idea_transition"
    assert "idea.IDEA-OLD" in events[0]["node_ids"]


# ===========================================================================
# 10. breaker_trip / breaker_reset / engine.auto_paused  →  "breaker"
# ===========================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("audit_event", ["breaker_trip", "breaker_reset", "engine.auto_paused"])
async def test_breaker_events(
    audit_event: str,
    audit_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ev_mod = _load_ev_mod(monkeypatch)

    queue: asyncio.Queue = asyncio.Queue()
    harness = _EventHarness(ev_mod, queue)
    await harness.start()
    try:
        audit_file.write_text(
            _jl(TS, audit_event, {"breaker_name": "daily_loss", "reason": "test"})
        )
        events = await _drain(queue, 1, timeout=5.0)
    finally:
        await harness.stop()

    assert events[0]["kind"] == "breaker"
    assert "core.safety" in events[0]["node_ids"]
    assert "infra.daemon" in events[0]["node_ids"]


# ===========================================================================
# 11. alert.critical / alert.warning / alert.fired  →  "alert"
# ===========================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("audit_event", ["alert.critical", "alert.warning", "alert.fired"])
async def test_alert_events(
    audit_event: str,
    audit_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ev_mod = _load_ev_mod(monkeypatch)

    queue: asyncio.Queue = asyncio.Queue()
    harness = _EventHarness(ev_mod, queue)
    await harness.start()
    try:
        audit_file.write_text(
            _jl(TS, audit_event, {"message": "test alert", "tier": "critical"})
        )
        events = await _drain(queue, 1, timeout=5.0)
    finally:
        await harness.stop()

    assert events[0]["kind"] == "alert"
    assert "infra.alerting" in events[0]["node_ids"]


# ===========================================================================
# 12. Ignored events do NOT produce output (only heartbeats come)
# ===========================================================================

@pytest.mark.asyncio
async def test_ignored_events_no_output(
    audit_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """safety_gate_decision and reconciler.pass should be ignored."""
    ev_mod = _load_ev_mod(monkeypatch, heartbeat_s=0.1, poll_s=0.05)

    queue: asyncio.Queue = asyncio.Queue()
    harness = _EventHarness(ev_mod, queue)
    await harness.start()
    try:
        audit_file.write_text(
            _jl(TS, "safety_gate_decision", {"allowed": True}) +
            _jl(TS, "reconciler.pass", {})
        )
        # We should get a heartbeat (not a fill/opinion/etc) within heartbeat window
        events = await _drain(queue, 1, timeout=5.0)
    finally:
        await harness.stop()

    assert events[0]["kind"] == "heartbeat"


# ===========================================================================
# 13. Multiple events in sequence
# ===========================================================================

@pytest.mark.asyncio
async def test_multiple_events_in_sequence(
    audit_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ev_mod = _load_ev_mod(monkeypatch)

    queue: asyncio.Queue = asyncio.Queue()
    harness = _EventHarness(ev_mod, queue)
    await harness.start()
    try:
        lines = (
            _jl(TS, "order.submitted", {"ticker": "AAPL", "side": "BUY", "qty": 1.0, "status": "filled"}) +
            _jl(TS, "persist_opinion", {
                "id": "OP3", "advisor_id": "A1.congress", "ticker": "AAPL",
                "idea_id": "IDEA-99", "stance_score": 0.6, "confidence": 0.7,
            }) +
            _jl(TS, "breaker_trip", {"breaker_name": "daily_loss", "reason": "test"})
        )
        audit_file.write_text(lines)
        events = await _drain(queue, 3, timeout=5.0)
    finally:
        await harness.stop()

    kinds = [e["kind"] for e in events]
    assert "fill" in kinds
    assert "opinion" in kinds
    assert "breaker" in kinds


# ===========================================================================
# 14. Truncation / rotation resilience
# ===========================================================================

@pytest.mark.asyncio
async def test_rotation_resilience(
    audit_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the file shrinks (rotation), the stream recovers and reads from start."""
    ev_mod = _load_ev_mod(monkeypatch)

    # Pre-populate so the file is non-empty when the generator opens
    big_content = _jl(TS, "safety_gate_decision", {"ignored": True}) * 10
    audit_file.write_text(big_content)

    queue: asyncio.Queue = asyncio.Queue()
    harness = _EventHarness(ev_mod, queue)
    await harness.start()
    try:
        # Truncate to a smaller file with a real event
        audit_file.write_text(
            _jl(TS, "order.submitted", {
                "ticker": "GOOG", "side": "BUY", "qty": 1.0, "status": "filled"
            })
        )
        events = await _drain(queue, 1, timeout=5.0)
    finally:
        await harness.stop()

    assert events[0]["kind"] == "fill"
    assert "trade.GOOG" in events[0]["node_ids"]


# ===========================================================================
# 15. Partial line (no trailing newline yet)
# ===========================================================================

@pytest.mark.asyncio
async def test_partial_line_waits_for_newline(
    audit_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial line (no \\n yet) should not be emitted until complete."""
    ev_mod = _load_ev_mod(monkeypatch, heartbeat_s=30.0, poll_s=0.05)

    queue: asyncio.Queue = asyncio.Queue()
    harness = _EventHarness(ev_mod, queue)
    await harness.start()
    try:
        # Write partial JSON (no newline)
        partial = json.dumps({
            "ts": TS, "event": "order.submitted",
            "payload": {"ticker": "FB", "side": "BUY", "qty": 1.0, "status": "filled"}
        })
        audit_file.write_text(partial)  # no trailing \n

        # Nothing should come yet (heartbeat interval is 30s)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.4)

        # Complete the line
        with open(audit_file, "a") as f:
            f.write("\n")

        events = await _drain(queue, 1, timeout=5.0)
    finally:
        await harness.stop()

    assert events[0]["kind"] == "fill"
    assert "trade.FB" in events[0]["node_ids"]


# ===========================================================================
# 16. Only NEW lines streamed (seek to EOF on start)
# ===========================================================================

@pytest.mark.asyncio
async def test_seek_to_end_on_start(
    audit_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-existing lines in the file must NOT be replayed."""
    ev_mod = _load_ev_mod(monkeypatch, heartbeat_s=0.1, poll_s=0.05)

    # Write existing content BEFORE the generator starts
    audit_file.write_text(
        _jl(TS, "order.submitted", {
            "ticker": "OLD", "side": "BUY", "qty": 1.0, "status": "filled"
        })
    )

    queue: asyncio.Queue = asyncio.Queue()
    harness = _EventHarness(ev_mod, queue)
    await harness.start()
    try:
        # The generator should have sought to EOF; no fill from "OLD" should appear.
        # Only a heartbeat (due to short interval) should come.
        events = await _drain(queue, 1, timeout=5.0)
    finally:
        await harness.stop()

    # Should be heartbeat, not a fill for "OLD"
    assert events[0]["kind"] == "heartbeat"


# ===========================================================================
# 17. Unknown advisor ID in persist_opinion falls back gracefully
# ===========================================================================

@pytest.mark.asyncio
async def test_unknown_advisor_falls_back(
    audit_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ev_mod = _load_ev_mod(monkeypatch)

    queue: asyncio.Queue = asyncio.Queue()
    harness = _EventHarness(ev_mod, queue)
    await harness.start()
    try:
        audit_file.write_text(
            _jl(TS, "persist_opinion", {
                "id": "OP4", "advisor_id": "A9.unknown", "ticker": "XYZ",
                "idea_id": "IDEA-1", "stance_score": 0.5, "confidence": 0.5,
            })
        )
        events = await _drain(queue, 1, timeout=5.0)
    finally:
        await harness.stop()

    # Unknown advisor is dropped but idea_id gives us a node_id
    assert events[0]["kind"] == "opinion"
    assert "A9.unknown" not in events[0]["node_ids"]
    assert "idea.IDEA-1" in events[0]["node_ids"]


# ===========================================================================
# 18. _map_audit_line unit tests (pure, synchronous)
# ===========================================================================

def test_map_opinion_unit() -> None:
    from cockpit.api.events import _map_audit_line
    line = _jl(TS, "persist_opinion", {
        "id": "X", "advisor_id": "A1.activist", "ticker": "NFLX",
        "idea_id": "I99", "stance_score": 0.8, "confidence": 0.9,
    })
    ev = _map_audit_line(line)
    assert ev is not None
    assert ev.kind == "opinion"
    assert "A1.activist" in ev.node_ids
    assert "idea.I99" in ev.node_ids


def test_map_fill_submitted_unit() -> None:
    from cockpit.api.events import _map_audit_line
    line = _jl(TS, "order.submitted", {
        "ticker": "TSLA", "side": "SELL", "qty": 5.0, "status": "filled"
    })
    ev = _map_audit_line(line)
    assert ev is not None
    assert ev.kind == "fill"
    assert ev.node_ids == ["exec.adapter", "trade.TSLA"]


def test_map_fill_reconciled_unit() -> None:
    from cockpit.api.events import _map_audit_line
    line = _jl(TS, "order.reconciled_fill", {
        "ticker": "amzn", "side": "BUY", "new_status": "filled"
    })
    ev = _map_audit_line(line)
    assert ev is not None
    assert ev.kind == "fill"
    # ticker should be uppercased
    assert "trade.AMZN" in ev.node_ids


def test_map_cover_unit() -> None:
    from cockpit.api.events import _map_audit_line
    line = _jl(TS, "exit_monitor.trigger", {
        "ticker": "T", "reason": "reversal", "shares": 100
    })
    ev = _map_audit_line(line)
    assert ev is not None
    assert ev.kind == "cover"
    assert "exec.exit_monitor" in ev.node_ids
    assert "trade.T" in ev.node_ids


def test_map_outcome_unit() -> None:
    from cockpit.api.events import _map_audit_line
    line = _jl(TS, "exit_monitor.closed", {
        "idea_id": "ABC", "ticker": "NVDA", "label_kind": "normal",
        "exit_price": 500.0, "outcome_id": "OUT2",
    })
    ev = _map_audit_line(line)
    assert ev is not None
    assert ev.kind == "outcome"
    assert "idea.ABC" in ev.node_ids


def test_map_idea_new_unit() -> None:
    from cockpit.api.events import _map_audit_line
    line = _jl(TS, "idea_state_transition", {"idea_id": "NEW1", "new_state": "GATHERING"})
    ev = _map_audit_line(line)
    assert ev is not None
    assert ev.kind == "idea_new"
    assert ev.node_ids == ["idea.NEW1"]


def test_map_idea_transition_unit() -> None:
    from cockpit.api.events import _map_audit_line
    line = _jl(TS, "idea_state_transition", {"idea_id": "OLD1", "new_state": "MONITORED"})
    ev = _map_audit_line(line)
    assert ev is not None
    assert ev.kind == "idea_transition"
    assert ev.node_ids == ["idea.OLD1"]


def test_map_breaker_trip_unit() -> None:
    from cockpit.api.events import _map_audit_line
    line = _jl(TS, "breaker_trip", {"breaker_name": "daily_loss", "reason": "test"})
    ev = _map_audit_line(line)
    assert ev is not None
    assert ev.kind == "breaker"
    assert set(ev.node_ids) == {"core.safety", "infra.daemon"}


def test_map_breaker_reset_unit() -> None:
    from cockpit.api.events import _map_audit_line
    line = _jl(TS, "breaker_reset", {"breaker_name": "daily_loss"})
    ev = _map_audit_line(line)
    assert ev is not None
    assert ev.kind == "breaker"


def test_map_alert_critical_unit() -> None:
    from cockpit.api.events import _map_audit_line
    line = _jl(TS, "alert.critical", {"message": "boom", "tier": "critical"})
    ev = _map_audit_line(line)
    assert ev is not None
    assert ev.kind == "alert"
    assert ev.node_ids == ["infra.alerting"]


def test_map_alert_warning_unit() -> None:
    from cockpit.api.events import _map_audit_line
    line = _jl(TS, "alert.warning", {"message": "warn", "tier": "warning"})
    ev = _map_audit_line(line)
    assert ev is not None
    assert ev.kind == "alert"
    assert ev.node_ids == ["infra.alerting"]


def test_map_ignored_event_unit() -> None:
    from cockpit.api.events import _map_audit_line
    line = _jl(TS, "safety_gate_decision", {"allowed": True})
    ev = _map_audit_line(line)
    assert ev is None


def test_map_empty_line_unit() -> None:
    from cockpit.api.events import _map_audit_line
    assert _map_audit_line("") is None
    assert _map_audit_line("   ") is None
    assert _map_audit_line("not json {{{") is None


def test_map_engine_auto_paused_unit() -> None:
    from cockpit.api.events import _map_audit_line
    line = _jl(TS, "engine.auto_paused", {"reason": "kill switch"})
    ev = _map_audit_line(line)
    assert ev is not None
    assert ev.kind == "breaker"
    assert "core.safety" in ev.node_ids
    assert "infra.daemon" in ev.node_ids
