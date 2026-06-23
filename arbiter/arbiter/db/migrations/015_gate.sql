-- 015_gate.sql  — Paper→live gate tables (Lane 12c)
-- Insert-only by convention (INTERFACES.md §11.2).
-- Primary keys are ULIDs stored as TEXT.
-- All timestamps are tz-aware UTC ISO strings stored as TEXT.

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ---------------------------------------------------------------------------
-- gate_approvals
-- Stores manual approvals of the paper→live transition.
-- Each approval expires 30 days after approved_at.
-- New approval = new row (insert-only; never UPDATE).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gate_approvals (
    id              TEXT PRIMARY KEY,   -- ULID
    approved_by     TEXT NOT NULL,      -- human identifier / username
    approved_at     TEXT NOT NULL,      -- tz-aware UTC ISO string
    expires_at      TEXT NOT NULL,      -- approved_at + 30 days (tz-aware UTC ISO string)
    criteria_hash   TEXT NOT NULL,      -- hash of the criteria set at time of approval
    note            TEXT,               -- optional human note
    supersedes_id   TEXT,               -- for corrections (insert-only convention)
    is_superseded   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_gate_approvals_approved_at
    ON gate_approvals (approved_at);

CREATE INDEX IF NOT EXISTS idx_gate_approvals_expires_at
    ON gate_approvals (expires_at);

-- ---------------------------------------------------------------------------
-- gate_hash_lock
-- Stores the criteria hash that was in effect when a run first checked the
-- gate.  Changing the criteria set mid-run is detected by comparing the live
-- hash against the locked hash.
-- One row per run_id (run_id = ULID identifying the current cycle/session).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gate_hash_lock (
    run_id          TEXT PRIMARY KEY,   -- ULID identifying this run/session
    criteria_hash   TEXT NOT NULL,      -- SHA-256 hex of the frozen criteria config
    locked_at       TEXT NOT NULL       -- tz-aware UTC ISO string
);

-- ---------------------------------------------------------------------------
-- gate_ramp
-- Tracks the current live-trading ramp stage.
-- Stages: 10 -> 25 -> 50 -> 100 (manual step-up only).
-- Insert-only: advancing the stage adds a new row; the latest row wins.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gate_ramp (
    id              TEXT PRIMARY KEY,   -- ULID
    stage_pct       INTEGER NOT NULL,   -- 10 | 25 | 50 | 100
    advanced_by     TEXT NOT NULL,      -- human identifier / username
    advanced_at     TEXT NOT NULL,      -- tz-aware UTC ISO string
    note            TEXT
);

CREATE INDEX IF NOT EXISTS idx_gate_ramp_advanced_at
    ON gate_ramp (advanced_at);
