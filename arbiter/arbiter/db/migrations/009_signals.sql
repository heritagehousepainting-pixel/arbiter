-- 009_signals.sql  — A1 signal detection tables (Lane 6)
-- Insert-only (corrections via supersede_row).
-- All primary keys are ULIDs stored as TEXT.
-- All timestamps are tz-aware UTC ISO strings stored as TEXT.

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ---------------------------------------------------------------------------
-- signal_events
-- Raw detected signals emitted by detection.py.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signal_events (
    id                TEXT PRIMARY KEY,   -- ULID
    signal_type       TEXT NOT NULL,      -- e.g. "cluster_buy", "single_insider_buy", "congress_sector"
    ticker            TEXT NOT NULL,
    source            TEXT NOT NULL,      -- "form4" | "congress"
    person_ids        TEXT NOT NULL,      -- JSON array of person_id strings
    filing_ids        TEXT NOT NULL,      -- JSON array of filing ULIDs that compose this signal
    window_start      TEXT NOT NULL,      -- earliest filing_ts in window (tz-aware UTC ISO)
    window_end        TEXT NOT NULL,      -- latest filing_ts in window (tz-aware UTC ISO)
    conviction_score  REAL NOT NULL,      -- raw conviction [0,1] before scoring
    meta_json         TEXT,               -- additional detection metadata (JSON)
    as_of             TEXT NOT NULL,      -- information timestamp (tz-aware UTC ISO)
    supersedes_id     TEXT,
    is_superseded     INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signal_events_ticker_as_of
    ON signal_events (ticker, as_of);

CREATE INDEX IF NOT EXISTS idx_signal_events_signal_type
    ON signal_events (signal_type);

-- ---------------------------------------------------------------------------
-- signal_type_scores
-- Per signal-type track record cache (populated/refreshed by Lane 14 via
-- score_provider; only placeholder data until outcomes exist).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signal_type_scores (
    id              TEXT PRIMARY KEY,   -- ULID
    signal_type     TEXT NOT NULL,
    sample_count    INTEGER NOT NULL DEFAULT 0,
    accuracy        REAL,               -- NULL = cold-start (no outcomes yet)
    alpha_bps_avg   REAL,               -- NULL = cold-start
    gate_pass       INTEGER NOT NULL DEFAULT 0,  -- 1 if meets minimum sample/accuracy threshold
    as_of           TEXT NOT NULL,      -- when this snapshot was computed
    supersedes_id   TEXT,
    is_superseded   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_type_scores_type_as_of
    ON signal_type_scores (signal_type, as_of);

-- ---------------------------------------------------------------------------
-- person_scores
-- Per-insider / per-member track record cache (same pattern as signal_type_scores).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS person_scores (
    id              TEXT PRIMARY KEY,   -- ULID
    person_id       TEXT NOT NULL,
    person_name     TEXT,
    source          TEXT NOT NULL,      -- "form4" | "congress"
    sample_count    INTEGER NOT NULL DEFAULT 0,
    accuracy        REAL,               -- NULL = cold-start
    alpha_bps_avg   REAL,               -- NULL = cold-start
    gate_pass       INTEGER NOT NULL DEFAULT 0,
    as_of           TEXT NOT NULL,
    supersedes_id   TEXT,
    is_superseded   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_person_scores_person_as_of
    ON person_scores (person_id, as_of);
