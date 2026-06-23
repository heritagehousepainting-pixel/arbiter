-- 026_outcome_stance_attribution.sql — real attribution (sub-project #5a, D6).
--
-- Two additive, idempotent concerns (both ALTERs are guarded by the migration
-- runner's duplicate-column check — see db/migrate.py):
--
--   1. outcomes.stance_score — the advisor's ACTUAL directional forecast in
--      [-1, 1], carried from the persisted opinion onto the resolved outcome so
--      the Brier scores against the real stance (not a binary-reconstruction).
--      NOT NULL DEFAULT 0.0 so legacy proxy rows backfill to a neutral stance
--      (p_hat=0.5 → BSS≈0, benign dilution; not skipped).
--
--   2. opinions.idea_id — the opinion→idea link (nullable; legacy / abstain /
--      source-overlap rows stay NULL).  Mirrors the orders.idea_id pattern
--      (migration 023).  Set at opinion-persist time, never updated.
--
-- Insert-only is preserved: neither column is ever UPDATEd in place.
ALTER TABLE outcomes  ADD COLUMN stance_score REAL NOT NULL DEFAULT 0.0;  -- advisor's forecast
ALTER TABLE opinions  ADD COLUMN idea_id TEXT;                            -- opinion→idea link
CREATE INDEX IF NOT EXISTS idx_opinions_idea_id ON opinions (idea_id);
