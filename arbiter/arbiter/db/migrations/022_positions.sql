-- 022_positions.sql — WP-B (Phase-2 persistence: position continuity)
--
-- Durable snapshot of the SimExecutor so paper positions survive across runs
-- and ``arbiter status`` reflects real open_positions (PHASE2-PERSISTENCE-PLAN
-- FROZEN decision #2).  We do NOT reconstruct from ``orders`` (no fill price /
-- cost basis recoverable there); this snapshot is the source of truth.
--
-- This is mutable runtime state, NOT a fact table: ``snapshot_executor`` wipes
-- and rewrites ``sim_positions`` and upserts the single ``sim_account`` row each
-- cycle.  It is therefore deliberately exempt from the §11.2 insert-only rule
-- (which governs immutable FACT tables: filings, opinions, outcomes, trust).
--
-- Tables added:
--   sim_positions  — one row per held ticker (current shares + avg cost).
--   sim_account    — singleton (id=1) cash + realized P&L ledger.

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ---------------------------------------------------------------------------
-- sim_positions
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sim_positions (
    ticker      TEXT PRIMARY KEY,
    shares      REAL NOT NULL,
    avg_price   REAL NOT NULL,
    updated_at  TEXT NOT NULL            -- tz-aware UTC ISO (caller's clock)
);

-- ---------------------------------------------------------------------------
-- sim_account  (singleton row, id is pinned to 1)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sim_account (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    cash        REAL NOT NULL,
    realized_pl REAL NOT NULL,
    updated_at  TEXT NOT NULL            -- tz-aware UTC ISO (caller's clock)
);
