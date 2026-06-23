"""Tests for arbiter.db.audit (Lane 2).

Verifies:
  - audit() appends a well-formed JSON line to the audit file.
  - read_audit() round-trips the written record.
  - Multiple appends accumulate; file is strictly append-only (no truncation).
  - ts sentinel "NO_CLOCK" is written when no timestamp is supplied.
  - Explicit ts string is preserved verbatim.
  - read_audit() returns [] when the file does not exist.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from arbiter.db.audit import audit, read_audit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _audit_path(tmp_path: Path, name: str = "audit.jsonl") -> Path:
    return tmp_path / name


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_audit_creates_file(tmp_path) -> None:
    path = _audit_path(tmp_path)
    assert not path.exists()
    audit("test_event", {"key": "value"}, audit_path=path)
    assert path.exists()


def test_audit_writes_valid_json(tmp_path) -> None:
    path = _audit_path(tmp_path)
    audit("my_event", {"x": 1}, audit_path=path)

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "my_event"
    assert record["payload"] == {"x": 1}
    assert "ts" in record


def test_audit_roundtrip(tmp_path) -> None:
    path = _audit_path(tmp_path)
    payload = {"advisor_id": "A1.insider", "ticker": "AAPL", "stance": 0.8}
    audit("opinion_inserted", payload, ts="2025-01-01T00:00:00+00:00", audit_path=path)

    records = read_audit(path)
    assert len(records) == 1
    rec = records[0]
    assert rec["event"] == "opinion_inserted"
    assert rec["payload"] == payload
    assert rec["ts"] == "2025-01-01T00:00:00+00:00"


def test_audit_multiple_appends(tmp_path) -> None:
    path = _audit_path(tmp_path)
    for i in range(5):
        audit(f"event_{i}", {"i": i}, ts=f"2025-01-0{i+1}T00:00:00+00:00", audit_path=path)

    records = read_audit(path)
    assert len(records) == 5
    for i, rec in enumerate(records):
        assert rec["event"] == f"event_{i}"
        assert rec["payload"]["i"] == i


def test_audit_no_clock_sentinel(tmp_path) -> None:
    """When no ts is supplied and _clock is None, the sentinel must appear."""
    import arbiter.db.audit as audit_mod

    original_clock = audit_mod._clock
    try:
        audit_mod._clock = None  # ensure sentinel
        path = _audit_path(tmp_path, "no_clock.jsonl")
        audit("sentinel_test", {}, audit_path=path)
        records = read_audit(path)
        assert records[0]["ts"] == "NO_CLOCK"
    finally:
        audit_mod._clock = original_clock


def test_audit_explicit_ts_preserved(tmp_path) -> None:
    path = _audit_path(tmp_path)
    ts = "2025-12-31T23:59:59+00:00"
    audit("ts_test", {}, ts=ts, audit_path=path)
    records = read_audit(path)
    assert records[0]["ts"] == ts


def test_read_audit_empty_when_no_file(tmp_path) -> None:
    path = tmp_path / "nonexistent.jsonl"
    result = read_audit(path)
    assert result == []


def test_audit_file_is_append_only(tmp_path) -> None:
    """Verify that each call ADDS to the file, never overwrites."""
    path = _audit_path(tmp_path)

    audit("first", {"seq": 1}, ts="2025-01-01T00:00:00+00:00", audit_path=path)
    size_after_one = path.stat().st_size

    audit("second", {"seq": 2}, ts="2025-01-02T00:00:00+00:00", audit_path=path)
    size_after_two = path.stat().st_size

    assert size_after_two > size_after_one, (
        "File size must grow on second write (append-only)"
    )

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_audit_injectable_clock(tmp_path) -> None:
    """When _clock is set the returned value is used as ts."""
    import arbiter.db.audit as audit_mod

    original_clock = audit_mod._clock
    try:
        audit_mod._clock = lambda: "CLOCK_INJECTED"
        path = _audit_path(tmp_path, "clock_inject.jsonl")
        audit("clock_test", {}, audit_path=path)
        records = read_audit(path)
        assert records[0]["ts"] == "CLOCK_INJECTED"
    finally:
        audit_mod._clock = original_clock


def test_audit_payload_survives_serialization(tmp_path) -> None:
    """Complex payloads round-trip through JSON correctly."""
    path = _audit_path(tmp_path)
    payload = {
        "nested": {"a": [1, 2, 3]},
        "float": 0.123456789,
        "bool": True,
        "null": None,
    }
    audit("complex_payload", payload, ts="2025-01-01T00:00:00+00:00", audit_path=path)
    records = read_audit(path)
    assert records[0]["payload"] == payload
