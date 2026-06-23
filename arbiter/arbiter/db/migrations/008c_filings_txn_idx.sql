-- 008c_filings_txn_idx.sql  — add per-transaction index to filings (Lane 2 / 5c)
-- Adds txn_idx so multi-transaction Form 4 filings can dedup per-transaction
-- rather than per-accession (which would silently drop all but the first row).
-- The column is nullable so Congress filings (no txn_idx) are unaffected.

ALTER TABLE filings ADD COLUMN txn_idx INTEGER;

-- Composite index for the per-transaction dedup query used by writer.py.
CREATE INDEX IF NOT EXISTS idx_filings_accession_txn_idx
    ON filings (accession, txn_idx)
    WHERE accession IS NOT NULL AND txn_idx IS NOT NULL;
