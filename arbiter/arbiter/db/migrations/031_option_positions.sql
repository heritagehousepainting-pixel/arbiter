-- 031_option_positions.sql — open option positions (P2 paper-trading lifecycle).
--
-- Design: INSERT-ONLY — position openness is DERIVED from the absence of a
-- matching row in ``option_outcomes`` (joined on idea_id + occ_symbol).
-- Closing = inserting an outcome row.  This table is NEVER updated.
--
-- Column names match OptionPositionRow.to_dict() in arbiter/options/positions.py
-- exactly.

CREATE TABLE IF NOT EXISTS option_positions (
    id                      TEXT PRIMARY KEY,   -- ULID
    idea_id                 TEXT NOT NULL,       -- FK → ideas.idea_id
    shadow_id               TEXT,               -- FK → option_shadow_log.id (NULL when not shadowed)
    underlying              TEXT NOT NULL,       -- underlying equity ticker (e.g. "AAPL")
    occ_symbol              TEXT NOT NULL,       -- OCC option symbol
    side                    TEXT NOT NULL,       -- "call" | "put"
    strike                  REAL NOT NULL,       -- strike price (USD)
    expiry                  TEXT NOT NULL,       -- ISO date string of contract expiry
    contracts_qty           INTEGER NOT NULL,    -- number of contracts held
    entry_premium           REAL NOT NULL,       -- total premium paid to open (USD, positive)
    entry_limit_price       REAL NOT NULL,       -- limit price per share submitted to broker
    delta_at_open           REAL,               -- contract delta at position open
    iv_at_open              REAL,               -- implied volatility at position open
    underlying_open_price   REAL NOT NULL,       -- underlying equity price at open (USD)
    thesis_horizon_date     TEXT NOT NULL,       -- ISO date on/after which horizon trigger fires
    original_conviction     REAL NOT NULL,       -- conviction score at open (signed: +bullish, -bearish)
    broker_order_id         TEXT NOT NULL,       -- broker-assigned order id (Alpaca "id" field)
    open_ts                 TEXT NOT NULL,       -- tz-aware UTC ISO position open timestamp
    created_at              TEXT NOT NULL        -- tz-aware UTC ISO insertion timestamp
);

CREATE INDEX IF NOT EXISTS idx_option_positions_idea_id
    ON option_positions (idea_id);

CREATE INDEX IF NOT EXISTS idx_option_positions_underlying
    ON option_positions (underlying);

CREATE INDEX IF NOT EXISTS idx_option_positions_occ_symbol
    ON option_positions (occ_symbol);

-- Composite index for the open-position query:
-- list_open_positions joins on (idea_id, occ_symbol) against option_outcomes.
CREATE INDEX IF NOT EXISTS idx_option_positions_idea_occ
    ON option_positions (idea_id, occ_symbol);
