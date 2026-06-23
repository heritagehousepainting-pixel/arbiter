-- 001a_breakers.sql  — Circuit-breaker state table (Lane 4a)
-- The breaker_state table is already created by 001_core.sql.
-- This migration is a no-op guard: it re-asserts the CREATE IF NOT EXISTS
-- so that 001a runs cleanly even against a DB that was migrated from
-- 001_core.sql alone, and ensures lexical ordering places it after the core.
-- If 001_core.sql is ever split out, this file owns the canonical definition.

CREATE TABLE IF NOT EXISTS breaker_state (
    breaker_name TEXT PRIMARY KEY,  -- canonical name, e.g. "daily_loss"
    latched      INTEGER NOT NULL DEFAULT 0,   -- 1 = tripped and latched
    latched_at   TEXT,              -- tz-aware UTC ISO string; NULL when not latched
    reason       TEXT               -- human-readable trip reason
);
