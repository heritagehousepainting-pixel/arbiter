-- 011_trust.sql  — Trust ledger extended tables (Lane 11)
-- Insert-only; corrections via supersede_row (INTERFACES.md §11.2).
-- All PKs are ULIDs (TEXT). All timestamps are tz-aware UTC ISO strings.

-- ---------------------------------------------------------------------------
-- trust_ledger_snapshots
-- Full snapshot of a WeightBundle emission.  One row per weekly update run.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trust_ledger_snapshots (
    id              TEXT PRIMARY KEY,       -- ULID
    as_of           TEXT NOT NULL,          -- tz-aware UTC ISO string (reference timestamp)
    total_outcomes  INTEGER NOT NULL,       -- total non-abstain outcomes available at update time
    advisor_count   INTEGER NOT NULL,       -- number of advisors in this bundle
    phase3_active   INTEGER NOT NULL DEFAULT 0,  -- 1 when >= 60 outcomes triggered activation
    regime_frozen   INTEGER NOT NULL DEFAULT 0,  -- 1 if update was forced despite freeze
    notes           TEXT,
    supersedes_id   TEXT,
    is_superseded   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trust_snapshots_as_of
    ON trust_ledger_snapshots (as_of);

-- ---------------------------------------------------------------------------
-- trust_advisor_scores
-- Per-advisor component scores for each snapshot.  Auditable decomposition.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trust_advisor_scores (
    id              TEXT PRIMARY KEY,       -- ULID
    snapshot_id     TEXT NOT NULL REFERENCES trust_ledger_snapshots(id),
    advisor_id      TEXT NOT NULL,
    brier_skill     REAL,                   -- BSS (NULL if no non-abstain outcomes)
    calibration     REAL NOT NULL DEFAULT 1.0,
    coverage        REAL NOT NULL,
    composite_trust REAL,                   -- geometric mean (NULL if brier_skill is NULL)
    final_weight    REAL NOT NULL,          -- after all caps/floors/shadow ramp
    ci_low          REAL NOT NULL,
    ci_high         REAL NOT NULL,
    shadow          INTEGER NOT NULL DEFAULT 0,
    n_outcomes      INTEGER NOT NULL,
    n_non_abstain   INTEGER NOT NULL,
    cap_reason      TEXT,                   -- e.g. "mirofish_cap", "negative_skill", "thin_sample"
    supersedes_id   TEXT,
    is_superseded   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trust_advisor_scores_advisor
    ON trust_advisor_scores (advisor_id, snapshot_id);

-- ---------------------------------------------------------------------------
-- trust_correlation_entries
-- Individual pairwise ρ entries for each snapshot.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trust_correlation_entries (
    id              TEXT PRIMARY KEY,       -- ULID
    snapshot_id     TEXT NOT NULL REFERENCES trust_ledger_snapshots(id),
    advisor_a       TEXT NOT NULL,
    advisor_b       TEXT NOT NULL,
    rho             REAL NOT NULL,
    is_prior        INTEGER NOT NULL DEFAULT 0,  -- 1 = default 0.5 prior (sparse sample)
    n_co_obs        INTEGER NOT NULL DEFAULT 0,  -- number of co-observations used
    fingerprint_collision INTEGER NOT NULL DEFAULT 0,  -- 1 = set by fingerprint boost
    created_at      TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_trust_corr_pair
    ON trust_correlation_entries (snapshot_id, advisor_a, advisor_b);

-- ---------------------------------------------------------------------------
-- trust_regime_events
-- Regime change event log.  Consumed by RegimeTracker.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trust_regime_events (
    id          TEXT PRIMARY KEY,           -- ULID
    regime_id   TEXT NOT NULL,              -- e.g. "bull_2024", "bear_q3_2025"
    changed_at  TEXT NOT NULL,             -- tz-aware UTC ISO string
    detected_by TEXT,                       -- source that detected the regime change
    notes       TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trust_regime_changed_at
    ON trust_regime_events (changed_at);
