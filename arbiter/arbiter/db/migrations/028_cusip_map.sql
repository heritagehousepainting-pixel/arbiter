-- arbiter/arbiter/db/migrations/028_cusip_map.sql
-- Cached CUSIP -> ticker resolutions (additive upsert cache, NOT trade state).
CREATE TABLE IF NOT EXISTS cusip_map (
    cusip       TEXT PRIMARY KEY,
    ticker      TEXT NOT NULL,
    issuer_name TEXT,
    source      TEXT NOT NULL,   -- 'seed' | 'alpaca_name' | 'manual'
    confidence  REAL NOT NULL,   -- [0,1]; only >= 0.9 are trusted for trading
    resolved_at TEXT NOT NULL
);
