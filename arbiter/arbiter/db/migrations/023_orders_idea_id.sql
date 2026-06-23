-- 023_orders_idea_id.sql — exit/sell monitor (sub-project #2), amendment B5.
--
-- Add an optional idea_id link on the orders ledger so the sell/close-out path
-- can tie an order to its owning idea EXACTLY, instead of relying on the
-- (ticker, horizon_bucket) join (fragile if the one-live-bucket-per-held-ticker
-- invariant ever breaks).  Populated at BUY submit time by the engine; legacy
-- rows stay NULL and fall back to the (ticker, bucket) join.
--
-- Additive, idempotent: the migration runner skips ADD COLUMN when the column
-- already exists.

ALTER TABLE orders ADD COLUMN idea_id TEXT;

CREATE INDEX IF NOT EXISTS idx_orders_idea_id ON orders (idea_id);
