-- 008b_filings_accession.sql  — extend filings table for Lane 5c writer
-- Adds columns needed by the identity/writer layer that were not present in
-- the base 001_core.sql schema.  Migration runner tracks applied files so
-- re-runs are no-ops.

ALTER TABLE filings ADD COLUMN accession TEXT;
ALTER TABLE filings ADD COLUMN is_amendment INTEGER NOT NULL DEFAULT 0;
-- Form 4 numeric fields (optional; Congress filings may omit them).
ALTER TABLE filings ADD COLUMN shares REAL;
ALTER TABLE filings ADD COLUMN price REAL;

CREATE INDEX IF NOT EXISTS idx_filings_accession
    ON filings (accession)
    WHERE accession IS NOT NULL;
