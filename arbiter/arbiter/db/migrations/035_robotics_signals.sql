CREATE TABLE IF NOT EXISTS robotics_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    headline TEXT NOT NULL,
    summary TEXT NOT NULL,
    category TEXT NOT NULL,          -- compute|brain|components|integrator|deployment|other
    symbols TEXT NOT NULL,           -- comma-separated universe symbols
    trigger_hit INTEGER NOT NULL,    -- 0/1: matched a watch-trigger
    trigger_name TEXT,               -- universe symbol whose trigger fired (nullable)
    sources TEXT NOT NULL            -- comma-separated URLs
);
CREATE INDEX IF NOT EXISTS idx_robotics_signals_as_of ON robotics_signals (as_of);
CREATE INDEX IF NOT EXISTS idx_robotics_signals_trigger ON robotics_signals (trigger_hit);
