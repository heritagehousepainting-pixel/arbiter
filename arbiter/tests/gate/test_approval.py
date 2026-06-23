"""Tests for arbiter.gate.approval — paper→live manual approval gate.

Covers:
- record_approval() inserts a row; is_approved() returns True
- Approval older than 30 days → expired → is_approved() returns False
- No approval at all → is_approved() returns False (default LIVE off, fail-closed)
- current_approval() returns None when expired
- Expiry is computed from as_of + 30 days (not wall-clock)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from ulid import ULID

from arbiter.gate.approval import (
    APPROVAL_EXPIRY_DAYS,
    is_approved,
    current_approval,
    record_approval,
)
from arbiter.gate.criteria import CRITERIA_HASH


UTC = timezone.utc
NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    """Return an in-memory connection with the gate_approvals table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE gate_approvals (
            id            TEXT PRIMARY KEY,
            approved_by   TEXT NOT NULL,
            approved_at   TEXT NOT NULL,
            expires_at    TEXT NOT NULL,
            criteria_hash TEXT NOT NULL,
            note          TEXT,
            supersedes_id TEXT,
            is_superseded INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Default: no approval
# ---------------------------------------------------------------------------

class TestNoApproval:

    def test_is_approved_false_when_no_rows(self):
        conn = _conn()
        assert is_approved(conn, as_of=NOW) is False

    def test_current_approval_none_when_no_rows(self):
        conn = _conn()
        assert current_approval(conn, as_of=NOW) is None


# ---------------------------------------------------------------------------
# Valid approval
# ---------------------------------------------------------------------------

class TestValidApproval:

    def test_record_returns_id(self):
        conn = _conn()
        row_id = record_approval(conn, approved_by="operator", as_of=NOW)
        assert isinstance(row_id, str) and len(row_id) > 0

    def test_record_returns_valid_ulid(self):
        """ID returned by record_approval must be a valid Crockford-base32 ULID."""
        conn = _conn()
        row_id = record_approval(conn, approved_by="operator", as_of=NOW)
        # ULID() parse will raise ValueError if the string is not a valid ULID.
        parsed = ULID.from_str(row_id)
        assert str(parsed) == row_id

    def test_record_returns_26_char_ulid(self):
        """A valid ULID is exactly 26 characters."""
        conn = _conn()
        row_id = record_approval(conn, approved_by="operator", as_of=NOW)
        assert len(row_id) == 26

    def test_is_approved_true_after_record(self):
        conn = _conn()
        record_approval(conn, approved_by="operator", as_of=NOW)
        assert is_approved(conn, as_of=NOW) is True

    def test_current_approval_returns_row(self):
        conn = _conn()
        record_approval(conn, approved_by="operator", as_of=NOW)
        row = current_approval(conn, as_of=NOW)
        assert row is not None
        assert row["approved_by"] == "operator"

    def test_expires_at_is_30_days_out(self):
        conn = _conn()
        record_approval(conn, approved_by="operator", as_of=NOW)
        row = current_approval(conn, as_of=NOW)
        assert row is not None
        # expires_at should be NOW + 30 days
        expected_expires = NOW + timedelta(days=APPROVAL_EXPIRY_DAYS)
        assert row["expires_at"] == expected_expires.isoformat()

    def test_criteria_hash_stored(self):
        conn = _conn()
        record_approval(conn, approved_by="operator", as_of=NOW)
        row = current_approval(conn, as_of=NOW)
        assert row is not None
        assert row["criteria_hash"] == CRITERIA_HASH


# ---------------------------------------------------------------------------
# Expired approval (>30 days old)
# ---------------------------------------------------------------------------

class TestExpiredApproval:

    def test_approval_expired_after_30_days(self):
        conn = _conn()
        # Record an approval 31 days ago
        approved_at = NOW - timedelta(days=31)
        record_approval(conn, approved_by="operator", as_of=approved_at)

        # Check from NOW (31 days after approval — past expiry)
        assert is_approved(conn, as_of=NOW) is False

    def test_approval_exactly_at_expiry_boundary_is_expired(self):
        """expires_at == as_of: the query uses > (strict), so this is expired."""
        conn = _conn()
        approved_at = NOW - timedelta(days=APPROVAL_EXPIRY_DAYS)
        record_approval(conn, approved_by="operator", as_of=approved_at)

        # as_of == expires_at → NOT > → expired
        assert is_approved(conn, as_of=NOW) is False

    def test_approval_one_second_before_expiry_is_valid(self):
        """expires_at = NOW + 1 second → still valid."""
        conn = _conn()
        approved_at = NOW - timedelta(days=APPROVAL_EXPIRY_DAYS) + timedelta(seconds=1)
        record_approval(conn, approved_by="operator", as_of=approved_at)

        assert is_approved(conn, as_of=NOW) is True

    def test_current_approval_none_when_expired(self):
        conn = _conn()
        approved_at = NOW - timedelta(days=31)
        record_approval(conn, approved_by="operator", as_of=approved_at)

        assert current_approval(conn, as_of=NOW) is None

    def test_new_approval_after_expiry_restores_access(self):
        conn = _conn()
        # Old, expired approval
        old_approved_at = NOW - timedelta(days=31)
        record_approval(conn, approved_by="operator", as_of=old_approved_at)

        # New, fresh approval
        record_approval(conn, approved_by="operator2", as_of=NOW)

        assert is_approved(conn, as_of=NOW) is True


# ---------------------------------------------------------------------------
# Superseded approval
# ---------------------------------------------------------------------------

class TestSupersededApproval:

    def test_superseded_approval_is_not_active(self):
        conn = _conn()
        row_id = record_approval(conn, approved_by="operator", as_of=NOW)

        # Supersede the row manually (insert-only: new row + flip flag)
        conn.execute(
            "UPDATE gate_approvals SET is_superseded = 1 WHERE id = ?", (row_id,)
        )
        conn.commit()

        assert is_approved(conn, as_of=NOW) is False


# ---------------------------------------------------------------------------
# Note and custom criteria_hash
# ---------------------------------------------------------------------------

class TestApprovalMetadata:

    def test_note_stored(self):
        conn = _conn()
        record_approval(conn, approved_by="operator", as_of=NOW, note="LGTM")
        row = current_approval(conn, as_of=NOW)
        assert row is not None
        assert row["note"] == "LGTM"

    def test_custom_criteria_hash(self):
        conn = _conn()
        custom_hash = "b" * 64
        record_approval(
            conn, approved_by="operator", as_of=NOW, criteria_hash=custom_hash
        )
        row = current_approval(conn, as_of=NOW)
        assert row is not None
        assert row["criteria_hash"] == custom_hash
