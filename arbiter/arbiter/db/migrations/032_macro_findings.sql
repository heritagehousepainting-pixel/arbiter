CREATE TABLE IF NOT EXISTS macro_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    severity TEXT NOT NULL,
    affected_tickers TEXT NOT NULL,  -- comma-separated
    sources TEXT NOT NULL            -- comma-separated
);
CREATE INDEX IF NOT EXISTS idx_macro_findings_expires ON macro_findings (expires_at);
