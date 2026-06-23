-- 008_identity.sql  — Identity resolution store (Lane 5c)
-- Insert-only.  People are resolved to a canonical person_id (ULID) once and
-- never updated.  Canonical_name is the normalized form used for deduplication.

CREATE TABLE IF NOT EXISTS people (
    person_id      TEXT PRIMARY KEY,   -- ULID
    canonical_name TEXT NOT NULL,
    source         TEXT NOT NULL,      -- 'form4' | 'congress'
    created_at     TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_people_canonical_source
    ON people (canonical_name, source);
