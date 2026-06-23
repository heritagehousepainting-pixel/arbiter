-- 025_trust_weights_cap_reason.sql — Learning loop (sub-project #4), amendment D1.
-- Additive column on trust_weights so the weight resolver and warm-start path can
-- distinguish negative-skill suppression ("negative_skill") from onboarding/cold
-- (NULL) and graduated rows.  ALTER TABLE … ADD COLUMN is idempotent via the
-- migration runner's duplicate-column guard (see db/migrate.py).
ALTER TABLE trust_weights ADD COLUMN cap_reason TEXT;
