-- arbiter/arbiter/db/migrations/027_form13f_holdings.sql
-- Raw quarterly 13F-HR holdings snapshots (insert-only). Diffed quarter-over-
-- quarter by form13f_normalize.py to produce form13f filing rows.
CREATE TABLE IF NOT EXISTS form13f_holdings (
    id           TEXT PRIMARY KEY,           -- ULID
    person_id    TEXT NOT NULL,              -- manager (FK people.person_id)
    accession    TEXT NOT NULL,              -- EDGAR accession (idempotency)
    filing_date  TEXT NOT NULL,              -- tz-aware ISO; PIT as_of source
    report_date  TEXT NOT NULL,              -- quarter-end the snapshot describes
    cusip        TEXT NOT NULL,
    ticker       TEXT,                        -- nullable when CUSIP unresolved
    issuer_name  TEXT,
    value_usd    REAL NOT NULL DEFAULT 0,
    shares       REAL NOT NULL DEFAULT 0,
    put_call     TEXT,                        -- NULL = outright shares; 'Put'/'Call' otherwise
    created_at   TEXT NOT NULL,
    UNIQUE(person_id, accession, cusip, put_call)
);
CREATE INDEX IF NOT EXISTS idx_form13f_holdings_person_report
    ON form13f_holdings(person_id, report_date);
