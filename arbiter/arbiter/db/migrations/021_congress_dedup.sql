-- 021_congress_dedup.sql
-- Enforce per-transaction idempotency at the DB level for all filings.
-- The (accession, txn_idx) pair is the synthetic key used by the Congress
-- adapter (and is compatible with the EDGAR adapter's accession+txn_idx scheme).
-- Existing data is dup-free per audit (2026-06-19), so this index applies cleanly.
CREATE UNIQUE INDEX IF NOT EXISTS idx_filings_accession_txn
    ON filings(accession, txn_idx)
    WHERE accession IS NOT NULL AND txn_idx IS NOT NULL;
