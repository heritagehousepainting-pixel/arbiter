-- 012_calibration.sql — Calibration layer schema (Lane 9)
-- Stores fitted model metadata per (advisor_id, horizon_bucket).
-- Actual model coefficients are NOT stored (models are re-fit from outcomes
-- on startup); this table provides audit trail + staleness detection for fusion.
--
-- Insert-only per INTERFACES.md §10 (§11.2): corrections via supersede_row().

CREATE TABLE IF NOT EXISTS calibration_params (
    id              TEXT PRIMARY KEY,   -- ULID; supplied by Python layer (generate_ulid())
    advisor_id      TEXT NOT NULL,
    horizon_bucket  TEXT NOT NULL,      -- HorizonBucket enum value
    model_type      TEXT NOT NULL,      -- 'platt' | 'isotonic' | 'cold_start'
    n_outcomes      INTEGER NOT NULL,   -- non-zero outcomes used to fit
    as_of           TEXT NOT NULL,      -- tz-aware UTC ISO string; from caller clock
    supersedes_id   TEXT,               -- points to previous calibration_params row
    is_superseded   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT             -- tz-aware UTC ISO string; supplied by Python layer
);

CREATE INDEX IF NOT EXISTS idx_calibration_params_advisor_bucket
    ON calibration_params (advisor_id, horizon_bucket, as_of);
