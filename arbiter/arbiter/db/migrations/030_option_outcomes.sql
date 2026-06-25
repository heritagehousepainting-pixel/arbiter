-- 030_option_outcomes.sql — options outcome store (P2, isolated from equity).
--
-- ISOLATION CONTRACT: this table is ENTIRELY SEPARATE from the equity
-- ``outcomes`` table.  Rows here NEVER reach ``run_outcome_sweep()`` or
-- the trust/calibration paths.  ``option_pl_pct`` is display-only; the
-- only field that may later be cross-referenced with the equity trust
-- ledger is ``underlying_alpha_bps`` (direction validation, not P&L).
--
-- Insert-only design (INTERFACES.md §11.2):
--   Primary key is a ULID stored as TEXT.
--   Timestamps are tz-aware UTC ISO strings.
--   No UPDATE is ever issued against this table.
--
-- Column names match OptionOutcomeRow.to_dict() in arbiter/options/types.py
-- exactly.

CREATE TABLE IF NOT EXISTS option_outcomes (
    id                      TEXT PRIMARY KEY,   -- ULID
    shadow_id               TEXT,               -- FK → option_shadow_log.id (NULL for unshadowed P2 orders)
    idea_id                 TEXT NOT NULL,       -- FK → ideas.idea_id
    underlying              TEXT NOT NULL,       -- underlying equity ticker
    occ_symbol              TEXT NOT NULL,       -- OCC symbol of the closed contract
    side                    TEXT NOT NULL,       -- "call" | "put"
    open_ts                 TEXT NOT NULL,       -- tz-aware UTC ISO position open timestamp
    close_ts                TEXT NOT NULL,       -- tz-aware UTC ISO position close timestamp
    close_reason            TEXT NOT NULL,       -- "premium_stop" | "horizon_expiry" |
                                                --   "conviction_reversal" | "expiry_approach" | "manual"
    entry_premium           REAL NOT NULL,       -- total premium paid to open (USD, positive)
    exit_premium            REAL NOT NULL,       -- total premium received on close (USD, positive)
    option_pl_pct           REAL NOT NULL,       -- (exit_premium - entry_premium) / entry_premium
                                                --   display-only; NOT used in trust scoring
    underlying_alpha_bps    REAL NOT NULL,       -- (underlying_close / underlying_open - 1) × 10_000
                                                --   direction-validation bridge to equity trust ledger
    delta_at_open           REAL,               -- contract delta at position open
    iv_at_open              REAL,               -- implied volatility at position open
    iv_at_close             REAL,               -- implied volatility at position close
    contracts_qty           INTEGER NOT NULL,    -- number of contracts held
    created_at              TEXT NOT NULL        -- tz-aware UTC ISO insertion timestamp
);

CREATE INDEX IF NOT EXISTS idx_option_outcomes_idea_id
    ON option_outcomes (idea_id);

CREATE INDEX IF NOT EXISTS idx_option_outcomes_underlying_close_ts
    ON option_outcomes (underlying, close_ts);

CREATE INDEX IF NOT EXISTS idx_option_outcomes_shadow_id
    ON option_outcomes (shadow_id)
    WHERE shadow_id IS NOT NULL;
