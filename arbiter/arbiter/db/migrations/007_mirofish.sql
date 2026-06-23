-- 007_mirofish.sql  —  MiroFish (A2) run cache  (Lane 7)
--
-- Stores the raw output of expensive MiroFish analysis runs.
-- Cache keys are (idea_fingerprint, as_of_date) — one entry per idea per day.
--
-- FORWARD-TEST-ONLY contract:
--   is_forward_test_only is always 1.  Any process that attempts to use
--   these cached results in a historical backtest is violating the
--   non-look-ahead contract.  The application layer enforces this via
--   BacktestCacheError in run_cache.py, but the flag is stored here so
--   audit queries can confirm the invariant.
--
-- Insert-only (INTERFACES.md §10):
--   There is no UPDATE path for this table.  Corrections are handled by
--   letting the next-day cache key produce a new row.

CREATE TABLE IF NOT EXISTS mirofish_run_cache (
    id                  TEXT PRIMARY KEY,    -- ULID
    idea_fingerprint    TEXT NOT NULL,       -- SHA-256 hex digest of (ticker|thesis|horizon_days)
    as_of_date          TEXT NOT NULL,       -- ISO date string (as_of.date().isoformat())
    run_id              TEXT NOT NULL,       -- shared run_group_id across opinions in this run
    raw_opinions_json   TEXT NOT NULL,       -- JSON array of raw opinion dicts from MiroFish
    is_forward_test_only INTEGER NOT NULL DEFAULT 1,  -- always 1; guard against backtest misuse
    created_at          TEXT NOT NULL,       -- tz-aware UTC ISO string

    -- Uniqueness: one cache entry per idea per day.
    -- Duplicate inserts raise IntegrityError (caller must check-then-act).
    UNIQUE (idea_fingerprint, as_of_date)
);

CREATE INDEX IF NOT EXISTS idx_mirofish_cache_fingerprint_date
    ON mirofish_run_cache (idea_fingerprint, as_of_date);
