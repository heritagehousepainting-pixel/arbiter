-- 029_options_shadow.sql — options expression layer, P1 shadow tables.
--
-- Two tables for the shadow (zero-risk) phase of the options overlay:
--
--   option_shadow_log  — one row per gate evaluation (gate fired or not);
--                        the complete audit trail for calibrating thresholds
--                        from real Alpaca chain data.
--
--   option_iv_history  — daily ATM-IV snapshots per underlying; accumulates
--                        the history needed for a proper IV-rank gate in P2.
--
-- Insert-only design (INTERFACES.md §11.2):
--   All primary keys are ULIDs stored as TEXT.
--   Timestamps are tz-aware UTC ISO strings.
--   No UPDATE is ever issued against these tables.
--
-- Column names match OptionShadowRow.to_dict() and IVHistoryRow.to_dict()
-- in arbiter/options/types.py exactly.

-- ---------------------------------------------------------------------------
-- option_shadow_log
-- ---------------------------------------------------------------------------
-- One row per gate evaluation.  Written unconditionally so that both
-- "gate fired" and "gate rejected" rows are visible for calibration.

CREATE TABLE IF NOT EXISTS option_shadow_log (
    id                      TEXT PRIMARY KEY,   -- ULID
    idea_id                 TEXT NOT NULL,       -- FK → ideas.idea_id
    underlying              TEXT NOT NULL,       -- equity ticker
    as_of                   TEXT NOT NULL,       -- tz-aware UTC ISO decision timestamp
    gate_express            INTEGER NOT NULL,    -- 1 = gate fired, 0 = rejected
    gate_reason             TEXT NOT NULL,       -- short reason code (e.g. "OK", "CONVICTION_TOO_LOW")
    side                    TEXT,               -- "call" | "put" | NULL when gate_express=0
    occ_symbol              TEXT,               -- selected contract OCC symbol; NULL when not found
    strike                  REAL,               -- strike price (USD)
    expiry                  TEXT,               -- ISO date string of contract expiry
    delta                   REAL,               -- contract delta at snapshot time
    iv                      REAL,               -- implied volatility at snapshot time
    bid                     REAL,               -- bid at snapshot time
    ask                     REAL,               -- ask at snapshot time
    open_interest           INTEGER,            -- open interest in contracts
    volume                  INTEGER,            -- daily volume in contracts
    est_premium             REAL,               -- estimated total premium outlay (USD)
    delta_adjusted_notional REAL,               -- |delta| × 100 × underlying_price × contracts_qty (USD)
    contracts_qty           INTEGER,            -- number of contracts sized
    conviction              REAL NOT NULL,      -- conviction score evaluated
    horizon_days            REAL NOT NULL,      -- thesis horizon in days
    catalyst_tag            TEXT,               -- catalyst tag (e.g. "13D", "form4_cluster")
    ivr_estimate            REAL,               -- IV rank / proxy used
    realized_vol_proxy      REAL,               -- realized vol proxy used (P1 cold-start)
    created_at              TEXT NOT NULL       -- tz-aware UTC ISO insertion timestamp
);

CREATE INDEX IF NOT EXISTS idx_option_shadow_underlying_as_of
    ON option_shadow_log (underlying, as_of);

CREATE INDEX IF NOT EXISTS idx_option_shadow_idea_id
    ON option_shadow_log (idea_id);

CREATE INDEX IF NOT EXISTS idx_option_shadow_gate_express
    ON option_shadow_log (gate_express);

-- ---------------------------------------------------------------------------
-- option_iv_history
-- ---------------------------------------------------------------------------
-- Daily ATM-IV snapshots accumulated from day 1 so that IV-rank is
-- computable by P2 from locally-stored data.  One row per underlying
-- per cycle (the engine writes this once per full cycle per ticker of
-- interest, not per contract).

CREATE TABLE IF NOT EXISTS option_iv_history (
    id          TEXT PRIMARY KEY,   -- ULID
    underlying  TEXT NOT NULL,      -- equity ticker
    as_of       TEXT NOT NULL,      -- tz-aware UTC ISO snapshot timestamp
    atm_iv      REAL NOT NULL,      -- ATM implied volatility (annualised decimal, e.g. 0.38)
    occ_symbol  TEXT NOT NULL,      -- OCC symbol of the contract used as ATM proxy
    created_at  TEXT NOT NULL       -- tz-aware UTC ISO insertion timestamp
);

CREATE INDEX IF NOT EXISTS idx_option_iv_history_underlying_as_of
    ON option_iv_history (underlying, as_of);
